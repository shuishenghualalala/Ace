/**
 * 更新包下载控制器（单例：同一时刻只有一个下载）。
 *
 * 能力：
 *   - startDownload：全新下载；若已有不同版本在下载则抢占（舍旧下新），同版本则去重。
 *   - pause / resume：基于 Range 断点续传（服务器不支持 206 则从头再来）。
 *   - retry：错误/超时后从 .part 续传（无 part 则等同 start）。
 *   - 无进展超时：30s 收不到字节 → abort → 图标转红（保留 .part 供续传）。
 *   - quit：before-quit 时 abort 当前下载（.part 留盘，下次启动 sweep 清理）。
 *
 * 落盘：独占写入 `targetPath + '.part'`，通过同 inode 的 exclusive link 发布后写入
 * signature-bound update-state.downloaded；任何 identity/link/digest 漂移均拒绝。
 * 进度通过注入的 sendProgress 回调推送（由 index.ts 桥接到 renderer）。
 *
 * ponytail: 进度推送按 chunk 节流——大文件每 chunk 都 send 会刷爆 IPC，故只在整百分比变化时发。
 */
import * as fs from 'fs';
import * as path from 'path';
import type { IncomingMessage, RequestOptions } from 'http';
import https from 'https';
import { app } from 'electron';
import { sanitizeRequestHeaders } from '../request';
import { buildUpdateUrl, isExpectedUpdateUrl, normalizeVersion } from './update-url';
import {
  configuredUpdatePublicKey,
  verifyUpdateArtifact,
  type VerifiedUpdateArtifact,
} from './update-integrity';
import {
  MAX_UPDATE_PACKAGE_BYTES,
  MAX_UPDATE_SIGNATURE_BYTES,
  createSecureExclusiveFile,
  ensurePrivateUpdateDirectory,
  openSecureResumeFile,
  publishOpenFileExclusive,
  removeManagedUpdateFile,
  sameFileObject,
  snapshotSecureFile,
  writeAllToFile,
  type SecureFileSnapshot,
  type SecureOpenFile,
} from './update-file-security';
import { readUpdateState, setDownloadedRecord } from './update-state';
import type {
  DownloadedUpdateRecord,
  UpdateFileIdentity,
  VersionUpdateDownloadProgressPayload,
} from '../../shared/types';

const NO_PROGRESS_TIMEOUT_MS = 30_000;
const MAX_DOWNLOAD_DURATION_MS = 6 * 60 * 60 * 1000;
const MAX_UPDATE_REDIRECTS = 5;

type AbortReason = 'pause' | 'cancel' | 'timeout' | 'supersede' | 'quit' | 'policy';
type ControllerStatus = 'idle' | 'downloading' | 'paused' | 'completed' | 'error';

export interface Inflight {
  version: string;
  url: string;
  type: 'force' | 'reminder';
  message?: string;
  targetPath: string;
  partPath: string;
  receivedBytes: number;
  totalBytes: number;
  status: ControllerStatus;
  abort: AbortController;
  abortReason: AbortReason | null;
  partSnapshot: SecureFileSnapshot | null;
  partObjectIdentity: UpdateFileIdentity | null;
  noProgressTimer: ReturnType<typeof setTimeout> | null;
  lastEmittedPercent: number | null;
  redirectCount: number;
}

let inflight: Inflight | null = null;
let progressSender: ((p: VersionUpdateDownloadProgressPayload) => void) | null = null;

export function configureUpdateController(
  send: ((p: VersionUpdateDownloadProgressPayload) => void) | null,
): void {
  progressSender = send;
}

/** 当前在用的文件路径（cleanup 需跳过，避免删到正在写的 .part / 目标）。 */
export function activeFilePaths(): string[] {
  return inflight
    ? [inflight.partPath, inflight.targetPath, `${inflight.targetPath}.sig`, `${inflight.targetPath}.sig.part`]
    : [];
}

function updatesDir(): string {
  const userData = app.getPath('userData');
  fs.mkdirSync(userData, { recursive: true, mode: 0o700 });
  const directory = path.join(fs.realpathSync.native(userData), 'updates');
  ensurePrivateUpdateDirectory(directory);
  return directory;
}

function send(payload: VersionUpdateDownloadProgressPayload): void {
  try {
    progressSender?.(payload);
  } catch {
    /* webContents 可能已销毁 */
  }
}

