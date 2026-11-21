import {
  detectedRuntimePlatform,
  isMacOSPlatform,
  isWindowsPlatform,
  windowsSetupRequired,
  type SecurityCapabilities,
} from './security-mode';
import { formatSecurityRule, type SecurityRuleView } from './security-rules';
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
import { openDialog, openMenu, type MenuItem, type OverlayHandle } from '../components/overlays';

export interface SecurityCenterSnapshot {
  loading: boolean;
  settingUp?: boolean;
  error: string;
  workspaceId: string;
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
  onInstall(): void;
  onRuleToggle(rule: SecurityRuleView): void;
  onRuleDelete(rule: SecurityRuleView): void;
  onAuditExport(): void;
  onAuditPurge(): void;
  onAuditPageChange?(page: number): void;
  onAuditPageSizeChange?(size: number): void;
  onAuditQueryChange?(query: Partial<SecurityAuditQuery>): void;
}

export interface SecurityCenterView {
  element: HTMLElement;
  update(snapshot: SecurityCenterSnapshot): void;
  dispose(): void;
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
  variant: 'secondary' | 'primary' = 'secondary',
): HTMLButtonElement {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = `mw-button mw-button--${variant} mw-button--default`;
  element.dataset.securityAction = action;
  element.textContent = label;
  return element;
}

function moreButton(action: string, label: string): HTMLButtonElement {
  const trigger = button('更多', action);
  trigger.setAttribute('aria-label', label);
  trigger.setAttribute('aria-haspopup', 'menu');
  trigger.setAttribute('aria-expanded', 'false');
  return trigger;
}

type SecurityDisclosure = 'rules' | 'audit';

function disclosure(id: SecurityDisclosure, title: string, body: HTMLElement, count?: number): HTMLDetailsElement {
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  details.className = 'security-center__section security-center__disclosure';
  details.dataset.securityDisclosure = id;
  summary.className = 'security-center__section-title';
  summary.append(text('span', '', title));
  if (count !== undefined) {
    summary.append(text('span', 'security-center__section-count', `共 ${count} 条`));
  }
  body.className = 'security-center__disclosure-body';
  details.append(summary, body);
  return details;
}

