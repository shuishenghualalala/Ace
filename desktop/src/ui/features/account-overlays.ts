import {
  activateExistingModal,
  type OverlayHandle,
} from '../components/overlays';
import { createIcon } from '../components/icon';

export type AccountOverlayId =
  | 'user-agreement-modal'
  | 'version-install-modal'
  | 'force-update-overlay'
  | 'feedback-modal'
  | 'feedback-detail-modal'
  | 'session-preview-modal';

export interface AccountOverlayOptions {
  trigger?: HTMLElement | undefined;
  initialFocus?: HTMLElement | undefined;
  dismissible?: boolean;
  onClose?: (() => void) | undefined;
}

const handles = new Map<AccountOverlayId, OverlayHandle>();

interface ElementOptions {
  id?: string;
  className?: string;
  text?: string;
  attrs?: Record<string, string>;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  options: ElementOptions = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (options.id) node.id = options.id;
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  for (const [name, value] of Object.entries(options.attrs ?? {})) {
    node.setAttribute(name, value);
  }
  return node;
}

function overlayRoot(id: AccountOverlayId, panelClass = ''): {
  root: HTMLDivElement;
  panel: HTMLElement;
} {
  const root = element('div', { id, className: 'modal-overlay' });
  const panel = element('section', {
    className: `modal-content ${panelClass}`.trim(),
  });
  root.hidden = true;
  root.append(panel);
  return { root, panel };
}

function modalHeader(titleText: string, closeId?: string): HTMLElement {
  const header = element('header', { className: 'modal-header' });
  const title = element('h2', { className: 'modal-title', text: titleText });
  header.append(title);
  if (closeId) {
    const close = element('button', {
      id: closeId,
      className: 'modal-close mw-button mw-button--ghost mw-button--small mw-button--icon',
      attrs: { type: 'button', 'aria-label': '关闭', title: '关闭' },
    });
    close.append(createIcon('icon-close', { size: 16 }));
    header.append(close);
  }
  return header;
}

function modalFooter(...buttons: HTMLButtonElement[]): HTMLElement {
  const footer = element('footer', { className: 'modal-footer' });
  footer.append(...buttons);
  return footer;
}

function actionButton(
  id: string,
  text: string,
  className = 'btn-cancel',
): HTMLButtonElement {
  return element('button', {
    id,
    className,
    text,
    attrs: { type: 'button' },
  });
}

const LEGAL_CONTENT = {
  'user-agreement': {
    title: 'Crew 使用规范',
    sections: [
      ['一、原则', '使用 Crew 时应遵守适用法律法规、组织内部管理要求以及产品使用规范。'],
      ['二、适用范围', '本规范适用于通过桌面客户端使用 Crew 的个人用户、团队成员和管理员。'],
      ['三、用户行为规范', '不得利用本产品生成、传播违法违规内容，不得绕过安全边界、冒用他人身份或破坏系统稳定性。'],
      ['四、本地凭据安全', '请妥善保管本地配置、密钥等凭据，避免因主动泄露或不当使用造成风险。'],
    ],
  },
  'privacy-policy': {
    title: 'Crew 隐私政策',
    sections: [
      ['一、信息处理', '产品会按功能需要处理会话、配置、日志和用户主动上传的文件。'],
      ['二、信息使用', '相关信息仅用于提供产品功能、问题定位、安全审计和体验优化。'],
      ['三、信息存储', '数据保存范围与期限以正式产品配置、组织策略和适用法律要求为准。'],
      ['四、用户权利', '用户可通过本地数据管理功能查看、导出或删除自己的工作数据。'],
    ],
  },
} as const;

