/**
 * 事项处理空间：事项头部、稳定动作命令（完成/重开/延期/取消/归档/停止跟踪）、
 * 永久删除二次确认、处理 Session 入口与详情面板。
 *
 * 动作经 workApi.actOnItem（后端验证版本+命名动作），删除经 deleteItem
 * （confirm=delete_work_item 二次确认）。不复制 fetch。
 */

import { workApi } from '../../backend-client';
import type { WorkItem } from '../../backend-client';
import { openDrawer } from '../../components/overlays';
import { mergeItem, removeItem } from '../../stores/work-store';
import { renderItemDetails } from './item-details';
import { epochMilliseconds, epochSeconds } from './time';

export type ItemAction = 'complete' | 'reopen' | 'postpone' | 'cancel' | 'archive' | 'stop_tracking';

export const ITEM_ACTION_LABELS: Record<ItemAction, string> = {
  complete: '完成',
  reopen: '重开',
  postpone: '延期',
  cancel: '取消',
  archive: '归档',
  stop_tracking: '停止跟踪',
};

const ITEM_STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  in_progress: '处理中',
  completed: '已完成',
  cancelled: '已取消',
  archived: '已归档',
  tracking_stopped: '已停止跟踪',
};

export interface ItemSpaceOptions {
  onBack?(): void;
  backLabel?: string;
  onDeleted?(): void;
  notice?: string;
  onOpenSession?(sessionId: string): void;
}

export interface WorkItemDrawerOptions {
  onDeleted?(): void;
}

/**
 * 判断哪些动作在当前状态下可见。
 * 已完成可重开；所有未处置事项均可归档或停止跟踪。
 * 已处置（取消/归档/停止跟踪）只可删除。
 */
export function availableActions(item: WorkItem): ItemAction[] {
  if (item.disposition && item.disposition !== 'active') return [];
  const status = item.business_status ?? 'pending';
  if (status === 'completed') return ['reopen', 'archive', 'stop_tracking'];
  return ['complete', 'postpone', 'cancel', 'archive', 'stop_tracking'];
}

/** 执行一个事项动作并合并结果到 store。 */
export async function applyItemAction(
  item: WorkItem,
  action: ItemAction,
  dueAt?: number,
): Promise<WorkItem> {
  const payload: { action: ItemAction; expected_version: number; due_at?: number } = {
    action,
    expected_version: item.version,
  };
  if (dueAt !== undefined) payload.due_at = dueAt;
  const updated = await workApi.actOnItem(item.item_id, payload);
  mergeItem(updated);
  return updated;
}

/** 永久删除事项（需 confirm=delete_work_item）。成功后从 store 移除。 */
export async function deleteWorkItem(item: WorkItem): Promise<void> {
  await workApi.deleteItem(item.item_id, {
    expected_version: item.version,
    confirm: 'delete_work_item',
  });
  removeItem(item.item_id);
}

/** 将事项元数据及处理会话完整沉淀到个人知识。 */
export async function saveItemKnowledge(item: WorkItem): Promise<void> {
  await workApi.saveItemKnowledge(item.item_id, true);
}

/** Explicitly create the item's single processing conversation. */
export async function startItemProcessingSession(item: WorkItem): Promise<WorkItem> {
  const updated = await workApi.startItemProcessingSession(item.item_id, {
    expected_version: item.version,
  });
  mergeItem(updated);
  return updated;
}

/** 更新事项的用户可配置字段并合并最新版本。 */
export async function updateWorkItem(
  item: WorkItem,
  changes: {
    title: string;
    description?: string;
    due_at?: number | null;
    priority?: string | null;
    category?: string | null;
    related_system?: string | null;
  },
): Promise<WorkItem> {
  const updated = await workApi.updateItem(item.item_id, {
    expected_version: item.version,
    ...changes,
  });
  mergeItem(updated);
  return updated;
}

/**
 * 渲染事项处理空间到容器。包含：
 * - 头部：标题、状态徽标、处置标记
 * - 动作工具栏：依据 availableActions 显示命令按钮
 * - 删除按钮（带二次确认状态）
 * - 详情面板：元数据 + 活动流
 */
