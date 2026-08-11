import { createIcon } from '../components/icon';
import {
  SECURITY_MODE_OPTIONS,
  type ConversationSecurityMode,
} from './security-approval';
import {
  formatCapabilitySummary,
  detectedRuntimePlatform,
  isMacOSPlatform,
  isWindowsPlatform,
  type SecurityCapabilities,
} from './security-mode';
import {
  formatSecurityRule,
  formatSecurityRulePermissions,
  type SecurityRuleView,
} from './security-rules';
import {
  actionTypeLabel,
  approvalChoiceLabel,
  decisionSourceLabel,
  EMPTY_SECURITY_AUDIT_QUERY,
  modeLabel,
  type SecurityAuditQuery,
  type SecurityAuditView,
} from './security-audit';
import { bindPagination, renderPagination } from '../pagination';
import { openDialog } from '../components/overlays';

// Strict security is the default. Compatibility remains an explicit user choice.
const STRICT_SECURITY_TOGGLE_VISIBLE = true;

export interface SecurityCenterSnapshot {
  loading: boolean;
  setupAction?: 'install' | 'uninstall' | null;
  error: string;
  workspaceId: string;
  strictSecurityEnabled: boolean;
  mode: ConversationSecurityMode;
  capabilities: SecurityCapabilities | null;
  rules: SecurityRuleView[];
  audits: SecurityAuditView[];
  auditPage?: number;
  auditPageSize?: number;
  auditTotal?: number;
  auditQuery?: SecurityAuditQuery;
}

export interface SecurityCenterActions {
  onRefresh(): void;
  onStrictSecurityChange(enabled: boolean): void;
  onModeChange(mode: ConversationSecurityMode): void;
  onInstall(): void;
  onUninstall(): void;
  onRuleToggle(rule: SecurityRuleView): void;
  onRuleDelete(rule: SecurityRuleView): void;
  onAuditExport(): void;
  onAuditPurge(): void;
  onAuditPageChange?(page: number): void;
  onAuditPageSizeChange?(size: number): void;
  onAuditQueryChange?(query: SecurityAuditQuery): void;
}

export interface SecurityCenterView {
  element: HTMLElement;
  update(snapshot: SecurityCenterSnapshot): void;
}

function text(tag: keyof HTMLElementTagNameMap, className: string, value: string): HTMLElement {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

function button(
  label: string,
  action: string,
  variant: 'secondary' | 'danger' | 'primary' = 'secondary',
): HTMLButtonElement {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = `mw-button mw-button--${variant} mw-button--default`;
  element.dataset.securityAction = action;
  element.textContent = label;
  return element;
}

function statusCard(label: string, value: string, detail: string, tone: string): HTMLElement {
  const card = document.createElement('article');
  card.className = 'security-center__status-card';
  card.dataset.tone = tone;
  card.append(
    text('span', 'security-center__status-label', label),
    text('strong', 'security-center__status-value', value),
    text('span', 'security-center__status-detail', detail),
  );
  return card;
}

function renderStrictSecuritySection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const row = document.createElement('label');
  const copy = document.createElement('span');
  const toggle = document.createElement('input');
  section.className = 'security-center__section';
  row.className = 'security-center__preference';
  copy.className = 'security-center__preference-copy';
  toggle.type = 'checkbox';
  toggle.checked = snapshot.strictSecurityEnabled;
  toggle.dataset.securityStrictToggle = '';
  toggle.setAttribute('role', 'switch');
  copy.append(
    text('strong', 'security-center__preference-title', '严格安全约束'),
    text(
      'span',
      'security-center__section-description',
      snapshot.strictSecurityEnabled
        ? '已阻止不安全传输、未验证安装和宽松默认审批。'
        : '兼容模式放宽旧传输、完整性校验和默认审批；受管隔离仍保持启用。',
    ),
  );
  row.append(copy, toggle);
  section.append(
    text('h2', 'security-center__section-title', '全局安全策略'),
    row,
  );
  return section;
}

