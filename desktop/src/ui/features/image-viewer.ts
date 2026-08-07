/**
 * 对话图片的统一查看/复制/定位交互。
 *
 * 图片字节不经过 renderer：复制动作只把本地绝对路径交给 preload，主进程会
 * 重新按当前账号的 task_workspaces/uploads 边界授权，再执行剪贴板或定位动作。
 */

import { notify } from '../state';
import { isAbsoluteLocalPath } from '../tool-screenshot';
import {
  activateExistingModal,
  type OverlayHandle,
} from '../components/overlays';

let imageViewer: HTMLElement | null = null;
let imageViewerHandle: OverlayHandle | null = null;

export async function copyImageToClipboard(localPath: string): Promise<boolean> {
  if (!isAbsoluteLocalPath(localPath)) {
    notify('该图片不是可复制的本地文件');
    return false;
  }
  try {
    await window.Crew.copyImage(localPath);
    notify('图片已复制');
    return true;
  } catch {
    notify('复制图片失败，可在文件夹中打开后重试');
    return false;
  }
}

export async function revealImageInFolder(localPath: string): Promise<boolean> {
  if (!isAbsoluteLocalPath(localPath)) {
    notify('该图片不是本地文件');
    return false;
  }
  try {
    await window.Crew.revealImage(localPath);
    return true;
  } catch {
    notify('无法在文件夹中显示该图片');
    return false;
  }
}

function actionButton(label: string, className: string): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `btn-v2 ${className}`;
  button.textContent = label;
  return button;
}

export function openImageViewer(
  src: string,
  caption: string,
  localPath = '',
): void {
  closeImageViewer();

  const viewer = document.createElement('div');
  viewer.className = 'chat-image-viewer';
  viewer.setAttribute('role', 'dialog');
  viewer.setAttribute('aria-modal', 'true');
  viewer.setAttribute('aria-label', caption ? `查看图片：${caption}` : '查看图片');

  const dialog = document.createElement('div');
  dialog.className = 'chat-image-viewer__dialog';
  const body = document.createElement('div');
  body.className = 'chat-image-viewer__body';
  const image = document.createElement('img');
  image.className = 'chat-image-viewer__image';
  image.src = src;
  image.alt = caption;
  image.draggable = false;
  body.appendChild(image);

  const footer = document.createElement('div');
  footer.className = 'chat-image-viewer__footer';
  const captionEl = document.createElement('span');
  captionEl.className = 'chat-image-viewer__caption';
  captionEl.textContent = caption;
  const actions = document.createElement('div');
  actions.className = 'chat-image-viewer__actions';

  if (isAbsoluteLocalPath(localPath)) {
    const copyButton = actionButton('复制图片', 'chat-image-viewer__copy');
    copyButton.addEventListener('click', () => void copyImageToClipboard(localPath));
    const revealButton = actionButton('在文件夹中显示', 'chat-image-viewer__reveal');
    revealButton.addEventListener('click', () => void revealImageInFolder(localPath));
    actions.append(copyButton, revealButton);
  }
  const closeButton = actionButton('关闭', 'chat-image-viewer__close');
  closeButton.addEventListener('click', () => closeImageViewer());
  actions.appendChild(closeButton);
  footer.append(captionEl, actions);
  dialog.append(body, footer);
  viewer.appendChild(dialog);
  imageViewer = viewer;
  imageViewerHandle = activateExistingModal({
    root: viewer,
    panel: dialog,
    initialFocus:
      actions.querySelector<HTMLButtonElement>('.chat-image-viewer__copy') ?? closeButton,
    onClose: () => {
      viewer.remove();
      if (imageViewer === viewer) imageViewer = null;
      imageViewerHandle = null;
    },
  });
}

export function closeImageViewer(): void {
  if (!imageViewer) return;
  imageViewerHandle?.close();
  if (imageViewer) {
    imageViewer.remove();
    imageViewer = null;
  }
  imageViewerHandle = null;
}