export function renderItemSpace(
  container: HTMLElement,
  item: WorkItem,
  options: ItemSpaceOptions = {},
): void {
  container.className = 'mw-work-item-space';
  container.innerHTML = '';
  container.dataset.itemId = item.item_id;

  const topbar = document.createElement('div');
  topbar.className = 'mw-work-item-space__topbar';
  if (options.onBack) {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'mw-work-item-space__back';
    back.textContent = options.backLabel ?? '返回事项';
    back.addEventListener('click', options.onBack);
    topbar.append(back);
  }

  const header = document.createElement('header');
  header.className = 'mw-work-item-space__header';

  const title = document.createElement('h1');
  title.className = 'mw-work-item-space__title';
  title.textContent = item.title;

  const statusBadge = document.createElement('span');
  statusBadge.className = 'mw-work-item-space__status';
  statusBadge.textContent = ITEM_STATUS_LABELS[item.business_status ?? 'pending'] ?? '状态未知';
  if (item.disposition && item.disposition !== 'active') {
    statusBadge.textContent += ` · ${ITEM_STATUS_LABELS[item.disposition] ?? '已结束'}`;
  }
  header.append(title, statusBadge);
  const headerActions = document.createElement('div');
  headerActions.className = 'mw-work-item-space__header-actions';
  topbar.append(header, headerActions);
  container.append(topbar);

  const edit = document.createElement('button');
  edit.type = 'button';
  edit.className = 'mw-work-item-space__action mw-work-item-space__edit';
  edit.textContent = '编辑事项';
  edit.addEventListener('click', () => {
    if (container.querySelector('.mw-work-item-space__edit-form')) return;
    const form = document.createElement('form');
    const formHeader = document.createElement('div');
    const formHeading = document.createElement('h2');
    const formHint = document.createElement('p');
    const titleInput = document.createElement('input');
    const descriptionInput = document.createElement('textarea');
    const dueInput = document.createElement('input');
    const priorityInput = document.createElement('select');
    const categoryInput = document.createElement('input');
    const relatedSystemInput = document.createElement('select');
    const metadata = document.createElement('div');
    const formActions = document.createElement('div');
    const cancel = document.createElement('button');
    const submit = document.createElement('button');
    form.className = 'mw-work-item-space__edit-form';
    formHeader.className = 'mw-work-item-space__edit-header';
    formHeading.className = 'mw-work-item-space__edit-title';
    formHeading.textContent = '编辑事项';
    formHint.textContent = '更新事项本身的信息，不会自动创建或修改 AI 对话。';
    formHeader.append(formHeading, formHint);
    titleInput.required = true;
    titleInput.value = item.title;
    titleInput.setAttribute('aria-label', '事项标题');
    descriptionInput.value = item.description ?? '';
    descriptionInput.setAttribute('aria-label', '事项描述');
    dueInput.type = 'date';
    dueInput.setAttribute('aria-label', '截止日期');
    if (item.due_at) {
      dueInput.value = new Date(epochMilliseconds(item.due_at)).toISOString().slice(0, 10);
    }
    for (const [value, label] of [
      ['', '未设置优先级'],
      ['high', '高优先级'],
      ['medium', '中优先级'],
      ['low', '低优先级'],
    ]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      priorityInput.append(option);
    }
    priorityInput.value = item.priority ?? '';
    priorityInput.setAttribute('aria-label', '优先级');
    categoryInput.value = item.category ?? '';
    categoryInput.placeholder = '事项分类';
    categoryInput.setAttribute('aria-label', '事项分类');
    for (const [value, label] of [
      ['', '不关联系统'],
      ['portal', '门户 / 公文'],
      ['mail', '邮件系统'],
      ['calendar', '日程系统'],
      ['todo', '待办系统'],
      ['meeting', '会议系统'],
      ['hr', '人力系统'],
    ]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      relatedSystemInput.append(option);
    }
    relatedSystemInput.value = item.related_system ?? '';
    relatedSystemInput.setAttribute('aria-label', '关联系统');
    const field = (label: string, control: HTMLElement, wide = false): HTMLLabelElement => {
      const wrapper = document.createElement('label');
      const caption = document.createElement('span');
      wrapper.className = 'mw-work-item-space__edit-field';
      if (wide) wrapper.dataset.wide = 'true';
      caption.textContent = label;
      wrapper.append(caption, control);
      return wrapper;
    };
    metadata.className = 'mw-work-item-space__edit-meta';
    metadata.append(
      field('截止日期', dueInput),
      field('优先级', priorityInput),
      field('事项分类', categoryInput),
      field('关联系统', relatedSystemInput),
    );
    cancel.type = 'button';
    cancel.className = 'mw-work-item-space__edit-cancel';
    cancel.textContent = '取消';
    submit.type = 'submit';
    submit.className = 'mw-work-item-space__edit-submit';
    submit.textContent = '保存更改';
    formActions.className = 'mw-work-item-space__edit-actions';
    formActions.append(cancel, submit);
    form.append(
      formHeader,
      field('事项标题', titleInput, true),
      field('事项描述', descriptionInput, true),
      metadata,
      formActions,
    );
    container.dataset.editing = 'true';
    body.hidden = true;
    headerActions.hidden = true;
    cancel.addEventListener('click', () => {
      form.remove();
      delete container.dataset.editing;
      body.hidden = false;
      headerActions.hidden = false;
      edit.focus();
    });
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      submit.disabled = true;
      const dueAt = dueInput.value
        ? Math.floor(new Date(`${dueInput.value}T23:59:00`).getTime() / 1000)
        : null;
      void updateWorkItem(item, {
        title: titleInput.value.trim(),
        description: descriptionInput.value.trim(),
        due_at: dueAt,
        priority: priorityInput.value || null,
        ...(categoryInput.value.trim() !== (item.category ?? '')
          ? { category: categoryInput.value.trim() || null }
          : {}),
        ...(relatedSystemInput.value !== (item.related_system ?? '')
          ? { related_system: relatedSystemInput.value || null }
          : {}),
      }).then((updated) => {
        renderItemSpace(container, updated, { ...options, notice: '事项已更新' });
      }).catch((error) => {
        submit.disabled = false;
        feedback.dataset.state = 'error';
        feedback.textContent = `保存失败：${error instanceof Error ? error.message : String(error)}`;
      });
    });
    topbar.after(form);
    titleInput.focus();
  });
  headerActions.append(edit);

  if (options.onOpenSession) {
    const sessionAction = document.createElement('button');
    sessionAction.type = 'button';
    sessionAction.className = item.processing_session_id
      ? 'mw-work-item-space__action mw-work-item-space__open-session'
      : 'mw-work-item-space__action mw-work-item-space__start-session';
    sessionAction.textContent = item.processing_session_id ? '继续 AI 协作' : '使用 AI 协助';
    sessionAction.addEventListener('click', () => {
      if (item.processing_session_id) {
        options.onOpenSession?.(item.processing_session_id);
        return;
      }
      sessionAction.disabled = true;
      void startItemProcessingSession(item)
        .then((updated) => {
          if (updated.processing_session_id) {
            options.onOpenSession?.(updated.processing_session_id);
          }
        })
        .catch((error) => {
          sessionAction.disabled = false;
          feedback.dataset.state = 'error';
          feedback.textContent = `创建 AI 协作失败：${error instanceof Error ? error.message : String(error)}`;
        });
    });
    headerActions.append(sessionAction);
  }

  const feedback = document.createElement('p');
  feedback.className = 'mw-work-item-space__feedback';
  feedback.setAttribute('aria-live', 'polite');
  feedback.textContent = options.notice ?? '';
  container.append(feedback);

  const body = document.createElement('div');
  const detailsRegion = document.createElement('section');
  const detailsHeading = document.createElement('h2');
  const description = document.createElement('p');
  const aside = document.createElement('aside');
  const actionsHeading = document.createElement('h2');
  body.className = 'mw-work-item-space__body';
  detailsRegion.className = 'mw-work-item-space__details-region';
  detailsHeading.className = 'mw-work-item-space__section-title';
  detailsHeading.textContent = '事项详情';
  description.className = 'mw-work-item-space__description';
  description.textContent = item.description || '暂无描述';
  aside.className = 'mw-work-item-space__aside';
  actionsHeading.className = 'mw-work-item-space__section-title';
  actionsHeading.textContent = '事项操作';
  detailsRegion.append(detailsHeading, description);
  aside.append(actionsHeading);
  body.append(detailsRegion, aside);
  container.append(body);

  const actions = availableActions(item);
  const toolbar = document.createElement('div');
  const workflow = document.createElement('div');
  const scheduling = document.createElement('div');
  const lifecycle = document.createElement('div');
  const schedulingLabel = document.createElement('span');
  const dueInput = document.createElement('input');
  toolbar.className = 'mw-work-item-space__toolbar';
  workflow.className = 'mw-work-item-space__action-group mw-work-item-space__action-group--workflow';
  scheduling.className = 'mw-work-item-space__action-group';
  lifecycle.className = 'mw-work-item-space__action-group mw-work-item-space__action-group--lifecycle';
  schedulingLabel.className = 'mw-work-item-space__action-label';
  schedulingLabel.textContent = '调整截止时间';
  dueInput.type = 'datetime-local';
  dueInput.className = 'mw-work-item-space__due';
  dueInput.setAttribute('aria-label', '新的截止时间');
  if (item.due_at) {
    const local = new Date(epochMilliseconds(item.due_at) - new Date().getTimezoneOffset() * 60_000);
    dueInput.value = local.toISOString().slice(0, 16);
  }
  if (actions.includes('postpone')) scheduling.append(schedulingLabel, dueInput);

  for (const action of actions) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mw-work-item-space__action';
    if (action === 'complete' || action === 'reopen') {
      btn.classList.add('mw-work-item-space__action--primary');
    }
    btn.dataset.action = action;
    btn.textContent = ITEM_ACTION_LABELS[action];
    btn.addEventListener('click', () => {
      const dueAt = action === 'postpone'
        ? epochSeconds(new Date(dueInput.value).getTime())
        : undefined;
      if (action === 'postpone' && !Number.isFinite(dueAt)) {
        feedback.dataset.state = 'error';
        feedback.textContent = '请选择新的截止时间';
        dueInput.focus();
        return;
      }
      btn.disabled = true;
      feedback.removeAttribute('data-state');
      feedback.textContent = `正在${ITEM_ACTION_LABELS[action]}…`;
      void applyItemAction(item, action, dueAt)
        .then((updated) => renderItemSpace(container, updated, {
          ...options,
          notice: `已${ITEM_ACTION_LABELS[action]}`,
        }))
        .catch((error) => {
          btn.disabled = false;
          feedback.dataset.state = 'error';
          feedback.textContent = `操作失败：${error instanceof Error ? error.message : String(error)}`;
        });
    });
    if (action === 'complete' || action === 'reopen') workflow.append(btn);
    else if (action === 'postpone') scheduling.append(btn);
    else lifecycle.append(btn);
  }
  if (workflow.childElementCount) toolbar.append(workflow);
  if (scheduling.childElementCount > 1) toolbar.append(scheduling);
  if (lifecycle.childElementCount) toolbar.append(lifecycle);
  const knowledgeBtn = document.createElement('button');
  knowledgeBtn.type = 'button';
  knowledgeBtn.className = 'mw-work-item-space__action mw-work-item-space__knowledge';
  knowledgeBtn.dataset.action = 'save-knowledge';
  knowledgeBtn.textContent = '沉淀到个人知识';
  knowledgeBtn.addEventListener('click', () => {
    knowledgeBtn.disabled = true;
    feedback.textContent = '正在沉淀事项…';
    void saveItemKnowledge(item)
      .then(() => { feedback.textContent = '已沉淀到个人知识'; })
      .catch((error) => {
        knowledgeBtn.disabled = false;
        feedback.dataset.state = 'error';
        feedback.textContent = `沉淀失败：${error instanceof Error ? error.message : String(error)}`;
      });
  });
  aside.append(toolbar, knowledgeBtn);

  const deleteWrapper = document.createElement('div');
  deleteWrapper.className = 'mw-work-item-space__delete';
  const deleteBtn = document.createElement('button');
  const deleteImpact = document.createElement('p');
  deleteBtn.type = 'button';
  deleteBtn.className = 'mw-work-item-space__delete-btn';
  deleteBtn.textContent = '删除';
  deleteImpact.className = 'mw-work-item-space__delete-impact';
  deleteImpact.textContent = '永久删除会移除事项和活动记录；处理会话、附件文件和已沉淀知识不会自动删除。';
  deleteImpact.hidden = true;
  let confirming = false;
  deleteBtn.addEventListener('click', () => {
    if (!confirming) {
      confirming = true;
      deleteBtn.textContent = '确认永久删除？再次点击';
      deleteBtn.dataset.confirming = 'true';
      deleteImpact.hidden = false;
      return;
    }
    deleteBtn.disabled = true;
    deleteBtn.textContent = '删除中…';
    void deleteWorkItem(item)
      .then(() => {
        if (options.onDeleted) options.onDeleted();
        else container.textContent = '事项已删除';
      })
      .catch((error) => {
        deleteBtn.disabled = false;
        deleteBtn.textContent = '删除';
        confirming = false;
        deleteImpact.hidden = true;
        deleteBtn.removeAttribute('data-confirming');
        feedback.dataset.state = 'error';
        feedback.textContent = `删除失败：${error instanceof Error ? error.message : String(error)}`;
      });
  });
  deleteWrapper.append(deleteImpact, deleteBtn);
  aside.append(deleteWrapper);

  const detailsSlot = document.createElement('div');
  detailsSlot.className = 'mw-work-item-space__details';
  detailsRegion.append(detailsSlot);
  void renderItemDetails(detailsSlot, item);
}

/**
 * 在共享右侧 Drawer 中打开完整事项空间。
 *
 * Drawer 只承载事项辅助信息与动作，Conversation Surface 继续留在原位；
 * 删除事项后关闭 Drawer，但不会删除或替换既有处理会话。
 */
export function openWorkItemDrawer(
  trigger: HTMLElement,
  item: WorkItem,
  options: WorkItemDrawerOptions = {},
): ReturnType<typeof openDrawer> {
  const content = document.createElement('div');
  const drawerRef: { current?: ReturnType<typeof openDrawer> } = {};
  renderItemSpace(content, item, {
    onDeleted: () => {
      drawerRef.current?.close();
      options.onDeleted?.();
    },
  });
  content.classList.add('mw-work-item-space--drawer');
  const drawer = openDrawer({
    trigger,
    title: '事项详情',
    content,
  });
  drawerRef.current = drawer;
  return drawer;
}
