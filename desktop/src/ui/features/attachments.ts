/**
 * 附件上传与预览：POST /api/upload + WS attachments。
 */

import { backendApi, type Attachment } from '../backend-client';
import { createIcon } from '../components/icon';
import {
  $,
  appendAttachment,
  clearAttachments,
  notify,
  removeAttachmentAt,
  state,
} from '../state';
import { messageStore } from '../stores/stores';
import { imageDisplayUrl } from '../tool-screenshot';
import { showConfirmDialog } from '../ui-feedback';
import { openImageViewer } from './image-viewer';
import { queryPrimaryComposer } from './composer-scope';

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      resolve(result.split(',')[1] ?? '');
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function fileExt(name: string): string {
  const dot = name.lastIndexOf('.');
  if (dot < 0) return 'FILE';
  return name.slice(dot + 1).slice(0, 4).toUpperCase();
}

type ClipboardFileSource = Pick<DataTransfer, 'files' | 'items'>;

function sameClipboardFile(left: File, right: File): boolean {
  return left === right || (
    left.name === right.name
    && left.size === right.size
    && left.type === right.type
  );
}

function uniqueFiles(files: File[]): File[] {
  return files.filter(
    (candidate, index) => !files.slice(0, index).some((file) => sameClipboardFile(file, candidate)),
  );
}

/**
 * Chromium/Electron may expose one pasted bitmap through both `files` and
 * `items`. Those are two views of the same DataTransfer payload, not two
 * independent sources, and their File wrappers can have different generated
 * `lastModified` values. Prefer the canonical `files` list whenever present;
 * only fall back to `items` for browsers that leave `files` empty for a bitmap.
 */
export function uniqueClipboardFiles(data: ClipboardFileSource): File[] {
  const files = Array.from(data.files ?? []);
  if (files.length > 0) return uniqueFiles(files);

  const itemFiles: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== 'file') continue;
    const candidate = item.getAsFile();
    if (candidate) itemFiles.push(candidate);
  }
  return uniqueFiles(itemFiles);
}

// ---- 文档类附件提示 ----
// 默认上传链路只把 pdf/word/excel/ppt 当「二进制文件」传路径给模型（读不到内容），
// 真正解析需要可选技能 file-qa。这里检测到这类附件且未安装时，弹引导条一键安装。
const DOC_EXTS = new Set(['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']);
const FILE_QA_SLUG = 'file-qa';
/** file-qa 是否已安装的会话级缓存：null=未查询。避免每次渲染都打 /api/skills。 */
let fileQaInstalledCache: boolean | null = null;
let attachmentController: AbortController | null = null;

function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i).toLowerCase();
}

function hasDocAttachment(): boolean {
  return state.attachments.some((a) => DOC_EXTS.has(extOf(a.name)));
}

async function isFileQaInstalled(): Promise<boolean> {
  if (fileQaInstalledCache != null) return fileQaInstalledCache;
  try {
    const skills = await backendApi.skills();
    fileQaInstalledCache = skills.some((s) => s.slug === FILE_QA_SLUG);
  } catch {
    fileQaInstalledCache = false;
  }
  return fileQaInstalledCache;
}

/** 按当前附件状态 + file-qa 安装状态，决定是否显示引导条。 */
async function refreshFileQaHint(): Promise<void> {
  const hint = $('#chat-fileqa-hint');
  if (!hint) return;
  // 无文档类附件、或已装 file-qa → 隐藏；否则提示安装。
  if (!hasDocAttachment() || (await isFileQaInstalled())) {
    hint.hidden = true;
    return;
  }
  hint.hidden = false;
}

/**
 * 渲染待发附件预览。图片 → 缩略图卡片（真实预览，可点击查看大图，对齐微信/图三）；
 * 非图片 → 文件信息卡片（扩展名 + 文件名 + 大小）。
 * 全程用 DOM 构造（textContent），不走 innerHTML，XSS 安全。
 */
export function renderAttachmentPreview(): void {
  const box = queryPrimaryComposer('[data-attachment-preview]');
  if (!box) return;
  renderAttachmentList(box, state.attachments, removeMainAttachment);
  void refreshFileQaHint();
}

