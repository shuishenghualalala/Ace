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
 * 落盘：写到 `targetPath + '.part'`，完成后 rename 为 targetPath 并写入 update-state.downloaded。
 * 进度通过注入的 sendProgress 回调推送（由 index.ts 桥接到 renderer）。
 *
 * ponytail: 进度推送按 chunk 节流——大文件每 chunk 都 send 会刷爆 IPC，故只在整百分比变化时发。
 */
import * as fs from 'fs';
import * as path from 'path';
import { randomUUID } from 'node:crypto';
import http from 'http';
import https from 'https';
import { app } from 'electron';
import { buildUpdateUrl } from './update-url';
import {
  configuredUpdatePublicKey,
  verifyPackageIntegrity,
  verifyPackageSignature,
} from './update-integrity';
import { isSecureRemoteUrl } from '../request';
import { evaluateVersionUpdate } from '../version-compare';
import { readUpdateState, setDownloadedRecord } from './update-state';
import type {
  DownloadedUpdateRecord,
  VersionUpdateDownloadProgressPayload,
} from '../../shared/types';

const NO_PROGRESS_TIMEOUT_MS = 30_000;

type AbortReason = 'pause' | 'cancel' | 'timeout' | 'supersede' | 'quit';
type ControllerStatus = 'idle' | 'downloading' | 'paused' | 'completed' | 'error';

interface Inflight {
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
  task?: Promise<void>;
  writeStream: fs.WriteStream | null;
  noProgressTimer: ReturnType<typeof setTimeout> | null;
  lastEmittedPercent: number | null;
}

let inflight: Inflight | null = null;
const downloading = new Set<Promise<void>>();
let progressSender: ((p: VersionUpdateDownloadProgressPayload) => void) | null = null;
let strictSecurityEnabled = (): boolean => true;

export function configureUpdateController(
  send: ((p: VersionUpdateDownloadProgressPayload) => void) | null,
  strictSecurityResolver: () => boolean = (): boolean => true,
): void {
  progressSender = send;
  strictSecurityEnabled = strictSecurityResolver;
}

/** 当前在用的文件路径（cleanup 需跳过，避免删到正在写的 .part / 目标）。 */
export function activeFilePaths(): string[] {
  return inflight
    ? [inflight.partPath, inflight.targetPath, `${inflight.targetPath}.sig`, `${inflight.targetPath}.sig.part`]
    : [];
}

function updatesDir(): string {
  return path.join(app.getPath('userData'), 'updates');
}

function send(version: string, payload: VersionUpdateDownloadProgressPayload): void {
  try {
    progressSender?.({ ...payload, version });
  } catch {
    /* webContents 可能已销毁 */
  }
}

function fileNameFromUrl(urlString: string): string {
  try {
    const parsed = new URL(urlString);
    const last = parsed.pathname.split('/').pop() || '';
    const decoded = decodeURIComponent(last).trim();
    if (!decoded) return `crew-update-${Date.now()}`;
    return decoded.split('').map((c) => (c.charCodeAt(0) < 32 || '<>:"/\\|?*'.includes(c) ? '_' : c)).join('');
  } catch {
    return `crew-update-${Date.now()}`;
  }
}

function safeUnlink(p: string): void {
  try {
    fs.rmSync(p, { force: true });
  } catch {
    /* ignore */
  }
}

async function downloadAndVerifySignature(
  packageUrl: string, packagePath: string, signal: AbortSignal,
): Promise<void> {
  const publicKey = configuredUpdatePublicKey();
  if (!publicKey) {
    throw new Error('无法验证更新包签名：未配置更新签名公钥');
  }
  const signaturePath = `${packagePath}.sig`;
  const temporaryPath = `${signaturePath}.part`;
  safeUnlink(temporaryPath);
  const response = await fetch(`${packageUrl}.sig`, { redirect: 'follow', signal });
  if (!response.ok || !response.body || new URL(response.url).protocol !== 'https:') {
    throw new Error(`更新包签名下载失败（HTTP ${response.status}）`);
  }
  const declared = Number.parseInt(response.headers.get('content-length') || '0', 10) || 0;
  if (declared > 16 * 1024) throw new Error('更新包签名响应过大');
  const chunks: Buffer[] = [];
  let received = 0;
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value) continue;
    received += value.byteLength;
    if (received > 16 * 1024) throw new Error('更新包签名响应过大');
    chunks.push(Buffer.from(value));
  }
  signal.throwIfAborted();
  fs.writeFileSync(temporaryPath, Buffer.concat(chunks), { mode: 0o600 });
  fs.renameSync(temporaryPath, signaturePath);
  const verified = verifyPackageSignature(packagePath, signaturePath, publicKey);
  if (!verified.ok) {
    safeUnlink(signaturePath);
    throw new Error(verified.message || '更新包签名无效');
  }
}

