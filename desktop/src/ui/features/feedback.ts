/**
 * 反馈页 UI
 *
 * 功能：
 * - 标题、描述、截图（最多 10 张）
 * - 自动保存草稿到 localStorage（crew.feedbackDraft）—— 草稿服务端无概念，仅本地
 * - 提交调 window.Crew.submitFeedback（服务地址由 CREW_FEEDBACK_BASE_URL 配置）
 * - “提交记录”列表数据源为服务端，不在本地维护业务副本
 * - 行内“查看”打开详情弹窗（展示标题/描述/状态/时间）
 *
 * HTML 模板：assets/index.html 中 #feedback-modal / #feedback-detail-modal
 * CSS：assets/styles/config.css
 */

import { $, escapeHtml, loadFromStorage, notify, saveToStorage, state } from '../state';
import type { FeedbackDraft, FeedbackImage, FeedbackListItem, FeedbackStatus } from '../state';

const DRAFT_KEY = 'crew.feedbackDraft';
const IMAGE_LIMIT = 10;
const LIST_SIZE = 20;
/** 列表刷新冷却(ms)：两次请求间的最小间隔，防止"打开即刷"+手动连点造成短时重复请求。 */
const LIST_FETCH_COOLDOWN = 3000;
let lastListFetchAt = 0;
// 分页视图状态（view 层，不进全局 store）：当前页 + 服务端返回的总条数
let currentPage = 1;
let totalCount = 0;

function readDraft(): FeedbackDraft {
  const stored = loadFromStorage<Partial<FeedbackDraft>>(DRAFT_KEY, {});
  return {
    title: typeof stored.title === 'string' ? stored.title : '',
    description: typeof stored.description === 'string' ? stored.description : '',
    images: Array.isArray(stored.images) ? stored.images.slice(0, IMAGE_LIMIT) : [],
  };
}

function persistDraft(): void {
  saveToStorage(DRAFT_KEY, state.feedbackDraft);
}

/**
 * 服务端反馈列表的本地缓存（cache-aside）：服务端是唯一真相源，本地只存上次拉取的副本，
 * 用于打开反馈页时秒开、避免每次查看都请求服务端。
 * 触发刷新（手动按钮 / 提交成功）时整体覆盖。
 */
function listCacheKey(): string {
  return 'crew.feedbackList';
}

function readListCache(): FeedbackListItem[] {
  const key = listCacheKey();
  if (!key) return [];
  const stored = loadFromStorage<FeedbackListItem[]>(key, []);
  return Array.isArray(stored) ? stored : [];
}

function persistListCache(items: FeedbackListItem[]): void {
  const key = listCacheKey();
  if (!key) return;
  saveToStorage(key, items);
}

function readTitleInput(): string {
  return ($('#feedback-title') as HTMLInputElement | null)?.value.trim() || '';
}

function readDescriptionInput(): string {
  return ($('#feedback-description') as HTMLTextAreaElement | null)?.value.trim() || '';
}

function setTitleCount(): void {
  const input = $('#feedback-title') as HTMLInputElement | null;
  const countEl = $('#feedback-title-count');
  if (!input || !countEl) return;
  const len = input.value.length;
  const max = 120;
  countEl.textContent = `${len}/${max}`;
  countEl.classList.toggle('is-warning', len >= max - 10 && len < max);
  countEl.classList.toggle('is-limit', len >= max);
}

function statusLabel(status: FeedbackStatus): string {
  switch (status) {
    case 'PENDING':
      return '待处理';
    case 'PROCESSING':
      return '处理中';
    case 'RESOLVED':
      return '已解决';
    case 'CLOSED':
      return '已关闭';
    default:
      return status;
  }
}