function legalArticle(
  key: keyof typeof LEGAL_CONTENT,
): HTMLElement {
  const wrapper = element('article', {
    id: `${key}-content`,
    className: `agreement-content-wrapper${key === 'user-agreement' ? ' active' : ''}`,
  });
  const content = element('div', { className: 'agreement-content' });
  const definition = LEGAL_CONTENT[key];
  content.append(
    element('h2', { text: definition.title }),
    element('p', {
      className: 'agreement-meta',
      text: '正式条款以产品发布时经审核的版本为准',
    }),
  );
  for (const [heading, copy] of definition.sections) {
    content.append(element('h3', { text: heading }), element('p', { text: copy }));
  }
  wrapper.append(content);
  return wrapper;
}

function createAgreementOverlay(): HTMLElement {
  const { root, panel } = overlayRoot(
    'user-agreement-modal',
    'agreement-modal-content',
  );
  const body = element('div', { className: 'modal-body agreement-modal-body' });
  const tabs = element('div', {
    className: 'agreement-tabs',
    attrs: { role: 'tablist', 'aria-label': '协议类型' },
  });
  for (const [key, label] of [
    ['user-agreement', '用户使用规范'],
    ['privacy-policy', '隐私政策'],
  ] as const) {
    tabs.append(
      element('button', {
        className: `agreement-tab${key === 'user-agreement' ? ' active' : ''}`,
        text: label,
        attrs: { type: 'button', 'data-agreement-tab': key, role: 'tab' },
      }),
    );
  }
  body.append(tabs, legalArticle('user-agreement'), legalArticle('privacy-policy'));
  panel.append(
    modalHeader('用户协议与隐私政策', 'agreement-modal-close'),
    body,
  );
  return root;
}

function createVersionInstallOverlay(): HTMLElement {
  const { root, panel } = overlayRoot(
    'version-install-modal',
    'version-install-dialog',
  );
  const body = element('div', { className: 'modal-body version-install-body' });
  const copy = element('div', { className: 'version-install-copy' });
  copy.append(
    element('strong', {
      id: 'version-install-version',
      className: 'version-install-version',
      text: '新版本',
    }),
    element('p', {
      id: 'version-install-message',
      text: '更新包已下载完成，是否现在安装？安装程序启动后应用会退出。',
    }),
  );
  const icon = element('span', {
    className: 'version-install-icon',
    attrs: { 'aria-hidden': 'true' },
  });
  icon.append(createIcon('icon-refresh', { size: 20 }));
  body.append(icon, copy);
  panel.append(
    modalHeader('安装更新', 'version-install-close'),
    body,
    modalFooter(
      actionButton(
        'version-install-later',
        '取消',
        'mw-button mw-button--secondary mw-button--default',
      ),
      actionButton(
        'version-install-now',
        '现在安装',
        'mw-button mw-button--primary mw-button--default',
      ),
    ),
  );
  return root;
}

function createForceUpdateOverlay(): HTMLElement {
  const root = element('div', { id: 'force-update-overlay' });
  const panel = element('section', { className: 'force-update-card' });
  const header = element('header', { className: 'force-update-header' });
  const title = element('h2', {
    className: 'force-update-title',
    text: '需要更新后继续使用',
  });
  const body = element('div', { className: 'force-update-body' });
  const icon = element('span', {
    className: 'force-update-icon',
    attrs: { 'aria-hidden': 'true' },
  });
  icon.append(createIcon('icon-warning', { size: 20 }));
  header.append(title);
  const progress = element('div', {
    id: 'force-update-progress',
    className: 'force-update-progress',
  });
  const track = element('div', { className: 'force-update-progress-bar' });
  track.append(
    element('div', {
      id: 'force-update-progress-fill',
      className: 'force-update-progress-fill',
    }),
  );
  progress.append(
    track,
    element('div', {
      id: 'force-update-progress-text',
      className: 'force-update-progress-text',
      text: '准备下载…',
    }),
  );
  body.append(
    icon,
    element('div', {
      id: 'force-update-version',
      className: 'force-update-version',
      text: '新版本',
    }),
    element('p', {
      id: 'force-update-message',
      className: 'force-update-message',
      text: '当前版本过低，请更新后继续使用。',
    }),
    progress,
  );
  const actions = element('footer', { className: 'force-update-actions' });
  actions.append(
    actionButton(
      'force-update-exit',
      '退出程序',
      'mw-button mw-button--secondary mw-button--default',
    ),
    actionButton(
      'force-update-action',
      '立即更新',
      'mw-button mw-button--primary mw-button--default',
    ),
  );
  panel.append(header, body, actions);
  root.hidden = true;
  root.append(panel);
  return root;
}

