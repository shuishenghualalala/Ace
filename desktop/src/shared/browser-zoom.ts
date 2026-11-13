export const BROWSER_ZOOM_FACTORS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3] as const;
export type BrowserZoomAction = 'in' | 'out' | 'reset';

export function nextBrowserZoom(current: number, action: BrowserZoomAction): number {
  if (action === 'reset') return 1;
  return action === 'in'
    ? BROWSER_ZOOM_FACTORS.find((factor) => factor > current + 0.001) ?? BROWSER_ZOOM_FACTORS.at(-1)!
    : [...BROWSER_ZOOM_FACTORS].reverse().find((factor) => factor < current - 0.001) ?? BROWSER_ZOOM_FACTORS[0];
}

export function browserZoomShortcut(input: {
  key?: string; type?: string; control?: boolean; meta?: boolean; alt?: boolean;
}): BrowserZoomAction | null {
  if (input.type && input.type !== 'keyDown') return null;
  if ((!input.control && !input.meta) || input.alt) return null;
  if (input.key === '+' || input.key === '=') return 'in';
  if (input.key === '-') return 'out';
  if (input.key === '0') return 'reset';
  return null;
}
