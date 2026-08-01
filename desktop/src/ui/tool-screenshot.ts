/** 从 browser_use screenshot 工具卡片数据中提取导出的图片绝对路径（纯函数）。
 *
 * save_screenshot 的结果就是导出文件的绝对路径字符串；args 是 JSON 文本
 * （tool_call 卡片上展示的形态）。非截图 action / 非图片路径返回 ''。
 */

export function screenshotResultPath(tool: {
  name: string;
  args?: string | undefined;
  result?: string | undefined;
}): string {
  if (tool.name !== 'browser_use') return '';
  let args: unknown;
  try {
    args = JSON.parse(tool.args ?? '');
  } catch {
    return '';
  }
  if (
    typeof args !== 'object'
    || args === null
    || Array.isArray(args)
    || (args as Record<string, unknown>).action !== 'screenshot'
  ) return '';
  const result = (tool.result ?? '').trim();
  if (
    !isAbsoluteLocalPath(result)
    || /[\0\r\n"'<>]/.test(result)
    || !/\.(?:png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(result)
  ) return '';
  return result;
}

/** 拼出 crew-file 协议 URL（主进程 crew-file-protocol.ts 负责边界校验）。
 *  注意必须带 host 占位段：scheme 以 standard 注册后 Chromium 按标准 URL
 *  解析，空 host 会被判 Invalid URL，请求根本到不了协议处理器。 */
export function crewFileUrl(absolutePath: string): string {
  return `crew-file://img/${encodeURIComponent(absolutePath)}`;
}

/** Renderer 只把明确的本地绝对路径交给私有协议。HTTP/data/blob 等 URL
 * 保持原样，避免把网络资源误编码成本地文件请求。 */
export function isAbsoluteLocalPath(value: string): boolean {
  return value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value) || value.startsWith('\\\\');
}

export function imageDisplayUrl(source: string): string {
  return isAbsoluteLocalPath(source) ? crewFileUrl(source) : source;
}