function createFeedbackOverlay(): HTMLElement {
  const { root, panel } = overlayRoot('feedback-modal', 'feedback-modal-content');
  const body = element('div', { className: 'modal-body feedback-body' });
  const form = element('section', { className: 'settings-card feedback-form-card' });
  const grid = element('div', { className: 'feedback-form-grid' });
  const titleField = element('div', {
    className: 'feedback-field feedback-field-span-2',
  });
  const titleLabel = element('label', {
    className: 'form-label',
    text: '问题标题',
  });
  titleLabel.htmlFor = 'feedback-title';
  const titleWrap = element('div', { className: 'feedback-input-wrap' });
  titleWrap.append(
    element('input', {
      id: 'feedback-title',
      className: 'form-input feedback-title-input',
      attrs: {
        type: 'text',
        placeholder: '请输入问题标题',
        maxlength: '120',
      },
    }),
    element('span', {
      id: 'feedback-title-count',
      className: 'feedback-char-count',
      text: '0/120',
    }),
  );
  titleField.append(titleLabel, titleWrap);

  const description = element('textarea', {
    id: 'feedback-description',
    className: 'form-input feedback-textarea',
    attrs: {
      rows: '3',
      placeholder: '请尽量描述复现步骤、预期结果和实际结果',
    },
  });
  const descriptionField = element('div', {
    className: 'feedback-field feedback-field-span-2',
  });
  const descriptionLabel = element('label', {
    className: 'form-label',
    text: '问题描述',
  });
  descriptionLabel.htmlFor = description.id;
  descriptionField.append(descriptionLabel, description);

  const uploadField = element('div', {
    className: 'feedback-field feedback-field-span-2',
  });
  uploadField.append(
    element('span', { className: 'form-label', text: '问题截图（最多 10 张）' }),
  );
  const upload = element('div', { className: 'feedback-upload-card' });
  const uploadMeta = element('div', { className: 'feedback-upload-meta' });
  uploadMeta.append(
    element('span', { className: 'form-hint', text: '支持点击上传或粘贴图片' }),
    element('span', {
      id: 'feedback-screenshot-count',
      className: 'feedback-upload-count',
      text: '0/10',
    }),
  );
  upload.append(
    uploadMeta,
    element('div', {
      id: 'feedback-screenshot-preview',
      className: 'feedback-upload-preview',
    }),
  );
  uploadField.append(upload);
  grid.append(titleField, descriptionField, uploadField);
  const formActions = element('div', { className: 'hub-actions feedback-actions' });
  formActions.append(
    actionButton('feedback-submit-btn', '提交反馈', 'btn btn-primary btn-sm'),
    actionButton('feedback-reset-btn', '清空内容', 'btn btn-sm'),
  );
  form.append(grid, formActions);

  const history = element('section', {
    className: 'settings-card feedback-history-card',
  });
  history.append(
    element('h3', { className: 'settings-title', text: '提交记录' }),
    element('div', {
      id: 'feedback-history-list',
      className: 'feedback-history-list',
      text: '暂无提交记录',
    }),
  );
  body.append(form, history);
  panel.append(modalHeader('问题反馈', 'feedback-modal-close'), body);
  return root;
}

