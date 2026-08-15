/**
 * 反馈服务模块（使用 Node 18+ 内置 fetch / FormData）
 * baseURL 从环境变量 `CREW_FEEDBACK_BASE_URL` 读取；开源版默认不连接任何外部服务。
 */

import type {
  FeedbackListArgs,
  FeedbackPayloadArgs,
  FeedbackSubmitArgs,
} from '../shared/ipc-schemas';
import {
  FeedbackConsentAuthority,
  redactFeedbackSecrets,
  type FeedbackApprovalResult,
  type FeedbackCancelResult,
  type FeedbackConsentContext,
  type FeedbackPreviewResult,
  type FeedbackSecurityOptions,
  type FeedbackTraceEntry,
} from './feedback-security';

export type {
  FeedbackConsentContext,
  FeedbackTraceEntry,
} from './feedback-security';

export const FEEDBACK_CONFIG = {
  baseURL: process.env['CREW_FEEDBACK_BASE_URL']?.trim() || '',
  timeout: 30000,
};

/** 单张附件图片最大字节数（转 data URL 前的体积上限）。 */
const MAX_FEEDBACK_IMAGE_BYTES = 4 * 1024 * 1024;
const MAX_FEEDBACK_RESPONSE_BYTES = 2 * 1024 * 1024;
const ALLOWED_FEEDBACK_IMAGE_TYPES = new Set([
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/webp',
]);

export interface FeedbackAttachment {
  name: string;
  dataUrl: string;
}

export interface FeedbackResponse {
  success: boolean;
  canceled?: boolean | undefined;
  resultCode?: string | undefined;
  statusCode?: number | undefined;
  message?: string | undefined;
  data?: Record<string, unknown> | undefined;
  isHTML?: boolean | undefined;
  error?: Error | undefined;
}

export type FeedbackServiceOptions = FeedbackSecurityOptions;

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

const FEEDBACK_STATUSES = new Set<FeedbackListItem['status']>([
  'PENDING',
  'PROCESSING',
  'RESOLVED',
  'CLOSED',
]);

function boundedRemoteText(
  value: unknown,
  maxChars: number,
  required = false,
): string | undefined {
  if (typeof value !== 'string' || (required && value.length === 0)) return undefined;
  return redactFeedbackSecrets(value, maxChars);
}

function safeFeedbackErrorMessage(
  error: unknown,
  fallback: string,
  maxChars = 300,
): string {
  const message = error instanceof Error && typeof error.message === 'string'
    ? error.message
    : (typeof error === 'string' ? error : fallback);
  return redactFeedbackSecrets(message, maxChars);
}

function parseFeedbackListItem(raw: unknown): FeedbackListItem | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  const id = value['id'];
  const title = boundedRemoteText(value['title'], 200, true);
  const status = value['status'];
  const createdAt = boundedRemoteText(value['createdAt'], 64, true);
  if (
    !Number.isSafeInteger(id)
    || Number(id) < 0
    || !title
    || !FEEDBACK_STATUSES.has(status as FeedbackListItem['status'])
    || !createdAt
  ) {
    return null;
  }
  const item: FeedbackListItem = {
    id: Number(id),
    title,
    status: status as FeedbackListItem['status'],
    createdAt,
  };
  const description = boundedRemoteText(value['description'], 5000);
  const images = boundedRemoteText(value['images'], 32 * 1024);
  const adminReply = value['adminReply'] === null
    ? null
    : boundedRemoteText(value['adminReply'], 5000);
  const updatedAt = boundedRemoteText(value['updatedAt'], 64);
  if (description !== undefined) item.description = description;
  if (images !== undefined) item.images = images;
  if (adminReply !== undefined) item.adminReply = adminReply;
  if (updatedAt !== undefined) item.updatedAt = updatedAt;
  return item;
}

function normalizeFeedbackBaseURL(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    if (
      parsed.protocol !== 'https:'
      || parsed.username
      || parsed.password
      || parsed.search
      || parsed.hash
    ) {
      return null;
    }
    return parsed.toString().replace(/\/+$/, '');
  } catch {
    return null;
  }
}

/** Resolve one server-returned image path without allowing an origin escape. */
function resolveImageUrl(baseURL: string, imagePath: string): string {
  if (
    !imagePath
    || imagePath.startsWith('/')
    || imagePath.includes('\\')
    || imagePath.includes('?')
    || imagePath.includes('#')
  ) {
    throw new Error('反馈图片路径无效');
  }
  const base = new URL(`${baseURL}/`);
  const resolved = new URL(imagePath, base);
  if (resolved.origin !== base.origin || !resolved.pathname.startsWith(base.pathname)) {
    throw new Error('反馈图片路径越过服务边界');
  }
  return resolved.toString();
}