function renderModeSection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const list = document.createElement('div');
  section.className = 'security-center__section';
  list.className = 'security-center__mode-grid';
  section.append(
    text('h2', 'security-center__section-title', '会话安全模式'),
    text(
      'p',
      'security-center__section-description',
      `当前工作空间：${snapshot.workspaceId}。完全访问权限只作用于当前会话，不会静默继承到后续新会话。`,
    ),
  );
  for (const option of SECURITY_MODE_OPTIONS) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'security-center__mode';
    item.dataset.securityMode = option.value;
    item.dataset.selected = String(option.value === snapshot.mode);
    item.dataset.tone = option.value === 'full_access' ? 'danger' : 'neutral';
    item.setAttribute('aria-pressed', String(option.value === snapshot.mode));
    item.append(
      text('strong', 'security-center__mode-label', option.label),
      text('span', 'security-center__mode-description', option.desc),
    );
    list.append(item);
  }
  section.append(list);
  return section;
}

function renderCapabilitySection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const overview = document.createElement('div');
  const actions = document.createElement('div');
  const capabilities = snapshot.capabilities;
  const capabilitiesLoaded = capabilities !== null;
  const platform = detectedRuntimePlatform(capabilities?.platform);
  const isWindows = isWindowsPlatform(platform);
  const isMacOS = isMacOSPlatform(platform);
  const helperReady = capabilitiesLoaded && capabilities.helper_present === true;
  const sandboxReady = helperReady && Boolean(capabilities?.filesystem_sandbox);
  const networkReady = sandboxReady && Boolean(capabilities?.managed_network);
  const unsupportedReason = platform
    ? (isWindows || isMacOS ? '' : '当前设备尚未提供原生防护')
    : '正在检测本机防护能力';

  section.className = 'security-center__section';
  overview.className = 'security-center__status-grid';
  actions.className = 'security-center__actions';
  overview.append(
    statusCard(
      '本机运行组件',
      helperReady ? '已就绪' : '未就绪',
      capabilities?.detail ?? '等待能力检测',
      helperReady ? 'success' : 'warning',
    ),
    statusCard(
      '文件沙箱',
      sandboxReady ? '已启用' : '未启用',
      formatCapabilitySummary(capabilities ?? {}),
      sandboxReady ? 'success' : 'warning',
    ),
    statusCard(
      '联网管控',
      networkReady ? '受控' : '未受控',
      networkReady ? '网络请求进入受管规则' : '不能声称出网已受保护',
      networkReady ? 'success' : 'danger',
    ),
  );

  const setupAction = snapshot.setupAction ?? null;
  const protectionReady = networkReady;
  const setupBusy = setupAction !== null;
  const install = button(
    setupAction === 'install' ? '正在安装…' : '安装 / 修复防护',
    'install',
    'primary',
  );
  const uninstall = button(
    setupAction === 'uninstall' ? '正在卸载…' : '卸载防护',
    'uninstall',
    'danger',
  );
  // 安装/修复与卸载互斥：未完整启用时只允许安装，完整启用后只允许卸载。
  // setupAction 还会锁住 UAC/IPC 进行中的短窗口，避免重复触发。
  install.disabled = !isWindows || snapshot.loading || setupBusy || protectionReady;
  uninstall.disabled = !isWindows || snapshot.loading || setupBusy || !protectionReady;
  install.setAttribute('aria-busy', String(setupAction === 'install'));
  uninstall.setAttribute('aria-busy', String(setupAction === 'uninstall'));
  if (isWindows) actions.append(install, uninstall);

  section.append(
    text('h2', 'security-center__section-title', '本机防护能力'),
    overview,
    actions,
  );
  if (unsupportedReason) {
    section.append(text('p', 'security-center__policy-note', unsupportedReason));
  } else if (isMacOS) {
    const macNote = !capabilitiesLoaded
      ? '安全能力检测未返回，请点击刷新重试。'
      : !helperReady
      ? '未找到 macOS 原生安全运行组件，请重新构建或重新安装应用；无需管理员权限。'
      : !sandboxReady
        ? 'macOS 原生运行组件存在，但文件沙箱 live probe 未通过；请重启应用或重新构建运行组件。'
        : !networkReady
          ? '文件沙箱已启用，但联网管控 live probe 未通过；当前不能声称出网已受保护。'
          : '系统内置原生防护，运行组件随应用提供，无需手动安装或申请管理员权限。';
    section.append(text('p', 'security-center__policy-note', macNote));
  }
  return section;
}

