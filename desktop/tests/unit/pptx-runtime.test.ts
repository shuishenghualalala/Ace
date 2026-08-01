import { describe, expect, it } from 'vitest';

import { fitPptxSlideSvg, PPTX_RUNTIME_UNSUPPORTED, toPptxPreviewError } from '../../src/ui/pptx-runtime';

describe('PPTX preview runtime errors', () => {
  it('turns low-level Wasm compatibility failures into an actionable local-only message', () => {
    const error = toPptxPreviewError(new Error(
      'Wasm init failed — Tier-3 error: imported global does not match the expected type',
    ));
    expect(error.message).toContain(PPTX_RUNTIME_UNSUPPORTED);
    expect(error.message).toContain('完全退出并重新启动');
    expect(error.message).toContain('不会上传');
  });

  it('keeps unrelated PPT parsing errors intact', () => {
    const original = new Error('Invalid PPTX archive');
    expect(toPptxPreviewError(original)).toBe(original);
  });
});

describe('fitPptxSlideSvg', () => {
  it('adds a viewBox to pptx-svg fixed-size output and fills the preview frame', () => {
    const fitted = fitPptxSlideSvg(
      '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" style="background:#fff"><text x="700">标题</text></svg>',
    );
    expect(fitted).toMatchObject({ width: 960, height: 540 });
    expect(fitted.svg).toContain('viewBox="0 0 960 540"');
    expect(fitted.svg).toContain('width="100%"');
    expect(fitted.svg).toContain('height="100%"');
    expect(fitted.svg).toContain('preserveAspectRatio="xMidYMid meet"');
  });

  it('preserves an existing non-16:9 viewBox for arbitrary slide sizes', () => {
    const fitted = fitPptxSlideSvg('<svg viewBox="0 0 1024 768" width="1024" height="768"></svg>');
    expect(fitted).toMatchObject({ width: 1024, height: 768 });
    expect(fitted.svg.match(/viewBox=/g)).toHaveLength(1);
  });
});
