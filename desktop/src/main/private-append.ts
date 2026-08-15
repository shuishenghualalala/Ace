import fs from 'node:fs';
import path from 'node:path';

/**
 * Append to a regular file using an owner-only, symlink-resistant handle.
 * Never follows a symlink at the final path component.
 */
export function appendPrivateSync(file: string, content: string): void {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const noFollow =
    process.platform !== 'win32' && typeof fs.constants.O_NOFOLLOW === 'number'
      ? fs.constants.O_NOFOLLOW
      : 0;
  const fd = fs.openSync(
    file,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_APPEND | noFollow,
    0o600,
  );
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile()) throw new Error('unsafe append target');
    if (process.platform !== 'win32') fs.fchmodSync(fd, 0o600);
    fs.writeSync(fd, content, null, 'utf8');
  } finally {
    fs.closeSync(fd);
  }
}