function statusClass(status: FeedbackStatus): string {
  switch (status) {
    case 'PENDING':
      return 'is-pending';
    case 'PROCESSING':
      return 'is-processing';
    case 'RESOLVED':
      return 'is-resolved';
    case 'CLOSED':
      return 'is-closed';
    default:
      return '';
  }
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

function renderPreview(): void {
  const preview = $('#feedback-screenshot-preview');
  const countEl = $('#feedback-screenshot-count');
  if (!preview) return;

  const canAdd = state.feedbackDraft.images.length < IMAGE_LIMIT;
  const thumbs = state.feedbackDraft.images
    .map(
      (img, i) => `
        <div class="feedback-upload-thumb" data-feedback-index="${i}">
          <img class="feedback-upload-image" src="${img.dataUrl}" alt="${escapeHtml(img.name)}" />
          <div class="feedback-upload-thumb-overlay">
            <button class="feedback-upload-thumb-action" type="button" data-feedback-view="${i}" aria-label="查看" title="查看">🔍</button>
            <button class="feedback-upload-thumb-action is-delete" type="button" data-feedback-remove="${i}" aria-label="删除" title="删除">🗑</button>
          </div>
        </div>
      `,
    )
    .join('');
  const addBtn = canAdd
    ? `<button class="feedback-upload-add" type="button" id="feedback-upload-add-btn" aria-label="上传截图">
        <div class="feedback-upload-add-icon">+</div>
        <span class="feedback-upload-add-text">上传</span>
      </button>`
    : '';
  preview.innerHTML = thumbs + addBtn;
  if (countEl) countEl.textContent = `${state.feedbackDraft.images.length}/${IMAGE_LIMIT}`;
}

/**
 * 从服务端拉取当前用户的反馈列表。
 * 反馈页"提交记录"的唯一数据源；打开弹窗与提交成功后均调用刷新。
 */
/** 请求期间禁用所有翻页按钮，防连点/并发；完成后由 renderList 重建正确可用态。 */
function setPaginationButtonsDisabled(disabled: boolean): void {
  document.querySelectorAll<HTMLButtonElement>('[data-feedback-page]').forEach((b) => {
    b.disabled = disabled;
  });
}

async function loadFeedbackList(opts: { page?: number; force?: boolean } = {}): Promise<void> {
  const page = opts.page ?? 1;
  const force = opts.force ?? false;
  // 冷却：非强制调用（手动刷新）距上次请求不足 LIST_FETCH_COOLDOWN 直接返回，形成反滥用真空期。
  // 分页 / 提交成功传 force=true 绕过——分页靠按钮 in-flight disable 防连点，无需再受真空期限制；
  // 提交后数据刚变更必须拉最新，否则新记录被冷却吞掉。
  const now = Date.now();
  if (!force && now - lastListFetchAt < LIST_FETCH_COOLDOWN) return;
  lastListFetchAt = now;

  const listEl = $('#feedback-history-list');
  const refreshBtn = document.getElementById('feedback-refresh-btn') as HTMLButtonElement | null;
  if (refreshBtn) refreshBtn.disabled = true;
  setPaginationButtonsDisabled(true);
  // 仅在无缓存数据时显示加载占位；有缓存则刷新期间保留旧数据，避免列表闪烁
  if (listEl && state.feedbackList.length === 0) {
    listEl.innerHTML = '<div class="entity-loading">加载中…</div>';
  }

  let result: { success: boolean; list?: FeedbackListItem[]; total?: number; message?: string } | null = null;
  try {
    result = (await window.Crew?.getFeedbackList?.({ page, size: LIST_SIZE })) || null;
  } catch (err) {
    result = { success: false, message: (err as Error).message };
  }

  // 释放刷新按钮：冷却窗口内保持 disabled（反滥用真空期），窗口结束再放开
  if (refreshBtn) {
    const remaining = LIST_FETCH_COOLDOWN - (Date.now() - lastListFetchAt);
    if (remaining > 0) window.setTimeout(() => (refreshBtn.disabled = false), remaining);
    else refreshBtn.disabled = false;
  }

  if (!result?.success) {
    const msg = result?.message || '未知错误';
    // 失败：有数据则重渲染（恢复分页按钮可用态）并提示；无数据显示错误占位
    if (state.feedbackList.length === 0) {
      if (listEl) listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(msg)}</div>`;
    } else {
      renderList();
    }
    notify(`加载反馈列表失败：${msg}`);
    return;
  }

  const list = result.list ?? [];
  const total = result.total ?? list.length;
  // 页码越界（如记录被删、总页数减少）：当前页空但总数>0，回落第 1 页
  if (list.length === 0 && total > 0 && page > 1) {
    return loadFeedbackList({ page: 1, force: true });
  }

  state.feedbackList = list;
  totalCount = total;
  currentPage = page;
  // 仅缓存第 1 页（打开弹窗秒开用）
  if (page === 1) persistListCache(state.feedbackList);
  renderList();
}

/** 渲染服务端反馈列表为表格 + 分页条；行内"查看"打开详情。删除入口暂隐藏（见文件头注释）。 */
function renderList(): void {
  const listEl = $('#feedback-history-list');
  if (!listEl) return;
  const items = state.feedbackList;
  if (items.length === 0) {
    listEl.innerHTML = '<div class="entity-empty">暂无提交记录</div>';
    return;
  }
  const totalPages = Math.max(1, Math.ceil(totalCount / LIST_SIZE));
  const page = Math.min(Math.max(1, currentPage), totalPages);
  listEl.innerHTML = `
    <div class="feedback-history-table-wrap">
      <table class="feedback-history-table">
        <thead>
          <tr>
            <th>标题</th>
            <th>状态</th>
            <th>时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${items
            .map(
              (it) => `
            <tr>
              <td><div class="feedback-history-table__title" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</div></td>
              <td><span class="feedback-history-item__chip ${statusClass(it.status)}">${statusLabel(it.status)}</span></td>
              <td><div class="feedback-history-table__time">${escapeHtml(formatTime(it.createdAt))}</div></td>
              <td><button type="button" class="feedback-history-detail-btn" data-feedback-detail-id="${it.id}">查看</button></td>
            </tr>
          `,
            )
            .join('')}
        </tbody>
      </table>
    </div>
    <div class="feedback-pagination">
      <button type="button" class="btn btn-sm feedback-page-btn" data-feedback-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
      <span class="feedback-pagination__info">第 ${page} / ${totalPages} 页 · 共 ${totalCount} 条</span>
      <button type="button" class="btn btn-sm feedback-page-btn" data-feedback-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>下一页</button>
    </div>
  `;
  listEl.querySelectorAll<HTMLButtonElement>('[data-feedback-detail-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = Number(btn.getAttribute('data-feedback-detail-id'));
      const item = state.feedbackList.find((f) => f.id === id);
      if (item) openFeedbackDetail(item);
    });
  });
  listEl.querySelectorAll<HTMLButtonElement>('[data-feedback-page]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = Number(btn.getAttribute('data-feedback-page'));
      if (Number.isFinite(target)) void loadFeedbackList({ page: target, force: true });
    });
  });
}

/** 安全解析服务端 images 字段（相对路径 JSON 串）为路径数组；空/解析失败/非数组返回 []。 */
function parseFeedbackImages(images?: string): string[] {
  if (!images) return [];
  try {
    const arr: unknown = JSON.parse(images);
    return Array.isArray(arr) ? arr.filter((s): s is string => typeof s === 'string' && s.length > 0) : [];
  } catch {
    return [];
  }
}

/**
 * 逐个拉取附件图片（主进程 fetch → data URL）并回填缩略图。
 * 成功：渲染 <img>，点击放大；失败：保留占位并标注"加载失败"（当前静态根未确认时属预期）。
 */
async function hydrateDetailImages(paths: string[]): Promise<void> {
  const wrap = document.getElementById('feedback-detail-images');
  if (!wrap) return;
  await Promise.all(
    paths.map(async (path, i) => {
      const tile = wrap.querySelector<HTMLButtonElement>(`[data-image-index="${i}"]`);
      if (!tile) return;
      let dataUrl: string | null = null;
      try {
        const res = (await window.Crew?.getFeedbackImage?.(path)) || null;
        if (res?.success && typeof res.dataUrl === 'string') dataUrl = res.dataUrl;
      } catch {
        // 保留失败占位
      }
      tile.classList.remove('is-loading');
      if (dataUrl) {
        tile.classList.add('is-loaded');
        const image = document.createElement('img');
        image.src = dataUrl;
        image.alt = `截图 ${i + 1}`;
        tile.replaceChildren(image);
        tile.addEventListener('click', () => window.open(dataUrl, '_blank'));
      } else {
        tile.classList.add('is-broken');
        const placeholder = document.createElement('span');
        placeholder.className = 'feedback-detail-image-placeholder';
        placeholder.textContent = '加载失败';
        tile.replaceChildren(placeholder);
        tile.title = `图片暂不可用:${path}`;
      }
    }),
  );
}

/**
 * 打开详情弹窗。
 * 实测 /api/feedback/list 会返回 adminReply / updatedAt(2026-07-17)，此处有则展示、无则省略——
 * IPC 透传服务端原始字段未做裁剪，按运行时是否存在决定渲染。
 * images 为相对路径 JSON 串、静态文件根未确认，缩略图经主进程 fetch 回填，加载失败则占位。
 * 排版弃用旧 table（标签宽度参差、正文局促），改为 顶部 meta + 分段正文 + 次要信息。
 */
function openFeedbackDetail(item: FeedbackListItem): void {
  const titleEl = $('#feedback-detail-title');
  if (titleEl) titleEl.textContent = item.title || '反馈详情';

  // 顶部 meta：状态 chip + 提交时间
  const meta = `
    <div class="feedback-detail-meta">
      <span class="feedback-detail-status ${statusClass(item.status)}">${statusLabel(item.status)}</span>
      <span class="feedback-detail-meta-time">提交于 ${escapeHtml(formatTime(item.createdAt))}</span>
    </div>`;

  // 问题描述（独立成块，便于长文阅读）
  const desc = `
    <section class="feedback-detail-section">
      <h4 class="feedback-detail-section__label">问题描述</h4>
      <div class="feedback-detail-section__body feedback-detail-description">${escapeHtml(item.description || '（无）')}</div>
    </section>`;

  // 附件截图：images 为相对路径 JSON 串；空数组则不渲染该区块
  const imagePaths = parseFeedbackImages(item.images);
  const imagesSection = imagePaths.length
    ? `
    <section class="feedback-detail-section">
      <h4 class="feedback-detail-section__label">附件截图(${imagePaths.length})</h4>
      <div class="feedback-detail-images" id="feedback-detail-images">
        ${imagePaths
          .map(
            (p, i) => `
          <button type="button" class="feedback-detail-image is-loading" data-image-index="${i}" title="${escapeHtml(p)}">
            <span class="feedback-detail-image-placeholder">…</span>
          </button>`,
          )
          .join('')}
      </div>
    </section>`
    : '';

  // 管理员回复：服务端返回才展示
  const reply = item.adminReply
    ? `
    <section class="feedback-detail-section feedback-detail-section--reply">
      <h4 class="feedback-detail-section__label">管理员回复</h4>
      <div class="feedback-detail-section__body">${escapeHtml(item.adminReply)}</div>
    </section>`
    : '';

  // 次要信息：更新时间（有则展示）
  const footItems: string[] = [];
  if (item.updatedAt) footItems.push(`<span>更新于 ${escapeHtml(formatTime(item.updatedAt))}</span>`);
  const foot = footItems.length ? `<div class="feedback-detail-foot">${footItems.join('')}</div>` : '';

  const body = $('#feedback-detail-body');
  if (body) body.innerHTML = meta + desc + imagesSection + reply + foot;
  $('#feedback-detail-modal')?.classList.add('show');
  if (imagePaths.length) void hydrateDetailImages(imagePaths);
}

function closeFeedbackDetail(): void {
  $('#feedback-detail-modal')?.classList.remove('show');
}

function resetForm(): void {
  state.feedbackDraft = { title: '', description: '', images: [] };
  persistDraft();
  const titleEl = document.getElementById('feedback-title') as HTMLInputElement | null;
  if (titleEl) titleEl.value = '';
  const descEl = document.getElementById('feedback-description') as HTMLTextAreaElement | null;
  if (descEl) descEl.value = '';
  setTitleCount();
  renderPreview();
}

async function addImagesFromFiles(files: File[]): Promise<void> {
  const available = IMAGE_LIMIT - state.feedbackDraft.images.length;
  if (available <= 0) return;

  const slots = files.slice(0, available);
  const newImages: FeedbackImage[] = [];
  for (let i = 0; i < slots.length; i++) {
    const file = slots[i];
    const dataUrl = await readFileAsDataUrl(file);
    if (!dataUrl) continue;
    newImages.push({
      name: file.name || `screenshot-${Date.now()}-${i + 1}.png`,
      dataUrl,
    });
  }
  if (newImages.length === 0) return;
  state.feedbackDraft.images = [...state.feedbackDraft.images, ...newImages].slice(0, IMAGE_LIMIT);
  persistDraft();
  renderPreview();
}

function readFileAsDataUrl(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

async function handleUploadClick(): Promise<void> {
  const result = await window.Crew?.selectFile?.({
    multiSelect: true,
    returnType: 'dataUrl',
    filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'] }],
  });
  if (!Array.isArray(result)) return;
  await addImagesFromObjects(result);
}

async function addImagesFromObjects(items: Array<{ name: string; dataUrl: string }>): Promise<void> {
  const available = IMAGE_LIMIT - state.feedbackDraft.images.length;
  if (available <= 0) return;
  const newImages: FeedbackImage[] = items
    .filter((it) => it.dataUrl && it.dataUrl.startsWith('data:'))
    .slice(0, available)
    .map((it) => ({ name: it.name, dataUrl: it.dataUrl }));
  if (newImages.length === 0) return;
  state.feedbackDraft.images = [...state.feedbackDraft.images, ...newImages].slice(0, IMAGE_LIMIT);
  persistDraft();
  renderPreview();
}

async function handlePaste(event: ClipboardEvent): Promise<void> {
  const target = event.target as HTMLElement | null;
  if (target?.closest('input, textarea, [contenteditable="true"]')) return;
  const items = Array.from(event.clipboardData?.items ?? []);
  const files: File[] = [];
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  if (files.length === 0) return;
  event.preventDefault();
  await addImagesFromFiles(files);
}

async function submitFeedback(): Promise<void> {
  const title = readTitleInput();
  const description = readDescriptionInput();
  if (!title) {
    notify('请输入问题标题');
    return;
  }
  if (!description) {
    notify('请输入问题描述');
    return;
  }

  // 同步到 state（draft）
  state.feedbackDraft.title = title;
  state.feedbackDraft.description = description;
  persistDraft();

  const submitBtn = document.getElementById('feedback-submit-btn') as HTMLButtonElement | null;
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = '提交中...';
  }

  const payload: { title: string; description: string; images: Array<{ name: string; dataUrl: string }> } = {
    title,
    description,
    images: state.feedbackDraft.images.map((img) => ({ name: img.name, dataUrl: img.dataUrl })),
  };
  let result: { success: boolean; message?: string; resultCode?: string; statusCode?: number } | null = null;
  try {
    result = (await window.Crew?.submitFeedback?.(payload)) || null;
  } catch (err) {
    result = { success: false, message: (err as Error).message };
  }

  if (submitBtn) {
    submitBtn.disabled = false;
    submitBtn.textContent = '提交反馈';
  }

  if (result?.success) {
    resetForm();
    notify('反馈已提交');
    // 提交成功后刷新服务端列表（不再本地追加副本）；force=true 绕过冷却 + 回第 1 页，确保新记录立即可见
    await loadFeedbackList({ page: 1, force: true });
  } else {
    notify(result?.message || '提交失败');
  }
}

function openFeedbackModal(): void {
  // 恢复草稿到表单
  const titleInput = document.getElementById('feedback-title') as HTMLInputElement | null;
  const descInput = document.getElementById('feedback-description') as HTMLTextAreaElement | null;
  if (titleInput) titleInput.value = state.feedbackDraft.title;
  if (descInput) descInput.value = state.feedbackDraft.description;
  setTitleCount();
  renderPreview();
  const modal = document.getElementById('feedback-modal');
  modal?.classList.add('show');
  modal?.setAttribute('style', 'display: flex;');
  // 先用本地缓存秒开列表，再后台静默拉取最新数据覆盖（受 LIST_FETCH_COOLDOWN 约束，不会与手动刷新打架）
  state.feedbackList = readListCache();
  currentPage = 1;
  totalCount = state.feedbackList.length; // 缓存仅为第 1 页，真实 total 由后台刷新回填
  renderList();
  void loadFeedbackList({ page: 1 });
}

function closeFeedbackModal(): void {
  const modal = document.getElementById('feedback-modal');
  modal?.classList.remove('show');
  modal?.setAttribute('style', 'display: none;');
}

export function bindFeedbackUi(): void {
  // 清理已废弃的旧"本地提交历史"数据（数据源已改为服务端列表，见 listCacheKey）
  localStorage.removeItem('crew.feedbackHistory');
  // 启动时从 localStorage 恢复草稿与列表缓存（best-effort：userInfo 未就绪时缓存 key 为空，打开弹窗时会再读一次）
  state.feedbackDraft = readDraft();
  state.feedbackList = readListCache();

  $('#feedback-btn')?.addEventListener('click', openFeedbackModal);
  document.getElementById('feedback-modal-close')?.addEventListener('click', closeFeedbackModal);
  document.getElementById('feedback-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeFeedbackModal();
  });

  document.getElementById('feedback-submit-btn')?.addEventListener('click', () => {
    void submitFeedback();
  });
  document.getElementById('feedback-reset-btn')?.addEventListener('click', () => {
    resetForm();
    renderPreview();
  });

  // 草稿自动保存
  document.getElementById('feedback-title')?.addEventListener('input', () => {
    state.feedbackDraft.title = readTitleInput();
    persistDraft();
    setTitleCount();
  });
  document.getElementById('feedback-description')?.addEventListener('input', () => {
    state.feedbackDraft.description = readDescriptionInput();
    persistDraft();
  });

  // 截图预览区点击代理：上传 / 查看 / 删除
  document.getElementById('feedback-screenshot-preview')?.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    const addBtn = target.closest<HTMLElement>('#feedback-upload-add-btn');
    if (addBtn) {
      void handleUploadClick();
      return;
    }
    const viewBtn = target.closest<HTMLElement>('[data-feedback-view]');
    if (viewBtn) {
      // 简化：直接打开新窗口查看大图
      const i = Number(viewBtn.getAttribute('data-feedback-view'));
      const img = state.feedbackDraft.images[i];
      if (img) window.open(img.dataUrl, '_blank');
      return;
    }
    const removeBtn = target.closest<HTMLElement>('[data-feedback-remove]');
    if (removeBtn) {
      const i = Number(removeBtn.getAttribute('data-feedback-remove'));
      if (Number.isFinite(i)) {
        state.feedbackDraft.images = state.feedbackDraft.images.filter((_, idx) => idx !== i);
        persistDraft();
        renderPreview();
      }
    }
  });

  // 剪贴板粘贴
  document.addEventListener('paste', (event) => {
    void handlePaste(event);
  });

  // 列表刷新按钮：手动请求服务端并覆盖本地缓存（刷新当前页，受冷却约束）
  document.getElementById('feedback-refresh-btn')?.addEventListener('click', () => {
    void loadFeedbackList({ page: currentPage });
  });

  // 详情弹窗关闭：右上 × / 底部按钮 / 点遮罩
  document.getElementById('feedback-detail-close')?.addEventListener('click', closeFeedbackDetail);
  document.getElementById('feedback-detail-close-btn')?.addEventListener('click', closeFeedbackDetail);
  document.getElementById('feedback-detail-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeFeedbackDetail();
  });
}