function renderRulesSection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const list = document.createElement('ul');
  section.className = 'security-center__section';
  list.className = 'security-center__rule-list';
  section.append(
    text('h2', 'security-center__section-title', '永久授权规则'),
    text(
      'p',
      'security-center__section-description',
      '规则按当前工作空间生效；禁用可恢复，删除不可撤销。',
    ),
  );

  if (!snapshot.rules.length) {
    list.append(text('li', 'security-center__empty', '没有永久授权规则'));
  }
  for (const rule of snapshot.rules) {
    const item = document.createElement('li');
    const copy = document.createElement('div');
    const actions = document.createElement('div');
    item.className = 'security-center__rule';
    item.dataset.enabled = String(rule.enabled !== false);
    item.dataset.ruleId = String(rule.rule_id ?? '');
    copy.className = 'security-center__rule-copy';
    actions.className = 'security-center__rule-actions';
    const detail = [rule.action_detail?.trim(), formatSecurityRulePermissions(rule)]
      .filter(Boolean)
      .join('\n');
    copy.append(
      text('strong', 'security-center__rule-title', formatSecurityRule(rule)),
      ...(detail
        ? [text('pre', 'security-center__rule-detail', detail)]
        : []),
      text(
        'span',
        'security-center__rule-meta',
        `范围：${snapshot.workspaceId} · ${rule.enabled === false ? '已禁用' : '已启用'}`,
      ),
    );
    const toggle = button(rule.enabled === false ? '启用' : '禁用', 'toggle-rule');
    toggle.dataset.ruleId = String(rule.rule_id ?? '');
    const remove = button('删除', 'delete-rule', 'danger');
    remove.dataset.ruleId = String(rule.rule_id ?? '');
    actions.append(toggle, remove);
    item.append(copy, actions);
    list.append(item);
  }
  section.append(list);
  return section;
}

function auditSelect(
  label: string,
  filter: string,
  value: string,
  options: Array<[string, string]>,
): HTMLLabelElement {
  const control = document.createElement('label');
  const select = document.createElement('select');
  control.className = 'security-center__audit-filter';
  select.dataset.securityAuditFilter = filter;
  for (const [optionValue, optionLabel] of options) {
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = optionLabel;
    select.append(option);
  }
  select.value = value;
  control.append(text('span', 'security-center__audit-filter-label', label), select);
  return control;
}

function appendAuditDetail(
  list: HTMLDListElement,
  label: string,
  value: string | undefined,
): void {
  if (!value) return;
  list.append(
    text('dt', 'security-center__audit-detail-label', label),
    text('dd', 'security-center__audit-detail-value', value),
  );
}

