import {
  createHash,
  randomBytes,
  randomUUID,
  timingSafeEqual,
} from 'node:crypto';

import type {
  FeedbackPayloadArgs,
  FeedbackSubmitArgs,
} from '../shared/ipc-schemas';

const DEFAULT_CONSENT_TTL_MS = 2 * 60 * 1000;
const MAX_CONSENT_TTL_MS = 5 * 60 * 1000;
const DEFAULT_TRACE_LIMIT = 80;
const MAX_TRACE_LIMIT = 200;
const MAX_PENDING_PREVIEWS = 32;
const MAX_AUTHORITIES = 32;
const MAX_FEEDBACK_IMAGES = 9;
const MAX_FEEDBACK_IMAGE_BYTES = 4 * 1024 * 1024;
const MAX_FEEDBACK_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024;
const ALLOWED_IMAGE_TYPES = new Set([
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/webp',
]);
const DATA_URL_RE = /^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$/;
const SENSITIVE_ASSIGNMENT_RE =
  /\b(authorization|proxy-authorization|api[_-]?key|access[_-]?key|auth[_-]?token|secret|token|password|passwd|pwd|cookie|session)\b(\s*[:=]\s*)(?:(?:bearer|basic)\s+)?(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi;
const PROXY_ASSIGNMENT_RE =
  /\b((?:https?|all|no|ftp)_proxy)\b(\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi;
const BEARER_RE = /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}/gi;
const URL_RE = /\bhttps?:\/\/[^\s<>"']+/gi;

export interface FeedbackConsentContext {
  desktopSessionId: string;
  origin: string;
  ownerId: string;
  webContentsId: number;
}

export interface FeedbackImagePreview {
  name: string;
  mimeType: string;
  bytes: number;
  digest: string;
}

export interface FeedbackSanitizedText {
  title: string;
  description: string;
}

export interface FeedbackPreviewSuccess {
  success: true;
  previewId: string;
  payload: FeedbackSanitizedText;
  expiresAt: number;
  titleDigest: string;
  descriptionDigest: string;
  images: FeedbackImagePreview[];
}

export interface FeedbackApprovalSuccess {
  success: true;
  authority: string;
  payload: FeedbackSanitizedText;
  expiresAt: number;
}

export interface FeedbackSecurityFailure {
  success: false;
  message: string;
  canceled?: boolean;
}

export type FeedbackPreviewResult = FeedbackPreviewSuccess | FeedbackSecurityFailure;
export type FeedbackApprovalResult = FeedbackApprovalSuccess | FeedbackSecurityFailure;

export interface FeedbackCancelResult {
  success: boolean;
  canceled: boolean;
  message: string;
}

export interface FeedbackTraceEntry {
  timestamp: number;
  event:
    | 'preview-created'
    | 'preview-canceled'
    | 'consent-approved'
    | 'submit-started'
    | 'submit-succeeded'
    | 'submit-failed'
    | 'submit-canceled'
    | 'submit-rejected';
  contextDigest: string;
  ownerDigest: string;
  payloadDigest?: string;
}

export interface FeedbackSecurityOptions {
  now?: () => number;
  consentTtlMs?: number;
  traceLimit?: number;
  feedbackDisabled?: () => boolean;
  cleanupAttachment?: (buffer: Buffer) => void;
}

interface PreparedAttachment extends FeedbackImagePreview {
  buffer: Buffer;
  ext: string;
}

interface PreparedFeedback {
  payload: FeedbackPayloadArgs;
  titleDigest: string;
  descriptionDigest: string;
  imageDigests: string[];
  payloadDigest: string;
  attachments: PreparedAttachment[];
}

interface PendingPreview {
  previewId: string;
  payload: FeedbackSanitizedText;
  contextDigest: string;
  ownerDigest: string;
  expiresAt: number;
  titleDigest: string;
  descriptionDigest: string;
  imageDigests: string[];
  payloadDigest: string;
  images: FeedbackImagePreview[];
}

interface AuthorityRecord {
  authority: string;
  contextDigest: string;
  ownerDigest: string;
  expiresAt: number;
  titleDigest: string;
  descriptionDigest: string;
  imageDigests: string[];
  payloadDigest: string;
  state: 'issued' | 'active';
  canceled: boolean;
  controller: AbortController | null;
}

export interface FeedbackSubmissionClaim {
  authority: string;
  form: FormData;
  signal: AbortSignal;
  isCanceled: () => boolean;
}

export type FeedbackSubmissionClaimResult =
  | { success: true; claim: FeedbackSubmissionClaim }
  | FeedbackSecurityFailure;

function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex');
}

function sameDigest(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left, 'hex');
  const rightBuffer = Buffer.from(right, 'hex');
  return leftBuffer.length === rightBuffer.length
    && leftBuffer.length > 0
    && timingSafeEqual(leftBuffer, rightBuffer);
}