function renderCapabilitySection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const card = document.createElement('div');
  const copy = document.createElement('div');
  const capabilities = snapshot.capabilities;
  const platform = detectedRuntimePlatform(capabilities?.platform);
  const isWindows = isWindowsPlatform(platform);
  const isMacOS = isMacOSPlatform(platform);
  const setupRequired = windowsSetupRequired(capabilities);
  const networkAccess = capabilities?.workspace_network_access === true;
  const recovering = capabilities?.status === 'recovering';
  const ready = capabilities?.status === 'ready' || (capabilities?.status === undefined
    && capabilities?.helper_present === true
    && capabilities.runtime_stale !== true
    && capabilities.filesystem_sandbox === true
    && (networkAccess ? capabilities.direct_network === true : capabilities.managed_network === true));
  const backend = capabilities?.backend;
  const platformName = backend === 'windows_sandbox_account' || isWindows ? 'Windows'
    : backend === 'macos_seatbelt' || isMacOS ? 'macOS'
    : backend === 'linux_bwrap' || platform === 'linux' ? 'Linux' : '';
  let label: string;
  let description: string;
  let tone: 'neutral' | 'success' | 'warning';
  if (snapshot.settingUp) {
    label = '正在设置本机防护';
    description = '设置完成后，Ace 会重新检测防护状态。';
    tone = 'neutral';
  } else if (snapshot.loading) {
    label = '正在检测本机防护';
    description = 'Ace 会自动检测此设备支持的防护。';
    tone = 'neutral';
  } else if (recovering) {
    label = '正在恢复 Windows 沙箱';
    description = 'Ace 正在撤回上次执行留下的临时权限。若涉及较大的目录，Windows 可能需要几分钟。';
    tone = 'neutral';
  } else if (capabilities?.reason === 'windows_sandbox_account_repair_required') {
    label = 'Windows 沙箱账号需要管理员检查';
    description = capabilities.detail
      ?? '请由管理员检查本机 Ace 沙箱账号并恢复后重新检测。自动准备不会更改账号。';
    tone = 'warning';
  } else if (setupRequired) {
    label = 'Windows 防护需要设置';
    description = 'Windows 会请求一次管理员确认。完成后，Ace 自动使用本机防护。';
    tone = 'warning';
  } else if (ready) {
    label = '本机防护已就绪';
    description = `${platformName ? `${platformName} 防护` : '文件沙箱'}已可用于 Ace 的受管命令；${networkAccess ? '允许工作区命令直接联网。' : '工作区命令使用受控联网。'}`;
    tone = 'success';
  } else if (capabilities?.reason === 'windows_sandbox_busy') {
    label = 'Windows 沙箱正忙';
    description = '请等待当前操作结束，再重新检测。';
    tone = 'warning';
  } else if (capabilities?.reason === 'windows_acl_recovery_failed') {
    label = 'Windows 沙箱恢复未完成';
    description = '请重新检测本机防护。';
    tone = 'warning';
  } else {
    label = capabilities ? '本机防护暂不可用' : '暂时无法确认本机防护状态';
    description = !capabilities ? '请重新检测。'
      : !platformName ? '当前设备尚未提供原生防护。'
      : capabilities.helper_present === false || capabilities.runtime_stale === true
        ? '应用的本机防护组件缺失或版本不匹配，请更新或重新安装 Ace。'
        : '本机防护自检未通过。';
    tone = 'warning';
  }
  section.className = 'security-center__section';
  card.className = 'security-center__protection';
  card.dataset.tone = tone;
  copy.className = 'security-center__protection-copy';
  copy.append(
    text('strong', 'security-center__protection-title', label),
    text('p', 'security-center__section-description', description),
  );
  card.append(copy);
  const actions = document.createElement('div');
  actions.className = 'security-center__actions';
  if (setupRequired && (!snapshot.loading || snapshot.settingUp)) {
    const install = button(snapshot.settingUp ? '正在设置…' : '设置 Windows 防护', 'install', 'primary');
    install.disabled = Boolean(snapshot.settingUp);
    install.setAttribute('aria-busy', String(Boolean(snapshot.settingUp)));
    actions.append(install);
  }
  if (!snapshot.loading && !snapshot.settingUp && !recovering) {
    if (!ready && !setupRequired) actions.append(button('重新检测', 'refresh'));
    else if (snapshot.error) actions.append(button('重试加载', 'refresh'));
  }
  if (actions.childElementCount) card.append(actions);
  section.append(card);
  return section;
}

function renderRulesSection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const list = document.createElement('ul');
  list.className = 'security-center__rule-list';
  section.append(
    text('p', 'security-center__section-description', '收到审批请求时，选择「始终允许此操作」并确认即可保存规则。'),
    text('p', 'security-center__section-description', '规则仅在当前账号和工作空间内生效。后续操作须匹配获批的完整动作，或审批时展示的命令前缀。'),
  );

  if (!snapshot.rules.length) {
    list.append(text('li', 'security-center__empty', '此工作空间还没有已保存的规则。'));
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
    copy.append(
      text('strong', 'security-center__rule-title', formatSecurityRule(rule)),
      ...(rule.action_detail?.trim()
        ? [text('pre', 'security-center__rule-detail', rule.action_detail.trim())]
        : []),
      text(
        'span',
        'security-center__rule-meta',
        rule.enabled === false ? '已禁用' : '已启用',
      ),
    );
    const toggle = button(rule.enabled === false ? '启用' : '禁用', 'toggle-rule');
    toggle.dataset.ruleId = String(rule.rule_id ?? '');
    const more = moreButton('rule-menu', '授权规则的更多操作');
    more.dataset.ruleId = String(rule.rule_id ?? '');
    actions.append(toggle, more);
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

function openAuditDetail(event: SecurityAuditView, trigger: HTMLElement): OverlayHandle {
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
  command.textContent = event.action_detail || event.action_summary || '旧记录未保存动作详情';
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
  });
  const dialog = handle.element.querySelector<HTMLElement>('.mw-dialog');
  dialog?.classList.add('security-center__audit-dialog');
  dialog?.setAttribute('data-security-audit-dialog', '');
  return handle;
}

