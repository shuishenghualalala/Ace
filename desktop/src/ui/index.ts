/**
 * Crew Desktop — 渲染层入口（网关对话 + Crew 能力接入）。
 *
 * 本文件是渲染层薄壳：只保留 init / bindGlobalEvents / setTab / setSystemPanel /
 * 频道弹窗 / 历史工作区 dock 挂载等顶层编排。会话与聊天流式逻辑位于：
 *  - features/chat-controller.ts   流式 dispatch / 渲染 / 发送 / 撤回 / 召唤
 *  - features/session-controller.ts 打开会话 / 历史回填 / 网关引导 / 状态水合
 *  - features/history-mapping.ts   后端历史 → ChatMessage 纯映射
 */

import { bindChannelConfigModal, bindModelConfigModal, openChannelConfigModal, renderConfigModels, renderPlatforms, toggleChannelConnection } from './features/config-panes';
import { bindModelPicker } from './features/model-picker';
import { bindComposerToolbar, syncCraftLabel } from './features/composer-toolbar';
import { bindComposerContextRing } from './features/composer-context-ring';
import { autoresizeTextarea, bindComposerIme, createComposerImeState, resetTextareaHeight, shouldComposerSend } from './features/composer-input';
import { bindSkillsTab, initSkillsPage } from './features/skills-page';
import { bindAgentsTab, initAgentsPage } from './features/agents-page';
import { refreshKanbanBoard, renderKanbanBoard } from './features/kanban-board';
import { bindAuditTab, initAuditPage } from './features/audit-page';
import { bindSystemTab, disposeSystemTab, renderSystemLogs, renderSystemOverview } from './features/system-page';
import { initUsagePage } from './features/usage-panel';
import { bindCronTab, renderCronTaskBoard, setCronCallbacks } from './features/cron-page';
import { bindWikiTab, setWikiAgentEntryHandler, setWikiAgentKbDeletedHandler } from './features/wiki-page';
import { bindSitesTab, renderSitesPage, syncSiteAnnotationEntry, syncSiteComposerMarker } from './features/sites-page';
import { bindBlueprintSurface } from './features/blueprint-surface';
import { forgetWikiAgentKb, initWikiAgent, openWikiAgent } from './features/wiki-agent';
import { assignSessionAgentDisplay, bindWorkspaceUi, createSessionInWorkspace, renderWorkspaceHistory, setWorkspacesUiCallbacks } from './features/workspaces';
import { bindSessionManageUi, setOpenSessionCallback } from './features/session-manage';
import { bindFeedbackUi } from './features/feedback';
import { bindSettingsUi, registerConfigPaneRenderers } from './features/settings';
import { initBackendStatusGuard, isBackendConnected, isBackendInitBypassActive, sealBackendInitBypass } from './features/backend-status-guard';
import { bindHistoryPanelToggle, applyHistoryCollapsed } from './features/history-collapse';
import { bindInspectorUi, openInspectorToTab, refreshInspector, setPlanBoardActions } from './features/inspector';
import { bindAttachments } from './features/attachments';
import {
  copyImageToClipboard,
  openImageViewer,
  revealImageInFolder,
} from './features/image-viewer';
import { bindComposerMention, isMentionOpen } from './features/composer-mention';
import { bindScenarioHub } from './features/scenarios-hub';
import { bindVersionUpdateUi } from './features/version-update';
import { armSubScenario, clearScenarioChip } from './features/scenario-arm';
import { loadRunningIntroCopy } from './features/running-intro';
import { installStreamDebugGlobal } from './stream-debug';
import { newTurnRequestId, resumeSessionGeneration } from './features/session-busy';
import {
  $,
  $$,
  notify,
  patchBook,
  setEditFrom,
  state,
  truncateMessagesFrom,
  type SystemPanelKey,
  type TabKey,
} from './state';
import {
  bookFor,
  cancelEdit,
  getMessages,
  patchPlanReviewMessages,
  refreshSessions,
  renderChat,
  sendMessage,
  setBusyWithUi,
  setChatCallbacks,
  setStatusWithUi,
  stopGeneration,
  updateComposerControls,
  updateGatewayDot,
  withdrawMessage,
} from './features/chat-controller';
import {
  bootstrapBackend,
  ensureDesktopGateway,
  hydrateBackendState,
  openSession,
  setSessionControllerSetTab,
} from './features/session-controller';
import { hydrateMissingTurnFileCounts } from './features/turn-file-counts';
import { externalAgentsEnabled } from './features/external-agents-feature';
import { initAuthFlow } from './features/login';
import { backendApi } from './backend-client';

