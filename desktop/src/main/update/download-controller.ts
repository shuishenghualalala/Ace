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
import { app } from 'electron';
import { buildUpdateUrl } from './update-url';
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
  writeStream: fs.WriteStream | null;
  noProgressTimer: ReturnType<typeof setTimeout> | null;
  lastEmittedPercent: number | null;
}

let inflight: Inflight | null = null;
let progressSender: ((p: VersionUpdateDownloadProgressPayload) => void) | null = null;

export function configureUpdateController(send: ((p: VersionUpdateDownloadProgressPayload) => void) | null): void {
  progressSender = send;
}

/** 当前在用的文件路径（cleanup 需跳过，避免删到正在写的 .part / 目标）。 */
export function activeFilePaths(): string[] {
  return inflight ? [inflight.partPath, inflight.targetPath] : [];
}

function updatesDir(): string {
  return path.join(app.getPath('userData'), 'updates');
}

function send(payload: VersionUpdateDownloadProgressPayload): void {
  try {
    progressSender?.(payload);
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

function safeEndStream(inf: Inflight): Promise<void> {
  const stream = inf.writeStream;
  inf.writeStream = null;
  if (!stream) return Promise.resolve();
  return new Promise((resolve) => {
    stream.once('error', () => resolve());
    stream.once('finish', () => resolve());
    try {
      stream.end();
    } catch {
      resolve();
    }
  });
}

async function runDownload(inf: Inflight): Promise<void> {
  try {
    const resumeFrom = inf.receivedBytes;
    const headers: Record<string, string> = {};
    if (resumeFrom > 0) headers['Range'] = `bytes=${resumeFrom}-`;

    const response = await fetch(inf.url, { headers, redirect: 'follow', signal: inf.abort.signal });
    const rangeHonored = response.status === 206;
    if (!response.ok) {
      throw new Error(`下载失败（HTTP ${response.status}）`);
    }
    if (!response.body) {
      throw new Error('下载失败（响应体为空）');
    }

    let startOffset = resumeFrom;
    if (resumeFrom > 0 && !rangeHonored) {
      // 服务器不支持断点（返回 200 全量）→ 丢弃旧 part，从头再来
      startOffset = 0;
      inf.receivedBytes = 0;
    }
    const contentLength = Number.parseInt(response.headers.get('content-length') || '0', 10) || 0;
    inf.totalBytes = contentLength > 0 ? contentLength + startOffset : 0;

    fs.mkdirSync(path.dirname(inf.targetPath), { recursive: true });
    inf.writeStream = fs.createWriteStream(inf.partPath, { flags: startOffset > 0 ? 'a' : 'w' });

    inf.status = 'downloading';
    inf.lastEmittedPercent = null;
    send({ phase: 'downloading', percent: pct(inf), receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
    resetNoProgressTimer(inf);

    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      inf.receivedBytes += value.byteLength;
      if (!inf.writeStream.write(Buffer.from(value))) {
        await new Promise<void>((resolve) => inf.writeStream?.once('drain', () => resolve()));
      }
      clearNoProgressTimer(inf);
      resetNoProgressTimer(inf);
      emitProgress(inf);
    }
    clearNoProgressTimer(inf);
    await safeEndStream(inf);

    // 收尾：rename .part → 目标，落 update-state.downloaded
    fs.renameSync(inf.partPath, inf.targetPath);
    const size = fs.statSync(inf.targetPath).size;
    const record: DownloadedUpdateRecord = {
      filePath: inf.targetPath,
      version: inf.version,
      size,
      type: inf.type,
      ...(inf.message ? { message: inf.message } : {}),
    };
    setDownloadedRecord(record);
    inf.status = 'completed';
    send({ phase: 'downloaded', percent: 100, receivedBytes: size, totalBytes: size });
  } catch (err) {
    await handleRunError(inf, err);
  }
}

async function handleRunError(inf: Inflight, err: unknown): Promise<void> {
  clearNoProgressTimer(inf);
  const reason = inf.abortReason;

  if (reason === 'supersede' || reason === 'cancel') {
    try {
      inf.writeStream?.destroy();
    } catch {
      /* ignore */
    }
    inf.writeStream = null;
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
    send({ phase: 'error', message: '下载超时，可点击图标重试续传', percent: pct(inf), receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
    return;
  }
  // 网络错误 / 未知 abort：保留 .part 供重试
  inf.status = 'error';
  const message = (err as Error)?.name === 'AbortError'
    ? '下载已中断，可点击图标重试'
    : ((err as Error)?.message || '下载失败');
  send({ phase: 'error', message, percent: pct(inf), receivedBytes: inf.receivedBytes, totalBytes: inf.totalBytes });
}

interface StartArgs {
  version: string;
  type: 'force' | 'reminder';
  message?: string;
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
    safeUnlink(old.partPath);
  }

  // 丢弃旧版本已完成的下载包（磁盘清理 + 避免装错版本）
  const state = readUpdateState();
  if (state.downloaded && state.downloaded.version !== args.version) {
    safeUnlink(state.downloaded.filePath);
    setDownloadedRecord(null);
  }

  let url: string;
  try {
    url = buildUpdateUrl(args.version);
  } catch (err) {
    send({ phase: 'error', message: (err as Error).message });
    return { success: false, message: (err as Error).message };
  }

  const targetPath = path.join(updatesDir(), fileNameFromUrl(url));
  const partPath = `${targetPath}.part`;
  safeUnlink(partPath); // 全新下载：清掉同目标的残留 .part

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
  const inf = inflight;
  runDownload(inf).catch((e) => {
    console.error('[update-download] unhandled:', (e as Error)?.message);
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
  inflight.status = 'downloading';
  const inf = inflight;
  runDownload(inf).catch((e) => {
    console.error('[update-download] resume unhandled:', (e as Error)?.message);
  });
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
export function disposeForQuit(): void {
  if (!inflight) return;
  inflight.abortReason = 'quit';
  try {
    inflight.abort.abort();
  } catch {
    /* ignore */
  }
}
