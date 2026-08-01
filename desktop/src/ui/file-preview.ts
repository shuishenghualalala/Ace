export type FilePreviewKind =
  | 'html'
  | 'svg'
  | 'markdown'
  | 'pdf'
  | 'image'
  | 'docx'
  | 'pptx'
  | 'xlsx'
  | 'legacy-office'
  | 'code';

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'tif', 'tiff']);

export function filePreviewKind(filePath: string): FilePreviewKind {
  const match = filePath.trim().toLowerCase().match(/\.([^.\\/]+)$/);
  const ext = match?.[1] ?? '';
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'svg') return 'svg';
  if (ext === 'md' || ext === 'markdown' || ext === 'mdown') return 'markdown';
  if (ext === 'pdf') return 'pdf';
  if (IMAGE_EXTENSIONS.has(ext)) return 'image';
  if (ext === 'docx') return 'docx';
  if (ext === 'pptx') return 'pptx';
  if (ext === 'xlsx') return 'xlsx';
  if (ext === 'doc' || ext === 'ppt' || ext === 'xls') return 'legacy-office';
  return 'code';
}

export function isTextPreviewKind(kind: FilePreviewKind): boolean {
  return kind === 'html' || kind === 'svg' || kind === 'markdown';
}

export function isBinaryPreviewKind(kind: FilePreviewKind): boolean {
  return kind === 'pdf' || kind === 'image' || kind === 'docx' || kind === 'pptx' || kind === 'xlsx';
}

export function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binary = window.atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

const OFFLINE_CSP = [
  "default-src 'none'",
  "img-src data: blob: file:",
  "style-src 'unsafe-inline' data: blob: file:",
  "font-src data: blob: file:",
  "media-src data: blob: file:",
  "script-src 'unsafe-inline' blob: file:",
  "connect-src 'none'",
  "frame-src 'none'",
  "object-src 'none'",
  "form-action 'none'",
].join('; ');

function directoryHref(filePath: string): string | null {
  const normalized = filePath.replace(/\\/g, '/');
  const slash = normalized.lastIndexOf('/');
  if (slash < 0) return null;
  const directory = normalized.slice(0, slash + 1);
  if (/^[a-z]:\//i.test(directory)) return `file:///${encodeURI(directory)}`;
  if (directory.startsWith('/')) return `file://${encodeURI(directory)}`;
  return null;
}

function injectIntoHead(source: string, markup: string): string {
  if (/<head(?:\s[^>]*)?>/i.test(source)) {
    return source.replace(/<head(?:\s[^>]*)?>/i, (head) => `${head}${markup}`);
  }
  return `<head>${markup}</head>${source}`;
}

/** 注入离线 CSP；HTML/SVG 即使含远程 URL 也不会发起网络请求。 */
export function buildOfflinePreviewDocument(filePath: string, source: string): string {
  const href = directoryHref(filePath);
  const safeHref = href?.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  const head = `<meta http-equiv="Content-Security-Policy" content="${OFFLINE_CSP}">${safeHref ? `<base href="${safeHref}">` : ''}`;
  return injectIntoHead(source, head);
}