function policyFlagEnabled(value: string | undefined): boolean {
  return /^(?:1|true|yes|on|disabled)$/i.test(value?.trim() ?? '');
}

function hasControlCharacters(value: string): boolean {
  for (const character of value) {
    const code = character.charCodeAt(0);
    if (code <= 0x1f || code === 0x7f) return true;
  }
  return false;
}

export function feedbackDisabledByEnterprisePolicy(
  environment: Readonly<NodeJS.ProcessEnv> = process.env,
): boolean {
  return policyFlagEnabled(environment['ACE_ENTERPRISE_DISABLE_FEEDBACK'])
    || policyFlagEnabled(environment['CREW_FEEDBACK_DISABLED']);
}

function redactUrl(raw: string): string {
  try {
    const parsed = new URL(raw);
    if (parsed.username) parsed.username = '[REDACTED]';
    if (parsed.password) parsed.password = '[REDACTED]';
    for (const name of Array.from(parsed.searchParams.keys())) {
      if (
        /(?:token|secret|password|passwd|pwd|key|auth|credential|cookie|session|signature|sig)/i
          .test(name)
      ) {
        parsed.searchParams.set(name, '[REDACTED]');
      }
    }
    return parsed.toString();
  } catch {
    return '[REDACTED_URL]';
  }
}

function stripUnsafeTextControls(value: string): string {
  let sanitized = '';
  for (const character of value.replace(/\r\n?/g, '\n')) {
    const code = character.codePointAt(0) ?? 0;
    if (
      (code <= 0x1f && character !== '\n' && character !== '\t')
      || code === 0x7f
      || (code >= 0x202a && code <= 0x202e)
      || (code >= 0x2066 && code <= 0x2069)
    ) {
      sanitized += ' ';
    } else {
      sanitized += character;
    }
  }
  return sanitized;
}

/** The only text sanitizer permitted before feedback preview, multipart, or diagnostics. */
export function redactFeedbackSecrets(value: string, maxChars = 5000): string {
  const bounded = stripUnsafeTextControls(value.slice(0, maxChars));
  return bounded
    .replace(PROXY_ASSIGNMENT_RE, (_match, name: string, separator: string) => (
      `${name}${separator}[REDACTED_PROXY]`
    ))
    .replace(SENSITIVE_ASSIGNMENT_RE, (_match, name: string, separator: string) => (
      `${name}${separator}[REDACTED]`
    ))
    .replace(BEARER_RE, '$1 [REDACTED]')
    .replace(URL_RE, (url) => redactUrl(url));
}

