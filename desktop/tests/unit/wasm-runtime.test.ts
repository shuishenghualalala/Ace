import fs from 'node:fs';

import { describe, expect, it, vi } from 'vitest';

import {
  configurePptxWasmRuntime,
  mergeV8Flags,
  PPTX_WASM_V8_FLAGS,
} from '../../src/main/wasm-runtime';

describe('PPTX Wasm Electron runtime flags', () => {
  it('keeps existing V8 flags and appends required flags once', () => {
    expect(mergeV8Flags('--max-old-space-size=2048 --experimental-wasm-stringref', PPTX_WASM_V8_FLAGS))
      .toBe('--max-old-space-size=2048 --experimental-wasm-stringref');
  });

  it('configures Chromium without replacing product flags', () => {
    const appendSwitch = vi.fn();
    const merged = configurePptxWasmRuntime({
      getSwitchValue: () => '--trace-warnings',
      appendSwitch,
    });
    expect(merged).toContain('--trace-warnings');
    expect(appendSwitch).toHaveBeenCalledWith('js-flags', merged);
  });

  it('allows WebAssembly compilation without enabling JavaScript eval', () => {
    const html = fs.readFileSync(new URL('../../assets/index.html', import.meta.url), 'utf8');
    expect(html).toContain("script-src 'self' 'wasm-unsafe-eval'");
    expect(html).not.toMatch(/script-src[^;]*'unsafe-eval'/);
  });
});
