/** Safe local-image -> OS clipboard bridge used by the trusted renderer IPC. */

import { clipboard, nativeImage, type NativeImage } from 'electron';
import { IPC_ARG_VALIDATION_FAILED } from '../shared/constants';
import {
  readResolvedCrewFile,
  resolveCrewFilePath,
  type ResolvedCrewFile,
} from './crew-file-protocol';

const MAX_CLIPBOARD_IMAGE_FILE_BYTES = 32 * 1024 * 1024;
const MAX_CLIPBOARD_IMAGE_PIXELS = 80_000_000;

interface ImageClipboardDeps {
  decode: (bytes: Buffer) => Pick<NativeImage, 'getSize' | 'isEmpty'>;
  write: (image: Pick<NativeImage, 'getSize' | 'isEmpty'>) => void;
}

const defaultDeps: ImageClipboardDeps = {
  decode: (bytes) => nativeImage.createFromBuffer(bytes),
  write: (image) => clipboard.writeImage(image as NativeImage),
};

/** Authorize a renderer-provided path in the current account's canonical image
 * roots. Callers must use `filePath`, never the original renderer string. */
export function authorizeOwnedImagePath(
  rawPath: string,
  crewHome: string,
  ownerSegment: string | null,
): ResolvedCrewFile {
  if (
    typeof rawPath !== 'string'
    || rawPath.length === 0
    || rawPath.length > 4_096
    || rawPath.includes('\0')
  ) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid image path`);
  }
  let encodedPath: string;
  try {
    encodedPath = encodeURIComponent(rawPath);
  } catch {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid image path`);
  }
  const resolved = ownerSegment
    ? resolveCrewFilePath(
      `crew-file://img/${encodedPath}`,
      crewHome,
      ownerSegment,
    )
    : null;
  if (!resolved) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: image is outside the current account`);
  }
  return resolved;
}

export async function writeOwnedImageToClipboard(
  rawPath: string,
  crewHome: string,
  ownerSegment: string | null,
  deps: ImageClipboardDeps = defaultDeps,
  authorizationStillCurrent: () => boolean = () => true,
): Promise<{ ok: true }> {
  const resolved = authorizeOwnedImagePath(rawPath, crewHome, ownerSegment);
  if (resolved.identity.size <= 0 || resolved.identity.size > MAX_CLIPBOARD_IMAGE_FILE_BYTES) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: image file is too large`);
  }
  const bytes = await readResolvedCrewFile(resolved);
  if (!bytes) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: image file is empty`);
  }
  if (!authorizationStillCurrent()) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: image account changed`);
  }
  const image = deps.decode(bytes);
  const size = image.getSize();
  if (
    image.isEmpty()
    || !Number.isSafeInteger(size.width)
    || !Number.isSafeInteger(size.height)
    || size.width <= 0
    || size.height <= 0
    || size.width > 32_768
    || size.height > 32_768
    || size.width * size.height > MAX_CLIPBOARD_IMAGE_PIXELS
  ) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: unsupported or oversized image`);
  }
  if (!authorizationStillCurrent()) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: image account changed`);
  }
  deps.write(image);
  return { ok: true };
}