function contextIdentity(context: FeedbackConsentContext): {
  contextDigest: string;
  ownerDigest: string;
} {
  if (
    !context
    || typeof context.desktopSessionId !== 'string'
    || context.desktopSessionId.length === 0
    || context.desktopSessionId.length > 256
    || typeof context.origin !== 'string'
    || context.origin.length === 0
    || context.origin.length > 4096
    || typeof context.ownerId !== 'string'
    || context.ownerId.length === 0
    || context.ownerId.length > 512
    || !Number.isSafeInteger(context.webContentsId)
    || context.webContentsId < 0
  ) {
    throw new Error('反馈授权上下文无效');
  }
  const ownerDigest = sha256(context.ownerId);
  const contextDigest = sha256(JSON.stringify({
    desktopSessionId: context.desktopSessionId,
    origin: context.origin,
    ownerDigest,
    webContentsId: context.webContentsId,
  }));
  return { contextDigest, ownerDigest };
}

function hasExpectedImageSignature(buffer: Buffer, mimeType: string): boolean {
  if (mimeType === 'image/png') {
    return buffer.length >= 8
      && buffer.subarray(0, 8).equals(Buffer.from([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
      ]));
  }
  if (mimeType === 'image/jpeg') {
    return buffer.length >= 3
      && buffer[0] === 0xff
      && buffer[1] === 0xd8
      && buffer[2] === 0xff;
  }
  if (mimeType === 'image/gif') {
    const signature = buffer.subarray(0, 6).toString('ascii');
    return signature === 'GIF87a' || signature === 'GIF89a';
  }
  if (mimeType === 'image/webp') {
    return buffer.length >= 12
      && buffer.subarray(0, 4).toString('ascii') === 'RIFF'
      && buffer.subarray(8, 12).toString('ascii') === 'WEBP';
  }
  return false;
}

function dataUrlToAttachment(name: string, dataUrl: string): PreparedAttachment {
  if (
    !name
    || name.length > 128
    || /[\/\\]/.test(name)
    || hasControlCharacters(name)
    || name === '.'
    || name === '..'
  ) {
    throw new Error('反馈附件名称无效');
  }
  const match = DATA_URL_RE.exec(dataUrl);
  const mimeType = match?.[1]?.toLowerCase() ?? '';
  const base64 = match?.[2] ?? '';
  if (!ALLOWED_IMAGE_TYPES.has(mimeType) || !base64) {
    throw new Error('反馈附件必须是受支持的 base64 图片');
  }
  if (base64.length > Math.ceil(MAX_FEEDBACK_IMAGE_BYTES * 4 / 3) + 4) {
    throw new Error('反馈附件过大');
  }
  const buffer = Buffer.from(base64, 'base64');
  if (
    buffer.length === 0
    || buffer.length > MAX_FEEDBACK_IMAGE_BYTES
    || buffer.toString('base64').replace(/=+$/, '') !== base64.replace(/=+$/, '')
    || !hasExpectedImageSignature(buffer, mimeType)
  ) {
    buffer.fill(0);
    throw new Error('反馈附件编码无效或超过大小上限');
  }
  const safeName = redactFeedbackSecrets(name, 128);
  const ext = mimeType === 'image/jpeg' ? 'jpg' : (mimeType.split('/')[1] ?? 'bin');
  const digest = sha256(Buffer.concat([
    Buffer.from(`${safeName}\0${mimeType}\0`, 'utf8'),
    buffer,
  ]));
  return {
    name: safeName,
    mimeType,
    bytes: buffer.length,
    digest,
    buffer,
    ext,
  };
}