function openAuditDetail(event: SecurityAuditView, trigger: HTMLElement): void {
  const content = document.createElement('div');
  const overview = document.createElement('div');
  const metadata = document.createElement('dl');
  const command = document.createElement('pre');
  const title = event.session_title || '未命名对话';
  const project = event.workspace_name || event.workspace_id || '未知项目';
  const currentMode = event.current_approval_mode || event.approval_mode;
  content.className = 'security-center__audit-detail';
  overview.className = 'security-center__audit-detail-overview';
  metadata.className = 'security-center__audit-detail-grid';
  command.className = 'security-center__audit-command';
  command.textContent = [
    event.action_detail || event.action_summary || '旧记录未保存动作详情',
    event.additional_permissions_summary
      ? `额外权限：${event.additional_permissions_summary}`
      : '',
  ].filter(Boolean).join('\n');
  overview.append(
    text('span', 'security-center__audit-detail-kicker', actionTypeLabel(event.action_type)),
    text('strong', 'security-center__audit-detail-summary', event.action_summary || '安全事件'),
    text(
      'span',
      'security-center__audit-detail-choice',
      `${approvalChoiceLabel(event)} · ${modeLabel(currentMode)}`,
    ),
  );
  appendAuditDetail(metadata, '对话标题', title);
  appendAuditDetail(metadata, '会话 ID', event.session_id);
  appendAuditDetail(metadata, '项目', project);
  appendAuditDetail(metadata, '项目 ID', event.workspace_id);
  appendAuditDetail(metadata, '项目目录', event.workspace_root);
  appendAuditDetail(metadata, '用户选择', approvalChoiceLabel(event));
  appendAuditDetail(metadata, '事件审批模式', modeLabel(event.approval_mode));
  appendAuditDetail(metadata, '当前审批模式', modeLabel(currentMode));
  appendAuditDetail(metadata, '决定来源', decisionSourceLabel(event.decision_source));
  appendAuditDetail(metadata, '工具', event.tool_name);
  appendAuditDetail(metadata, '请求 ID', event.request_id);
  appendAuditDetail(metadata, '任务 ID', event.task_id);
  content.append(
    overview,
    metadata,
    text('h3', 'security-center__audit-detail-heading', '脱敏后的完整动作'),
    command,
    text(
      'p',
      'security-center__audit-detail-note',
      '动作在写入审计库前已强制脱敏；旧记录可能只保留动作哈希。',
    ),
  );
  const handle = openDialog({
    trigger,
    title: '安全审计详情',
    content,
    actions: [{ label: '关闭', variant: 'secondary' }],
  });
  const dialog = handle.element.querySelector<HTMLElement>('.mw-dialog');
  dialog?.classList.add('security-center__audit-dialog');
  dialog?.setAttribute('data-security-audit-dialog', '');
}

