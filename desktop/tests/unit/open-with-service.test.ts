import fs from 'node:fs/promises';
import { describe, expect, it } from 'vitest';
import {
  execFileText,
  macApplicationSupportsExtension,
  parseMacApplicationManifest,
} from '../../src/main/open-with-service';

describe('open-with host process boundary', () => {
  it('rejects executable lookup through PATH', async () => {
    await expect(execFileText('powershell.exe', [])).rejects.toThrow(
      /absolute path/,
    );
  });

  it('uses fixed system helpers and no process-name cleanup', async () => {
    const source = await fs.readFile('src/main/open-with-service.ts', 'utf8');
    expect(source).toContain('C:\\\\Windows\\\\System32\\\\taskkill.exe');
    expect(source).toContain('/usr/bin/xdg-mime');
    expect(source).toContain('/usr/bin/gio');
    expect(source).not.toContain("spawn('taskkill'");
    expect(source).not.toContain("execFileText('xdg-mime'");
    expect(source).not.toContain("spawnDetached('gio'");
    expect(source).not.toContain("spawnDetached('powershell.exe'");
  });
});

describe('open-with service macOS manifest matching', () => {
  it('finds an application by declared file extension', () => {
    const application = parseMacApplicationManifest('/Applications/WPS Office.app', {
      CFBundleDisplayName: 'WPS Office',
      CFBundleIdentifier: 'com.kingsoft.wpsoffice.mac',
      CFBundleDocumentTypes: [{
        CFBundleTypeExtensions: ['doc', 'docx'],
      }],
    });
    expect(application).not.toBeNull();
    expect(application?.name).toBe('WPS Office');
    expect(macApplicationSupportsExtension(application!, '.docx')).toBe(true);
    expect(macApplicationSupportsExtension(application!, '.xlsx')).toBe(false);
  });

  it('finds an application by the standard content type when extensions are omitted', () => {
    const application = parseMacApplicationManifest('/Applications/Pages.app', {
      CFBundleName: 'Pages',
      CFBundleIdentifier: 'com.apple.iWork.Pages',
      CFBundleDocumentTypes: [{
        LSItemContentTypes: ['org.openxmlformats.wordprocessingml.document'],
      }],
    });
    expect(application).not.toBeNull();
    expect(macApplicationSupportsExtension(application!, 'docx')).toBe(true);
  });

  it('ignores wildcard-only handlers and manifests without a bundle id', () => {
    const wildcard = parseMacApplicationManifest('/Applications/Generic.app', {
      CFBundleName: 'Generic',
      CFBundleIdentifier: 'example.generic',
      CFBundleDocumentTypes: [{ CFBundleTypeExtensions: ['*'] }],
    });
    expect(macApplicationSupportsExtension(wildcard!, 'pptx')).toBe(false);
    expect(parseMacApplicationManifest('/Applications/Broken.app', {
      CFBundleName: 'Broken',
    })).toBeNull();
  });
});
