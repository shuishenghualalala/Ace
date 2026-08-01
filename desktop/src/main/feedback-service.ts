/**
 * 反馈服务模块（使用 Node 18+ 内置 fetch / FormData）
 * baseURL 从环境变量 `CREW_FEEDBACK_BASE_URL` 读取；开源版默认不连接任何外部服务。
 */

import { randomUUID } from 'node:crypto';
import type { FeedbackSubmitArgs, FeedbackListArgs } from '../shared/ipc-schemas';

export const FEEDBACK_CONFIG = {
  baseURL: process.env['CREW_FEEDBACK_BASE_URL']?.trim() || '',
  timeout: 30000,
};

/** 单张附件图片最大字节数（转 data URL 前的体积上限）。 */
const MAX_FEEDBACK_IMAGE_BYTES = 10 * 1024 * 1024;

export interface FeedbackAttachment {
  name: string;
  dataUrl: string;
}

export interface FeedbackResponse {
  success: boolean;
  resultCode?: string | undefined;
  statusCode?: number | undefined;
  message?: string | undefined;
  data?: Record<string, unknown> | undefined;
  isHTML?: boolean | undefined;
  error?: Error | undefined;
}

export interface FeedbackListRequest {
  page?: number | undefined;
  size?: number | undefined;
  status?: 'PENDING' | 'PROCESSING' | 'RESOLVED' | 'CLOSED' | undefined;
}

export interface FeedbackListItem {
  id: number;
  title: string;
  description?: string | undefined;
  status: 'PENDING' | 'PROCESSING' | 'RESOLVED' | 'CLOSED';
  createdAt: string;
  images?: string | undefined;
  adminReply?: string | null | undefined;
  updatedAt?: string | undefined;
}

export interface FeedbackListResponse {
  success: boolean;
  resultCode?: string | undefined;
  message?: string | undefined;
  total?: number | undefined;
  list?: FeedbackListItem[] | undefined;
  error?: Error | undefined;
}

export interface FeedbackImageResponse {
  success: boolean;
  dataUrl?: string | undefined;
  message?: string | undefined;
  error?: Error | undefined;
}

function dataUrlToBuffer(dataUrl: string): { buffer: Buffer; mimeType: string; ext: string } | null {
  const matches = dataUrl.match(/^data:(.+);base64,(.+)$/);
  if (!matches) return null;
  const mimeType = matches[1];
  const base64Data = matches[2];
  const buffer = Buffer.from(base64Data, 'base64');
  const ext = mimeType.split('/')[1] || 'png';
  return { buffer, mimeType, ext };
}

/**
 * 把 images 字段的相对路径解析为完整 URL；绝对 URL 原样返回。
 * 相对路径的静态文件根当前未确认(实测 4 处候选根 404)，服务端确认后仅需改此一处拼接。
 */
