/**
 * 通用确认对话框（基于 #confirm-dialog 静态 HTML）
 */

interface ConfirmDialogOptions {
  title?: string;
  message?: string;
  confirmText?: string;
  cancelText?: string;
}

/**
 * 显示确认对话框，返回 Promise<boolean>。
 * - Esc 取消
 * - 点击 overlay 关闭区域取消
 * - 缺失 DOM 时降级为 window.confirm
 */
export function showConfirmDialog({
  title = '确认操作',
  message = '确定要执行此操作吗？',
  confirmText = '确认',
  cancelText = '取消',
}: ConfirmDialogOptions = {}): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = document.getElementById('confirm-dialog');
    const titleEl = document.getElementById('confirm-title');
    const messageEl = document.getElementById('confirm-message');
    const okBtn = document.getElementById('confirm-ok');
    const cancelBtn = document.getElementById('confirm-cancel');

    // 缺失 DOM 时降级到原生 confirm
    if (!dialog || !titleEl || !messageEl || !okBtn || !cancelBtn) {
      resolve(window.confirm(message));
      return;
    }

    titleEl.textContent = title;
    messageEl.textContent = message;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    // 用 includes 而非全等：「全局卸载」这类带限定词的破坏性动作同样要红色警示。
    okBtn.classList.toggle('is-danger', confirmText.includes('卸载') || confirmText.includes('删除'));

    dialog.style.display = 'flex';
    dialog.classList.add('show');

    let settled = false;

    const cleanup = (): void => {
      dialog.classList.remove('show');
      dialog.style.display = 'none';
      okBtn.removeEventListener('click', onConfirm);
      cancelBtn.removeEventListener('click', onCancel);
      dialog.removeEventListener('click', onOverlayClick);
      document.removeEventListener('keydown', onEsc);
    };

    const finish = (result: boolean) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(result);
    };

    const onConfirm = () => finish(true);
    const onCancel = () => finish(false);
    const onOverlayClick = (e: Event) => {
      if (e.target === dialog) finish(false);
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') finish(false);
    };

    okBtn.addEventListener('click', onConfirm);
    cancelBtn.addEventListener('click', onCancel);
    dialog.addEventListener('click', onOverlayClick);
    document.addEventListener('keydown', onEsc);
  });
}
