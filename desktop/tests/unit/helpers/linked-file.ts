import { symlink } from 'node:fs/promises';
import path from 'node:path';

/** Create a path to an existing fixture file across a real filesystem link. */
export async function linkFixtureFile(target: string, linkPath: string): Promise<string> {
  try {
    await symlink(target, linkPath, 'file');
    return linkPath;
  } catch (error) {
    if (process.platform !== 'win32' || (error as NodeJS.ErrnoException).code !== 'EPERM') throw error;
    const junction = linkPath + '.dir';
    await symlink(path.dirname(target), junction, 'junction');
    return path.join(junction, path.basename(target));
  }
}