function renderAuditSection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const header = document.createElement('div');
  const filters = document.createElement('div');
  const sessionFilter = document.createElement('form');
  const sessionInput = document.createElement('input');
  const list = document.createElement('ol');
  const footerActions = document.createElement('div');
  const pager = document.createElement('div');
  const total = snapshot.auditTotal ?? snapshot.audits.length;
  const query = snapshot.auditQuery ?? EMPTY_SECURITY_AUDIT_QUERY;
  section.className = 'security-center__section';
  header.className = 'security-center__section-heading';
  filters.className = 'security-center__audit-filters';
  sessionFilter.className = 'security-center__audit-session-filter';
  sessionInput.type = 'search';
  sessionInput.value = query.sessionId;
  sessionInput.placeholder = '筛选会话 ID';
  sessionInput.setAttribute('aria-label', '筛选会话 ID');
  sessionInput.dataset.securityAuditSession = '';
  list.className = 'security-center__audit-list';
  footerActions.className = 'security-center__actions';
  header.append(
    text('h2', 'security-center__section-title', '安全审计记录'),
    text('span', 'security-center__section-count', `共 ${total} 条`),
  );
  const applySessionFilter = button('筛选', 'filter-audit');
  applySessionFilter.type = 'submit';
  sessionFilter.append(sessionInput, applySessionFilter);
  filters.append(
    auditSelect('事件', 'action-type', query.actionType, [
      ['', '全部事件'],
      ['approval_requested', '等待审批'],
      ['approval_decision', '用户审批'],
      ['exec_decision', '命令判定'],
      ['file_decision', '文件判定'],
      ['network_decision', '网络判定'],
      ['exec_result', '命令执行结果'],
      ['rule_created', '创建授权规则'],
      ['rule_disabled', '停用授权规则'],
      ['rule_deleted', '删除授权规则'],
      ['audit_purged', '清理审计记录'],
    ]),
    auditSelect('结果', 'decision', query.decision, [
      ['', '全部结果'],
      ['allow', '允许'],
      ['deny', '拒绝'],
      ['pending', '等待审批'],
      ['ask', '需要审批'],
      ['once', '仅一次'],
      ['session', '当前会话'],
      ['always', '始终允许'],
      ['reject', '用户拒绝'],
      ['completed', '执行成功'],
      ['failed', '执行失败'],
      ['cancelled', '已取消'],
      ['error', '运行错误'],
      ['enabled', '已启用'],
      ['disabled', '已停用'],
      ['deleted', '已删除'],
    ]),
    auditSelect('排序', 'sort', query.sort, [
      ['newest', '最新优先'],
      ['oldest', '最早优先'],
    ]),
    sessionFilter,
  );
  if (!snapshot.audits.length) {
    list.append(text('li', 'security-center__empty', '没有安全审计记录'));
  }
  snapshot.audits.forEach((event, index) => {
    const item = document.createElement('li');
    const open = document.createElement('button');
    const heading = document.createElement('span');
    const meta = document.createElement('span');
    const summary = document.createElement('span');
    item.className = 'security-center__audit-item';
    open.type = 'button';
    open.className = 'security-center__audit-open';
    open.dataset.securityAuditIndex = String(index);
    open.dataset.securityAuditEvent = event.event_id || String(index);
    heading.className = 'security-center__audit-heading';
    meta.className = 'security-center__audit-meta';
    summary.className = 'security-center__audit-summary';
    heading.append(
      text('strong', 'security-center__audit-type', actionTypeLabel(event.action_type)),
      text('span', 'security-center__audit-choice', approvalChoiceLabel(event)),
    );
    meta.textContent = [
      event.timestamp ? new Date(event.timestamp * 1000).toLocaleString() : '未知时间',
      event.session_id ? `会话 ${event.session_id}` : '无会话',
      event.workspace_name || event.workspace_id || '',
    ].filter(Boolean).join(' · ');
    summary.textContent = event.action_summary || event.tool_name || '查看安全事件详情';
    open.append(heading, summary, meta);
    item.append(open);
    list.append(item);
  });
  pager.innerHTML = renderPagination(
    {
      page: snapshot.auditPage ?? 1,
      pageSize: snapshot.auditPageSize ?? 20,
      total,
    },
    { id: 'security-audit', pageSizeChoices: [10, 20, 50, 100] },
  );
  footerActions.append(
    button('导出 JSONL', 'export'),
    button('清理 30 天前记录', 'purge', 'danger'),
  );
  section.append(header, filters, list, pager, footerActions);
  return section;
}

