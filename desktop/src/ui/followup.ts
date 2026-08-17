/**
 * 追问选择框（ask_followup_question）— desktop 端渲染与事件绑定。
 *
 * 普通追问对齐 web 端 FollowupQuestionCard；权限请求使用不阻断页面的浮层。
 * 取消时通知后端（followup_cancel），与 web 行为一致。
 */

import type { FollowupAnswer } from './backend-client';
import type { PendingFollowup } from './state';

const FREE_TEXT_OPTION = '__free_text__';

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] ?? c));
}

const PERMISSION_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/></svg>`;
const PERMISSION_NOTICE_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>`;
const STAFFING_ICON = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 20a6 6 0 0 0-12 0"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M16 11h6"/></svg>`;
const STAFFING_CHECK_ICON = `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>`;

interface PermissionPromptParts {
  action: string;
  reason: string;
}

interface PermissionPresentation {
  title: string;
  context: string;
  summary: string;
  note: string;
  command?: string;
}

function permissionPromptParts(text: string): PermissionPromptParts {
  const normalized = text.trim();
  const body = normalized.replace(/^即将执行[：:]\s*/, '');
  const sections = body.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
  const reason = sections.find((part) => /^原因[：:]/.test(part))
    ?.replace(/^原因[：:]\s*/, '')
    .trim() ?? '';
  return { action: sections[0] ?? body, reason };
}

function clipped(text: string, limit = 72): string {
  const chars = Array.from(text.trim());
  return chars.length <= limit ? chars.join('') : `${chars.slice(0, limit - 1).join('')}…`;
}

function quoted(text: string): string {
  const value = clipped(text, 60);
  return value ? `“${value}”` : '内容';
}

function permissionNote(reason: string, action: string): string {
  const normalized = reason.trim();
  if (/提交表单|按\s*Enter/i.test(normalized) || /submit|press/i.test(action)) {
    return '此操作会向当前网站提交输入内容。';
  }
  if (/上传/.test(normalized)) return '文件内容将发送到当前网站。';
  if (/下载/.test(normalized)) return '文件将保存到当前任务的下载目录。';
  if (/高风险最终动作|外部副作用/.test(normalized)) return '此操作可能在当前网站产生外部影响。';
  return '';
}

function parseActionObject(action: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(action);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function permissionToolName(title: string): string {
  return title.split(/[·•]/).at(-1)?.trim() ?? '';
}

function normalizedBrowserAction(
  toolName: string,
  action: Record<string, unknown>,
): Record<string, unknown> {
  if (toolName === 'browser_use') return action;
  const mapped = { ...action };
  const actionByTool: Record<string, string> = {
    browser_type: 'type',
    browser_press: 'press',
    browser_click: 'click',
    browser_navigate: 'navigate',
    browser_upload: 'upload',
    browser_download: 'download',
  };
  if (toolName === 'browser_tabs' && action.action === 'new') mapped.action = 'tab_new';
  else if (toolName === 'browser_dialog' && action.action === 'accept') mapped.action = 'accept';
  else if (actionByTool[toolName]) mapped.action = actionByTool[toolName];
  return mapped;
}

function browserPermissionPresentation(
  action: Record<string, unknown>,
  reason: string,
): PermissionPresentation {
  const kind = String(action.action ?? '').toLowerCase();
  const text = String(action.text ?? action.value ?? '');
  const url = clipped(String(action.url ?? ''), 96);
  const note = permissionNote(reason, kind);
  switch (kind) {
    case 'type':
      return {
        title: action.submit ? '允许提交网页表单？' : '允许填写网页内容？',
        context: '内置浏览器 · 当前页面',
        summary: action.submit ? `输入${quoted(text)}并提交` : `输入${quoted(text)}`,
        note,
      };
    case 'press': {
      const key = clipped(String(action.key ?? '按键'), 32);
      return {
        title: key.toLowerCase() === 'enter' ? '允许提交网页表单？' : '允许执行按键操作？',
        context: '内置浏览器 · 当前页面',
        summary: `按下 ${key}`,
        note,
      };
    }
    case 'click':
      return {
        title: '允许点击页面控件？',
        context: '内置浏览器 · 当前页面',
        summary: '点击当前页面中的指定控件',
        note,
      };
    case 'navigate':
    case 'tab_new':
      return {
        title: '允许打开此网页？',
        context: '内置浏览器',
        summary: url ? `打开 ${url}` : '打开目标网页',
        note,
      };
    case 'upload': {
      const paths = Array.isArray(action.paths) ? action.paths : [];
      return {
        title: '允许上传文件？',
        context: '内置浏览器 · 当前页面',
        summary: paths.length > 0 ? `上传 ${paths.length} 个文件` : '向当前页面上传文件',
        note: permissionNote(reason || '上传', kind),
      };
    }
    case 'download':
      return {
        title: '允许下载文件？',
        context: '内置浏览器 · 当前页面',
        summary: '将页面文件保存到任务目录',
        note: permissionNote(reason || '下载', kind),
      };
    case 'dialog_accept':
    case 'accept':
      return {
        title: '允许确认网页对话框？',
        context: '内置浏览器 · 当前页面',
        summary: '确认当前网页弹出的对话框',
        note,
      };
    default:
      return {
        title: '允许执行浏览器操作？',
        context: '内置浏览器 · 当前页面',
        summary: '执行当前页面请求的操作',
        note,
      };
  }
}

function permissionPresentation(question: PendingFollowup): PermissionPresentation {
  const toolName = permissionToolName(question.title);
  const parts = permissionPromptParts(question.questions[0]?.question ?? '');
  const actionObject = parseActionObject(parts.action);
  if (toolName.startsWith('wiki_')) {
    return {
      title: '允许执行 Wiki 操作？',
      context: 'Wiki 知识库',
      summary: clipped(parts.action, 160) || '执行知识库变更操作',
      note: '',
    };
  }
  if ((toolName === 'browser_use' || toolName.startsWith('browser_')) && actionObject) {
    return browserPermissionPresentation(normalizedBrowserAction(toolName, actionObject), parts.reason);
  }
  if (/shell|bash|command|terminal/i.test(toolName)) {
    return {
      title: '允许执行此命令？',
      context: '终端',
      summary: '运行以下命令',
      command: clipped(parts.action, 240),
      note: permissionNote(parts.reason, parts.action),
    };
  }
  if (/write|edit|patch|file/i.test(toolName)) {
    return {
      title: '允许修改文件？',
      context: '当前工作区',
      summary: parts.action && !actionObject ? clipped(parts.action, 120) : '修改工作区中的文件',
      note: '',
    };
  }
  return {
    title: '允许执行此操作？',
    context: '当前任务',
    summary: parts.action && !actionObject ? clipped(parts.action, 120) : '执行代理请求的操作',
    note: permissionNote(parts.reason, parts.action),
  };
}

function permissionButtonClass(label: string, value: string): string {
  const choice = `${label} ${value}`.toLowerCase();
  if (/allow_once|允许一次|允许本次|确认执行/.test(choice)) return ' permission-dialog__button--primary';
  if (/always|session_exact|始终允许|本次对话|allow_batch|本批次/.test(choice)) return ' permission-dialog__button--persistent';
  return ' permission-dialog__button--secondary';
}

function followupSourceHtml(question: PendingFollowup): string {
  const agentName = question.origin?.agentName;
  if (!agentName) return '';
  if (question.origin?.type === 'team_control') {
    return `<div class="followup-card__source followup-card__source--team">
      <span class="session__team-logo" aria-hidden="true"><i></i><i></i></span>
      <span>${escapeHtml(agentName)}</span>
    </div>`;
  }
  return `<div class="followup-card__source">${escapeHtml(agentName)} 正在询问</div>`;
}

export function isRuntimeStaffingFollowup(
  question: PendingFollowup | null | undefined,
): boolean {
  return question?.origin?.mentionIntent === 'runtime_staffing';
}

function followupOptionsHtml(
  question: PendingFollowup['questions'][number],
  options = question.options,
  includeFreeText = question.allowFreeText,
): string {
  const inputType = question.multiSelect ? 'checkbox' : 'radio';
  const optionHtml = options.map((option) => {
    const description = option.description
      ? `<span class="followup-card__option-description">${escapeHtml(option.description)}</span>`
      : '';
    return `
      <label class="followup-card__option">
        <input type="${inputType}" name="followup_${escapeHtml(question.id)}" value="${escapeHtml(option.value)}" data-qid="${escapeHtml(question.id)}" />
        <span class="followup-card__option-copy"><span>${escapeHtml(option.label)}</span>${description}</span>
      </label>`;
  }).join('');
  const freeTextOption = includeFreeText ? `
      <label class="followup-card__option followup-card__option--free">
        <input type="${inputType}" name="followup_${escapeHtml(question.id)}" value="${FREE_TEXT_OPTION}" data-qid="${escapeHtml(question.id)}" data-free="1" />
        <span>其他（自定义输入）</span>
      </label>
      <input type="text" class="followup-card__free-input" data-qid="${escapeHtml(question.id)}" placeholder="请输入你的回答…" hidden />`
    : '';
  return `${optionHtml}${freeTextOption}`;
}

function permissionDialogHtml(question: PendingFollowup): string {
  const firstQuestion = question.questions[0];
  const presentation = permissionPresentation(question);
  const dialogId = `permission-${question.questionId.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  const options = firstQuestion?.options ?? [];
  const rank = (label: string, value: string): number => {
    const choice = `${label} ${value}`.toLowerCase();
    if (/deny|拒绝|取消/.test(choice)) return 0;
    if (/always|session_exact|始终允许|本次对话|allow_batch|本批次/.test(choice)) return 1;
    return 2;
  };
  const orderedOptions = [...options].sort(
    (left, right) => rank(left.label, left.value) - rank(right.label, right.value),
  );
  const actions = orderedOptions.map((option) => `
      <button type="button" class="permission-dialog__button${permissionButtonClass(option.label, option.value)}" data-permission-qid="${escapeHtml(firstQuestion?.id ?? '')}" data-permission-value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</button>`).join('');
  return `
    <div class="followup-card followup-card--permission" data-followup-id="${escapeHtml(question.questionId)}" role="dialog" aria-modal="false" aria-labelledby="${dialogId}-title" aria-describedby="${dialogId}-description" tabindex="-1">
      ${followupSourceHtml(question)}
      <div class="followup-card__header">
        <span class="followup-card__header-icon">${PERMISSION_ICON}</span>
        <div class="followup-card__header-copy">
          <div class="followup-card__title" id="${dialogId}-title">${escapeHtml(presentation.title)}</div>
          <div class="followup-card__subtitle" id="${dialogId}-description">Crew 请求执行以下操作</div>
        </div>
      </div>
      <div class="permission-dialog__body">
        <div class="permission-dialog__operation">
          <span class="permission-dialog__context">${escapeHtml(presentation.context)}</span>
          <strong class="permission-dialog__summary">${escapeHtml(presentation.summary)}</strong>
          ${presentation.command ? `<code class="permission-dialog__command">${escapeHtml(presentation.command)}</code>` : ''}
        </div>
        ${presentation.note ? `<div class="permission-dialog__notice">${PERMISSION_NOTICE_ICON}<span>${escapeHtml(presentation.note)}</span></div>` : ''}
      </div>
      <div class="permission-dialog__actions">${actions}</div>
    </div>`;
}