function renderAuditSection(snapshot: SecurityCenterSnapshot): HTMLElement {
  const section = document.createElement('section');
  const filters = document.createElement('div');
  const sessionFilter = document.createElement('form');
  const sessionInput = document.createElement('input');
  const list = document.createElement('ol');
  const footerActions = document.createElement('div');
  const total = snapshot.auditTotal ?? snapshot.audits.length;
  const pageSize = snapshot.auditPageSize ?? 20;
  const query = snapshot.auditQuery ?? EMPTY_SECURITY_AUDIT_QUERY;
  filters.className = 'security-center__audit-filters';
  sessionFilter.className = 'security-center__audit-session-filter';
  sessionInput.type = 'search';
  sessionInput.value = query.sessionId;
  sessionInput.placeholder = '筛选会话 ID';
  sessionInput.setAttribute('aria-label', '筛选会话 ID');
  sessionInput.dataset.securityAuditSession = '';
  list.className = 'security-center__audit-list';
  footerActions.className = 'security-center__actions';
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
  section.append(filters, list);
  if (total > pageSize) {
    const pager = document.createElement('div');
    pager.innerHTML = renderPagination(
      { page: snapshot.auditPage ?? 1, pageSize, total },
      { id: 'security-audit', pageSizeChoices: [10, 20, 50, 100] },
    );
    section.append(pager);
  }
  footerActions.append(moreButton('audit-menu', '审计记录的更多操作'));
  section.append(footerActions);
  return section;
}

