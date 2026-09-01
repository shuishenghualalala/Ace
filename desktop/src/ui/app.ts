/**
 * Crew desktop v2 — 渲染层入口（网关对话 + crew 能力接入）。
 *
 * X2 重构后本文件是薄壳：只保留 init / bindGlobalEvents / setTab / setSystemPanel /
 * 频道弹窗 / 历史工作区 dock 挂载等顶层编排。会话与聊天流式逻辑已迁出至：
 *  - features/chat-controller.ts   流式 dispatch / 渲染 / 发送 / 撤回 / 召唤
 *  - features/session-controller.ts 打开会话 / 历史回填 / 网关引导 / 状态水合
 *  - features/history-mapping.ts   后端历史 → ChatMessage 纯映射
 */

import { bindChannelConfigModal, bindModelConfigModal, openChannelConfigModal, renderConfigModels, renderPlatforms, toggleChannelConnection } from './features/config-panes';
import { backendApi } from './backend-client';
import { bindSitesTab, renderSitesPage, syncSiteAnnotationEntry, syncSiteComposerMarker } from './features/sites-page';
import { bindBlueprintSurface } from './features/blueprint-surface';
import { bindModelPicker } from './features/model-picker';
import { bindComposerToolbar, syncCraftLabel } from './features/composer-toolbar';
import { bindComposerContextRing } from './features/composer-context-ring';
import { createComposerContextView } from './features/composer-context-view';
import { activateAgentsPage, disposeAgentsPage, initAgentsPage } from './features/agents-page';
import {
  activateSkillsPage,
  bindSkillsPageLifecycle,
  initSkillsPage,
} from './features/skills-page';
import {
  WORK_FEATURE_STATES,
  enterWorkMode,
  leaveWorkMode,
  activateWorkLocation,
  refreshWorkHistory,
  setWorkHistoryCommands,
} from './features/work/navigation';
import {
  openCreateWorkItemDialog,
  renderWorkItemContext,
  resolveWorkItem,
} from './features/work/items';
import { openWorkItemDrawer } from './features/work/item-space';
import type { WorkItem } from './backend-client';
import { setNotificationClickHandler } from './features/work/notifications';
import { initSystemTrayStatus } from './features/system-tray';
import { refreshKanbanBoard, renderKanbanBoard } from './features/kanban-board';
import { bindSystemTab, disposeSystemTab, renderSystemLogs, renderSystemOverview } from './features/system-page';
import { initUsagePage } from './features/usage-panel';
import { activateCronPage, renderCronTaskBoard, setCronCallbacks } from './features/cron-page';
import {
  bindWikiTab,
  refreshWikiData,
  setWikiAgentEntryHandler,
  setWikiAgentKbDeletedHandler,
} from './features/wiki-page';
import { enterWikiAgentMode, forgetWikiAgentKb, initWikiAgent } from './features/wiki-agent';
import {
  assignSessionAgentDisplay,
  bindWorkspaceUi,
  createSessionInWorkspace,
  createWorkspaceFromFolderPicker,
  getHistoryLoadError,
  loadSessionsList,
  loadWorkspaces,
  openWorkspaceModal,
  renderStudioHistory,
  renderWorkspaceHistory,
  setWorkspacesUiCallbacks,
} from './features/workspaces';
import {
  bindSessionManageUi,
  openSessionManage,
  setOpenSessionCallback,
} from './features/session-manage';
import { mountSessionHistoryView } from './features/session-history-view';
import { bindSettingsUi, registerConfigPaneRenderers } from './features/settings';
import { mountSettingsDataPanes } from './features/settings-data';
import { requireRendererLogin } from './features/auth-gate';
import { initAuthFlow } from './features/login';
import { initBackendStatusGuard, isBackendConnected, isBackendInitBypassActive, sealBackendInitBypass } from './features/backend-status-guard';
import { bindHistoryPanelToggle, applyHistoryCollapsed } from './features/history-collapse';
import { bindInspectorUi, openInspectorToTab, refreshInspector, setPlanBoardActions } from './features/inspector';
import { bindAttachments } from './features/attachments';
import { bindTooltips } from './components/tooltip';
import { queryPrimaryComposer } from './features/composer-scope';
import {
  copyImageToClipboard,
  openImageViewer,
  revealImageInFolder,
} from './features/image-viewer';
import { bindComposerMention, isMentionOpen } from './features/composer-mention';
import { createMainPanelAttachments } from './features/attachments';
import {
  createMainComposerActions,
  mountConversationPanel,
} from './features/conversation-panel';
import { resolveChatRenderTargetId, isStudioView } from './features/studio-chrome-state';
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
  setActiveSessionId,
  state,
  type SystemPanelKey,
  type TabKey,
} from './state';
import {
  bookFor,
  getMessages,
  patchPlanReviewMessages,
  refreshSessions,
  renderChat,
  sendMessage,
  setBusyWithUi,
  setChatCallbacks,
  setStatusWithUi,
  updateGatewayDot,
  withdrawMessage,
} from './features/chat-controller';
import {
  bootstrapBackend,
  hydrateBackendState,
  openSession,
  setSessionControllerSetTab,
} from './features/session-controller';
import { hydrateMissingTurnFileCounts } from './features/turn-file-counts';
import { bindSecurityApprovalUi } from './features/security-approval';
import { activateSecurityPage, initSecurityPage } from './features/security-center';
import { stopStreamWatchdog } from './features/stream-watchdog';
import type { RendererAdapter } from './adapters/renderer-adapter';
import { createApplicationShell, type ApplicationShell } from './layouts/application-shell';
import {
  productModeStore,
  updateProductModeView,
  type ProductMode,
} from './stores/product-mode-store';
import {
  bindExternalAgentsFeatureUi,
  externalAgentsEnabled,
} from './features/external-agents-feature';
import {
  bindSecurityModuleFeatureUi,
  securityModuleEnabled,
} from './features/security-mode';
import {
  isWorkLocation,
  type ShellLocation,
  type WorkLocation,
} from './features/sidebar-nav';