function setTab(tab: TabKey): void {
  // 后端服务未就绪时阻断页面切换，遮罩已由 backend-status-guard 展示。
  // init 阶段旁路：允许构建 UI 骨架（遮罩覆盖下用户看不到）。
  if (!isBackendInitBypassActive() && !isBackendConnected()) {
    return;
  }
  if (tab === 'agents' && !externalAgentsEnabled()) {
    tab = 'chat';
  }
  state.activeTab = tab;
  $$('.nav-item, .more-item').forEach((item) => item.classList.toggle('active', item.getAttribute('data-tab') === tab));
  $$('.tab-pane').forEach((pane) => pane.classList.remove('active'));
  document.getElementById(`${tab}-tab`)?.classList.add('active');
  document.body.classList.toggle('history-workspace-active', tab === 'chat');
  document.body.classList.toggle('welcome-active', tab === 'chat' && !state.activeSessionId);
  if (tab === 'chat') {
    renderChat();
    syncSiteAnnotationEntry();
    renderCronTaskBoard();
    if (state.mode === 'dynamic_kanban' && state.activeSessionId) {
      void refreshKanbanBoard(state.activeSessionId);
    } else {
      renderKanbanBoard();
    }
  }
  if (tab === 'system') {
    setSystemPanel('overview');
  }
  if (tab === 'sites') renderSitesPage();
}

function setSystemPanel(panel: SystemPanelKey): void {
  state.activeSystemPanel = panel;
  // 修复 I-7：overview / logs / usage 三个面板都要渲染，否则点击 logs / usage 会显示空。
  if (panel === 'overview') renderSystemOverview();
  else if (panel === 'logs') void renderSystemLogs();
  else if (panel === 'usage') void import('./features/usage-panel').then((m) => m.renderUsagePage?.());
}

