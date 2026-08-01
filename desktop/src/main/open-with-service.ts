import { execFile, spawn } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { pathToFileURL } from 'url';

export interface OpenWithApplication {
  id: string;
  name: string;
}

interface OpenWithApplicationInternal extends OpenWithApplication {
  launchTarget: string;
}

interface MacApplicationManifest {
  CFBundleDisplayName?: unknown;
  CFBundleName?: unknown;
  CFBundleIdentifier?: unknown;
  CFBundleDocumentTypes?: unknown;
}

interface MacCatalogEntry {
  appPath: string;
  bundleId: string;
  name: string;
  extensions: Set<string>;
  contentTypes: Set<string>;
}

const MAC_CONTENT_TYPES_BY_EXTENSION: Record<string, string[]> = {
  doc: ['com.microsoft.word.doc'],
  docx: ['org.openxmlformats.wordprocessingml.document'],
  gif: ['com.compuserve.gif'],
  jpeg: ['public.jpeg'],
  jpg: ['public.jpeg'],
  md: ['net.daringfireball.markdown', 'public.plain-text'],
  pdf: ['com.adobe.pdf'],
  png: ['public.png'],
  ppt: ['com.microsoft.powerpoint.ppt'],
  pptx: ['org.openxmlformats.presentationml.presentation'],
  txt: ['public.plain-text'],
  xls: ['com.microsoft.excel.xls'],
  xlsx: ['org.openxmlformats.spreadsheetml.sheet'],
};

const MAC_FRIENDLY_APPLICATION_NAMES: Record<string, string> = {
  'com.kingsoft.wpsoffice.mac': 'WPS Office',
  'com.microsoft.VSCode': 'Visual Studio Code',
};

let macCatalogPromise: Promise<MacCatalogEntry[]> | null = null;

function normalizedExtension(filePath: string): string {
  return path.extname(filePath).replace(/^\./, '').trim().toLowerCase();
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

export function parseMacApplicationManifest(
  appPath: string,
  raw: unknown,
): MacCatalogEntry | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const manifest = raw as MacApplicationManifest;
  const bundleId = typeof manifest.CFBundleIdentifier === 'string'
    ? manifest.CFBundleIdentifier.trim()
    : '';
  if (!bundleId) return null;
  const displayName = typeof manifest.CFBundleDisplayName === 'string'
    ? manifest.CFBundleDisplayName.trim()
    : '';
  const bundleName = typeof manifest.CFBundleName === 'string'
    ? manifest.CFBundleName.trim()
    : '';
  const name = MAC_FRIENDLY_APPLICATION_NAMES[bundleId]
    || displayName
    || bundleName
    || path.basename(appPath, '.app');
  const extensions = new Set<string>();
  const contentTypes = new Set<string>();
  const documentTypes = Array.isArray(manifest.CFBundleDocumentTypes)
    ? manifest.CFBundleDocumentTypes
    : [];
  for (const item of documentTypes) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const record = item as Record<string, unknown>;
    for (const extension of stringArray(record['CFBundleTypeExtensions'])) {
      if (extension !== '*') extensions.add(extension.replace(/^\./, ''));
    }
    for (const contentType of stringArray(record['LSItemContentTypes'])) {
      contentTypes.add(contentType);
    }
  }
  return { appPath, bundleId, name, extensions, contentTypes };
}

export function macApplicationSupportsExtension(
  application: Pick<MacCatalogEntry, 'extensions' | 'contentTypes'>,
  extension: string,
): boolean {
  const normalized = extension.replace(/^\./, '').trim().toLowerCase();
  if (!normalized) return false;
  if (application.extensions.has(normalized)) return true;
  return (MAC_CONTENT_TYPES_BY_EXTENSION[normalized] ?? [])
    .some((contentType) => application.contentTypes.has(contentType));
}

function execFileText(command: string, args: string[], timeout = 8_000): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(command, args, {
      encoding: 'utf8',
      maxBuffer: 4 * 1024 * 1024,
      timeout,
      windowsHide: true,
    }, (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    });
  });
}

async function macApplicationPaths(): Promise<string[]> {
  const roots = [
    '/Applications',
    '/System/Applications',
    '/System/Applications/Utilities',
    path.join(os.homedir(), 'Applications'),
  ];
  const result = new Set<string>();
  for (const root of roots) {
    const entries = await fs.promises.readdir(root, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.name.toLowerCase().endsWith('.app')) continue;
      if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
      result.add(path.join(root, entry.name));
    }
  }
  return [...result];
}

async function readMacApplication(appPath: string): Promise<MacCatalogEntry | null> {
  try {
    const output = await execFileText('/usr/bin/plutil', [
      '-convert',
      'json',
      '-o',
      '-',
      path.join(appPath, 'Contents', 'Info.plist'),
    ]);
    return parseMacApplicationManifest(appPath, JSON.parse(output));
  } catch {
    return null;
  }
}

async function loadMacCatalog(): Promise<MacCatalogEntry[]> {
  if (macCatalogPromise) return macCatalogPromise;
  macCatalogPromise = (async () => {
    const appPaths = await macApplicationPaths();
    const result: MacCatalogEntry[] = [];
    const batchSize = 8;
    for (let offset = 0; offset < appPaths.length; offset += batchSize) {
      const batch = await Promise.all(
        appPaths.slice(offset, offset + batchSize).map(readMacApplication),
      );
      for (const application of batch) {
        if (application) result.push(application);
      }
    }
    return result;
  })().catch((error) => {
    macCatalogPromise = null;
    throw error;
  });
  return macCatalogPromise;
}