function createSessionPreviewOverlay(): HTMLElement {
  const { root, panel } = overlayRoot(
    'session-preview-modal',
    'session-preview-modal__content',
  );
  root.classList.add('session-preview-modal');
  const header = modalHeader('会话预览', 'session-preview-close');
  const title = header.querySelector<HTMLElement>('.modal-title');
  if (title) title.id = 'session-preview-title';
  const body = element('div', {
    className: 'modal-body session-preview-modal__body',
  });
  body.append(
    element('div', {
      id: 'session-preview-messages',
      className: 'chat-messages web-flow session-preview-messages',
    }),
  );
  panel.append(
    header,
    body,
    modalFooter(actionButton('session-preview-done', '关闭')),
  );
  return root;
}

function createFeedbackDetailOverlay(): HTMLElement {
  const root = document.createElement('div');
  const panel = document.createElement('section');
  const header = document.createElement('header');
  const title = document.createElement('h2');
  const close = document.createElement('button');
  const body = document.createElement('div');
  const footer = document.createElement('footer');
  const done = document.createElement('button');

  root.id = 'feedback-detail-modal';
  root.className = 'modal-overlay';
  root.hidden = true;
  panel.className = 'modal-content feedback-detail-modal-content';
  header.className = 'modal-header';
  title.id = 'feedback-detail-title';
  title.className = 'modal-title';
  title.textContent = '反馈详情';
  close.id = 'feedback-detail-close';
  close.className = 'mw-button mw-button--ghost mw-button--small mw-button--icon';
  close.type = 'button';
  close.setAttribute('aria-label', '关闭');
  close.title = '关闭';
  close.append(createIcon('icon-close', { size: 16 }));
  body.id = 'feedback-detail-body';
  body.className = 'modal-body feedback-detail-body';
  footer.className = 'modal-footer feedback-detail-footer';
  done.id = 'feedback-detail-close-btn';
  done.className = 'mw-button mw-button--secondary mw-button--default';
  done.type = 'button';
  done.textContent = '关闭';
  header.append(title, close);
  footer.append(done);
  panel.append(header, body, footer);
  root.append(panel);
  (document.getElementById('renderer-overlay-host') ?? document.body).append(root);
  return root;
}

export function ensureAccountOverlay(id: AccountOverlayId): HTMLElement | null {
  const existing = document.getElementById(id);
  if (existing) return existing;
  const creators: Record<AccountOverlayId, () => HTMLElement> = {
    'user-agreement-modal': createAgreementOverlay,
    'version-install-modal': createVersionInstallOverlay,
    'force-update-overlay': createForceUpdateOverlay,
    'feedback-modal': createFeedbackOverlay,
    'feedback-detail-modal': createFeedbackDetailOverlay,
    'session-preview-modal': createSessionPreviewOverlay,
  };
  const root = creators[id]();
  (document.getElementById('renderer-overlay-host') ?? document.body).append(root);
  return root;
}

export function openAccountOverlay(
  id: AccountOverlayId,
  options: AccountOverlayOptions = {},
): OverlayHandle | null {
  const root = ensureAccountOverlay(id);
  if (!root) return null;
  const panel =
    root.querySelector<HTMLElement>('.modal-content, .force-update-card') ?? root;

  handles.get(id)?.dispose();
  const handle = activateExistingModal({
    root,
    panel,
    trigger: options.trigger,
    initialFocus: options.initialFocus,
    dismissible: options.dismissible ?? true,
    onClose: () => {
      if (handles.get(id) === handle) handles.delete(id);
      options.onClose?.();
    },
  });
  handles.set(id, handle);
  return handle;
}

export function closeAccountOverlay(id: AccountOverlayId): void {
  const handle = handles.get(id);
  if (handle) {
    handle.close();
    return;
  }
  const root = document.getElementById(id);
  root?.classList.remove('show');
  if (root) root.hidden = true;
}

export function resetAccountOverlaysForTest(): void {
  for (const handle of [...handles.values()]) handle.dispose();
  handles.clear();
}