function resolveImageUrl(baseURL: string, path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseURL}/${path.replace(/^\/+/, '')}`;
}

export class FeedbackService {
  private baseURL: string = FEEDBACK_CONFIG.baseURL;

  setBaseURL(url: string): void {
    this.baseURL = url;
  }

  getBaseURL(): string {
    return this.baseURL;
  }

  private configuredBaseURL(): string | null {
    const value = this.baseURL.trim().replace(/\/+$/, '');
    return value || null;
  }

  async submitFeedback(args: FeedbackSubmitArgs): Promise<FeedbackResponse> {
    try {
      const baseURL = this.configuredBaseURL();
      if (!baseURL) {
        return { success: false, message: '反馈服务未配置，请设置 CREW_FEEDBACK_BASE_URL' };
      }
      const form = new FormData();
      form.append('userInfo', '{}');

      const feedbackForSubmit = {
        title: args.title,
        description: args.description,
        image: [],
      };
      form.append('feedback', JSON.stringify(feedbackForSubmit));

      const images = args.images;
      if (Array.isArray(images) && images.length > 0) {
        for (const imageData of images) {
          if (typeof imageData === 'string') {
            const parsed = dataUrlToBuffer(imageData);
            if (parsed) {
              const filename = `screenshot-${Date.now()}-${randomUUID().slice(0, 8)}.${parsed.ext}`;
              const blob = new Blob([new Uint8Array(parsed.buffer)], { type: parsed.mimeType });
              form.append('images', blob, filename);
            }
          } else if (imageData && typeof imageData === 'object' && 'dataUrl' in imageData) {
            const parsed = dataUrlToBuffer(imageData.dataUrl);
            if (parsed) {
              const filename = imageData.name || `screenshot-${Date.now()}-${randomUUID().slice(0, 8)}.${parsed.ext}`;
              const blob = new Blob([new Uint8Array(parsed.buffer)], { type: parsed.mimeType });
              form.append('images', blob, filename);
            }
          }
        }
      }

      const url = `${baseURL}/api/feedback/submit`;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), FEEDBACK_CONFIG.timeout);

      const res = await fetch(url, {
        method: 'POST',
        body: form,
        signal: controller.signal,
      });
      clearTimeout(timer);

      const responseText = await res.text();

      if (responseText.startsWith('<') || responseText.startsWith('<!')) {
        return {
          success: false,
          statusCode: res.status,
          message: '服务器返回 HTML 页面，请检查接口地址或请求格式',
          isHTML: true,
        };
      }

      let result: { resultCode?: string; resultDesc?: string };
      try {
        result = JSON.parse(responseText);
      } catch (e) {
        return {
          success: false,
          statusCode: res.status,
          message: `JSON 解析失败: ${(e as Error).message}`,
        };
      }

      const isSuccessStatus = res.status >= 200 && res.status < 300;
      if (!isSuccessStatus || result.resultCode !== '000000') {
        return {
          success: false,
          resultCode: result.resultCode,
          statusCode: res.status,
          message: result.resultDesc || '提交反馈失败',
          data: result as unknown as Record<string, unknown>,
        };
      }

      return {
        success: true,
        resultCode: result.resultCode,
        statusCode: res.status,
        message: result.resultDesc || '反馈提交成功',
        data: result as unknown as Record<string, unknown>,
      };
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        return { success: false, message: '请求超时' };
      }
      return {
        success: false,
        message: (error as Error).message || '提交反馈失败',
        error: error as Error,
      };
    }
  }

  /**
   * 拉取反馈列表。
   * GET /api/feedback/list?page=&size=&status=
   *
   * 返回字段包含 id/title/description/status/createdAt；
   * adminReply / updatedAt 会返回；images 为相对路径 JSON 字符串(如 '["upload/xxx.png"]')；
   * images 的静态文件根未确认(4 处常见根均 404)，详情页暂不渲染图片。
   *
   */
  async getFeedbackList(params: FeedbackListArgs): Promise<FeedbackListResponse> {
    try {
      const baseURL = this.configuredBaseURL();
      if (!baseURL) {
        return { success: false, message: '反馈服务未配置，请设置 CREW_FEEDBACK_BASE_URL' };
      }
      const queryParams = new URLSearchParams();
      queryParams.set('page', String(params.page));
      queryParams.set('size', String(params.size));
      if (params.status) queryParams.set('status', params.status);

      const queryString = queryParams.toString();
      const url = `${baseURL}/api/feedback/list${queryString ? `?${queryString}` : ''}`;

      const headers: Record<string, string> = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json',
      };
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), FEEDBACK_CONFIG.timeout);

      const res = await fetch(url, {
        method: 'GET',
        headers,
        signal: controller.signal,
      });
      clearTimeout(timer);

      const responseText = await res.text();

      if (responseText.startsWith('<') || responseText.startsWith('<!')) {
        return {
          success: false,
          message: '服务器返回 HTML 页面，请检查接口地址或请求格式',
        };
      }

      let result: { resultCode?: string; resultDesc?: string; data?: { total?: number; list?: FeedbackListItem[] } };
      try {
        result = JSON.parse(responseText);
      } catch (e) {
        return {
          success: false,
          message: `JSON 解析失败: ${(e as Error).message}`,
        };
      }

      const isSuccessStatus = res.status >= 200 && res.status < 300;
      if (!isSuccessStatus || result.resultCode !== '000000') {
        return {
          success: false,
          resultCode: result.resultCode,
          message: result.resultDesc || '获取反馈列表失败',
        };
      }

      return {
        success: true,
        resultCode: result.resultCode,
        total: result.data?.total ?? 0,
        list: result.data?.list ?? [],
      };
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        return { success: false, message: '请求超时' };
      }
      return {
        success: false,
        message: (error as Error).message || '获取反馈列表失败',
        error: error as Error,
      };
    }
  }

  /**
   * 拉取反馈附件图片并转为 data URL。
   * renderer 受 CSP(img-src 'self' data: blob:)与 webSecurity:true 限制，无法直接加载/抓取远程
   * http 图片，故与 feedback:list 一样走主进程 fetch；转 data URL 后 renderer 内联渲染不受 CSP 拦截。
   */
  async getFeedbackImage(path: string): Promise<FeedbackImageResponse> {
    try {
      const baseURL = this.configuredBaseURL();
      if (!baseURL) {
        return { success: false, message: '反馈服务未配置，请设置 CREW_FEEDBACK_BASE_URL' };
      }
      const url = resolveImageUrl(baseURL, path);
      const headers: Record<string, string> = { 'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*' };

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), FEEDBACK_CONFIG.timeout);
      const res = await fetch(url, { method: 'GET', headers, signal: controller.signal });
      clearTimeout(timer);

      if (!res.ok) {
        return { success: false, message: `图片加载失败(HTTP ${res.status})` };
      }
      const contentType = res.headers.get('content-type') || 'image/png';
      if (!contentType.startsWith('image/')) {
        return { success: false, message: `响应非图片(${contentType})` };
      }
      const buffer = Buffer.from(await res.arrayBuffer());
      if (buffer.length > MAX_FEEDBACK_IMAGE_BYTES) {
        return { success: false, message: '图片过大' };
      }
      return { success: true, dataUrl: `data:${contentType};base64,${buffer.toString('base64')}` };
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        return { success: false, message: '请求超时' };
      }
      return { success: false, message: (error as Error).message || '图片加载失败', error: error as Error };
    }
  }

}

export const feedbackServiceInstance = new FeedbackService();

export const submitFeedback = feedbackServiceInstance.submitFeedback.bind(feedbackServiceInstance);
export const getFeedbackList = feedbackServiceInstance.getFeedbackList.bind(feedbackServiceInstance);
export const getFeedbackImage = feedbackServiceInstance.getFeedbackImage.bind(feedbackServiceInstance);