export function createSecurityCenterView(
  actions: SecurityCenterActions,
  options: { embedded?: boolean } = {},
): SecurityCenterView {
  const element = document.createElement('section');
  const header = document.createElement('header');
  const headerCopy = document.createElement('div');
  const headerActions = document.createElement('div');
  const refresh = button('刷新', 'refresh');
  const status = document.createElement('div');
  const content = document.createElement('main');
  let snapshot: SecurityCenterSnapshot | null = null;

  element.className = `page-shell page-shell--security security-center${options.embedded ? ' security-center--embedded' : ''}`;
  element.dataset.securityCenter = '';
  header.className = 'page-header page-header--hub';
  headerCopy.className = 'page-header__copy';
  headerActions.className = 'page-header__actions';
  refresh.prepend(createIcon('icon-refresh', { size: 16 }));
  headerCopy.append(
    text('h1', 'page-header__title', '安全中心'),
    text('p', 'page-header__desc', '安全+不影响使用才是真安全～'),
  );
  headerActions.append(refresh);
  header.append(headerCopy, headerActions);
  status.className = 'security-center__load-state';
  status.setAttribute('role', 'status');
  content.className = 'security-center__content';
  element.append(...(options.embedded ? [status, content] : [header, status, content]));

  const handleClick = (event: MouseEvent): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>(
      '[data-security-action], [data-security-mode], [data-security-audit-index]',
    );
    if (!target || !element.contains(target) || target.disabled) return;
    const auditIndex = target.dataset.securityAuditIndex;
    if (auditIndex !== undefined) {
      const auditEvent = snapshot?.audits[Number(auditIndex)];
      if (auditEvent) openAuditDetail(auditEvent, target);
      return;
    }
    const mode = target.dataset.securityMode as ConversationSecurityMode | undefined;
    if (mode) {
      actions.onModeChange(mode);
      return;
    }
    const action = target.dataset.securityAction;
    if (action === 'refresh') actions.onRefresh();
    else if (action === 'install') actions.onInstall();
    else if (action === 'uninstall') actions.onUninstall();
    else if (action === 'export') actions.onAuditExport();
    else if (action === 'purge') actions.onAuditPurge();
    else if (action === 'toggle-rule' || action === 'delete-rule') {
      const rule = snapshot?.rules.find(
        (item) => String(item.rule_id ?? '') === target.dataset.ruleId,
      );
      if (rule && action === 'toggle-rule') actions.onRuleToggle(rule);
      if (rule && action === 'delete-rule') actions.onRuleDelete(rule);
    }
  };
  element.addEventListener('click', handleClick);
  element.addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target instanceof HTMLInputElement && target.matches('[data-security-strict-toggle]')) {
      actions.onStrictSecurityChange(target.checked);
      return;
    }
    if (target instanceof HTMLSelectElement && target.matches('[data-security-audit-filter]')) {
      const query = snapshot?.auditQuery ?? EMPTY_SECURITY_AUDIT_QUERY;
      const filter = target.dataset.securityAuditFilter;
      if (filter === 'action-type') {
        actions.onAuditQueryChange?.({
          ...query,
          actionType: target.value as SecurityAuditQuery['actionType'],
        });
      } else if (filter === 'decision') {
        actions.onAuditQueryChange?.({
          ...query,
          decision: target.value as SecurityAuditQuery['decision'],
        });
      } else if (filter === 'sort') {
        actions.onAuditQueryChange?.({
          ...query,
          sort: target.value as SecurityAuditQuery['sort'],
        });
      }
    }
  });
  element.addEventListener('submit', (event) => {
    const form = (event.target as Element).closest<HTMLFormElement>(
      '.security-center__audit-session-filter',
    );
    if (!form || !element.contains(form)) return;
    event.preventDefault();
    const query = snapshot?.auditQuery ?? EMPTY_SECURITY_AUDIT_QUERY;
    const input = form.querySelector<HTMLInputElement>('[data-security-audit-session]');
    actions.onAuditQueryChange?.({ ...query, sessionId: input?.value.trim() ?? '' });
  });

  return {
    element,
    update(nextSnapshot) {
      snapshot = nextSnapshot;
      refresh.disabled = nextSnapshot.loading;
      refresh.setAttribute('aria-busy', String(nextSnapshot.loading));
      status.dataset.state = nextSnapshot.error
        ? 'error'
        : nextSnapshot.loading
          ? 'loading'
          : 'ready';
      status.hidden = !nextSnapshot.error && !nextSnapshot.loading;
      status.textContent = nextSnapshot.error
        ? `安全中心加载失败：${nextSnapshot.error}`
        : nextSnapshot.loading
          ? '正在加载安全状态…'
          : '';
      content.replaceChildren(
        ...(STRICT_SECURITY_TOGGLE_VISIBLE ? [renderStrictSecuritySection(nextSnapshot)] : []),
        renderModeSection(nextSnapshot),
        renderCapabilitySection(nextSnapshot),
        renderRulesSection(nextSnapshot),
        renderAuditSection(nextSnapshot),
      );
      bindPagination('security-audit', {
        onPageChange: (page) => actions.onAuditPageChange?.(page),
        onPageSizeChange: (size) => actions.onAuditPageSizeChange?.(size),
      });
    },
  };
}