async function readBoundedBody(response: Response, maxBytes: number): Promise<Buffer> {
  const declared = response.headers.get('content-length');
  if (declared !== null) {
    const size = Number(declared);
    if (!Number.isSafeInteger(size) || size < 0 || size > maxBytes) {
      throw new Error('反馈服务响应过大');
    }
  }
  if (!response.body) return Buffer.alloc(0);
  const reader = response.body.getReader();
  const chunks: Buffer[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new Error('反馈服务响应过大');
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, total);
}

async function fetchFeedback(
  url: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<Response> {
  const timeoutSignal = AbortSignal.timeout(FEEDBACK_CONFIG.timeout);
  const requestSignal = signal
    ? AbortSignal.any([signal, timeoutSignal])
    : timeoutSignal;
  const response = await fetch(url, {
    ...init,
    redirect: 'error',
    signal: requestSignal,
  });
  if (
    response.redirected
    || (response.url !== '' && response.url !== url)
  ) {
    try {
      await response.body?.cancel();
    } catch {
      // The response is rejected regardless of body cancellation support.
    }
    throw new Error('反馈服务响应越过精确请求地址');
  }
  return response;
}

export class FeedbackService {
  private baseURL: string = FEEDBACK_CONFIG.baseURL;
  private readonly consent: FeedbackConsentAuthority;

  constructor(options: FeedbackServiceOptions = {}) {
    this.consent = new FeedbackConsentAuthority(options);
  }

  setBaseURL(url: string): void {
    if (url.trim() && normalizeFeedbackBaseURL(url) === null) {
      throw new Error('反馈服务地址必须是无凭据、无查询参数的 HTTPS URL');
    }
    this.baseURL = url;
  }

  getBaseURL(): string {
    return this.baseURL;
  }

  private configuredBaseURL(): string | null {
    return normalizeFeedbackBaseURL(this.baseURL);
  }

  createPreview(
    args: FeedbackPayloadArgs,
    context: FeedbackConsentContext,
  ): FeedbackPreviewResult {
    return this.consent.createPreview(args, context, this.configuredBaseURL() !== null);
  }

  approvePreview(
    previewId: string,
    context: FeedbackConsentContext,
  ): FeedbackApprovalResult {
    return this.consent.approvePreview(
      previewId,
      context,
      this.configuredBaseURL() !== null,
    );
  }

  cancelPreview(
    previewId: string,
    context: FeedbackConsentContext,
  ): FeedbackCancelResult {
    return this.consent.cancelPreview(previewId, context);
  }

  cancelFeedback(
    authority: string,
    context: FeedbackConsentContext,
  ): FeedbackCancelResult {
    return this.consent.cancelFeedback(authority, context);
  }

  cancelAll(): void {
    this.consent.cancelAll();
  }

  readTrace(context: FeedbackConsentContext): FeedbackTraceEntry[] {
    return this.consent.readTrace(context);
  }

  clearTrace(context: FeedbackConsentContext): void {
    this.consent.clearTrace(context);
  }

  async submitFeedback(
    args: FeedbackSubmitArgs,
    context: FeedbackConsentContext,
  ): Promise<FeedbackResponse> {
    const baseURL = this.configuredBaseURL();
    const claimed = this.consent.claimSubmission(args, context, baseURL !== null);
    if (!claimed.success) return claimed;
    const { claim } = claimed;
    let outcome: 'succeeded' | 'failed' | 'canceled' = 'failed';
    try {
      if (!baseURL) {
        return { success: false, message: '反馈服务未配置，请设置 CREW_FEEDBACK_BASE_URL' };
      }

      const url = `${baseURL}/api/feedback/submit`;
      const res = await fetchFeedback(url, {
        method: 'POST',
        body: claim.form,
      }, claim.signal);
      const responseText = (await readBoundedBody(
        res,
        MAX_FEEDBACK_RESPONSE_BYTES,
      )).toString('utf8');

      if (responseText.startsWith('<') || responseText.startsWith('<!')) {
        return {
          success: false,
          statusCode: res.status,
          message: '服务器返回 HTML 页面，请检查接口地址或请求格式',
          isHTML: true,
        };
      }

      let result: unknown;
      try {
        result = JSON.parse(responseText);
      } catch {
        return {
          success: false,
          statusCode: res.status,
          message: '反馈服务返回了无效 JSON',
        };
      }
      if (!result || typeof result !== 'object' || Array.isArray(result)) {
        return {
          success: false,
          statusCode: res.status,
          message: '反馈服务返回了无效响应',
        };
      }
      const responseRecord = result as Record<string, unknown>;
      const resultCode = typeof responseRecord['resultCode'] === 'string'
        ? redactFeedbackSecrets(responseRecord['resultCode'], 64)
        : undefined;
      const resultDesc = typeof responseRecord['resultDesc'] === 'string'
        ? redactFeedbackSecrets(responseRecord['resultDesc'], 1000)
        : undefined;

      const isSuccessStatus = res.status >= 200 && res.status < 300;
      if (!isSuccessStatus || resultCode !== '000000') {
        return {
          success: false,
          resultCode,
          statusCode: res.status,
          message: resultDesc || '提交反馈失败',
        };
      }

      outcome = 'succeeded';
      return {
        success: true,
        resultCode,
        statusCode: res.status,
        message: resultDesc || '反馈提交成功',
      };
    } catch (error) {
      if (claim.isCanceled()) {
        outcome = 'canceled';
        return { success: false, canceled: true, message: '反馈提交已取消' };
      }
      if (error instanceof Error && error.name === 'AbortError') {
        return { success: false, message: '请求超时' };
      }
      return {
        success: false,
        message: safeFeedbackErrorMessage(error, '提交反馈失败'),
      };
    } finally {
      this.consent.completeSubmission(claim.authority, outcome);
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
      if (this.consent.isDisabled()) {
        return { success: false, message: '组织策略已禁用反馈功能' };
      }
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
      const res = await fetchFeedback(url, {
        method: 'GET',
        headers,
      });
      const responseText = (await readBoundedBody(
        res,
        MAX_FEEDBACK_RESPONSE_BYTES,
      )).toString('utf8');

      if (responseText.startsWith('<') || responseText.startsWith('<!')) {
        return {
          success: false,
          message: '服务器返回 HTML 页面，请检查接口地址或请求格式',
        };
      }

      let result: unknown;
      try {
        result = JSON.parse(responseText);
      } catch {
        return {
          success: false,
          message: '反馈服务返回了无效 JSON',
        };
      }
      if (!result || typeof result !== 'object' || Array.isArray(result)) {
        return { success: false, message: '反馈服务返回了无效响应' };
      }
      const responseRecord = result as Record<string, unknown>;
      const resultCode = boundedRemoteText(responseRecord['resultCode'], 64);
      const resultDesc = boundedRemoteText(responseRecord['resultDesc'], 1000);
      const data = responseRecord['data'];
      const dataRecord = data && typeof data === 'object' && !Array.isArray(data)
        ? data as Record<string, unknown>
        : null;

      const isSuccessStatus = res.status >= 200 && res.status < 300;
      if (!isSuccessStatus || resultCode !== '000000') {
        return {
          success: false,
          resultCode,
          message: resultDesc || '获取反馈列表失败',
        };
      }
      const totalValue = dataRecord?.['total'];
      const total = Number.isSafeInteger(totalValue)
        && Number(totalValue) >= 0
        ? Math.min(Number(totalValue), 1_000_000_000)
        : 0;
      const rawList = dataRecord?.['list'];
      const list = Array.isArray(rawList)
        ? rawList
          .slice(0, params.size)
          .map(parseFeedbackListItem)
          .filter((item): item is FeedbackListItem => item !== null)
        : [];

      return {
        success: true,
        resultCode,
        total,
        list,
      };
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        return { success: false, message: '请求超时' };
      }
      return {
        success: false,
        message: safeFeedbackErrorMessage(error, '获取反馈列表失败'),
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
      if (this.consent.isDisabled()) {
        return { success: false, message: '组织策略已禁用反馈功能' };
      }
      const baseURL = this.configuredBaseURL();
      if (!baseURL) {
        return { success: false, message: '反馈服务未配置，请设置 CREW_FEEDBACK_BASE_URL' };
      }
      const url = resolveImageUrl(baseURL, path);
      const headers: Record<string, string> = { 'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*' };

      const res = await fetchFeedback(url, { method: 'GET', headers });

      if (!res.ok) {
        return { success: false, message: `图片加载失败(HTTP ${res.status})` };
      }
      const contentType = (res.headers.get('content-type') || '').split(';', 1)[0]?.trim().toLowerCase() || '';
      if (!ALLOWED_FEEDBACK_IMAGE_TYPES.has(contentType)) {
        return { success: false, message: `响应非图片(${contentType})` };
      }
      const buffer = await readBoundedBody(res, MAX_FEEDBACK_IMAGE_BYTES);
      return { success: true, dataUrl: `data:${contentType};base64,${buffer.toString('base64')}` };
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        return { success: false, message: '请求超时' };
      }
      return {
        success: false,
        message: safeFeedbackErrorMessage(error, '图片加载失败'),
      };
    }
  }

}

export const feedbackServiceInstance = new FeedbackService();

export const submitFeedback = feedbackServiceInstance.submitFeedback.bind(feedbackServiceInstance);
export const getFeedbackList = feedbackServiceInstance.getFeedbackList.bind(feedbackServiceInstance);
export const getFeedbackImage = feedbackServiceInstance.getFeedbackImage.bind(feedbackServiceInstance);
