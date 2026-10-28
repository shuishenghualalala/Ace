import { describe, expect, it } from 'vitest';
import { browserZoomShortcut, nextBrowserZoom } from '../../src/shared/browser-zoom';

describe('browser page zoom', () => {
  it('steps through standard percentages, resets, and clamps at both limits', () => {
    expect(nextBrowserZoom(1, 'in')).toBe(1.1);
    expect(nextBrowserZoom(1, 'out')).toBe(0.9);
    expect(nextBrowserZoom(1.25, 'out')).toBe(1.1);
    expect(nextBrowserZoom(1.37, 'in')).toBe(1.5);
    expect(nextBrowserZoom(1.37, 'out')).toBe(1.25);
    expect(nextBrowserZoom(3, 'in')).toBe(3);
    expect(nextBrowserZoom(0.25, 'out')).toBe(0.25);
    expect(nextBrowserZoom(3, 'reset')).toBe(1);
  });
  it.each([
    ['+', 'in'], ['=', 'in'], ['-', 'out'], ['0', 'reset'],
  ] as const)('recognizes Ctrl and Cmd with %s', (key, action) => {
    expect(browserZoomShortcut({ key, control: true, type: 'keyDown' })).toBe(action);
    expect(browserZoomShortcut({ key, meta: true })).toBe(action);
    expect(browserZoomShortcut({ key })).toBeNull();
    expect(browserZoomShortcut({ key, control: true, alt: true })).toBeNull();
    expect(browserZoomShortcut({ key, control: true, type: 'keyUp' })).toBeNull();
  });
});
