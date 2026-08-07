/**
 * 公共 HTTP 请求封装模块
 *
 * 底层用 Node 内置 http/https 模块，并保留严格的响应头解析。
 * 这样上游返回的非法头部不会被桌面端当成可信输入；调用方仍使用原有
 * Request/ResponseResult 接口。
 */

import http from 'http';
import https from 'https';

const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

export interface RequestConfig {
  baseURL?: string;
  timeout?: number;
  headers?: Record<string, string>;
  strictSecurityEnabled?: boolean;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  path?: string;
  data?: Record<string, unknown> | null;
  headers?: Record<string, string>;
  timeout?: number;
}

export interface ResponseResult {
  success: boolean;
  statusCode?: number | undefined;
  data?: Record<string, unknown> | undefined;
  message?: string | undefined;
  isHTML?: boolean | undefined;
  isTimeout?: boolean | undefined;
  raw?: string | undefined;
  error?: Error | undefined;
}

export function isSecureRemoteUrl(url: string, strictSecurityEnabled: boolean): boolean {
  return !strictSecurityEnabled || new URL(url).protocol === 'https:';
}

/** Reject credential-bearing remote traffic over plaintext unless compatibility mode is explicit. */
export function requireSecureRemoteUrl(url: string, strictSecurityEnabled: boolean): void {
  if (!isSecureRemoteUrl(url, strictSecurityEnabled)) {
    throw new Error(
      '严格安全约束要求认证与凭据请求使用 HTTPS；可在安全中心关闭后启用兼容模式',
    );
  }
}

export class Request {
  private baseURL: string;
  private timeout: number;
  private headers: Record<string, string>;
  private strictSecurityEnabled: boolean;

  constructor(config: RequestConfig = {}) {
    this.baseURL = config.baseURL || '';
    this.timeout = config.timeout || 30000;
    this.headers = config.headers || {};
    this.strictSecurityEnabled = config.strictSecurityEnabled !== false;
  }

  setBaseURL(url: string): void {
    this.baseURL = url;
  }

  setStrictSecurityEnabled(enabled: boolean): void {
    this.strictSecurityEnabled = enabled;
  }

  setHeader(key: string, value: string): void {
    this.headers[key] = value;
  }

  removeHeader(key: string): void {
    delete this.headers[key];
  }

  setAuthToken(token: string): void {
    this.headers['Authorization'] = `Bearer ${token}`;
  }

  clearAuthToken(): void {
    delete this.headers['Authorization'];
  }