function prepareFeedbackPayload(raw: unknown): PreparedFeedback {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('反馈内容无效');
  }
  const source = raw as Record<string, unknown>;
  if (
    typeof source['title'] !== 'string'
    || source['title'].length === 0
    || source['title'].length > 200
    || typeof source['description'] !== 'string'
    || source['description'].length === 0
    || source['description'].length > 5000
  ) {
    throw new Error('反馈标题或描述无效');
  }
  const title = redactFeedbackSecrets(source['title'], 200).trim();
  const description = redactFeedbackSecrets(source['description'], 5000).trim();
  if (!title || !description) throw new Error('反馈标题或描述不能为空');

  const rawImages = source['images'];
  if (rawImages !== undefined && !Array.isArray(rawImages)) {
    throw new Error('反馈附件列表无效');
  }
  if ((rawImages?.length ?? 0) > MAX_FEEDBACK_IMAGES) {
    throw new Error(`反馈附件最多 ${MAX_FEEDBACK_IMAGES} 张`);
  }

  const attachments: PreparedAttachment[] = [];
  let totalBytes = 0;
  try {
    for (const image of rawImages ?? []) {
      if (!image || typeof image !== 'object' || Array.isArray(image)) {
        throw new Error('反馈附件无效');
      }
      const item = image as Record<string, unknown>;
      if (typeof item['name'] !== 'string' || typeof item['dataUrl'] !== 'string') {
        throw new Error('反馈附件无效');
      }
      const attachment = dataUrlToAttachment(item['name'], item['dataUrl']);
      totalBytes += attachment.bytes;
      if (totalBytes > MAX_FEEDBACK_TOTAL_IMAGE_BYTES) {
        attachment.buffer.fill(0);
        throw new Error('反馈附件总大小超过上限');
      }
      attachments.push(attachment);
    }

    const images = attachments.map((attachment) => ({
      name: attachment.name,
      dataUrl: `data:${attachment.mimeType};base64,${attachment.buffer.toString('base64')}`,
    }));
    const payload: FeedbackPayloadArgs = { title, description };
    if (images.length > 0) payload.images = images;
    const titleDigest = sha256(title);
    const descriptionDigest = sha256(description);
    const imageDigests = attachments.map((attachment) => attachment.digest);
    const payloadDigest = sha256(JSON.stringify({
      titleDigest,
      descriptionDigest,
      imageDigests,
    }));
    return {
      payload,
      titleDigest,
      descriptionDigest,
      imageDigests,
      payloadDigest,
      attachments,
    };
  } catch (error) {
    for (const attachment of attachments) attachment.buffer.fill(0);
    throw error;
  }
}

export class FeedbackConsentAuthority {
  private readonly previews = new Map<string, PendingPreview>();
  private readonly authorities = new Map<string, AuthorityRecord>();
  private readonly trace: FeedbackTraceEntry[] = [];
  private readonly now: () => number;
  private readonly consentTtlMs: number;
  private readonly traceLimit: number;
  private readonly feedbackDisabled: () => boolean;
  private readonly cleanupAttachment: (buffer: Buffer) => void;

  constructor(options: FeedbackSecurityOptions = {}) {
    this.now = options.now ?? Date.now;
    const requestedTtl = options.consentTtlMs ?? DEFAULT_CONSENT_TTL_MS;
    this.consentTtlMs = Number.isFinite(requestedTtl) && requestedTtl > 0
      ? Math.min(MAX_CONSENT_TTL_MS, requestedTtl)
      : DEFAULT_CONSENT_TTL_MS;
    const requestedTraceLimit = options.traceLimit ?? DEFAULT_TRACE_LIMIT;
    this.traceLimit = Number.isFinite(requestedTraceLimit) && requestedTraceLimit > 0
      ? Math.min(MAX_TRACE_LIMIT, Math.floor(requestedTraceLimit))
      : DEFAULT_TRACE_LIMIT;
    this.feedbackDisabled =
      options.feedbackDisabled ?? (() => feedbackDisabledByEnterprisePolicy());
    this.cleanupAttachment =
      options.cleanupAttachment ?? ((buffer) => { buffer.fill(0); });
  }

  isDisabled(): boolean {
    try {
      return this.feedbackDisabled();
    } catch {
      return true;
    }
  }

  private unavailable(configured: boolean): FeedbackSecurityFailure | null {
    if (this.isDisabled()) {
      return { success: false, message: '组织策略已禁用反馈功能' };
    }
    if (!configured) {
      return { success: false, message: '反馈服务未配置，请设置 CREW_FEEDBACK_BASE_URL' };
    }
    return null;
  }