function setTab(tab: TabKey): boolean {
  // 后端服务未就绪时阻断页面切换，遮罩已由 backend-status-guard 展示。
  // init 阶段旁路：允许构建 UI 骨架（遮罩覆盖下用户看不到）。
  if (!isBackendInitBypassActive() && !isBackendConnected()) {
    return false;
  }
  state.activeTab = tab;
  const productState = productModeStore.get();
  if (
    productState.productMode === 'assistant' &&
    productState.views.assistant.lastPosition !== tab
  ) {
    updateProductModeView({ lastPosition: tab });
  }
  $$('.tab-pane').forEach((pane) => pane.classList.remove('active'));
  document.getElementById(`${tab}-tab`)?.classList.add('active');
  document.body.classList.toggle('history-workspace-active', tab === 'chat');
  document.body.classList.toggle('welcome-active', tab === 'chat' && !state.activeSessionId);
  if (tab === 'chat') {
    renderChat();
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
  return true;
}

function activateTab(tab: TabKey): boolean {
  if (!setTab(tab)) return false;
  if (tab === 'agents') activateAgentsPage();
  else if (tab === 'skills') activateSkillsPage();
  else if (tab === 'security') activateSecurityPage();
  else if (tab === 'wiki') void refreshWikiData();
  else if (tab === 'sites') {
    syncSiteAnnotationEntry();
    void renderSitesPage();
  }
  else if (tab === 'cron') activateCronPage();
  return true;
}

function setSystemPanel(panel: SystemPanelKey): void {
  state.activeSystemPanel = panel;
  // 修复 I-7：overview / logs / usage 三个面板都要渲染，否则点击 logs / usage 会显示空。
  if (panel === 'overview') renderSystemOverview();
  else if (panel === 'logs') void renderSystemLogs();
  else if (panel === 'usage') void import('./features/usage-panel').then((m) => m.renderUsagePage?.());
}

/**
 * 「新建会话」前把 composer 复位为默认 agent 模式（per-session 绑定由 createSessionInWorkspace 置空）。
 */
function resetExpertForNewSession(): void {
  state.mode = 'agent';
  state.taskBoardOpen = false;
  syncCraftLabel();
}

function startNewChat(): void {
  if (!requireRendererLogin()) return;
  resetExpertForNewSession();
  createSessionInWorkspace('default', openSession);
  setTab('chat');
}

function bindGlobalEvents(): () => void {
  const controller = new AbortController();
  const on = (
    target: EventTarget | null | undefined,
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void => target?.addEventListener(type, listener, { signal: controller.signal });

  const composerHost = $('#chat-composer-root');
  // 主对话面板：接管既有 #chat-messages / #chat-composer-root（不自建 DOM），
  // 渲染仍由 chat-controller.renderChat 驱动；面板负责 Composer + 附件流接线。
  const mainPanelAttachments = createMainPanelAttachments();
  const conversationPanel = composerHost
    ? mountConversationPanel($('#chat-panel') as HTMLElement, {
      getSessionId: () => state.activeSessionId,
      attachments: mainPanelAttachments,
      actions: createMainComposerActions(isMentionOpen),
      resolveMessages: () => {
        const containerId = resolveChatRenderTargetId(isStudioView());
        const container = document.getElementById(containerId);
        return container ? { container, containerId } : null;
      },
      composerHost,
      primary: true,
      contextStaging: $('#composer-context-staging') as HTMLElement | null,
    })
    : null;
  const composerContextView = composerHost
    ? createComposerContextView(composerHost)
    : null;

  // Navigation and window commands are owned by application-shell.ts.

  document.querySelectorAll('.conn-row[data-channel]').forEach((card) => {
    on(card, 'click', (e) => {
      if ((e.target as HTMLElement).closest('[data-channel-toggle]')) return;
      if (!requireRendererLogin('请先登录后再配置渠道')) return;
      const channel = card.getAttribute('data-channel') || 'feishu';
      openChannelModal(channel);
    });
  });

  document.querySelectorAll('[data-channel-toggle]').forEach((btn) => {
    on(btn, 'click', (e) => {
      e.stopPropagation();
      if (!requireRendererLogin('请先登录后再操作渠道')) return;
      const channel = btn.getAttribute('data-channel-toggle') || '';
      if (!channel) return;
      void toggleChannelConnection(channel).catch((error) => notify(`渠道操作失败：${(error as Error).message}`));
    });
  });
  on($('#channel-connect-close'), 'click', closeChannelModal);
  on($('#channel-connect-overlay'), 'click', (e) => {
    if (e.target === e.currentTarget) closeChannelModal();
  });
  bindChannelConfigModal();
  bindModelConfigModal();

  on($('#new-chat-btn'), 'click', startNewChat);

  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');

  on($('#chat-messages'), 'click', (event) => {
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

  bindScenarioHub((item, parent) => {
    if (input) {
      input.value = item.query;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    }
    armSubScenario(parent.title, item.id);
  });

  on($('#chat-scenario-chip'), 'click', (e) => {
    if ((e.target as HTMLElement).closest('.scenario-chip__clear')) clearScenarioChip();
  });

  const disposeAttachments = bindAttachments();
  const disposeComposerMention = bindComposerMention();
  bindTooltips();
  bindModelPicker();
  const disposeComposerToolbar = bindComposerToolbar();
  bindComposerContextRing();
  mountSettingsDataPanes();
  // D10: capture disposers so we can tear these listeners + the 8s system
  // refresh interval down on page hide (they previously leaked for the
  // renderer's whole lifetime).
  const disposeSystem = bindSystemTab();
  const disposeSkillsLifecycle = bindSkillsPageLifecycle(() => {
    activateTab('chat');
  });
  bindWikiTab(() => activateTab('wiki'));
  bindSitesTab({
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
      await backendApi.setSessionAgentConfig(sessionId, {
        executor: 'builtin',
        capability_profiles: ['sites.authoring'],
      });
      await refreshSessions();
    },
  });
  bindBlueprintSurface();
  syncSiteComposerMarker();
  const disposeWorkspaceUi = bindWorkspaceUi(refreshSessions, openSession);
  setWorkspacesUiCallbacks({ setTab, renderChat });
  const historyHost = $('#session-history-root');
  const disposeSessionHistory = historyHost
    ? mountSessionHistoryView(historyHost, {
      openSession,
      createSession: (workspaceId) => {
        resetExpertForNewSession();
        createSessionInWorkspace(workspaceId, openSession);
        setTab('chat');
      },
      createWorkspace: () => {
        void createWorkspaceFromFolderPicker(openSession);
      },
      manageHistory: openSessionManage,
      openWorkspace: openWorkspaceModal,
      refreshSessions,
      retrySessions: async () => {
        await loadSessionsList();
      },
      retryWorkspaces: loadWorkspaces,
      getLoadErrors: getHistoryLoadError,
      renderStudioHistory: () => renderStudioHistory(openSession),
    })
    : () => {};
  bindSessionManageUi();
  bindSettingsUi();
  registerConfigPaneRenderers({ renderConfigModels, renderPlatforms });
  bindVersionUpdateUi();
  const disposeHistoryToggle = bindHistoryPanelToggle();
  bindInspectorUi();
  const disposeSecurityApproval = bindSecurityApprovalUi();
  setOpenSessionCallback(openSession);

  let disposed = false;
  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    controller.abort();
    disposeSessionHistory();
    disposeWorkspaceUi();
    disposeSystem();
    disposeSkillsLifecycle();
    disposeHistoryToggle();
    disposeSystemTab();
    disposeSecurityApproval();
    disposeAttachments();
    disposeComposerMention();
    disposeComposerToolbar();
    disposeAgentsPage();
    composerContextView?.dispose();
    conversationPanel?.dispose();
  };
  on(window, 'pagehide', dispose);
  return dispose;
}

function openChannelModal(channel: string): void {
  void openChannelConfigModal(channel).catch((error) => notify(`无法加载平台信息：${(error as Error).message}`));
}

function closeChannelModal(): void {
  const overlay = $('#channel-connect-overlay') as HTMLElement | null;
  if (overlay) overlay.hidden = true;
}

async function init(
  registerDispose: (dispose: () => void) => void,
): Promise<void> {
  installStreamDebugGlobal();
  // 安全 init 包装器：单步抛错不会阻断后续步骤；抛错时记日志 + 通知用户，
  // 避免「init 链路中途抛错 → 首屏空白 → 用户看不到任何东西」的白屏。
  // 每步独立 try-catch，单页初始化失败不会让 setTab('chat') 跑不到。
  const safe = (name: string, fn: () => void | Promise<void>): Promise<void> =>
    Promise.resolve()
      .then(fn)
      .catch((err) => {
        console.error(`[init] ${name} failed:`, err);
        notify(`初始化 ${name} 失败：${(err as Error).message}`);
      });

  // Phase 1: 同步骨架（必须先做完才能显示首屏）
  // 后端状态守卫：优先初始化，确保遮罩在首屏就能展示
  await safe('initBackendStatusGuard', initBackendStatusGuard);
  await safe('initSystemTrayStatus', () => {
    registerDispose(initSystemTrayStatus());
  });
  // 认证流程：email/remote 模式下在首屏前判定登录态，未登录则展示登录墙。
  // local/dev 模式下 isLoggedIn 恒为 true，不显示登录墙，直接放行。
  await safe('initAuthFlow', async () => { await initAuthFlow(); });
  // 把 openSession / setTab 注入 chat-controller + session-controller，破除循环依赖
  // （语义等价于抽离前这些顶层函数直接互相调用）。
  await safe('setChatCallbacks', () => setChatCallbacks({ openSession, setTab }));
  await safe('setCronCallbacks', () => setCronCallbacks({ openSession, setTab }));
  // Wiki ingest 进度帧：chat-controller 收到 WS 帧后转发给 wiki-page（回调注册，互不 import）。
  // Wiki Agent 对话（Phase 4）：初始化模式状态/发送参数 resolver，并把 wiki-page 的
  // 「Wiki 问答」/「让 AI 处理」挂点接到 wiki-agent 的进入流程（组合根接线，互不 import）。
  await safe('initWikiAgent', () => {
    initWikiAgent();
    setWikiAgentKbDeletedHandler(forgetWikiAgentKb);
    setWikiAgentEntryHandler((req) => {
      void enterWikiAgentMode(req);
    });
  });
  await safe('setSessionControllerSetTab', () => setSessionControllerSetTab(setTab));
  await safe('bindGlobalEvents', () => {
    registerDispose(bindGlobalEvents());
  });
  await safe('applyHistoryCollapsed', applyHistoryCollapsed);
  await safe('loadRunningIntroCopy', loadRunningIntroCopy);
  await safe('syncCraftLabel', syncCraftLabel);

  // Phase 2: 首屏（必须执行——独立两步，不要在同一行，避免隐式耦合）
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

  // Phase 3: 后台慢慢来（失败不影响首屏；用户已能看到 welcome/chat 再补这些）
  // 关闭 init 旁路：此后 setTab 受后端状态守卫约束（遮罩展示时阻断用户切换）
  sealBackendInitBypass();
  void safe('initSecurityPage', initSecurityPage);
  void safe('initUsagePage', initUsagePage);
  void safe('initAgentsPage', () => initAgentsPage({
    ensureChatSession: () => {
      resetExpertForNewSession();
      const sessionId = createSessionInWorkspace('default', openSession);
      if (sessionId) setTab('chat');
      return sessionId;
    },
    onSessionAgentAssigned: assignSessionAgentDisplay,
  }));
  void safe('initSkillsPage', initSkillsPage);
  // 登录墙已由 initAuthFlow 在 Phase 1 处理：local/dev 模式直接放行，
  // email/remote 模式未登录时登录墙覆盖全部 UI，登录后 reload 重新初始化。
  await safe('bootstrapBackend', bootstrapBackend);
  void safe('hydrateBackendState', hydrateBackendState);
}

/**
 * Mount the current Renderer composition root.
 *
 * The legacy DOM remains in place during vertical-slice migration. New slices
 * consume the adapter directly; disposal is centralized here from the start.
 */
export interface RendererRoot {
  element: HTMLDivElement;
  legacyOutlet: HTMLDivElement;
  overlayHost: HTMLDivElement;
}

/**
 * Establishes the one Renderer root while preserving the current legacy DOM.
 * Existing static roots are reused so remounting never duplicates the shell.
 */
export function ensureRendererRoot(host: HTMLElement): RendererRoot {
  const existing = host.querySelector<HTMLDivElement>(':scope > #renderer-root');
  if (existing) {
    const legacyOutlet = existing.querySelector<HTMLDivElement>(':scope > #renderer-legacy-outlet');
    const overlayHost = existing.querySelector<HTMLDivElement>(':scope > #renderer-overlay-host');
    if (!legacyOutlet || !overlayHost) throw new Error('Renderer root contract is incomplete');
    return { element: existing, legacyOutlet, overlayHost };
  }

  const element = document.createElement('div');
  const legacyOutlet = document.createElement('div');
  const overlayHost = document.createElement('div');
  element.id = 'renderer-root';
  element.dataset.rendererRoot = '';
  legacyOutlet.id = 'renderer-legacy-outlet';
  legacyOutlet.dataset.rendererScope = 'legacy';
  overlayHost.id = 'renderer-overlay-host';
  overlayHost.setAttribute('aria-label', '应用浮层');
  for (const child of [...host.childNodes]) {
    if (child instanceof Element && child.id === 'mw-icon-sprite') continue;
    legacyOutlet.append(child);
  }
  element.append(legacyOutlet, overlayHost);
  host.append(element);
  return { element, legacyOutlet, overlayHost };
}

interface MountedApplicationShell {
  shell: ApplicationShell;
  dispose(): void;
}


/**
 * Moves only staging nodes into the new shell. Unmigrated pages remain inside
 * the one legacy outlet until their vertical slice replaces them.
 */
function mountApplicationShell(
  rendererRoot: RendererRoot,
  adapter: RendererAdapter,
): MountedApplicationShell {
  const legacyOutlet = rendererRoot.legacyOutlet;
  const contextSource =
    legacyOutlet.querySelector<HTMLElement>('#legacy-context-content') ??
    document.createElement('div');
  if (!contextSource.id) {
    contextSource.id = 'legacy-context-content';
    const history = legacyOutlet.querySelector('#session-history-root');
    if (history) contextSource.append(history);
    legacyOutlet.prepend(contextSource);
  }

  let previousProductMode = productModeStore.get().productMode;
  let assistantSessionId = previousProductMode === 'assistant' ? state.activeSessionId : null;
  let requestedAssistantSessionId: string | null = null;
  function setWorkOverviewVisible(visible: boolean): void {
    workOverview.hidden = !visible;
    chatTab?.classList.toggle('work-overview-active', visible);
    if (visible) {
      chatTab?.classList.remove('work-session-active', 'work-item-active');
      if (welcomePanel) welcomePanel.hidden = true;
      const chatPanel = legacyOutlet.querySelector<HTMLElement>('#chat-panel');
      if (chatPanel) chatPanel.hidden = true;
    }
  }
  function setWorkItemContext(item: WorkItem | null): void {
    if (!item) {
      workItemContext.hidden = true;
      workItemContext.replaceChildren();
      workItemContext.removeAttribute('data-item-id');
      return;
    }
    renderWorkItemContext(workItemContext, item, {
      onBackToWorkbench: showWorkWorkbench,
      onOpenDetails: (trigger) => {
        openWorkItemDrawer(trigger, item, {
          onDeleted: () => setWorkItemContext(null),
        });
      },
      onUpdated: (updated) => setWorkItemContext(updated),
    });
  }
  function showWorkWorkbench(): void {
    setWorkItemContext(null);
    setWorkOverviewVisible(true);
    workContext.hidden = false;
    legacyOutlet.hidden = false;
    workPage.hidden = true;
    if (state.activeSessionId) setActiveSessionId(null);
    activateTab('chat');
    activateWorkLocation('workbench', workOverview);
  }
  function showWorkPage(location: WorkLocation, options: { itemId?: string } = {}): void {
    setWorkItemContext(null);
    setWorkOverviewVisible(false);
    workContext.hidden = location === 'items';
    legacyOutlet.hidden = true;
    workPage.hidden = false;
    activateWorkLocation(location, workPage, options);
  }
  function showSharedWorkPage(location: TabKey): boolean {
    if (!activateTab(location)) return false;
    setWorkItemContext(null);
    setWorkOverviewVisible(false);
    workContext.hidden = true;
    legacyOutlet.hidden = false;
    workPage.hidden = true;
    return true;
  }
  async function showWorkSession(
    sessionId: string,
    initialMessage?: string,
    item: WorkItem | null = null,
  ): Promise<void> {
    updateProductModeView({ lastPosition: 'workbench' });
    setWorkItemContext(item);
    setWorkOverviewVisible(false);
    chatTab?.classList.add('work-session-active');
    chatTab?.classList.toggle('work-item-active', Boolean(item));
    workPage.hidden = true;
    legacyOutlet.hidden = false;
    await openSession(sessionId);
    // 会话打开后刷新历史侧栏，让对应行高亮为选中态（state.activeSessionId 已更新）。
    refreshWorkHistory();
    if (welcomePanel) welcomePanel.hidden = true;
    const chatPanel = legacyOutlet.querySelector<HTMLElement>('#chat-panel');
    if (chatPanel) chatPanel.hidden = false;
    activateTab('chat');
    if (initialMessage) await sendMessage(initialMessage);
  }
  async function showWorkItem(itemId: string): Promise<void> {
    try {
      await resolveWorkItem(itemId);
    } catch (error) {
      notify(`打开事项失败：${error instanceof Error ? error.message : String(error)}`);
      return;
    }
    showWorkPage('workbench', { itemId });
  }
  const syncProductMode = (productMode: ProductMode): void => {
    const assistant = productMode === 'assistant';
    // body 挂 product-mode 标识，供 CSS 区分 work / assistant 差异（如 work 模式隐藏「对话/工作室」子导航）。
    document.body.classList.toggle('product-mode-work', !assistant);
    if (!assistant && previousProductMode === 'assistant') {
      assistantSessionId = state.activeSessionId;
    }
    assistantContext.hidden = !assistant;
    workContext.hidden = assistant;
    legacyOutlet.hidden = !assistant;
    workPage.hidden = assistant;
    if (!assistant) {
      void enterWorkMode(workContext);
      const lastPosition = productModeStore.get().views.work.lastPosition;
      if (lastPosition === 'knowledge' || lastPosition === 'items') {
        showWorkPage(lastPosition);
      }
      else if (lastPosition === 'workbench') showWorkWorkbench();
      else showSharedWorkPage(lastPosition as TabKey);
    } else {
      setWorkOverviewVisible(false);
      setWorkItemContext(null);
      chatTab?.classList.remove('work-session-active', 'work-item-active');
      leaveWorkMode();
      const targetSessionId = requestedAssistantSessionId ?? assistantSessionId;
      const assistantPosition = productModeStore.get().views.assistant.lastPosition as TabKey;
      if (state.activeSessionId !== targetSessionId) {
        setActiveSessionId(targetSessionId);
        if (targetSessionId && isBackendConnected()) {
          void openSession(targetSessionId, { activateChat: false }).catch((error) => {
            notify(`恢复通用助手会话失败：${error instanceof Error ? error.message : String(error)}`);
          }).finally(() => activateTab(assistantPosition));
        }
        else activateTab(assistantPosition);
      }
      else activateTab(assistantPosition);
    }
    previousProductMode = productMode;
  };
  const shell = createApplicationShell({
    commands: {
      minimize: () => adapter.bridge?.windowMinimize?.(),
      maximize: () => adapter.bridge?.windowMaximize?.(),
      close: () => adapter.bridge?.windowClose?.(),
      newChat: startNewChat,
    },
    features: {
      agents: externalAgentsEnabled() ? 'available' : 'hidden',
      security: securityModuleEnabled() ? 'available' : 'unavailable',
      work: WORK_FEATURE_STATES,
    },
    onNavigate: (location: ShellLocation, productMode: ProductMode) => {
      if (productMode === 'assistant') return activateTab(location as TabKey);
      if (!isWorkLocation(location)) return showSharedWorkPage(location);
      if (location === 'workbench') {
        showWorkWorkbench();
        return true;
      }
      if (workPage.dataset.workLocation === location && !workPage.hidden) return true;
      showWorkPage(location as WorkLocation);
      return true;
    },
    onProductModeChange: syncProductMode,
  });

  const assistantContext = document.createElement('div');
  const workContext = document.createElement('aside');
  workContext.className = 'mw-work-context';
  workContext.innerHTML = '<p class="mw-work-history__empty">暂无办公历史</p>';
  const workPage = document.createElement('section');
  workPage.className = 'mw-work-page';
  workPage.hidden = true;
  const welcomePanel = legacyOutlet.querySelector<HTMLElement>('#welcome-panel');
  const chatTab = legacyOutlet.querySelector<HTMLElement>('#chat-tab');
  const workOverview = document.createElement('div');
  const workItemContext = document.createElement('div');
  workItemContext.className = 'mw-work-item-context';
  workItemContext.hidden = true;
  workOverview.className = 'mw-work-overview';
  workOverview.dataset.workOverview = '';
  workOverview.hidden = true;
  const composerRoot = chatTab?.querySelector('#chat-composer-root') ?? null;
  if (composerRoot) {
    chatTab?.insertBefore(workItemContext, composerRoot);
    chatTab?.insertBefore(workOverview, composerRoot);
  } else {
    chatTab?.append(workItemContext, workOverview);
  }
  assistantContext.className = 'mw-assistant-context';
  assistantContext.append(...contextSource.childNodes);
  contextSource.remove();
  shell.slots.contextContent.append(assistantContext, workContext);
  shell.slots.page.append(legacyOutlet, workPage);
  legacyOutlet.querySelector('.chat-subnav-row')?.append(shell.slots.historyActions);
  setWorkHistoryCommands({
    newItem: (trigger) => {
      openCreateWorkItemDialog(trigger, (created) => {
        return showWorkItem(created.item_id);
      });
    },
    openItem: (itemId) => {
      void showWorkItem(itemId);
    },
    openWorkbench: showWorkWorkbench,
    manageItems: () => showWorkPage('items'),
    openSession: async (sessionId, mode, initialMessage, itemId) => {
      if (mode === 'assistant') {
        requestedAssistantSessionId = sessionId;
        shell.setProductMode('assistant');
        requestedAssistantSessionId = null;
        assistantSessionId = sessionId;
      } else {
        const item = itemId ? await resolveWorkItem(itemId) : null;
        await showWorkSession(sessionId, initialMessage, item);
        return;
      }
      await openSession(sessionId);
      activateTab('chat');
      if (initialMessage) await sendMessage(initialMessage);
    },
  });
  setNotificationClickHandler((itemId) => {
    shell.setProductMode('work');
    void showWorkItem(itemId);
  });
  rendererRoot.element.insertBefore(shell.element, rendererRoot.overlayHost);
  const disposeExternalAgentsFeature = bindExternalAgentsFeatureUi((enabled) => {
    shell.setFeatures({ agents: enabled ? 'available' : 'hidden' });
  });
  const disposeSecurityModuleFeature = bindSecurityModuleFeatureUi((enabled) => {
    shell.setFeatures({ security: enabled ? 'available' : 'unavailable' });
  });
  syncProductMode(productModeStore.get().productMode);

  return {
    shell,
    dispose() {
      leaveWorkMode();
      setWorkHistoryCommands({});
      const restoredContext = document.createElement('div');
      restoredContext.id = 'legacy-context-content';
      restoredContext.append(...assistantContext.childNodes);
      legacyOutlet.prepend(restoredContext);
      shell.slots.historyActions.remove();
      rendererRoot.element.insertBefore(legacyOutlet, rendererRoot.overlayHost);
      disposeExternalAgentsFeature();
      disposeSecurityModuleFeature();
      setNotificationClickHandler(null);
      shell.dispose();
      shell.element.remove();
    },
  };
}

export function mountRenderer(root: HTMLElement, adapter: RendererAdapter): () => void {
  let disposed = false;
  const disposeEvents: Array<() => void> = [];
  const rendererRoot = ensureRendererRoot(root);
  const mountedShell = mountApplicationShell(rendererRoot, adapter);
  root.dataset.rendererMounted = 'true';
  rendererRoot.element.dataset.rendererMounted = 'true';

  const registerDispose = (dispose: () => void): void => {
    if (disposed) dispose();
    else disposeEvents.push(dispose);
  };

  void init(registerDispose).catch((error) => {
    console.error('[renderer] mount failed:', error);
    notify(`初始化 Renderer 失败：${(error as Error).message}`);
  });

  return () => {
    if (disposed) return;
    disposed = true;
    for (const dispose of disposeEvents.splice(0)) dispose();
    const socket = state.socket;
    state.socket = null;
    socket?.dispose();
    stopStreamWatchdog();
    mountedShell.dispose();
    root.removeAttribute('data-renderer-mounted');
    rendererRoot.element.removeAttribute('data-renderer-mounted');
  };
}