function bindGlobalEvents(): void {
  $('#title-bar-minimize')?.addEventListener('click', () => void window.Crew?.windowMinimize?.());
  $('#title-bar-maximize')?.addEventListener('click', () => void window.Crew?.windowMaximize?.());
  $('#title-bar-close')?.addEventListener('click', () => void window.Crew?.windowClose?.());

  $$('.nav-item[data-tab]').forEach((item) => {
    item.addEventListener('click', () => {
      const tab = item.getAttribute('data-tab') as TabKey | null;
      if (tab) setTab(tab);
    });
  });

  $$('.more-item[data-tab="system"]').forEach((item) => item.addEventListener('click', () => setTab('system')));

  window.addEventListener('external-agents:config-change', () => {
    if (!externalAgentsEnabled() && state.activeTab === 'agents') {
      setTab('chat');
    }
    renderWorkspaceHistory(openSession);
  });

  // 系统页已改为单页总览；日志 / 使用统计迁入设置弹窗

  document.querySelectorAll('.conn-row[data-channel]').forEach((card) => {
    card.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).closest('[data-channel-toggle]')) return;
      const channel = card.getAttribute('data-channel') || 'feishu';
      openChannelModal(channel);
    });
  });

  document.querySelectorAll('[data-channel-toggle]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const channel = btn.getAttribute('data-channel-toggle') || '';
      if (!channel) return;
      void toggleChannelConnection(channel).catch((error) => notify(`渠道操作失败：${(error as Error).message}`));
    });
  });
  $('#channel-connect-close')?.addEventListener('click', closeChannelModal);
  $('#channel-connect-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeChannelModal();
  });
  bindChannelConfigModal();
  bindModelConfigModal();

  $('#new-chat-btn')?.addEventListener('click', () => {
    syncCraftLabel();
    createSessionInWorkspace('default', openSession);
    setTab('chat');
  });

  $('#new-chat-sidebar-btn')?.addEventListener('click', () => {
    syncCraftLabel();
    createSessionInWorkspace('default', openSession);
    setTab('chat');
  });

  const search = $('#history-search-input') as HTMLInputElement | null;
  const clear = $('#history-search-clear');
  // 防抖：连续输入（中文 IME、快速键入）时只在停顿后触发一次重渲，避免大列表
  // 上每次按键都走整树 reconcile。clear 按钮立即触发（dispatch input 事件，
  // 但内部对空值立即刷新，不等待 debounce）。
  let searchDebounceTimer: number | null = null;
  let lastCommittedFilter = '';
  const SEARCH_DEBOUNCE_MS = 150;
  function commitSearchRender(): void {
    if (searchDebounceTimer !== null) {
      window.clearTimeout(searchDebounceTimer);
      searchDebounceTimer = null;
    }
    lastCommittedFilter = state.historyFilter;
    renderWorkspaceHistory(openSession);
  }
  search?.addEventListener('input', () => {
    if (clear) clear.style.display = search.value.trim() ? '' : 'none';
    state.historyFilter = search.value;
    const value = search.value;
    // 空值（清空、clear 按钮 dispatch）立即刷新，无需防抖
    if (!value.trim()) {
      commitSearchRender();
      return;
    }
    if (value === lastCommittedFilter) return;
    if (searchDebounceTimer !== null) window.clearTimeout(searchDebounceTimer);
    searchDebounceTimer = window.setTimeout(commitSearchRender, SEARCH_DEBOUNCE_MS);
  });
  clear?.addEventListener('click', () => {
    if (!search) return;
    search.value = '';
    state.historyFilter = '';
    search.dispatchEvent(new Event('input'));
  });

  const input = $('#chat-input') as HTMLTextAreaElement | null;
  const imeState = createComposerImeState();
  const unbindComposerIme = input ? bindComposerIme(input, imeState) : null;
  const MAX_INPUT_HEIGHT = 180;

  const adjustInputHeight = (): void => {
    if (!input) return;
    autoresizeTextarea(input, MAX_INPUT_HEIGHT);
  };

  const resetInputHeight = (): void => {
    if (!input) return;
    resetTextareaHeight(input);
  };

  const submit = (): void => {
    if (!input) return;
    const text = input.value;
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    resetInputHeight();
    const sessionId = state.activeSessionId;
    // 撤回修改后重新发送：此刻才真正删掉 [editFromIdx..] 的旧内容（含被打断的半截回复）
    if (sessionId && state.editFromIdx[sessionId] != null) {
      const idx = state.editFromIdx[sessionId];
      const removed = truncateMessagesFrom(sessionId, idx);
      setEditFrom(sessionId, null);
      for (const m of removed) {
        state.userFoldedTurns.delete(m.id);
        state.userUnfoldedTurns.delete(m.id);
      }
    }
    sendMessage(text);
  };

  $('#chat-send-btn')?.addEventListener('click', submit);
  $('#chat-stop-btn')?.addEventListener('click', stopGeneration);

  // 取消编辑：还原被隐藏的内容（editFromIdx 清掉即可，因为从没真正删除过）
  $('#composer-edit-cancel')?.addEventListener('click', cancelEdit);

  input?.addEventListener('keydown', (event) => {
    if (!shouldComposerSend(event as KeyboardEvent, imeState)) return;
    if (isMentionOpen()) return;
    event.preventDefault();
    // 统一入口：busy 时也走 submit（sendMessage 内 isBusy→入队 + 弹待发卡片）。
    // 不再按回车隐式 steer——引导改为待发卡片上的显式按钮，避免「回车=引导 / 按钮=排队」的歧义。
    submit();
  });

  input?.addEventListener('input', () => {
    adjustInputHeight();
    // busy 时输入有内容 → 发送键，清空 → 停止键（见 updateComposerControls 的 --composing）。
    updateComposerControls();
  });

  $('#chat-messages')?.addEventListener('click', (event) => {
    const eventTarget = event.target instanceof Element ? event.target : null;
    if (!eventTarget) return;
    // Only renderer-constructed image controls are actionable. Message text,
    // markdown and tool output cannot opt into privileged IPC by merely
    // resembling one of the data attributes.
    const imageCopyBtn = eventTarget.closest<HTMLButtonElement>(
      'button.chat-image-action[data-image-copy-path]',
    );
    if (imageCopyBtn) {
      imageCopyBtn.disabled = true;
      void copyImageToClipboard(imageCopyBtn.getAttribute('data-image-copy-path') ?? '')
        .then((copied) => {
          if (!copied || !imageCopyBtn.isConnected) return;
          imageCopyBtn.classList.add('is-copied');
          imageCopyBtn.setAttribute('aria-label', '已复制');
          imageCopyBtn.setAttribute('data-tooltip', '已复制');
          window.setTimeout(() => {
            if (!imageCopyBtn.isConnected) return;
            imageCopyBtn.classList.remove('is-copied');
            imageCopyBtn.setAttribute('aria-label', '复制图片');
            imageCopyBtn.setAttribute('data-tooltip', '复制图片');
          }, 1_600);
        })
        .finally(() => { imageCopyBtn.disabled = false; });
      return;
    }
    const imageRevealBtn = eventTarget.closest<HTMLButtonElement>(
      'button.chat-image-action[data-image-reveal-path]',
    );
    if (imageRevealBtn) {
      void revealImageInFolder(imageRevealBtn.getAttribute('data-image-reveal-path') ?? '');
      return;
    }
    const imageViewBtn = eventTarget.closest<HTMLButtonElement>(
      'button.tool-card__image-view[data-image-view-src], '
      + 'button.msg__attachment-image-view[data-image-view-src]',
    );
    if (imageViewBtn) {
      openImageViewer(
        imageViewBtn.getAttribute('data-image-view-src') ?? '',
        imageViewBtn.getAttribute('data-image-caption') ?? '图片',
        imageViewBtn.getAttribute('data-image-local-path') ?? '',
      );
      return;
    }
    const copyBtn = eventTarget.closest('.chat-copy-btn') as HTMLElement | null;
    if (copyBtn) {
      void navigator.clipboard.writeText(copyBtn.getAttribute('data-copy') ?? '').then(() => notify('已复制'));
      return;
    }
    const editBtn = eventTarget.closest('.chat-edit-btn') as HTMLElement | null;
    if (editBtn) {
      withdrawMessage(editBtn.getAttribute('data-edit') ?? '');
      return;
    }
    // Plan 模式：对话流只保留「在看板中审阅」入口；完整审批在右侧计划看板。
    const planBtn = eventTarget.closest('[data-plan-action]') as HTMLElement | null;
    if (planBtn) {
      const act = planBtn.getAttribute('data-plan-action');
      const planSid = planBtn.getAttribute('data-plan-session') ?? '';
      if (act === 'open_board' && planSid) {
        if (planSid === state.activeSessionId) openInspectorToTab('plan');
        return;
      }
    }
  });

  // 计划看板四动作：编辑后批准 / 撤销 / 其他（落盘手改 + 反馈消息）。
  setPlanBoardActions({
    onApprove: async (plan) => {
      const planSid = state.activeSessionId;
      if (!planSid) return;
      const requestId = newTurnRequestId();
      const sent = await (state.socket?.planApprove(
        planSid,
        state.mode || 'agent',
        state.currentWorkspaceId,
        requestId,
        plan,
      ) ?? false);
      if (!sent) {
        notify('服务未连接，无法提交计划审批');
        return;
      }
      const cur = bookFor(planSid).pendingPlan;
      // 批准后立即落地为 approved（不可再改）；执行中看板只读，不再伪装成 pending。
      patchBook(planSid, {
        planActive: false,
        pendingPlan: cur ? { ...cur, plan, status: 'approved' } : { plan, planFile: '', status: 'approved' },
      });
      patchPlanReviewMessages(planSid, 'approved', plan);
      // 切回普通模式，避免后续消息再带 plan_active 把已落地计划重新拉回 active。
      // 不走 applyComposerMode：它会 planExit 把 phase 改成 cancelled。
      if (state.composerMode === 'plan') {
        state.composerMode = 'craft';
        syncCraftLabel();
      }
      setBusyWithUi(planSid, true);
      setStatusWithUi(planSid, 'running');
      resumeSessionGeneration(planSid, requestId);
      renderChat();
      refreshInspector();
    },
    onRejectAndExit: async () => {
      const planSid = state.activeSessionId;
      if (!planSid) return;
      const sent = await (state.socket?.planRejectAndExit(planSid) ?? false);
      if (!sent) {
        notify('服务未连接，无法撤销计划');
        return;
      }
      const cur = bookFor(planSid).pendingPlan;
      patchBook(planSid, {
        planActive: false,
        pendingPlan: cur ? { ...cur, status: 'rejected' } : null,
      });
      patchPlanReviewMessages(planSid, 'rejected');
      setBusyWithUi(planSid, false);
      setStatusWithUi(planSid, 'idle');
      renderChat();
      refreshInspector();
    },
    onFeedback: async (plan, feedback) => {
      const planSid = state.activeSessionId;
      if (!planSid) return;
      // C1：先落盘手改，再 revise，最后把反馈当用户消息发出。任一步失败须可见停在哪一步。
      const updated = await (state.socket?.planUpdate(planSid, plan) ?? false);
      if (!updated) {
        notify('无法更新计划（服务未连接或写入失败）。计划文件可能未改动，请重试。');
        return;
      }
      const rejected = await (state.socket?.planReject(planSid) ?? false);
      if (!rejected) {
        notify(
          '计划正文已写入，但未能进入修订状态。请刷新后重试「其他」反馈，或手动退出再进入 Plan。',
        );
        // 本地仍切到 editing，避免看板卡在待批；与磁盘已更新对齐
        const cur = bookFor(planSid).pendingPlan;
        patchBook(planSid, {
          planActive: true,
          pendingPlan: cur
            ? { ...cur, plan, status: 'editing' }
            : { plan, planFile: '', status: 'editing' },
        });
        patchPlanReviewMessages(planSid, 'editing', plan);
        renderChat();
        refreshInspector();
        return;
      }
      const cur = bookFor(planSid).pendingPlan;
      patchBook(planSid, {
        planActive: true,
        pendingPlan: cur
          ? { ...cur, plan, status: 'editing' }
          : { plan, planFile: '', status: 'editing' },
      });
      patchPlanReviewMessages(planSid, 'editing', plan);
      setBusyWithUi(planSid, false);
      setStatusWithUi(planSid, 'idle');
      renderChat();
      refreshInspector();
      // sendMessage 同步入队；连接失败时内部会 notify，此处再提示修订已落地
      sendMessage(`请根据以下反馈修改计划：\n${feedback}`);
    },
  });

  $$('.feature-card[data-prompt]').forEach((card) => {
    card.addEventListener('click', () => {
      const prompt = card.getAttribute('data-prompt') || '';
      state.editingResend = false;
      sendMessage(prompt);
      if (input) {
        input.value = '';
        resetInputHeight();
      }
    });
  });

  bindScenarioHub((item, parent) => {
    if (input) {
      input.value = item.query;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    }
    armSubScenario(parent.title, item.id);
  });

  $('#chat-scenario-chip')?.addEventListener('click', (e) => {
    if ((e.target as HTMLElement).closest('.scenario-chip__clear')) clearScenarioChip();
  });

  bindAttachments();
  bindComposerMention();
  bindModelPicker();
  bindComposerToolbar();
  bindComposerContextRing();
  // D10: capture disposers so we can tear these listeners + the 8s system
  // refresh interval down on page hide (they previously leaked for the
  // renderer's whole lifetime).
  const disposeSystem = bindSystemTab();
  const disposeCronTab = bindCronTab();
  bindSkillsTab(() => setTab('skills'));
  bindWikiTab(() => setTab('wiki'));
  bindAuditTab(() => setTab('audit'));
  bindWorkspaceUi(refreshSessions, openSession);
  setWorkspacesUiCallbacks({ setTab, renderChat });
  bindSessionManageUi();
  bindFeedbackUi();
  bindSettingsUi();
  registerConfigPaneRenderers({ renderConfigModels, renderPlatforms });
  bindVersionUpdateUi();
  bindHistoryPanelToggle();
  bindInspectorUi();
  setOpenSessionCallback(openSession);

  // D10: teardown — clear the system overview interval and unbind the cron-tab
  // click handler when the page is unloaded, so the 8s timer stops firing.
  window.addEventListener('pagehide', () => {
    disposeSystem();
    disposeCronTab();
    disposeSystemTab();
    unbindComposerIme?.();
  }, { once: true });
}