export function createSecurityCenterView(
  actions: SecurityCenterActions,
  options: { embedded?: boolean } = {},
): SecurityCenterView {
  const element = document.createElement('section');
  const header = document.createElement('header');
  const headerCopy = document.createElement('div');
  const status = document.createElement('div');
  const content = document.createElement(options.embedded ? 'div' : 'main');
  let snapshot: SecurityCenterSnapshot | null = null;
  let protection: HTMLElement | undefined;
  let rulesSection: HTMLDetailsElement | undefined;
  let auditSection: HTMLDetailsElement | undefined;
  let menu: OverlayHandle | undefined;
  let menuAnchor: HTMLButtonElement | undefined;
  let auditDetail: OverlayHandle | undefined;

  function showActions(anchor: HTMLButtonElement, label: string, items: MenuItem[]): void {
    const wasOpen = anchor.getAttribute('aria-expanded') === 'true';
    menu?.close();
    if (wasOpen) return;
    menuAnchor = anchor;
    menu = openMenu({ anchor, label, items, align: 'end', onClose: () => {
      menu = undefined;
      menuAnchor = undefined;
    } });
  }

  function updateDisclosure(previous: HTMLDetailsElement | undefined, next: HTMLDetailsElement): void {
    if (!previous) {
      content.append(next);
      return;
    }
    if (menuAnchor && previous.contains(menuAnchor)) menu?.close();
    next.open = previous.open;
    previous.replaceWith(next);
  }

  element.className = `page-shell page-shell--security security-center${options.embedded ? ' security-center--embedded' : ''}`;
  element.dataset.securityCenter = '';
  header.className = 'page-header page-header--hub';
  headerCopy.className = 'page-header__copy';
  headerCopy.append(text('h1', 'page-header__title', '安全中心'));
  header.append(headerCopy);
  status.className = 'security-center__load-state';
  status.setAttribute('role', 'status');
  content.className = 'security-center__content';
  element.append(...(options.embedded ? [status, content] : [header, status, content]));

  const handleClick = (event: MouseEvent): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>(
      '[data-security-action], [data-security-audit-index]',
    );
    if (!target || !element.contains(target) || target.disabled) return;
    const auditIndex = target.dataset.securityAuditIndex;
    if (auditIndex !== undefined) {
      const auditEvent = snapshot?.audits[Number(auditIndex)];
      if (auditEvent) {
        auditDetail?.close();
        auditDetail = openAuditDetail(auditEvent, target);
      }
      return;
    }
    const action = target.dataset.securityAction;
    if (action === 'refresh') actions.onRefresh();
    else if (action === 'install') actions.onInstall();
    else if (action === 'audit-menu') {
      showActions(target, '账号审计记录', [
        { id: 'export', label: '导出账号全部审计记录', onSelect: actions.onAuditExport },
        { id: 'purge', label: '清理账号内 30 天前的审计记录', danger: true, onSelect: actions.onAuditPurge },
      ]);
    } else if (action === 'toggle-rule' || action === 'rule-menu') {
      const rule = snapshot?.rules.find(
        (item) => String(item.rule_id ?? '') === target.dataset.ruleId,
      );
      if (!rule) return;
      if (action === 'toggle-rule') actions.onRuleToggle(rule);
      else showActions(target, '授权规则', [
        { id: 'delete-rule', label: '永久删除授权规则', danger: true, onSelect: () => actions.onRuleDelete(rule) },
      ]);
    }
  };
  element.addEventListener('click', handleClick);
  element.addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target instanceof HTMLSelectElement && target.matches('[data-security-audit-filter]')) {
      const filter = target.dataset.securityAuditFilter;
      if (filter === 'action-type') {
        actions.onAuditQueryChange?.({ actionType: target.value as SecurityAuditQuery['actionType'] });
      } else if (filter === 'decision') {
        actions.onAuditQueryChange?.({ decision: target.value as SecurityAuditQuery['decision'] });
      } else if (filter === 'sort') {
        actions.onAuditQueryChange?.({ sort: target.value as SecurityAuditQuery['sort'] });
      }
    }
  });
  element.addEventListener('submit', (event) => {
    const form = (event.target as Element).closest<HTMLFormElement>(
      '.security-center__audit-session-filter',
    );
    if (!form || !element.contains(form)) return;
    event.preventDefault();
    const input = form.querySelector<HTMLInputElement>('[data-security-audit-session]');
    actions.onAuditQueryChange?.({ sessionId: input?.value.trim() ?? '' });
  });

  return {
    element,
    dispose() {
      element.remove();
      menu?.dispose();
      auditDetail?.dispose();
    },
    update(nextSnapshot) {
      const previous = snapshot;
      snapshot = { ...nextSnapshot };
      status.hidden = !nextSnapshot.error;
      status.textContent = nextSnapshot.error ? `安全中心加载失败：${nextSnapshot.error}` : '';
      if (!protection || previous?.capabilities !== nextSnapshot.capabilities
        || previous.loading !== nextSnapshot.loading || previous.settingUp !== nextSnapshot.settingUp
        || Boolean(previous.error) !== Boolean(nextSnapshot.error)) {
        const next = renderCapabilitySection(nextSnapshot);
        if (protection) protection.replaceWith(next);
        else content.append(next);
        protection = next;
      }
      if (!rulesSection || previous?.rules !== nextSnapshot.rules || previous.workspaceId !== nextSnapshot.workspaceId) {
        const next = disclosure('rules', '已保存的授权规则', renderRulesSection(nextSnapshot), nextSnapshot.rules.length || undefined);
        updateDisclosure(rulesSection, next);
        rulesSection = next;
      }
      if (!auditSection || previous?.audits !== nextSnapshot.audits || previous.auditQuery !== nextSnapshot.auditQuery
        || previous.auditPage !== nextSnapshot.auditPage || previous.auditPageSize !== nextSnapshot.auditPageSize
        || previous.auditTotal !== nextSnapshot.auditTotal) {
        const total = nextSnapshot.auditTotal ?? nextSnapshot.audits.length;
        const next = disclosure('audit', '安全审计记录', renderAuditSection(nextSnapshot), total);
        updateDisclosure(auditSection, next);
        auditSection = next;
        bindPagination('security-audit', {
          onPageChange: (page) => actions.onAuditPageChange?.(page),
          onPageSizeChange: (size) => actions.onAuditPageSizeChange?.(size),
        });
      }
    },
  };
}
