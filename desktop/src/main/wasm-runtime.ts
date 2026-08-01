/** Electron 43 的渲染进程仍需显式开启 stringref；imported-strings 已被 V8 移除。 */
export const PPTX_WASM_V8_FLAGS = [
  '--experimental-wasm-stringref',
] as const;

export interface ChromiumCommandLine {
  getSwitchValue(name: string): string;
  appendSwitch(name: string, value?: string): void;
}

export function mergeV8Flags(existing: string, required: readonly string[]): string {
  const flags = existing.trim().split(/\s+/).filter(Boolean);
  for (const flag of required) {
    if (!flags.includes(flag)) flags.push(flag);
  }
  return flags.join(' ');
}

/** 必须在 app ready 之前调用；保留产品其他模块或启动参数已经设置的 V8 flags。 */
export function configurePptxWasmRuntime(commandLine: ChromiumCommandLine): string {
  const merged = mergeV8Flags(commandLine.getSwitchValue('js-flags'), PPTX_WASM_V8_FLAGS);
  commandLine.appendSwitch('js-flags', merged);
  return merged;
}
