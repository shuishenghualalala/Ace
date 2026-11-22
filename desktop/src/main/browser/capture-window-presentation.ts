export const CAPTURE_WINDOW_PRESENTATION_GRACE_MS = 100;

export interface CapturePresentationWindow {
  isDestroyed(): boolean;
  isVisible(): boolean;
  isMinimized(): boolean;
  isFocused(): boolean;
  getOpacity(): number;
  setOpacity(opacity: number): void;
  showInactive(): void;
  hide(): void;
  setSkipTaskbar(skip: boolean): void;
  on(event: 'focus', listener: () => void): void;
  removeListener(event: 'focus', listener: () => void): void;
}

export interface CaptureWindowPresentation {
  release(): void;
}

export function startCaptureWindowPresentation(
  window: CapturePresentationWindow | null,
  hideTaskbar = process.platform === 'win32',
): CaptureWindowPresentation | undefined {
  try {
    if (
      !window
      || window.isDestroyed()
      || window.isVisible()
      || window.isMinimized()
    ) {
      return undefined;
    }
  } catch {
    return undefined;
  }

  let originalOpacity: number;
  try {
    originalOpacity = window.getOpacity();
  } catch {
    return undefined;
  }

  let released = false;
  let taskbarHidden = false;
  const release = (preserveVisibility: boolean): void => {
    if (released) return;
    released = true;
    try {
      window.removeListener('focus', handleFocus);
    } catch {
      // Cleanup continues so a listener failure cannot strand a transparent window.
    }
    if (window.isDestroyed()) return;
    try {
      if (!preserveVisibility && !window.isFocused()) window.hide();
    } catch {
      // Opacity and taskbar state must still be restored when hiding fails.
    } finally {
      if (!window.isDestroyed()) {
        try {
          window.setOpacity(originalOpacity);
        } catch {
          // The window may have been destroyed between the state checks.
        }
        if (taskbarHidden) {
          try {
            window.setSkipTaskbar(false);
          } catch {
            // The window may have been destroyed between the state checks.
          }
        }
      }
    }
  };
  function handleFocus(): void {
    release(true);
  }

  try {
    if (hideTaskbar) {
      window.setSkipTaskbar(true);
      taskbarHidden = true;
    }
    window.setOpacity(0);
    window.on('focus', handleFocus);
    window.showInactive();
  } catch {
    release(true);
    return undefined;
  }
  return { release: () => release(false) };
}
