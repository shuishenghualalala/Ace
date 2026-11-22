import { describe, expect, it, vi } from 'vitest';

import {
  startCaptureWindowPresentation,
  type CapturePresentationWindow,
} from '../../src/main/browser/capture-window-presentation';

type MockWindow = CapturePresentationWindow & {
  destroyed: boolean;
  visible: boolean;
  minimized: boolean;
  focused: boolean;
  opacity: number;
  skipTaskbar: boolean;
  showInactive: ReturnType<typeof vi.fn>;
  hide: ReturnType<typeof vi.fn>;
  emitFocus(): void;
};

function createWindow(): MockWindow {
  const listeners = new Set<() => void>();
  const window = {
    destroyed: false,
    visible: false,
    minimized: false,
    focused: false,
    opacity: 0.8,
    skipTaskbar: false,
    isDestroyed: vi.fn(() => window.destroyed),
    isVisible: vi.fn(() => window.visible),
    isMinimized: vi.fn(() => window.minimized),
    isFocused: vi.fn(() => window.focused),
    getOpacity: vi.fn(() => window.opacity),
    setOpacity: vi.fn((opacity: number) => {
      window.opacity = opacity;
    }),
    showInactive: vi.fn(() => {
      window.visible = true;
    }),
    hide: vi.fn(() => {
      window.visible = false;
    }),
    setSkipTaskbar: vi.fn((skip: boolean) => {
      window.skipTaskbar = skip;
    }),
    on: vi.fn((_event: 'focus', listener: () => void) => {
      listeners.add(listener);
    }),
    removeListener: vi.fn((_event: 'focus', listener: () => void) => {
      listeners.delete(listener);
    }),
    emitFocus: () => {
      window.focused = true;
      for (const listener of listeners) listener();
    },
  };
  return window;
}

describe('capture window presentation', () => {
  it('does not change a visible, minimized, destroyed, or missing window', () => {
    expect(startCaptureWindowPresentation(null)).toBeUndefined();
    for (const state of ['visible', 'minimized', 'destroyed'] as const) {
      const window = createWindow();
      window[state] = true;
      expect(startCaptureWindowPresentation(window)).toBeUndefined();
      expect(window.showInactive).not.toHaveBeenCalled();
      expect(window.setOpacity).not.toHaveBeenCalled();
      expect(window.on).not.toHaveBeenCalled();
    }
  });

  it('presents without focus and restores hidden window state exactly once', () => {
    const window = createWindow();
    const presentation = startCaptureWindowPresentation(window, true);
    expect(presentation).toBeDefined();
    expect(window.setOpacity).toHaveBeenCalledWith(0);
    expect(window.showInactive).toHaveBeenCalledOnce();
    expect(window.visible).toBe(true);
    expect(window.skipTaskbar).toBe(true);
    expect(window.on).toHaveBeenCalledWith('focus', expect.any(Function));

    presentation?.release();
    expect(window.visible).toBe(false);
    expect(window.opacity).toBe(0.8);
    expect(window.skipTaskbar).toBe(false);
    expect(window.removeListener).toHaveBeenCalledOnce();
    const hideCalls = window.hide.mock.calls.length;
    const opacityCalls = window.setOpacity.mock.calls.length;
    presentation?.release();
    expect(window.hide).toHaveBeenCalledTimes(hideCalls);
    expect(window.setOpacity).toHaveBeenCalledTimes(opacityCalls);
  });

  it('leaves the window visible when the user takes focus during capture', () => {
    const window = createWindow();
    const presentation = startCaptureWindowPresentation(window, true);
    window.emitFocus();
    expect(window.visible).toBe(true);
    expect(window.opacity).toBe(0.8);
    expect(window.skipTaskbar).toBe(false);
    expect(window.hide).not.toHaveBeenCalled();

    presentation?.release();
    expect(window.visible).toBe(true);
    expect(window.hide).not.toHaveBeenCalled();
  });

  it('restores opacity when presentation setup fails', () => {
    const window = createWindow();
    window.showInactive.mockImplementation(() => {
      throw new Error('presentation failed');
    });
    expect(startCaptureWindowPresentation(window, true)).toBeUndefined();
    expect(window.opacity).toBe(0.8);
    expect(window.skipTaskbar).toBe(false);
    expect(window.removeListener).toHaveBeenCalledOnce();
    expect(window.hide).not.toHaveBeenCalled();
  });

  it('still restores opacity and the taskbar when hiding fails', () => {
    const window = createWindow();
    window.hide.mockImplementation(() => {
      throw new Error('hide failed');
    });
    const presentation = startCaptureWindowPresentation(window, true);
    expect(() => presentation?.release()).not.toThrow();
    expect(window.opacity).toBe(0.8);
    expect(window.skipTaskbar).toBe(false);
  });

  it('releases safely after the window is destroyed', () => {
    const window = createWindow();
    const presentation = startCaptureWindowPresentation(window, true);
    window.destroyed = true;
    expect(() => presentation?.release()).not.toThrow();
    expect(window.hide).not.toHaveBeenCalled();
    expect(window.setOpacity).toHaveBeenCalledOnce();
    expect(window.setSkipTaskbar).toHaveBeenCalledOnce();
  });
});
