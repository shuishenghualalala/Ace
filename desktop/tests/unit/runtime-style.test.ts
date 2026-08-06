// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest';

import {
  clearRuntimeStyle,
  clearRuntimeToken,
  setRuntimeStyle,
  setRuntimeToken,
} from '../../src/ui/components/runtime-style';

describe('runtime-style bridge', () => {
  it('publishes allowlisted values through the shared runtime contract', () => {
    const element = document.createElement('div');

    setRuntimeStyle(element, 'left', '12px');

    expect(element.classList.contains('mw-runtime-style')).toBe(true);
    expect(element.style.getPropertyValue('--mw-runtime-left')).toBe('12px');

    clearRuntimeStyle(element, 'left');

    expect(element.style.getPropertyValue('--mw-runtime-left')).toBe('');
    expect(element.classList.contains('mw-runtime-style')).toBe(false);
  });

  it('publishes only allowlisted runtime tokens', () => {
    const root = document.documentElement;

    setRuntimeToken(root, '--mw-inspector-width', '300px');
    expect(root.style.getPropertyValue('--mw-inspector-width')).toBe('300px');

    clearRuntimeToken(root, '--mw-inspector-width');
    expect(root.style.getPropertyValue('--mw-inspector-width')).toBe('');
    expect(() => setRuntimeToken(root, '--mw-unknown', '1px')).toThrow(
      'Unsupported runtime token',
    );
  });
});