  private cleanupPrepared(prepared: PreparedFeedback): void {
    let cleanupFailed = false;
    for (const attachment of prepared.attachments) {
      try {
        this.cleanupAttachment(attachment.buffer);
        if (attachment.buffer.some((value) => value !== 0)) cleanupFailed = true;
      } catch {
        cleanupFailed = true;
      } finally {
        attachment.buffer.fill(0);
      }
    }
    if (cleanupFailed) {
      throw new Error('反馈附件临时数据清理无法验证');
    }
  }

  private record(
    event: FeedbackTraceEntry['event'],
    identity: { contextDigest: string; ownerDigest: string },
    payloadDigest?: string,
  ): void {
    this.trace.push({
      timestamp: this.now(),
      event,
      ...identity,
      ...(payloadDigest ? { payloadDigest } : {}),
    });
    while (this.trace.length > this.traceLimit) this.trace.shift();
  }

  private pruneExpired(): void {
    const now = this.now();
    for (const [previewId, preview] of this.previews) {
      if (preview.expiresAt <= now) this.previews.delete(previewId);
    }
    for (const [authority, record] of this.authorities) {
      if (record.state === 'issued' && record.expiresAt <= now) {
        this.authorities.delete(authority);
      }
    }
  }

  private evictOldest<T>(map: Map<string, T>, max: number): void {
    while (map.size >= max) {
      const oldest = map.keys().next().value as string | undefined;
      if (!oldest) break;
      map.delete(oldest);
    }
  }

  private makeAuthoritySpace(): boolean {
    if (this.authorities.size < MAX_AUTHORITIES) return true;
    for (const [authority, record] of this.authorities) {
      if (record.state === 'issued') {
        this.authorities.delete(authority);
        return true;
      }
    }
    return false;
  }

  createPreview(
    raw: unknown,
    context: FeedbackConsentContext,
    configured: boolean,
  ): FeedbackPreviewResult {
    const unavailable = this.unavailable(configured);
    if (unavailable) return unavailable;
    let identity: ReturnType<typeof contextIdentity>;
    let prepared: PreparedFeedback;
    try {
      identity = contextIdentity(context);
      prepared = prepareFeedbackPayload(raw);
      this.cleanupPrepared(prepared);
    } catch (error) {
      return {
        success: false,
        message: redactFeedbackSecrets(
          error instanceof Error ? error.message : '反馈预览准备失败',
          300,
        ),
      };
    }
    this.pruneExpired();
    this.evictOldest(this.previews, MAX_PENDING_PREVIEWS);
    const previewId = randomUUID();
    const expiresAt = this.now() + this.consentTtlMs;
    const images = prepared.attachments.map(({ name, mimeType, bytes, digest }) => ({
      name,
      mimeType,
      bytes,
      digest,
    }));
    const payload: FeedbackSanitizedText = {
      title: prepared.payload.title,
      description: prepared.payload.description,
    };
    this.previews.set(previewId, {
      previewId,
      payload,
      ...identity,
      expiresAt,
      titleDigest: prepared.titleDigest,
      descriptionDigest: prepared.descriptionDigest,
      imageDigests: prepared.imageDigests,
      payloadDigest: prepared.payloadDigest,
      images,
    });
    this.record('preview-created', identity, prepared.payloadDigest);
    return {
      success: true,
      previewId,
      payload,
      expiresAt,
      titleDigest: prepared.titleDigest,
      descriptionDigest: prepared.descriptionDigest,
      images,
    };
  }