/** 把附件列表渲染进指定预览容器：主对话（before-input 槽位）与 Wiki 面板共用同一套卡片结构。 */
export function renderAttachmentList(
  box: HTMLElement,
  attachments: Attachment[],
  onRemove: (attId: string) => void,
): void {
  if (attachments.length === 0) {
    box.replaceChildren();
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.replaceChildren(...attachments.map((a) => buildAttachmentChip(a, onRemove)));
}

/** 主对话附件移除：操作全局 state.attachments 并重绘预览（buildRemoveBtn 缺省路径的显式版）。 */
function removeMainAttachment(attId: string): void {
  const idx = state.attachments.findIndex((x) => x.id === attId);
  if (idx >= 0) removeAttachmentAt(idx);
  renderAttachmentPreview();
}

/**
 * 对话面板的附件流抽象（重构计划步骤 4）：主对话包全局 state.attachments，
 * Wiki 问答面板包自己的 per-KB 附件列表；Composer hasDraft / 预览渲染只面向接口。
 */
export interface PanelAttachments {
  list(): Attachment[];
  add(files: FileList | File[] | null): Promise<void>;
  remove(id: string): void;
  takeForSend(): Attachment[];
  subscribe(cb: () => void): () => void;
}

/** 主对话附件 adapter：包现有全局 state.attachments 流（行为不变）。 */
export function createMainPanelAttachments(): PanelAttachments {
  return {
    list: () => [...state.attachments],
    add: (files) => handleFileSelect(files),
    remove: removeMainAttachment,
    takeForSend: takeAttachmentsForSend,
    subscribe: (cb) => messageStore.subscribe((next, prev) => {
      if (next.attachments !== prev.attachments) cb();
    }),
  };
}

/** 非图片附件：扩展名图标 + 文件名 + 类型/大小 + 移除。onRemove 缺省时操作主对话附件状态。 */
function buildFileChip(a: Attachment, onRemove?: (attId: string) => void): HTMLElement {
  const chip = document.createElement('div');
  chip.className = 'chat-attachment-chip';
  chip.dataset.attId = a.id;
  const icon = document.createElement('span');
  icon.className = 'chat-attachment-chip__icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = fileExt(a.name);
  const copy = document.createElement('div');
  copy.className = 'chat-attachment-chip__copy';
  const name = document.createElement('div');
  name.className = 'chat-attachment-chip__name';
  name.textContent = a.name;
  name.title = a.name;
  const meta = document.createElement('div');
  meta.className = 'chat-attachment-chip__meta';
  meta.textContent = `${a.type}${a.size ? ` · ${Math.max(1, Math.round(a.size / 1024))}KB` : ''}`;
  copy.append(name, meta);
  chip.append(icon, copy, buildRemoveBtn(a.id, onRemove));
  return chip;
}

/** 与主对话完全一致的附件预览卡片，移除回调由调用方提供（Wiki Agent 面板等复用）。 */
export function buildAttachmentChip(a: Attachment, onRemove: (attId: string) => void): HTMLElement {
  return a.type === 'image' ? buildImageCard(a, onRemove) : buildFileChip(a, onRemove);
}

/** 图片附件：缩略图（点击查看大图）+ 文件名 + 移除。 */
function buildImageCard(a: Attachment, onRemove?: (attId: string) => void): HTMLElement {
  const card = document.createElement('div');
  card.className = 'chat-attachment-thumb';
  card.dataset.attId = a.id;
  const view = document.createElement('button');
  view.type = 'button';
  view.className = 'chat-attachment-thumb__view';
  view.title = '点击查看大图';
  view.setAttribute('aria-label', `查看图片 ${a.name}`);
  const img = document.createElement('img');
  img.className = 'chat-attachment-thumb__img';
  img.src = imageDisplayUrl(a.path);
  img.alt = a.name;
  img.loading = 'lazy';
  img.draggable = false;
  view.appendChild(img);
  view.addEventListener('click', () => openImageViewer(imageDisplayUrl(a.path), a.name, a.path));
  const meta = document.createElement('div');
  meta.className = 'chat-attachment-thumb__meta';
  meta.textContent = a.name;
  meta.title = a.name;
  card.append(view, meta, buildRemoveBtn(a.id, onRemove));
  return card;
}

function buildRemoveBtn(attId: string, onRemove?: (attId: string) => void): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'chat-attachment-chip__remove';
  btn.dataset.removeAtt = attId;
  btn.setAttribute('aria-label', '移除');
  btn.append(createIcon('icon-close', { size: 16 }));
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (onRemove) {
      onRemove(attId);
      return;
    }
    const idx = state.attachments.findIndex((x) => x.id === attId);
    if (idx >= 0) removeAttachmentAt(idx);
    renderAttachmentPreview();
  });
  return btn;
}

export async function handleFileSelect(files: FileList | File[] | null): Promise<void> {
  if (!files?.length) return;
  for (const file of Array.from(files)) {
    try {
      const content = await readFileAsBase64(file);
      const uploaded = await backendApi.upload(file.name, content);
      appendAttachment(uploaded);
    } catch {
      notify(`上传失败：${file.name}`);
    }
  }
  renderAttachmentPreview();
}

export function takeAttachmentsForSend(): Attachment[] {
  const list = [...state.attachments];
  clearAttachments();
  renderAttachmentPreview();
  return list;
}

export function bindAttachments(): () => void {
  if (attachmentController) return () => {};
  attachmentController = new AbortController();
  const signal = attachmentController.signal;
  const input = $('#chat-file-input') as HTMLInputElement | null;
  $('#chat-attach-btn')?.addEventListener('click', () => input?.click(), { signal });
  input?.addEventListener('change', () => {
    void handleFileSelect(input.files);
    input.value = '';
  }, { signal });
  const dropZone = queryPrimaryComposer('.chat-input-container');
  const unbindDrop = dropZone
    ? bindFileDrop(dropZone, (files) => void handleFileSelect(files))
    : () => {};
  const chatInput = queryPrimaryComposer('[data-composer-input]');
  const unbindPaste = chatInput
    ? bindFilePaste(chatInput, (files) => void handleFileSelect(files))
    : () => {};
  bindFileQaHintActions(signal);
  return () => {
    if (!attachmentController) return;
    attachmentController.abort();
    attachmentController = null;
    unbindDrop();
    unbindPaste();
  };
}