function fileNameFromUrl(urlString: string): string {
  const parsed = new URL(urlString);
  const last = parsed.pathname.split('/').pop() || '';
  const decoded = decodeURIComponent(last);
  if (
    !decoded
    || decoded.trim() !== decoded
    || path.basename(decoded) !== decoded
    || decoded.split('').some((c) => c.charCodeAt(0) < 32 || '<>:"/\\|?*'.includes(c))
  ) {
    throw new Error('更新安装包文件名无效');
  }
  return decoded;
}

function safeUnlink(filePath: string): void {
  try {
    removeManagedUpdateFile(filePath);
  } catch {
    console.warn('[update-security] artifact-cleanup-rejected');
  }
}

function parseDeclaredLength(raw: string | null, label: string): number {
  if (raw === null) return 0;
  if (!/^(?:0|[1-9]\d*)$/.test(raw)) {
    throw new Error(`${label} Content-Length 无效`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} Content-Length 超出范围`);
  }
  return value;
}

async function downloadAndVerifySignature(
  packageUrl: string,
  packagePath: string,
  expectedVersion: string,
): Promise<VerifiedUpdateArtifact> {
  const publicKey = configuredUpdatePublicKey();
  if (!publicKey) {
    throw new Error('严格安全约束已阻止更新：未配置 ACE_UPDATE_PUBLIC_KEY');
  }
  const signaturePath = `${packagePath}.sig`;
  const temporaryPath = `${signaturePath}.part`;
  removeManagedUpdateFile(temporaryPath);
  removeManagedUpdateFile(signaturePath);
  const signatureUrl = `${packageUrl}.sig`;
  const response = await fetch(signatureUrl, {
    redirect: 'error',
    signal: AbortSignal.timeout(NO_PROGRESS_TIMEOUT_MS),
  });
  if (!response.ok || !response.body || response.url !== signatureUrl) {
    throw new Error(`更新包签名下载失败（HTTP ${response.status}）`);
  }
  const contentEncoding = response.headers.get('content-encoding');
  if (contentEncoding && contentEncoding.toLowerCase() !== 'identity') {
    throw new Error('更新包签名响应不得使用内容编码');
  }
  const declared = parseDeclaredLength(
    response.headers.get('content-length'),
    '更新包签名',
  );
  if (declared > MAX_UPDATE_SIGNATURE_BYTES) {
    throw new Error('更新包签名响应过大');
  }

  const temporary = createSecureExclusiveFile(temporaryPath);
  let received = 0;
  let published = false;
  try {
    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      received += value.byteLength;
      if (received > MAX_UPDATE_SIGNATURE_BYTES) {
        await reader.cancel();
        throw new Error('更新包签名响应过大');
      }
      writeAllToFile(temporary.fd, Buffer.from(value));
    }
    if (received === 0 || (declared > 0 && received !== declared)) {
      throw new Error('更新包签名响应长度不符');
    }
    publishOpenFileExclusive(
      temporaryPath,
      signaturePath,
      temporary.fd,
      temporary.identity,
      MAX_UPDATE_SIGNATURE_BYTES,
      'update signature',
    );
    published = true;
  } finally {
    fs.closeSync(temporary.fd);
    if (!published) safeUnlink(temporaryPath);
  }

  const verified = verifyUpdateArtifact(
    packagePath,
    signaturePath,
    publicKey,
    expectedVersion,
  );
  if (!verified.ok) {
    safeUnlink(signaturePath);
    throw new Error(verified.message || '更新包签名无效');
  }
  return verified.artifact!;
}

function pct(inf: Inflight): number | null {
  return inf.totalBytes > 0 ? Math.min(100, Math.max(0, Math.round((inf.receivedBytes / inf.totalBytes) * 100))) : null;
}

function emitProgress(inf: Inflight): void {
  const percent = pct(inf);
  // 整百分比变化才推送，避免大文件每 chunk 刷 IPC
  if (percent !== null && inf.lastEmittedPercent !== null && percent === inf.lastEmittedPercent) return;
  inf.lastEmittedPercent = percent;
  send({ phase: 'downloading', percent, receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
}

function clearNoProgressTimer(inf: Inflight): void {
  if (inf.noProgressTimer) {
    clearTimeout(inf.noProgressTimer);
    inf.noProgressTimer = null;
  }
}

function resetNoProgressTimer(inf: Inflight): void {
  clearNoProgressTimer(inf);
  inf.noProgressTimer = setTimeout(() => {
    inf.abortReason = 'timeout';
    try {
      inf.abort.abort();
    } catch {
      /* already aborted */
    }
  }, NO_PROGRESS_TIMEOUT_MS);
}

function requestOnce(
  urlString: string,
  resumeFrom: number,
  signal: AbortSignal,
): Promise<IncomingMessage> {
  return new Promise((resolve, reject) => {
    const url = new URL(urlString);
    const headers = sanitizeRequestHeaders(
      resumeFrom > 0 ? { Range: `bytes=${resumeFrom}-` } : {},
    );
    const options: RequestOptions = {
      protocol: 'https:',
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname,
      method: 'GET',
      headers,
      signal,
      timeout: NO_PROGRESS_TIMEOUT_MS,
    };
    const request = https.request(options, resolve);
    request.once('timeout', () => request.destroy(new Error('update request timed out')));
    request.once('error', reject);
    request.end();
  });
}

async function requestUpdateResponse(
  inf: Inflight,
  resumeFrom: number,
): Promise<IncomingMessage> {
  const expectedOrigin = new URL(buildUpdateUrl(inf.version)).origin;
  while (true) {
    const current = new URL(inf.url);
    if (
      current.protocol !== 'https:'
      || current.origin !== expectedOrigin
      || current.username
      || current.password
      || current.search
      || current.hash
    ) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：安装包地址超出构建时固定的 HTTPS 源');
    }

    const response = await requestOnce(inf.url, resumeFrom, inf.abort.signal);
    const statusCode = response.statusCode ?? 0;
    if (statusCode < 300 || statusCode >= 400) return response;

    const location = response.headers.location;
    response.destroy();
    if (!location) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：安装包重定向缺少目标地址');
    }
    const redirect = new URL(location, inf.url);
    if (
      redirect.protocol !== 'https:'
      || redirect.origin !== expectedOrigin
      || redirect.username
      || redirect.password
      || redirect.search
      || redirect.hash
    ) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：安装包重定向超出固定更新源');
    }
    if (inf.redirectCount >= MAX_UPDATE_REDIRECTS) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：安装包重定向次数过多');
    }
    inf.redirectCount += 1;
    inf.url = redirect.href;
  }
}

function contentLengthHeader(response: IncomingMessage): string | null {
  const raw = response.headers['content-length'];
  if (raw === undefined) return null;
  if (Array.isArray(raw)) throw new Error('安装包 Content-Length 重复');
  return raw;
}

export function validateDownloadResponse(
  inf: Inflight,
  response: IncomingMessage,
  resumeFrom: number,
): { append: boolean; totalBytes: number } {
  const statusCode = response.statusCode ?? 0;
  if (statusCode !== 200 && statusCode !== 206) {
    throw new Error(`下载失败（HTTP ${statusCode}）`);
  }
  const encoding = response.headers['content-encoding'];
  if (encoding && String(encoding).toLowerCase() !== 'identity') {
    inf.abortReason = 'policy';
    throw new Error('更新已阻止：安装包响应不得使用内容编码');
  }
  const contentLength = parseDeclaredLength(
    contentLengthHeader(response),
    '安装包',
  );

  if (statusCode === 206) {
    if (resumeFrom <= 0) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：全新下载收到未请求的分段响应');
    }
    const contentRange = response.headers['content-range'];
    if (typeof contentRange !== 'string') {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：断点响应缺少 Content-Range');
    }
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange);
    if (!match) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：断点响应 Content-Range 无效');
    }
    const start = Number(match[1]);
    const end = Number(match[2]);
    const total = Number(match[3]);
    if (
      !Number.isSafeInteger(start)
      || !Number.isSafeInteger(end)
      || !Number.isSafeInteger(total)
      || start !== resumeFrom
      || end < start
      || end + 1 !== total
      || contentLength <= 0
      || contentLength !== end - start + 1
      || total > MAX_UPDATE_PACKAGE_BYTES
    ) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：断点响应范围与本地片段不匹配');
    }
    return { append: true, totalBytes: total };
  }

  if (contentLength > MAX_UPDATE_PACKAGE_BYTES) {
    inf.abortReason = 'policy';
    throw new Error('更新已阻止：安装包超过大小上限');
  }
  return { append: false, totalBytes: contentLength };
}

function openDownloadWriter(
  inf: Inflight,
  append: boolean,
  resumeFrom: number,
): SecureOpenFile {
  if (append) {
    if (
      !inf.partSnapshot
      || inf.partSnapshot.identity.size !== resumeFrom
      || inf.receivedBytes !== resumeFrom
    ) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：断点下载记录缺失或大小不匹配');
    }
    try {
      const opened = openSecureResumeFile(
        inf.partPath,
        inf.partSnapshot,
        MAX_UPDATE_PACKAGE_BYTES,
        'partial update package',
      );
      inf.partObjectIdentity = opened.identity;
      return opened;
    } catch {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：断点下载片段已被替换或篡改');
    }
  }

  if (resumeFrom > 0) {
    if (!inf.partSnapshot) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：无法安全重置未知的断点下载片段');
    }
    try {
      removeManagedUpdateFile(inf.partPath, inf.partSnapshot.identity);
    } catch {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：断点下载片段在重置前发生变化');
    }
  }
  inf.receivedBytes = 0;
  inf.partSnapshot = null;
  try {
    const opened = createSecureExclusiveFile(inf.partPath);
    inf.partObjectIdentity = opened.identity;
    return opened;
  } catch {
    inf.abortReason = 'policy';
    throw new Error('更新已阻止：无法独占创建下载临时文件');
  }
}

function capturePartialSnapshot(inf: Inflight): boolean {
  if (inf.receivedBytes === 0) {
    inf.partSnapshot = null;
    inf.partObjectIdentity = null;
    safeUnlink(inf.partPath);
    return true;
  }
  if (!inf.partObjectIdentity) return false;
  try {
    const snapshot = snapshotSecureFile(
      inf.partPath,
      MAX_UPDATE_PACKAGE_BYTES,
      'partial update package',
    );
    if (
      snapshot.identity.size !== inf.receivedBytes
      || !sameFileObject(snapshot.identity, inf.partObjectIdentity)
    ) {
      throw new Error('partial update identity mismatch');
    }
    inf.partSnapshot = snapshot;
    inf.partObjectIdentity = snapshot.identity;
    return true;
  } catch {
    console.warn('[update-security] partial-resume-snapshot-rejected');
    safeUnlink(inf.partPath);
    inf.partSnapshot = null;
    inf.partObjectIdentity = null;
    inf.receivedBytes = 0;
    inf.totalBytes = 0;
    return false;
  }
}

async function runDownload(inf: Inflight): Promise<void> {
  let writer: SecureOpenFile | null = null;
  let response: IncomingMessage | null = null;
  const absoluteTimer = setTimeout(() => {
    inf.abortReason = 'timeout';
    inf.abort.abort();
  }, MAX_DOWNLOAD_DURATION_MS);
  try {
    const resumeFrom = inf.receivedBytes;
    if (resumeFrom < 0 || resumeFrom > MAX_UPDATE_PACKAGE_BYTES) {
      inf.abortReason = 'policy';
      throw new Error('更新已阻止：已有下载片段超过安装包大小上限');
    }

    response = await requestUpdateResponse(inf, resumeFrom);
    const responsePolicy = validateDownloadResponse(inf, response, resumeFrom);
    writer = openDownloadWriter(inf, responsePolicy.append, resumeFrom);
    inf.totalBytes = responsePolicy.totalBytes;
    inf.status = 'downloading';
    inf.lastEmittedPercent = null;
    send({
      phase: 'downloading',
      percent: pct(inf),
      receivedBytes: inf.receivedBytes,
      totalBytes: inf.totalBytes,
    });
    resetNoProgressTimer(inf);

    for await (const value of response) {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
      if (inf.receivedBytes + chunk.length > MAX_UPDATE_PACKAGE_BYTES) {
        inf.abortReason = 'policy';
        response.destroy();
        throw new Error('更新已阻止：安装包超过大小上限');
      }
      writeAllToFile(writer.fd, chunk);
      inf.receivedBytes += chunk.length;
      resetNoProgressTimer(inf);
      emitProgress(inf);
    }
    clearNoProgressTimer(inf);
    if (
      inf.receivedBytes <= 0
      || (inf.totalBytes > 0 && inf.receivedBytes !== inf.totalBytes)
    ) {
      throw new Error('安装包下载长度与响应声明不符');
    }

    const publishedIdentity = publishOpenFileExclusive(
      inf.partPath,
      inf.targetPath,
      writer.fd,
      writer.identity,
      MAX_UPDATE_PACKAGE_BYTES,
      'update package',
    );
    fs.closeSync(writer.fd);
    writer = null;
    inf.partSnapshot = null;
    inf.partObjectIdentity = publishedIdentity;

    let artifact: VerifiedUpdateArtifact;
    try {
      artifact = await downloadAndVerifySignature(
        inf.url,
        inf.targetPath,
        inf.version,
      );
    } catch (error) {
      inf.abortReason = 'policy';
      safeUnlink(inf.targetPath);
      safeUnlink(`${inf.targetPath}.sig`);
      safeUnlink(`${inf.targetPath}.sig.part`);
      throw error;
    }

    const record: DownloadedUpdateRecord = {
      schema: 1,
      filePath: inf.targetPath,
      version: inf.version,
      size: artifact.packageIdentity.size,
      type: inf.type,
      packageSha256: artifact.packageSha256,
      signatureSha256: artifact.signatureSha256,
      signatureMetadata: artifact.metadata,
      packageIdentity: artifact.packageIdentity,
      signatureIdentity: artifact.signatureIdentity,
      ...(inf.message ? { message: inf.message } : {}),
    };
    try {
      setDownloadedRecord(record);
    } catch {
      inf.abortReason = 'policy';
      safeUnlink(inf.targetPath);
      safeUnlink(`${inf.targetPath}.sig`);
      throw new Error('更新已阻止：无法原子持久化已验证安装包状态');
    }
    inf.status = 'completed';
    send({
      phase: 'downloaded',
      percent: 100,
      receivedBytes: record.size,
      totalBytes: record.size,
    });
  } catch (error) {
    clearNoProgressTimer(inf);
    response?.destroy();
    if (writer) {
      try {
        fs.fsyncSync(writer.fd);
      } catch {
        inf.abortReason = 'policy';
      }
      try {
        fs.closeSync(writer.fd);
      } catch {
        inf.abortReason = 'policy';
      }
    }
    await handleRunError(inf, error);
  } finally {
    clearTimeout(absoluteTimer);
  }
}

async function handleRunError(inf: Inflight, error: unknown): Promise<void> {
  console.warn(
    '[update-security] download-failed',
    inf.abortReason ?? 'request',
    error instanceof Error ? error.name : 'unknown',
  );
  clearNoProgressTimer(inf);
  const reason = inf.abortReason;

  if (reason === 'supersede' || reason === 'cancel') {
    safeUnlink(inf.partPath);
    return;
  }
  if (reason === 'policy') {
    safeUnlink(inf.partPath);
    inf.partSnapshot = null;
    inf.partObjectIdentity = null;
    inf.status = 'error';
    const rawMessage = error instanceof Error ? error.message : '';
    const policyMessage = rawMessage.startsWith('更新已阻止：')
      ? rawMessage.slice(0, 300)
      : '更新已被安全策略阻止，安装包或状态未通过安全校验';
    send({
      phase: 'error',
      message: policyMessage,
      percent: null,
      receivedBytes: 0,
      totalBytes: 0,
    });
    return;
  }

  const resumable = capturePartialSnapshot(inf);
  if (reason === 'pause') {
    if (!resumable) {
      inf.status = 'error';
      send({ phase: 'error', message: '下载片段完整性校验失败，已安全丢弃' });
    }
    return;
  }
  if (reason === 'quit') return;
  if (reason === 'timeout') {
    inf.status = 'error';
    send({
      phase: 'error',
      message: resumable ? '下载超时，可点击图标重试续传' : '下载超时，片段已安全丢弃',
      percent: pct(inf),
      receivedBytes: inf.receivedBytes,
      totalBytes: inf.totalBytes,
    });
    return;
  }
  inf.status = 'error';
  send({
    phase: 'error',
    message: resumable ? '下载失败，可点击图标重试续传' : '下载失败，片段已安全丢弃',
    percent: pct(inf),
    receivedBytes: inf.receivedBytes,
    totalBytes: inf.totalBytes,
  });
}

interface StartArgs {
  version: string;
  type: 'force' | 'reminder';
  message?: string;
  url?: string | undefined;
}

export function startDownload(args: StartArgs): { success: boolean; message?: string } {
  const normalizedVersion = normalizeVersion(args.version);
  if (!normalizedVersion) {
    const message = '更新已阻止：无法识别更新版本号';
    send({ phase: 'error', message });
    return { success: false, message };
  }
  // 同版本已在下载 / 暂停 → 去重（顺带修掉每 5min 心跳重新触发的竞态）
  if (
    inflight &&
    inflight.version === normalizedVersion &&
    (
      inflight.status === 'downloading'
      || inflight.status === 'paused'
      || inflight.status === 'completed'
    )
  ) {
    return {
      success: true,
      message: inflight.status === 'completed' ? '该版本已下载' : '该版本正在下载',
    };
  }

  // 不同版本在下载 / 暂停 → 抢占：abort 旧的，丢弃其 .part
  // （下载中的 .part 由其 catch(supersede) 在 destroy 流后删除；暂停中的流已关闭，这里直接删）
  if (inflight) {
    const old = inflight;
    old.abortReason = 'supersede';
    try {
      old.abort.abort();
    } catch {
      /* ignore */
    }
    if (old.status !== 'downloading') safeUnlink(old.partPath);
  }

  // 丢弃旧版本已完成的下载包（磁盘清理 + 避免装错版本）
  const state = readUpdateState();
  if (state.downloaded) {
    try {
      setDownloadedRecord(null);
      removeManagedUpdateFile(
        state.downloaded.filePath,
        state.downloaded.packageIdentity,
      );
      removeManagedUpdateFile(
        `${state.downloaded.filePath}.sig`,
        state.downloaded.signatureIdentity,
      );
    } catch {
      const message = '更新已阻止：无法安全清理先前的更新安装包';
      send({ phase: 'error', message });
      return { success: false, message };
    }
  }

  let url: string;
  try {
    url = buildUpdateUrl(normalizedVersion);
    if (args.url && !isExpectedUpdateUrl(args.url.trim(), normalizedVersion)) {
      throw new Error('更新安装包地址与构建时固定的更新源不匹配');
    }
  } catch (error) {
    const message = `更新已阻止：${(error as Error).message}`;
    send({ phase: 'error', message });
    return { success: false, message };
  }
  if (!configuredUpdatePublicKey()) {
    const message = '更新已阻止：构建未嵌入更新签名公钥';
    send({ phase: 'error', message });
    return { success: false, message };
  }

  let targetPath: string;
  let partPath: string;
  try {
    targetPath = path.join(updatesDir(), fileNameFromUrl(url));
    partPath = `${targetPath}.part`;
    removeManagedUpdateFile(partPath);
    removeManagedUpdateFile(targetPath);
    removeManagedUpdateFile(`${targetPath}.sig`);
    removeManagedUpdateFile(`${targetPath}.sig.part`);
  } catch {
    const message = '更新已阻止：更新目录或临时文件不安全';
    send({ phase: 'error', message });
    return { success: false, message };
  }

  inflight = {
    version: normalizedVersion,
    url,
    type: args.type,
    ...(args.message ? { message: args.message } : {}),
    targetPath,
    partPath,
    receivedBytes: 0,
    totalBytes: 0,
    status: 'downloading',
    abort: new AbortController(),
    abortReason: null,
    partSnapshot: null,
    partObjectIdentity: null,
    noProgressTimer: null,
    lastEmittedPercent: null,
    redirectCount: 0,
  };
  const inf = inflight;
  runDownload(inf).catch((e) => {
    console.error(
      '[update-download] unhandled',
      e instanceof Error ? e.name : 'unknown',
    );
  });
  return { success: true };
}

export function pauseDownload(): { success: boolean } {
  if (!inflight || inflight.status !== 'downloading') return { success: false };
  inflight.status = 'paused';
  inflight.abortReason = 'pause';
  try {
    inflight.abort.abort();
  } catch {
    /* ignore */
  }
  send({ phase: 'paused', percent: pct(inflight), receivedBytes: inflight.receivedBytes, totalBytes: inflight.totalBytes });
  return { success: true };
}

export function resumeDownload(): { success: boolean } {
  if (!inflight || (inflight.status !== 'paused' && inflight.status !== 'error')) return { success: false };
  inflight.abort = new AbortController();
  inflight.abortReason = null;
  inflight.redirectCount = 0;
  inflight.status = 'downloading';
  const inf = inflight;
  runDownload(inf).catch((e) => {
    console.error(
      '[update-download] resume unhandled',
      e instanceof Error ? e.name : 'unknown',
    );
  });
  return { success: true };
}

/** 重试：有 .part 则续传，否则等同全新下载。 */
export function retryDownload(args: StartArgs): { success: boolean; message?: string } {
  const normalizedVersion = normalizeVersion(args.version);
  if (
    normalizedVersion
    && inflight
    && inflight.version === normalizedVersion
    && (inflight.status === 'error' || inflight.status === 'paused')
  ) {
    // 复用已有 inflight（保留 receivedBytes / partPath）续传
    return resumeDownload();
  }
  return startDownload(args);
}

/** before-quit：中止当前下载（.part 留盘，下次启动 sweep 清理；不 emit）。 */
export function disposeForQuit(): void {
  if (!inflight) return;
  inflight.abortReason = 'quit';
  try {
    inflight.abort.abort();
  } catch {
    /* ignore */
  }
}
