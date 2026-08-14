import * as fs from 'fs';

interface GatewayResponse {
  ok: boolean;
  status: number;
  body: unknown;
}

interface WorkspaceDirectoryInfo {
  exists: boolean;
  canonicalPath: string | null;
}

type DirectoryFileSystem = Pick<typeof fs.promises, 'realpath' | 'stat'>;

/** Resolves one authenticated Workspace ID to its canonical directory state. */
export async function resolveWorkspaceDirectoryInfo(
  workspaceId: string,
  requestWorkspaces: () => Promise<GatewayResponse>,
  fileSystem: DirectoryFileSystem = fs.promises,
): Promise<WorkspaceDirectoryInfo> {
  const response = await requestWorkspaces();
  if (!response.ok || !Array.isArray(response.body)) {
    throw new Error(`Workspace 目录检查失败（${response.status}）`);
  }
  const workspace = response.body.find((item): item is Record<string, unknown> =>
    typeof item === 'object'
    && item !== null
    && !Array.isArray(item)
    && item['id'] === workspaceId);
  if (!workspace) throw new Error('Workspace 不存在');
  const rootPath = typeof workspace['root_path'] === 'string'
    ? workspace['root_path'].trim()
    : '';
  if (!rootPath) return { exists: false, canonicalPath: null };
  try {
    const canonicalPath = await fileSystem.realpath(rootPath);
    const stat = await fileSystem.stat(canonicalPath);
    const exists = stat.isDirectory();
    return { exists, canonicalPath: exists ? canonicalPath : null };
  } catch {
    return { exists: false, canonicalPath: null };
  }
}
