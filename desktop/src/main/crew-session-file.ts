/** Resolve the local Crew home directory shared by the desktop and Gateway. */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const DEFAULT_HOME_DIRNAME = '.Crew';

function repoRoot(): string {
  // dist/main → desktop → Crew
  return path.resolve(__dirname, '..', '..', '..');
}

// CREW_HOME/config.yaml are installation-level settings; cache the resolved
// path until the environment override changes.
let resolvedCrewHomeCache: string | null = null;
let resolvedCrewHomeEnv: string | null = null;

export function resolveCrewHome(): string {
  const fromEnv = process.env.CREW_HOME?.trim();
  const envCacheKey = fromEnv || null;
  if (resolvedCrewHomeCache && resolvedCrewHomeEnv === envCacheKey) return resolvedCrewHomeCache;
  if (fromEnv) {
    const expanded = fromEnv.replace(/^~(?=$|[/\\])/, os.homedir());
    resolvedCrewHomeCache = path.isAbsolute(expanded) ? expanded : path.join(os.homedir(), expanded);
    resolvedCrewHomeEnv = envCacheKey;
    return resolvedCrewHomeCache;
  }
  try {
    const configPath = path.join(repoRoot(), 'config', 'config.yaml');
    const content = fs.readFileSync(configPath, 'utf8');
    const match = content.match(/^\s*crew_home:\s*['"]?([^'"#\n]+?)['"]?\s*(?:#.*)?$/m);
    const raw = match?.[1]?.trim();
    if (raw) {
      resolvedCrewHomeCache = path.isAbsolute(raw) ? raw : path.join(os.homedir(), raw);
      resolvedCrewHomeEnv = envCacheKey;
      return resolvedCrewHomeCache;
    }
  } catch {
    // Unreadable config falls back to the per-user default.
  }
  resolvedCrewHomeCache = path.join(os.homedir(), DEFAULT_HOME_DIRNAME);
  resolvedCrewHomeEnv = envCacheKey;
  return resolvedCrewHomeCache;
}
