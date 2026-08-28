import * as fs from 'fs';
import * as path from 'path';

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
type WorkspacePathApi = Pick<typeof path, 'isAbsolute' | 'relative'>;

export interface ResolvedWorkspaceFile {
  filePath: string;
  identity: {
    dev: number;
    ino: number;
    nlink: number;
    size: number;
    mtimeMs: number;
    ctimeMs: number;
  };
}

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

/** Resolve one existing regular file strictly inside a canonical Workspace root. */
export async function resolveWorkspaceFilePath(
  rawPath: string,
  canonicalRoot: string,
  fileSystem: DirectoryFileSystem = fs.promises,
  pathApi: WorkspacePathApi = path,
): Promise<ResolvedWorkspaceFile | null> {
  if (!rawPath || rawPath.includes('\0') || !pathApi.isAbsolute(rawPath)) return null;
  try {
    const filePath = await fileSystem.realpath(rawPath);
    const relative = pathApi.relative(canonicalRoot, filePath);
    if (!relative || relative.startsWith('..') || pathApi.isAbsolute(relative)) return null;
    const stat = await fileSystem.stat(filePath);
    if (!stat.isFile() || stat.nlink !== 1) return null;
    return {
      filePath,
      identity: {
        dev: stat.dev,
        ino: stat.ino,
        nlink: stat.nlink,
        size: stat.size,
        mtimeMs: stat.mtimeMs,
        ctimeMs: stat.ctimeMs,
      },
    };
  } catch {
    return null;
  }
}