function staffingStatusHtml(question: PendingFollowup): string {
  const status = question.status ?? 'applying';
  const noteParts = (question.note ?? '').split('\n').map((part) => part.trim()).filter(Boolean);
  const defaultTitle = status === 'applied'
    ? '协作助手已加入，继续开工'
    : status === 'declined'
      ? '好，这次先不添加'
      : status === 'failed'
        ? '这位助手暂时没能加入'
        : '正在邀请协作助手加入……';
  const title = noteParts[0] ?? defaultTitle;
  const detail = noteParts.slice(1).join(' ');
  const icon = status === 'applying'
    ? '<span class="staffing-dialog__spinner" aria-hidden="true"></span>'
    : STAFFING_CHECK_ICON;
  return `
    <div class="followup-card followup-card--staffing followup-card--staffing-status followup-card--staffing-${escapeHtml(status)}" data-followup-id="${escapeHtml(question.questionId)}" role="status" aria-live="polite" aria-busy="${status === 'applying' ? 'true' : 'false'}">
      <div class="staffing-dialog__status-icon">${icon}</div>
      <div class="staffing-dialog__status-copy">
        <div class="followup-card__title">${escapeHtml(title)}</div>
        ${detail ? `<div class="followup-card__subtitle">${escapeHtml(detail)}</div>` : ''}
      </div>
    </div>`;
}

