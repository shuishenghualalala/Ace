import * as fs from 'node:fs';
import * as path from 'node:path';

export interface ExportFileOperations {
  rename(source: string, target: string): Promise<void>;
  unlink(file: string): Promise<void>;
  stat(file: string): Promise<{ isFile(): boolean }>;
}

const nativeOperations: ExportFileOperations = {
  rename: (source, target) => fs.promises.rename(source, target),
  unlink: (file) => fs.promises.unlink(file),
  stat: (file) => fs.promises.stat(file),
};

async function exists(file: string, operations: ExportFileOperations): Promise<boolean> {
  try {
    const stat = await operations.stat(file);
    return stat.isFile();
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === 'ENOENT' || code === 'ENOTDIR') return false;
    throw error;
  }
}

/**
 * Commit a fully closed download without exposing a partially written target.
 * POSIX rename replaces the destination atomically. Windows needs a same-dir
 * backup transaction because rename cannot replace an existing file.
 */
export async function commitDownloadedExport(
  tempPath: string,
  targetPath: string,
  platform: NodeJS.Platform = process.platform,
  operations: ExportFileOperations = nativeOperations,
): Promise<void> {
  if (platform !== 'win32') {
    await operations.rename(tempPath, targetPath);
    return;
  }

  const backupPath = path.join(
    path.dirname(targetPath),
    `.${path.basename(targetPath)}.${process.hrtime.bigint().toString()}.bak`,
  );
  let backupCreated = false;
  if (await exists(targetPath, operations)) {
    await operations.rename(targetPath, backupPath);
    backupCreated = true;
  }

  try {
    await operations.rename(tempPath, targetPath);
  } catch (error) {
    if (backupCreated) {
      try {
        await operations.rename(backupPath, targetPath);
        backupCreated = false;
      } catch (rollbackError) {
        throw new AggregateError([error, rollbackError], '追踪导出提交失败且无法恢复原文件');
      }
    }
    throw error;
  }

  if (backupCreated) await operations.unlink(backupPath);
}