async function listMacApplications(filePath: string): Promise<OpenWithApplicationInternal[]> {
  const extension = normalizedExtension(filePath);
  if (!extension) return [];
  const catalog = await loadMacCatalog();
  const byBundleId = new Map<string, OpenWithApplicationInternal>();
  for (const application of catalog) {
    if (!macApplicationSupportsExtension(application, extension)) continue;
    byBundleId.set(application.bundleId, {
      id: `mac:${application.bundleId}`,
      name: application.name,
      launchTarget: application.appPath,
    });
  }
  return [...byBundleId.values()].sort((a, b) => a.name.localeCompare(b.name));
}

interface LinuxDesktopApplication {
  name: string;
  desktopPath: string;
  mimeTypes: string[];
}

function parseLinuxDesktopFile(desktopPath: string, content: string): LinuxDesktopApplication | null {
  let inDesktopEntry = false;
  let name = '';
  let hidden = false;
  let noDisplay = false;
  let mimeTypes: string[] = [];
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith('[')) {
      inDesktopEntry = line === '[Desktop Entry]';
      continue;
    }
    if (!inDesktopEntry || !line || line.startsWith('#')) continue;
    const separator = line.indexOf('=');
    if (separator < 1) continue;
    const key = line.slice(0, separator);
    const value = line.slice(separator + 1);
    if (key === 'Name') name = value.trim();
    else if (key === 'Hidden') hidden = value.trim().toLowerCase() === 'true';
    else if (key === 'NoDisplay') noDisplay = value.trim().toLowerCase() === 'true';
    else if (key === 'MimeType') mimeTypes = value.split(';').map((item) => item.trim()).filter(Boolean);
  }
  if (!name || hidden || noDisplay || mimeTypes.length === 0) return null;
  return { name, desktopPath, mimeTypes };
}

async function listLinuxApplications(filePath: string): Promise<OpenWithApplicationInternal[]> {
  const mimeType = (await execFileText('xdg-mime', ['query', 'filetype', filePath])).trim();
  if (!mimeType) return [];
  const roots = [
    path.join(os.homedir(), '.local', 'share', 'applications'),
    '/usr/local/share/applications',
    '/usr/share/applications',
  ];
  const byDesktopName = new Map<string, OpenWithApplicationInternal>();
  for (const root of roots) {
    const entries = await fs.promises.readdir(root, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith('.desktop')) continue;
      const desktopPath = path.join(root, entry.name);
      const content = await fs.promises.readFile(desktopPath, 'utf8').catch(() => '');
      const application = parseLinuxDesktopFile(desktopPath, content);
      if (!application?.mimeTypes.includes(mimeType)) continue;
      byDesktopName.set(entry.name, {
        id: `linux:${entry.name}`,
        name: application.name,
        launchTarget: application.desktopPath,
      });
    }
  }
  return [...byDesktopName.values()].sort((a, b) => a.name.localeCompare(b.name));
}

async function listWindowsApplications(filePath: string): Promise<OpenWithApplicationInternal[]> {
  const extension = path.extname(filePath).toLowerCase();
  if (!extension) return [];
  const script = [
    '$extension=$args[0]',
    '$key="HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\\$extension\\OpenWithList"',
    '$props=Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue',
    '$names=@()',
    'if($props){foreach($p in $props.PSObject.Properties){if($p.Name -match "^[a-z]$" -and $p.Value){$names+=[string]$p.Value}}}',
    '$names | Select-Object -Unique | ForEach-Object {@{id=("win:"+$_);name=[IO.Path]::GetFileNameWithoutExtension($_);target=$_}} | ConvertTo-Json -Compress',
  ].join(';');
  const output = (await execFileText('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-Command',
    script,
    extension,
  ])).trim();
  if (!output) return [];
  const parsed = JSON.parse(output) as unknown;
  const rows = Array.isArray(parsed) ? parsed : [parsed];
  return rows.flatMap((row) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return [];
    const record = row as Record<string, unknown>;
    if (typeof record['id'] !== 'string'
      || typeof record['name'] !== 'string'
      || typeof record['target'] !== 'string') return [];
    return [{
      id: record['id'],
      name: record['name'],
      launchTarget: record['target'],
    }];
  });
}

async function listInternal(
  filePath: string,
  platform: NodeJS.Platform,
): Promise<OpenWithApplicationInternal[]> {
  if (platform === 'darwin') return listMacApplications(filePath);
  if (platform === 'linux') return listLinuxApplications(filePath);
  if (platform === 'win32') return listWindowsApplications(filePath);
  return [];
}

export async function listOpenWithApplications(
  filePath: string,
  platform: NodeJS.Platform = process.platform,
): Promise<OpenWithApplication[]> {
  const applications = await listInternal(filePath, platform);
  return applications.map(({ id, name }) => ({ id, name }));
}

function spawnDetached(command: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
    });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });
}

export async function openFileWithApplication(
  filePath: string,
  applicationId: string,
  platform: NodeJS.Platform = process.platform,
): Promise<void> {
  const application = (await listInternal(filePath, platform))
    .find((candidate) => candidate.id === applicationId);
  if (!application) throw new Error('所选程序不可用，或不支持此文件类型');
  if (platform === 'darwin') {
    await spawnDetached('/usr/bin/open', ['-a', application.launchTarget, filePath]);
    return;
  }
  if (platform === 'linux') {
    await spawnDetached('gio', [
      'launch',
      application.launchTarget,
      pathToFileURL(filePath).toString(),
    ]);
    return;
  }
  if (platform === 'win32') {
    const script = 'Start-Process -FilePath $args[0] -ArgumentList $args[1]';
    await spawnDetached('powershell.exe', [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      script,
      application.launchTarget,
      filePath,
    ]);
    return;
  }
  throw new Error('当前系统暂不支持指定程序打开');
}
