let returnFocus: HTMLElement | null = null;

function contactModal(): HTMLElement | null {
  return document.getElementById('contact-modal');
}

export function openContactModal(): void {
  const modal = contactModal();
  if (!modal) return;
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  modal.classList.add('show');
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  window.requestAnimationFrame(() => {
    document.getElementById('contact-modal-close')?.focus();
  });
}

export function closeContactModal(restoreFocus = true): void {
  const modal = contactModal();
  if (!modal) return;
  modal.classList.remove('show');
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  if (restoreFocus) returnFocus?.focus();
  returnFocus = null;
}

export function bindContactModal(): void {
  const modal = contactModal();
  if (!modal || modal.dataset.contactBound === '1') return;
  modal.dataset.contactBound = '1';

  document.getElementById('contact-modal-close')?.addEventListener('click', () => closeContactModal());
  modal.addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeContactModal();
  });
  modal.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeContactModal();
  });
}