  approvePreview(
    previewId: string,
    context: FeedbackConsentContext,
    configured: boolean,
  ): FeedbackApprovalResult {
    const unavailable = this.unavailable(configured);
    if (unavailable) return unavailable;
    this.pruneExpired();
    let identity: ReturnType<typeof contextIdentity>;
    try {
      identity = contextIdentity(context);
    } catch (error) {
      return {
        success: false,
        message: error instanceof Error ? error.message : '反馈授权上下文无效',
      };
    }
    const preview = this.previews.get(previewId);
    if (
      !preview
      || preview.expiresAt <= this.now()
      || !sameDigest(preview.contextDigest, identity.contextDigest)
    ) {
      if (preview) this.previews.delete(previewId);
      return { success: false, message: '反馈预览已失效，请重新预览并同意' };
    }
    this.previews.delete(previewId);
    if (!this.makeAuthoritySpace()) {
      return { success: false, message: '正在提交的反馈过多，请稍后重试' };
    }
    const authority = randomBytes(32).toString('base64url');
    const expiresAt = this.now() + this.consentTtlMs;
    this.authorities.set(authority, {
      authority,
      contextDigest: preview.contextDigest,
      ownerDigest: preview.ownerDigest,
      expiresAt,
      titleDigest: preview.titleDigest,
      descriptionDigest: preview.descriptionDigest,
      imageDigests: [...preview.imageDigests],
      payloadDigest: preview.payloadDigest,
      state: 'issued',
      canceled: false,
      controller: null,
    });
    this.record('consent-approved', identity, preview.payloadDigest);
    return { success: true, authority, payload: preview.payload, expiresAt };
  }

  cancelPreview(previewId: string, context: FeedbackConsentContext): FeedbackCancelResult {
    let identity: ReturnType<typeof contextIdentity>;
    try {
      identity = contextIdentity(context);
    } catch {
      return { success: false, canceled: false, message: '反馈预览取消失败' };
    }
    const preview = this.previews.get(previewId);
    if (!preview || !sameDigest(preview.contextDigest, identity.contextDigest)) {
      return { success: false, canceled: false, message: '反馈预览不存在或已失效' };
    }
    this.previews.delete(previewId);
    this.record('preview-canceled', identity, preview.payloadDigest);
    return { success: true, canceled: true, message: '反馈预览已取消' };
  }

  claimSubmission(
    args: FeedbackSubmitArgs,
    context: FeedbackConsentContext,
    configured: boolean,
  ): FeedbackSubmissionClaimResult {
    const unavailable = this.unavailable(configured);
    if (unavailable) {
      this.authorities.delete(args.authority);
      return unavailable;
    }
    this.pruneExpired();
    let identity: ReturnType<typeof contextIdentity>;
    try {
      identity = contextIdentity(context);
    } catch {
      return { success: false, message: '反馈授权上下文无效' };
    }
    const record = this.authorities.get(args.authority);
    if (!record || record.state !== 'issued') {
      this.record('submit-rejected', identity);
      return { success: false, message: '缺少有效的一次性反馈同意授权' };
    }
    if (
      record.expiresAt <= this.now()
      || !sameDigest(record.contextDigest, identity.contextDigest)
    ) {
      this.authorities.delete(args.authority);
      this.record('submit-rejected', identity, record.payloadDigest);
      return { success: false, message: '反馈同意授权已过期或不属于当前会话' };
    }

    let prepared: PreparedFeedback;
    try {
      prepared = prepareFeedbackPayload(args);
    } catch (error) {
      this.authorities.delete(args.authority);
      this.record('submit-rejected', identity, record.payloadDigest);
      return {
        success: false,
        message: redactFeedbackSecrets(
          error instanceof Error ? error.message : '反馈内容无效',
          300,
        ),
      };
    }
    if (
      !sameDigest(record.titleDigest, prepared.titleDigest)
      || !sameDigest(record.descriptionDigest, prepared.descriptionDigest)
      || record.imageDigests.length !== prepared.imageDigests.length
      || record.imageDigests.some(
        (digest, index) => !sameDigest(digest, prepared.imageDigests[index] ?? ''),
      )
      || !sameDigest(record.payloadDigest, prepared.payloadDigest)
    ) {
      this.authorities.delete(args.authority);
      this.record('submit-rejected', identity, record.payloadDigest);
      try {
        this.cleanupPrepared(prepared);
      } catch {
        return {
          success: false,
          message: '反馈附件临时数据清理失败，安全策略已阻止上传',
        };
      }
      return { success: false, message: '反馈内容在同意后发生变化，已拒绝提交' };
    }

    let form: FormData;
    try {
      form = new FormData();
      form.append('userInfo', '{}');
      form.append('feedback', JSON.stringify({
        title: prepared.payload.title,
        description: prepared.payload.description,
        image: [],
      }));
      for (const [index, attachment] of prepared.attachments.entries()) {
        const blob = new Blob(
          [new Uint8Array(attachment.buffer)],
          { type: attachment.mimeType },
        );
        form.append(
          'images',
          blob,
          `feedback-${index + 1}-${attachment.digest.slice(0, 12)}.${attachment.ext}`,
        );
      }
      this.cleanupPrepared(prepared);
    } catch {
      for (const attachment of prepared.attachments) attachment.buffer.fill(0);
      this.authorities.delete(args.authority);
      this.record('submit-rejected', identity, record.payloadDigest);
      return {
        success: false,
        message: '反馈附件临时数据清理失败，安全策略已阻止上传',
      };
    }
    if (this.isDisabled()) {
      this.authorities.delete(args.authority);
      this.record('submit-rejected', identity, record.payloadDigest);
      return { success: false, message: '组织策略已禁用反馈功能' };
    }

    record.state = 'active';
    record.controller = new AbortController();
    this.record('submit-started', identity, record.payloadDigest);
    return {
      success: true,
      claim: {
        authority: record.authority,
        form,
        signal: record.controller.signal,
        isCanceled: () => record.canceled,
      },
    };
  }