function staffingDialogHtml(question: PendingFollowup): string {
  if (['applying', 'applied', 'declined', 'failed'].includes(question.status ?? '')) {
    return staffingStatusHtml(question);
  }
  const firstQuestion = question.questions[0];
  const dialogId = `staffing-${question.questionId.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  const candidates = firstQuestion?.options.filter((option) => option.value !== 'decline') ?? [];
  const decline = firstQuestion?.options.find((option) => option.value === 'decline');
  return `
    <div class="followup-card followup-card--staffing" data-followup-id="${escapeHtml(question.questionId)}" role="alertdialog" aria-modal="true" aria-labelledby="${dialogId}-title" aria-describedby="${dialogId}-description" tabindex="-1">
      <div class="staffing-dialog__source">
        <span class="session__team-logo" aria-hidden="true"><i></i><i></i></span>
        <span>团队需要你的确认</span>
      </div>
      <div class="followup-card__header staffing-dialog__header">
        <span class="followup-card__header-icon staffing-dialog__header-icon">${STAFFING_ICON}</span>
        <div class="followup-card__header-copy">
          <div class="followup-card__title" id="${dialogId}-title">${escapeHtml(question.title || '给这项任务找一位帮手？')}</div>
          <div class="followup-card__subtitle" id="${dialogId}-description">${escapeHtml(firstQuestion?.question ?? '')}</div>
        </div>
      </div>
      <div class="staffing-dialog__body">
        ${question.note ? `<div class="staffing-dialog__scope">${STAFFING_CHECK_ICON}<span>${escapeHtml(question.note)}</span></div>` : ''}
        <div class="followup-card__question" data-qid="${escapeHtml(firstQuestion?.id ?? '')}">
          <div class="followup-card__qtext staffing-dialog__choice-title">选择一位协作助手</div>
          <div class="followup-card__options" role="radiogroup" aria-label="选择一位协作助手">
            ${firstQuestion ? followupOptionsHtml(firstQuestion, candidates, false) : ''}
          </div>
        </div>
      </div>
      <div class="staffing-dialog__actions">
        <button type="button" class="followup-card__dismiss" data-action="staffing-decline" data-staffing-qid="${escapeHtml(firstQuestion?.id ?? '')}" data-staffing-value="${escapeHtml(decline?.value ?? 'decline')}">${escapeHtml(decline?.label ?? '这次先不添加')}</button>
        <button type="button" class="followup-card__submit" data-action="submit" disabled>确认并继续</button>
      </div>
    </div>`;
}

/** 渲染追问卡片 HTML。 */
export function renderFollowupCard(question: PendingFollowup): string {
  if (isRuntimeStaffingFollowup(question)) return staffingDialogHtml(question);
  const isPermission = question.recordHistory === false;
  if (isPermission) return permissionDialogHtml(question);
  const blocks = question.questions
    .map((q) => {
      return `
        <div class="followup-card__question" data-qid="${escapeHtml(q.id)}">
          <div class="followup-card__qtext">${escapeHtml(q.question)}</div>
          <div class="followup-card__options" role="${q.multiSelect ? 'group' : 'radiogroup'}" aria-label="${escapeHtml(q.question)}">
            ${followupOptionsHtml(q)}
          </div>
        </div>`;
    })
    .join('');

  const header = `${followupSourceHtml(question)}
      ${question.title ? `<div class="followup-card__title">${escapeHtml(question.title)}</div>` : ''}`;

  return `
    <div class="followup-card" data-followup-id="${escapeHtml(question.questionId)}">
      ${header}
      ${blocks}
      <div class="followup-card__actions">
        <button type="button" class="followup-card__dismiss" data-action="cancel">取消</button>
        <button type="button" class="followup-card__submit" data-action="submit" disabled>提交</button>
      </div>
    </div>`;
}

/** Format a recorded follow-up using display labels, never internal option values. */
export function formatFollowupAnswerMessage(
  question: PendingFollowup,
  answers: FollowupAnswer[],
): string | null {
  if (question.recordHistory === false) return null;
  const questionById = new Map(question.questions.map((item) => [item.id, item]));
  const parts = answers.flatMap((answer) => {
    const item = questionById.get(answer.question_id);
    const labelByValue = new Map(item?.options.map((option) => [option.value, option.label]) ?? []);
    const values = answer.answers.map((value) => labelByValue.get(value) ?? value).filter(Boolean);
    return values.length ? [values.join(', ')] : [];
  });
  return parts.length ? `已选择：${parts.join('；')}` : null;
}

/** 渲染追问卡片为 DOM 节点（供 chat-controller keyed-diff 使用）。 */
export function renderFollowupCardElement(question: PendingFollowup): HTMLElement {
  const wrap = document.createElement('div');
  wrap.className = isRuntimeStaffingFollowup(question)
    ? 'followup-card-wrap followup-card-wrap--staffing'
    : question.recordHistory === false
      ? 'followup-card-wrap followup-card-wrap--permission'
      : 'followup-card-wrap';
  wrap.innerHTML = renderFollowupCard(question);
  return wrap;
}

interface FollowupBindings {
  onSubmit: (questionId: string, answers: FollowupAnswer[]) => void;
  onCancel: (questionId: string) => void;
}

/** 给当前渲染出的追问卡片绑定交互。每次 renderChat 重渲染后调用一次。
 *  防重复绑定：keyed-diff 在 questionId 不变时 reuse 同一 DOM 节点，
 *  若每次 renderChat 都叠加 addEventListener 会造成「点一次提交 N 条 answer」。
 *  用 data-bound 标记保证每卡只绑一次；节点被 replaceChild 重建时标记消失，自动重绑。 */
export function bindFollowupCard(root: HTMLElement, bindings: FollowupBindings): void {
  const card = root.querySelector<HTMLElement>('.followup-card');
  if (!card) return;
  if (card.dataset.bound === '1') return; // 已绑定，跳过
  card.dataset.bound = '1';
  const questionId = card.getAttribute('data-followup-id') ?? '';
  if (!questionId) return;
  const isPermission = card.classList.contains('followup-card--permission');

  if (isPermission) {
    const buttons = Array.from(card.querySelectorAll<HTMLButtonElement>('[data-permission-value]'));
    let resolved = false;
    card.addEventListener('click', (event) => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-permission-value]');
      if (!button || resolved) return;
      const qid = button.getAttribute('data-permission-qid') ?? '';
      const value = button.getAttribute('data-permission-value') ?? '';
      if (qid && value) {
        resolved = true;
        buttons.forEach((item) => { item.disabled = true; });
        bindings.onSubmit(questionId, [{ question_id: qid, answers: [value] }]);
      }
    });
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (resolved) return;
        resolved = true;
        buttons.forEach((item) => { item.disabled = true; });
        bindings.onCancel(questionId);
      }
    });
    // 这是非模态浮层：不抢占当前页面焦点，也不把 Tab 键限制在浮层内。
    return;
  }

  // 每个子问题当前选中：answers[qid] = Set<option>
  const selected: Record<string, Set<string>> = {};
  const freeText: Record<string, string> = {};
  for (const q of card.querySelectorAll<HTMLElement>('.followup-card__question')) {
    const qid = q.getAttribute('data-qid') ?? '';
    if (qid) {
      selected[qid] = new Set();
      freeText[qid] = '';
    }
  }
  // 取该问题的 multiSelect 约束
  const multiByQid: Record<string, boolean> = {};
  card.querySelectorAll<HTMLInputElement>('input[type="checkbox"], input[type="radio"]').forEach((input) => {
    const qid = input.getAttribute('data-qid') ?? '';
    if (qid) multiByQid[qid] = input.type === 'checkbox';
  });

  const submitBtn = card.querySelector<HTMLButtonElement>('.followup-card__submit');
  const refreshSubmit = () => {
    let can = true;
    for (const qid of Object.keys(selected)) {
      const cur = selected[qid];
      if (cur.size === 0) { can = false; break; }
      if (cur.has(FREE_TEXT_OPTION) && !freeText[qid]?.trim()) { can = false; break; }
    }
    if (submitBtn) submitBtn.disabled = !can;
  };

  card.addEventListener('change', (event) => {
    const input = event.target as HTMLInputElement;
    const qid = input.getAttribute('data-qid') ?? '';
    if (!qid) return;
    const multi = !!multiByQid[qid];
    const cur = selected[qid] ?? (selected[qid] = new Set());

    if (input.value === FREE_TEXT_OPTION) {
      // 切换「其他」
      if (cur.has(FREE_TEXT_OPTION)) {
        cur.delete(FREE_TEXT_OPTION);
      } else {
        if (!multi) cur.clear();
        cur.add(FREE_TEXT_OPTION);
      }
    } else {
      if (multi) {
        if (cur.has(input.value)) cur.delete(input.value);
        else { cur.delete(FREE_TEXT_OPTION); cur.add(input.value); }
      } else {
        cur.clear();
        cur.add(input.value);
      }
    }

    // 同步「其他」输入框显隐
    const freeInput = card.querySelector<HTMLInputElement>(`.followup-card__free-input[data-qid="${qid}"]`);
    if (freeInput) freeInput.hidden = !cur.has(FREE_TEXT_OPTION);
    refreshSubmit();
  });

  card.addEventListener('input', (event) => {
    const input = event.target as HTMLInputElement;
    if (!input.classList.contains('followup-card__free-input')) return;
    const qid = input.getAttribute('data-qid') ?? '';
    if (qid) freeText[qid] = input.value;
    refreshSubmit();
  });

  card.addEventListener('click', (event) => {
    const btn = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-action');
    if (action === 'cancel') {
      bindings.onCancel(questionId);
      return;
    }
    if (action === 'staffing-decline') {
      const qid = btn.getAttribute('data-staffing-qid') ?? '';
      const value = btn.getAttribute('data-staffing-value') ?? 'decline';
      if (!qid) return;
      btn.disabled = true;
      if (submitBtn) submitBtn.disabled = true;
      bindings.onSubmit(questionId, [{ question_id: qid, answers: [value] }]);
      return;
    }
    if (action === 'submit') {
      if (btn.disabled) return;
      const result: FollowupAnswer[] = [];
      for (const qid of Object.keys(selected)) {
        const cur = selected[qid];
        const texts: string[] = [];
        cur.forEach((opt) => {
          if (opt === FREE_TEXT_OPTION) {
            const t = freeText[qid]?.trim();
            if (t) texts.push(t);
          } else {
            texts.push(opt);
          }
        });
        result.push({ question_id: qid, answers: texts });
      }
      bindings.onSubmit(questionId, result);
    }
  });
}