  async request(options: RequestOptions): Promise<ResponseResult> {
    const {
      method = 'GET',
      path = '',
      data = null,
      headers = {},
      timeout = this.timeout,
    } = options;

    // 用 URL 构造器明确 join：path 必须是绝对路径或 baseURL 必须以 / 结尾，
    // 避免 `http://api.com` + `v1/login` 被错误拼成 `http://api.comv1/login`（I-14 修复）
    let urlObj: URL;
    try {
      const base = this.baseURL.endsWith('/') ? this.baseURL : `${this.baseURL}/`;
      // path 以 / 开头则去掉，让 URL 构造器在 base 目录下 join
      const normalizedPath = path.startsWith('/') ? path.slice(1) : path;
      urlObj = new URL(normalizedPath, base);
    } catch (error) {
      return {
        success: false,
        message: `Invalid URL: base=${this.baseURL} path=${path}`,
        error: error as Error,
      };
    }
    try {
      requireSecureRemoteUrl(urlObj.href, this.strictSecurityEnabled);
    } catch (error) {
      return {
        success: false,
        message: (error as Error).message,
        error: error as Error,
      };
    }

    // 基础请求头。Connection: close 避免连接池 stale 复用问题；Content-Length
    // 由 body 字节数算出（仅 POST/PUT/DELETE 带 body 时设）。
    const requestHeaders: Record<string, string> = {
      'Content-Type': 'application/json',
      'User-Agent': 'Mozilla/5.0',
      'Accept': 'application/json',
      'Connection': 'close',
      ...this.headers,
      ...headers,
    };

    let bodyData = '';
    if (data && method !== 'GET') {
      bodyData = JSON.stringify(data);
      requestHeaders['Content-Length'] = String(Buffer.byteLength(bodyData));
    }

    const isHttps = urlObj.protocol === 'https:';
    const requestLib = isHttps ? https : http;

    const requestOptions: http.RequestOptions = {
      hostname: urlObj.hostname,
      port: urlObj.port || (isHttps ? 443 : 80),
      path: urlObj.pathname + urlObj.search,
      method,
      headers: requestHeaders,
      timeout,
    };

    return new Promise<ResponseResult>((resolve) => {
      const req = requestLib.request(requestOptions, (res) => {
        const statusCode = res.statusCode ?? 0;

        // D9: Content-Length 预检——服务端声明超过上限就直接拒，避免缓冲整个 body。
        const contentLengthHeader = res.headers['content-length'];
        if (contentLengthHeader) {
          const declared = Number.parseInt(contentLengthHeader, 10);
          if (Number.isFinite(declared) && declared > MAX_RESPONSE_BYTES) {
            req.destroy();
            resolve({
              success: false,
              statusCode,
              message: `响应过大: Content-Length ${declared} > ${MAX_RESPONSE_BYTES}`,
            });
            return;
          }
        }

        // 流式读取：第一 chunk 探测 HTML（早期拒绝）+ 累计字节上限。
        let responseData = '';
        let firstChunkSeen = false;
        let rejected = false;

        res.on('data', (chunk: Buffer) => {
          if (rejected) return;
          const text = chunk.toString('utf8');

          // D9: peek 第一 chunk——以 '<' 开头判定为 HTML 错误页，提前拒绝而不是整段缓冲。
          if (!firstChunkSeen) {
            firstChunkSeen = true;
            const trimmed = text.replace(/^\s+/, '');
            if (
              trimmed.startsWith('<') ||
              trimmed.toLowerCase().startsWith('<html') ||
              text.startsWith('<!')
            ) {
              rejected = true;
              res.destroy();
              resolve({
                success: false,
                statusCode,
                message: '请求失败：返回 HTML 页面',
                isHTML: true,
              });
              return;
            }
          }

          responseData += text;

          // D9: 累计字节上限，避免无界缓冲。
          if (Buffer.byteLength(responseData, 'utf8') > MAX_RESPONSE_BYTES) {
            rejected = true;
            res.destroy();
            resolve({
              success: false,
              statusCode,
              message: `响应过大: 已超过 ${MAX_RESPONSE_BYTES} 字节上限`,
            });
            return;
          }
        });

        res.on('end', () => {
          if (rejected) return;
          const result = parseBody(statusCode, responseData);
          console.log('[Response]', method, urlObj?.toString(), 'Status:', statusCode, 'Body:', result);
          resolve(result);
        });

        res.on('error', (err) => {
          if (rejected) return;
          rejected = true;
          resolve({
            success: false,
            statusCode,
            message: `网络请求失败: ${err.message}`,
            error: err as Error,
          });
        });
      });

      req.on('timeout', () => {
        req.destroy();
        resolve({
          success: false,
          message: '请求超时',
          isTimeout: true,
        });
      });

      req.on('error', (err: NodeJS.ErrnoException) => {
        // 严格响应头解析失败时明确记录服务端返回的非法头部。
        const cause = (err as { cause?: { message?: string } }).cause;
        const detail = cause?.message || err.code || err.message;
        if (err.code === 'HPE_INVALID_HEADER_TOKEN') {
          console.error('[Request] HTTP 响应头解析错误（服务端返回非法响应头）:', detail);
        }
        resolve({
          success: false,
          message: `网络请求失败: ${err.message}${err.code ? ` (${err.code})` : ''}`,
          error: err as Error,
        });
      });

      if (bodyData) {
        req.write(bodyData);
      }
      req.end();
    });
  }

  get(path: string, options: RequestOptions = {}): Promise<ResponseResult> {
    return this.request({
      method: 'GET',
      path: path,
      ...options,
    });
  }

  post(path: string, data: Record<string, unknown> | null = null, options: RequestOptions = {}): Promise<ResponseResult> {
    return this.request({
      method: 'POST',
      path: path,
      data: data,
      ...options,
    });
  }

  put(path: string, data: Record<string, unknown> | null = null, options: RequestOptions = {}): Promise<ResponseResult> {
    return this.request({
      method: 'PUT',
      path: path,
      data: data,
      ...options,
    });
  }

  delete(path: string, options: RequestOptions = {}): Promise<ResponseResult> {
    return this.request({
      method: 'DELETE',
      path: path,
      ...options,
    });
  }
}

/** 收完响应体后的统一收尾：HTML 兜底判定 + JSON 解析。 */
function parseBody(statusCode: number, responseData: string): ResponseResult {
  if (responseData.startsWith('<') || responseData.startsWith('<html')) {
    return {
      success: false,
      statusCode,
      message: '请求失败：返回 HTML 页面',
      isHTML: true,
    };
  }
  try {
    const result = JSON.parse(responseData);
    return {
      success: true,
      statusCode,
      data: result,
    };
  } catch (e) {
    return {
      success: false,
      statusCode,
      message: `JSON 解析失败: ${(e as Error).message}`,
      raw: responseData,
    };
  }
}

export function createRequest(config?: RequestConfig): Request {
  return new Request(config);
}