function pct(inf: Inflight): number | null {
  return inf.totalBytes > 0 ? Math.min(100, Math.max(0, Math.round((inf.receivedBytes / inf.totalBytes) * 100))) : null;
}

function emitProgress(inf: Inflight): void {
  const percent = pct(inf);
  // 整百分比变化才推送，避免大文件每 chunk 刷 IPC
  if (percent !== null && inf.lastEmittedPercent !== null && percent === inf.lastEmittedPercent) return;
  inf.lastEmittedPercent = percent;
  send(inf.version, { phase: 'downloading', percent, receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
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

function safeEndStream(inf: Inflight, destroy = false): Promise<void> {
  const stream = inf.writeStream;
  inf.writeStream = null;
  if (!stream || stream.closed) return Promise.resolve();
  return new Promise((resolve) => {
    stream.once('error', () => { /* close still owns the file descriptor */ });
    stream.once('close', resolve);
    try {
      if (destroy) stream.destroy();
      else stream.end();
    } catch {
      resolve();
    }
  });
}

async function runDownload(inf: Inflight): Promise<void> {
  console.log('[Download] runDownload url:', inf.url, 'resumeFrom:', inf.receivedBytes);
  try {
    const resumeFrom = inf.receivedBytes;
    const signal = inf.abort.signal;
    signal.throwIfAborted();

    await new Promise<void>((resolve, reject) => {
      const urlObj = new URL(inf.url);
      const isHttps = urlObj.protocol === 'https:';
      const requestLib = isHttps ? https : http;

      const reqHeaders: Record<string, string> = {};
      if (resumeFrom > 0) reqHeaders['Range'] = `bytes=${resumeFrom}-`;

      const reqOpts: http.RequestOptions = {
        hostname: urlObj.hostname,
        port: urlObj.port || (isHttps ? 443 : 80),
        path: urlObj.pathname + urlObj.search,
        method: 'GET',
        headers: reqHeaders,
        signal,
      };

      const req = requestLib.request(reqOpts, (res) => {
        if (signal.aborted) {
          res.destroy();
          reject(signal.reason);
          return;
        }
        const statusCode = res.statusCode ?? 0;

        // 处理重定向
        if (statusCode >= 300 && statusCode < 400 && res.headers.location) {
          inf.url = new URL(res.headers.location, inf.url).href;
          console.log('[Download] redirect to:', inf.url);
          // 严格安全：手动 follow 重定向时，禁止落到非 HTTPS（替代 fetch 自动 follow 后的 isSecureRemoteUrl 校验）
          if (!isSecureRemoteUrl(inf.url, strictSecurityEnabled())) {
            reject(new Error('更新包下载被阻止：重定向地址未使用 HTTPS'));
            return;
          }
          req.destroy();
          resolve(runDownload(inf));
          return;
        }

        const rangeHonored = statusCode === 206;
        if (statusCode < 200 || statusCode >= 300) {
          reject(new Error(`下载失败（HTTP ${statusCode}）`));
          return;
        }

        let startOffset = resumeFrom;
        if (resumeFrom > 0 && !rangeHonored) {
          startOffset = 0;
          inf.receivedBytes = 0;
        }
        const contentLength = Number.parseInt(String(res.headers['content-length'] ?? '0'), 10) || 0;
        inf.totalBytes = contentLength > 0 ? contentLength + startOffset : 0;

        fs.mkdirSync(path.dirname(inf.targetPath), { recursive: true });
        inf.writeStream = fs.createWriteStream(inf.partPath, { flags: startOffset > 0 ? 'a' : 'w' });
        inf.writeStream.on('error', (error) => {
          res.destroy();
          reject(error);
        });

        inf.status = 'downloading';
        inf.lastEmittedPercent = null;
        send(inf.version, { phase: 'downloading', percent: pct(inf), receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
        resetNoProgressTimer(inf);

        res.on('data', (chunk: Buffer) => {
          inf.receivedBytes += chunk.byteLength;
          const drained = inf.writeStream?.write(chunk);
          if (drained === false) {
            res.pause();
            inf.writeStream?.once('drain', () => res.resume());
          }
          clearNoProgressTimer(inf);
          resetNoProgressTimer(inf);
          emitProgress(inf);
        });

        res.on('end', () => {
          resolve(completeDownload(inf, signal));
        });

        res.on('error', (err) => reject(err));
      });

      req.on('timeout', () => {
        req.destroy(new Error('下载超时'));
      });

      req.on('error', (err) => reject(err));

      req.end();
    });
  } catch (err) {
    await handleRunError(inf, err);
  }
}

async function completeDownload(inf: Inflight, signal: AbortSignal): Promise<void> {
  clearNoProgressTimer(inf);
  const stream = inf.writeStream;
  await safeEndStream(inf);
  if (stream?.errored) throw stream.errored;
  signal.throwIfAborted();
  fs.renameSync(inf.partPath, inf.targetPath);
  const size = fs.statSync(inf.targetPath).size;
  try {
    const integrity = verifyPackageIntegrity(inf.targetPath, inf.totalBytes);
    if (!integrity.ok) throw new Error(integrity.message);
    if (strictSecurityEnabled()) await downloadAndVerifySignature(inf.url, inf.targetPath, signal);
    signal.throwIfAborted();
    const record: DownloadedUpdateRecord = {
      filePath: inf.targetPath, version: inf.version, size, type: inf.type,
      ...(inf.message ? { message: inf.message } : {}),
    };
    const previous = readUpdateState().downloaded;
    setDownloadedRecord(record);
    // Older records may use the original unprefixed filename.
    if (previous && previous.filePath !== record.filePath && path.dirname(previous.filePath) === updatesDir()) {
      safeUnlink(previous.filePath);
      safeUnlink(`${previous.filePath}.sig`);
    }
  } catch (error) {
    if (inf.abortReason === 'quit' || inf.abortReason === 'pause') {
      fs.renameSync(inf.targetPath, inf.partPath);
    } else {
      safeUnlink(inf.targetPath);
      inf.receivedBytes = 0;
    }
    safeUnlink(`${inf.targetPath}.sig`);
    safeUnlink(`${inf.targetPath}.sig.part`);
    throw error;
  }
  inf.status = 'completed';
  send(inf.version, { phase: 'downloaded', percent: 100, receivedBytes: size, totalBytes: size });
}

function launchDownload(inf: Inflight, resume = false): void {
  const task: Promise<void> = (inf.task ?? Promise.resolve()).then(async () => {
    if (inflight !== inf || inf.task !== task || inf.status !== 'downloading') return;
    if (resume) {
      inf.abort = new AbortController();
      inf.abortReason = null;
      inf.receivedBytes = fs.existsSync(inf.partPath) ? fs.statSync(inf.partPath).size : 0;
    }
    await runDownload(inf);
  }).catch((error: unknown) => handleRunError(inf, error)).finally(() => downloading.delete(task));
  inf.task = task;
  downloading.add(task);
}

async function handleRunError(inf: Inflight, err: unknown): Promise<void> {
  const cause = err && typeof err === 'object' && 'cause' in err ? err.cause : undefined;
  console.log('[Download] handleRunError reason:', inf.abortReason, 'error:', (err as Error)?.message, 'cause:', cause, 'stack:', (err as Error)?.stack);
  clearNoProgressTimer(inf);
  const reason = inf.abortReason;

  if (reason === 'supersede' || reason === 'cancel') {
    await safeEndStream(inf, true);
    safeUnlink(inf.partPath);
    return; // 不 emit；由 startDownload 的新下载接管
  }

  await safeEndStream(inf);

  if (reason === 'pause') {
    // pauseDownload 已同步设置 status='paused' 并 emit，这里只负责 flush
    return;
  }
  if (reason === 'quit') {
    return; // 退出中不 emit；.part 留盘，下次启动 sweep 清理
  }
  if (reason === 'timeout') {
    inf.status = 'error';
    send(inf.version, { phase: 'error', message: '下载超时，可点击图标重试续传', percent: pct(inf), receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
    return;
  }
  // 网络错误 / 未知 abort：保留 .part 供重试
  inf.status = 'error';
  const message = (err as Error)?.name === 'AbortError'
    ? '下载已中断，可点击图标重试'
    : ((err as Error)?.message || '下载失败');
  send(inf.version, { phase: 'error', message, percent: pct(inf), receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
}

interface StartArgs {
  version: string;
  type: 'force' | 'reminder';
  message?: string;
  url?: string | undefined;
}

export function startDownload(args: StartArgs): { success: boolean; message?: string } {
  // 同版本已在下载 / 暂停 → 去重（顺带修掉每 5min 心跳重新触发的竞态）
  if (
    inflight &&
    inflight.version === args.version &&
    (inflight.status === 'downloading' || inflight.status === 'paused')
  ) {
    return { success: true, message: '该版本正在下载' };
  }

  console.log('[Download] startDownload called with args:', args);
  let url: string;
  if (args.url && args.url.trim()) {
    url = args.url.trim();
    console.log('[Download] using provided url:', url);
  } else {
    try {
      url = buildUpdateUrl(args.version);
      console.log('[Download] using built url:', url);
    } catch (err) {
      console.error('[Download] buildUpdateUrl error:', err);
      send(args.version, { phase: 'error', message: (err as Error).message });
      return { success: false, message: (err as Error).message };
    }
  }
  let protocol: string;
  try {
    protocol = new URL(url).protocol;
    if (protocol !== 'http:' && protocol !== 'https:') throw new Error();
  } catch {
    const message = '更新包下载地址无效';
    send(args.version, { phase: 'error', message });
    return { success: false, message };
  }
  if (strictSecurityEnabled()) {
    if (protocol !== 'https:') {
      const message = '更新包需要通过 HTTPS 下载，请使用 HTTPS 下载地址。';
      send(args.version, { phase: 'error', message });
      return { success: false, message };
    }
    if (!configuredUpdatePublicKey()) {
      const message = '无法验证更新包签名：未配置更新签名公钥';
      send(args.version, { phase: 'error', message });
      return { success: false, message };
    }
  }

  const previous = readUpdateState().downloaded;
  if (previous && evaluateVersionUpdate(previous.version, args.version).reason === 'newer'
    && verifyPackageIntegrity(previous.filePath, previous.size).ok) {
    const message = '已有更新版本的安装包';
    send(args.version, { phase: 'error', message });
    return { success: false, message };
  }
  if (inflight) {
    inflight.abortReason = 'supersede';
    inflight.abort.abort();
    if (inflight.status !== 'downloading') safeUnlink(inflight.partPath);
  }
  // Separate each attempt from active writers and the package named by the saved receipt.
  const targetPath = path.join(updatesDir(), `${randomUUID()}-${fileNameFromUrl(url)}`);
  const partPath = `${targetPath}.part`;

  inflight = {
    version: args.version,
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
    writeStream: null,
    noProgressTimer: null,
    lastEmittedPercent: null,
  };
  launchDownload(inflight);
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
  send(inflight.version, { phase: 'paused', percent: pct(inflight), receivedBytes: inflight.receivedBytes, totalBytes: inflight.totalBytes });
  return { success: true };
}

export function resumeDownload(): { success: boolean } {
  if (!inflight || (inflight.status !== 'paused' && inflight.status !== 'error')) return { success: false };
  inflight.status = 'downloading';
  launchDownload(inflight, true);
  return { success: true };
}

/** 重试：有 .part 则续传，否则等同全新下载。 */
export function retryDownload(args: StartArgs): { success: boolean; message?: string } {
  if (inflight && inflight.version === args.version && (inflight.status === 'error' || inflight.status === 'paused')) {
    // 复用已有 inflight（保留 receivedBytes / partPath）续传
    return resumeDownload();
  }
  return startDownload(args);
}

/** before-quit：中止当前下载（.part 留盘，下次启动 sweep 清理；不 emit）。 */
export async function disposeForQuit(): Promise<void> {
  if (inflight?.status === 'downloading') {
    inflight.status = 'paused';
    inflight.abortReason = 'quit';
    inflight.abort.abort();
  }
  await Promise.all(downloading);
}