  completeSubmission(
    authority: string,
    outcome: 'succeeded' | 'failed' | 'canceled',
  ): void {
    const record = this.authorities.get(authority);
    if (!record) return;
    this.authorities.delete(authority);
    this.record(
      outcome === 'succeeded'
        ? 'submit-succeeded'
        : (outcome === 'canceled' ? 'submit-canceled' : 'submit-failed'),
      {
        contextDigest: record.contextDigest,
        ownerDigest: record.ownerDigest,
      },
      record.payloadDigest,
    );
  }

  cancelFeedback(
    authority: string,
    context: FeedbackConsentContext,
  ): FeedbackCancelResult {
    let identity: ReturnType<typeof contextIdentity>;
    try {
      identity = contextIdentity(context);
    } catch {
      return { success: false, canceled: false, message: '反馈取消失败' };
    }
    const record = this.authorities.get(authority);
    if (!record || !sameDigest(record.contextDigest, identity.contextDigest)) {
      return { success: false, canceled: false, message: '反馈授权不存在或已失效' };
    }
    record.canceled = true;
    record.controller?.abort();
    this.authorities.delete(authority);
    this.record('submit-canceled', identity, record.payloadDigest);
    return { success: true, canceled: true, message: '反馈提交已取消' };
  }

  cancelAll(): void {
    for (const record of this.authorities.values()) {
      record.canceled = true;
      record.controller?.abort();
    }
    this.previews.clear();
    this.authorities.clear();
  }

  readTrace(context: FeedbackConsentContext): FeedbackTraceEntry[] {
    let identity: ReturnType<typeof contextIdentity>;
    try {
      identity = contextIdentity(context);
    } catch {
      return [];
    }
    return this.trace
      .filter((entry) => sameDigest(entry.contextDigest, identity.contextDigest))
      .map((entry) => ({ ...entry }));
  }

  clearTrace(context: FeedbackConsentContext): void {
    let identity: ReturnType<typeof contextIdentity>;
    try {
      identity = contextIdentity(context);
    } catch {
      return;
    }
    for (let index = this.trace.length - 1; index >= 0; index -= 1) {
      if (sameDigest(this.trace[index]?.contextDigest ?? '', identity.contextDigest)) {
        this.trace.splice(index, 1);
      }
    }
  }
}
