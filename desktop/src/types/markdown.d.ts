declare module '*.md' {
  const source: string;
  export default source;
}

declare module '*.md?raw' {
  const source: string;
  export default source;
}

declare const __HELP_DOC_VERSION__: string;