function openChannelModal(channel: string): void {
  void openChannelConfigModal(channel).catch((error) => notify(`无法加载平台信息：${(error as Error).message}`));
}

function closeChannelModal(): void {
  const overlay = $('#channel-connect-overlay') as HTMLElement | null;
  if (overlay) overlay.hidden = true;
}

function mountHistoryWorkspaceDock(): void {
  const dock = $('#history-workspace-dock');
  const shell = $('#history-workspace-shell');
  if (dock && shell && shell.parentElement !== dock) dock.appendChild(shell);
}

async function init(): Promise<void> {
  installStreamDebugGlobal();
  // 安全 init 包装器：单步抛错不会阻断后续步骤；抛错时记日志 + 通知用户，
  // 避免「init 链路中途抛错 → 首屏空白 → 用户看不到任何东西」的白屏。
  // 每步独立 try-catch：initAuditPage hang 不会让 setTab('chat') 跑不到。
  const safe = (name: string, fn: () => void | Promise<void>): Promise<void> =>
    Promise.resolve()
      .then(fn)
      .catch((err) => {
        console.error(`[init] ${name} failed:`, err);
        notify(`初始化 ${name} 失败：${(err as Error).message}`);
      });

  // 同步初始化骨架（必须先完成才能显示首屏）
  // 后端状态守卫：优先初始化，确保遮罩在首屏就能展示
  await safe('initBackendStatusGuard', initBackendStatusGuard);
  // 把 openSession / setTab 注入 chat-controller + session-controller，破除循环依赖
  // （语义等价于抽离前这些顶层函数直接互相调用）。
  await safe('setChatCallbacks', () => setChatCallbacks({ openSession, setTab }));
  await safe('setCronCallbacks', () => setCronCallbacks({ openSession, setTab }));
  // Wiki ingest 进度帧：chat-controller 收到 WS 帧后转发给 wiki-page（回调注册，互不 import）。
  // Wiki Agent 对话：初始化模式状态/发送参数 resolver，并把 wiki-page 的
  // 「Wiki 问答」/「让 AI 处理」挂点接到 wiki-agent 的进入流程（组合根接线，互不 import）。
  await safe('initWikiAgent', () => {
    initWikiAgent();
    setWikiAgentEntryHandler((req) => {
      void openWikiAgent(req);
    });
    setWikiAgentKbDeletedHandler(forgetWikiAgentKb);
  });
  await safe('setSessionControllerSetTab', () => setSessionControllerSetTab(setTab));
  // 组合根先注入“创建新会话 + 绑定外援”回调，Composer 与智能体管理页复用同一入口。
  await safe('initAgentsPage', () => initAgentsPage({
    ensureChatSession: () => {
      syncCraftLabel();
      const sessionId = createSessionInWorkspace(state.currentWorkspaceId || 'default', openSession);
      if (sessionId) setTab('chat');
      return sessionId;
    },
    onSessionAgentAssigned: assignSessionAgentDisplay,
  }));
  await safe('bindGlobalEvents', bindGlobalEvents);
  await safe('bindAgentsTab', () => bindAgentsTab(() => setTab('agents')));
  await safe('bindSitesTab', () => bindSitesTab({
    openInspirationAgent: async (item) => {
      if (!item.sessionId) throw new Error('这个灵感没有绑定创建对话');
      await openSession(item.sessionId);
      setTab('chat');
    },
    createInspirationSession: async () => {
      const workspaceId = state.currentWorkspaceId || 'default';
      const sessionId = createSessionInWorkspace(workspaceId, openSession);
      assignSessionAgentDisplay(sessionId, { name: '灵感', provider: 'sites', display_badge: '◇' });
      await backendApi.ensureSession(sessionId, { workspace_id: workspaceId, title: '新灵感' });
      await backendApi.setSessionAgentConfig(sessionId, { executor: 'builtin', inspiration_creation: true });
      await refreshSessions();
      setTab('chat');
      syncSiteComposerMarker();
      ($('#chat-input') as HTMLTextAreaElement | null)?.focus();
    },
  }));
  await safe('bindBlueprintSurface', bindBlueprintSurface);
  await safe('applyHistoryCollapsed', applyHistoryCollapsed);
  await safe('mountHistoryWorkspaceDock', mountHistoryWorkspaceDock);
  await safe('loadRunningIntroCopy', loadRunningIntroCopy);
  await safe('syncCraftLabel', syncCraftLabel);

  // 首屏渲染（独立执行以下步骤，避免隐式耦合）
  setTab('chat');
  setSystemPanel('overview');
  renderWorkspaceHistory(openSession);
  renderChat();
  updateGatewayDot();
  // 冷启动：当前会话若已有内存消息，补一次文件改动对账（清幽灵临时文件卡）
  const bootSid = state.activeSessionId;
  if (bootSid && getMessages(bootSid).length > 0) {
    void hydrateMissingTurnFileCounts(bootSid).then((changed) => {
      if (changed && state.activeSessionId === bootSid) renderChat();
    });
  }

  // 认证模式由 Gateway 配置决定。local/dev 直接通过；remote 未登录时保留
  // UI 骨架但不加载任何 owner 数据，登录成功后 reload 再进入该分支。
  let authenticated = false;
  await safe('initAuthFlow', async () => {
    authenticated = await initAuthFlow();
  });

  // 后台初始化（失败不影响首屏；用户看到 welcome/chat 后再加载）
  // 关闭 init 旁路：此后 setTab 受后端状态守卫约束（遮罩展示时阻断用户切换）
  sealBackendInitBypass();
  if (authenticated) {
    void safe('bootstrapLocalBackend', async () => {
      await ensureDesktopGateway();
      bootstrapBackend();
      await hydrateBackendState();
      await initSkillsPage();
      const lastId = localStorage.getItem('crew.lastActiveSession');
      if (lastId && state.sessions.some((session) => session.id === lastId) && state.activeTab === 'chat') {
        await openSession(lastId);
      }
    });
  }
  if (authenticated) {
    void safe('initAuditPage', initAuditPage);
    void safe('initUsagePage', initUsagePage);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => void init());
} else {
  void init();
}

export {};