/**
 * 粘贴上传：在目标输入框上 Ctrl+V 时，若剪贴板含文件（复制的文件 / 截图），
 * 交给 onFiles 处理。纯文本粘贴（无文件）不拦截，照常插入输入框。
 * 主对话与 Wiki 右栏 Composer 共用（各自传入自己的上传链路）。
 *
 * 注意：截图在部分浏览器只出现在 clipboardData.items（不在 files），因此 files
 * 为空时才回退 items；两者同时读取会把同一位图的不同包装重复上传。
 */
export function bindFilePaste(
  target: HTMLElement,
  onFiles: (files: File[]) => void,
): () => void {
  if (target.dataset.pasteUploadBound === 'true') return () => {};
  target.dataset.pasteUploadBound = 'true';
  const handlePaste = (e: ClipboardEvent): void => {
    const dt = e.clipboardData;
    if (!dt) return;
    const files = uniqueClipboardFiles(dt);
    if (files.length === 0) return;
    e.preventDefault();
    onFiles(files);
  };
  target.addEventListener('paste', handlePaste);
  return () => {
    target.removeEventListener('paste', handlePaste);
    delete target.dataset.pasteUploadBound;
  };
}

/** 引导条交互：一键安装 file-qa；关闭按钮本次会话隐藏（附件变化后仍会按规则重判）。 */
function bindFileQaHintActions(signal: AbortSignal): void {
  $('#chat-fileqa-install-btn')?.addEventListener('click', async () => {
    const btn = $('#chat-fileqa-install-btn') as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      // 与技能页同一个 /api/skills/{slug}/install 端点，同样落到 get_crew_home()/skills
      // ——机器级共享，影响本机所有登录账号。技能页会明确告知，这条引导条入口不能悄悄装。
      const agreed = await showConfirmDialog({
        title: '确认全局安装技能',
        message:
          '技能是本机全局共享能力，安装结果对本机所有登录账号生效。'
          + '确定安装「文件问答」吗？',
        confirmText: '全局安装',
        cancelText: '取消',
      });
      if (!agreed) return;
      const res = await backendApi.installSkill(FILE_QA_SLUG);
      if (res.ok) {
        fileQaInstalledCache = true;
        notify('「文件问答」技能已安装，可解析 PDF/Word/Excel/PPT 附件');
        await refreshFileQaHint();
      } else {
        notify('安装失败，请前往技能页手动安装');
      }
    } catch {
      notify('安装失败，请前往技能页手动安装');
    } finally {
      if (btn) btn.disabled = false;
    }
  }, { signal });
  $('#chat-fileqa-close-btn')?.addEventListener('click', () => {
    const hint = $('#chat-fileqa-hint');
    if (hint) hint.hidden = true;
  }, { signal });
}

/**
 * 拖拽上传：把拖到目标容器的文件交给 onFiles。
 * 主对话与 Wiki 右栏 Composer 共用（各自传入自己的上传链路）。
 *
 * 三个必须注意的点：
 *  - dragover 必须 preventDefault，否则浏览器会直接打开文件、drop 根本不触发；
 *  - 只认「文件类」拖拽（types 含 'Files'），否则拖选中文本/链接也会触发上传框；
 *  - dragenter/leave 在容器与子元素（textarea、按钮）间会成对抖动，用 depth 计数器
 *    防闪烁：进入子元素时 enter+1、leave-1，仅当归零才真正离开。
 */
export function bindFileDrop(
  zone: HTMLElement,
  onFiles: (files: File[]) => void,
): () => void {
  if (zone.dataset.fileDropBound === 'true') return () => {};
  zone.dataset.fileDropBound = 'true';
  const hasFiles = (e: DragEvent): boolean =>
    Boolean(e.dataTransfer) && Array.from(e.dataTransfer!.types).includes('Files');
  let depth = 0;

  const handleDragEnter = (e: DragEvent): void => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    zone.classList.add('is-drag-over');
  };
  const handleDragOver = (e: DragEvent): void => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  };
  const handleDragLeave = (): void => {
    if (depth > 0) depth -= 1;
    if (depth === 0) zone.classList.remove('is-drag-over');
  };
  const handleDrop = (e: DragEvent): void => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    zone.classList.remove('is-drag-over');
    if (e.dataTransfer) onFiles(Array.from(e.dataTransfer.files));
  };
  zone.addEventListener('dragenter', handleDragEnter);
  zone.addEventListener('dragover', handleDragOver);
  zone.addEventListener('dragleave', handleDragLeave);
  zone.addEventListener('drop', handleDrop);
  return () => {
    zone.removeEventListener('dragenter', handleDragEnter);
    zone.removeEventListener('dragover', handleDragOver);
    zone.removeEventListener('dragleave', handleDragLeave);
    zone.removeEventListener('drop', handleDrop);
    zone.classList.remove('is-drag-over');
    delete zone.dataset.fileDropBound;
  };
}
