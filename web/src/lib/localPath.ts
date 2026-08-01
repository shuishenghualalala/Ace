export function localPathDirectory(path: string): string {
  const value = String(path || "").trim();
  if (!value) return "";
  const normalized = value.replace(/\\/g, "/");
  const index = normalized.lastIndexOf("/");
  if (index <= 0) return "";
  return value.slice(0, index);
}

export function showLocalPathDialog(path: string): void {
  const directory = localPathDirectory(path);
  const lines = [
    "当前环境无法直接打开本地文件，可复制路径到 Finder/文件管理器中打开。",
    "",
    `文件：${path}`,
    directory ? `目录：${directory}` : "",
  ].filter(Boolean);
  window.alert(lines.join("\n"));
}

export async function openLocalPath(path: string): Promise<void> {
  const value = String(path || "").trim();
  if (!value) return;
  const bridge = window.Crew;
  if (bridge?.openPath) {
    try {
      const result = await bridge.openPath(value);
      if (!result) return;
    } catch {
      // Fall through to opening the containing folder or showing the path.
    }
    const directory = localPathDirectory(value);
    if (directory) {
      try {
        const result = await bridge.openPath(directory);
        if (!result) return;
      } catch {
        // Fall through to the path dialog.
      }
    }
  }
  showLocalPathDialog(value);
}
