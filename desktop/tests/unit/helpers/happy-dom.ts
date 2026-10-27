export function suppressUnsupportedIframeSchemeErrors(): void {
  const write = process.stderr.write;
  process.stderr.write = ((chunk: string | Uint8Array, ...args: unknown[]) => {
    const output = String(chunk);
    if (output.includes('NotSupportedError') && output.includes('ace-site://')) return true;
    return Reflect.apply(write, process.stderr, [chunk, ...args]) as boolean;
  }) as typeof process.stderr.write;
}
