import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const DEFAULT_HOME_DIRNAME = '.Crew';

function repoRoot(): string {
  return path.resolve(__dirname, '..', '..', '..');
}

let resolvedCrewHomeCache: string | null = null;
let resolvedCrewHomeEnv: string | null = null;

/** Resolve the runtime home shared by Desktop and the local Gateway. */
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
  for (const filename of ['config.yaml', 'config.yaml.example']) {
    try {
      const configPath = path.join(repoRoot(), 'config', filename);
      const content = fs.readFileSync(configPath, 'utf8');
      const match = content.match(/^\s*crew_home:\s*['"]?([^'"#\n]+?)['"]?\s*(?:#.*)?$/m);
      const raw = match?.[1]?.trim();
      if (raw) {
        resolvedCrewHomeCache = path.isAbsolute(raw) ? raw : path.join(os.homedir(), raw);
        resolvedCrewHomeEnv = envCacheKey;
        return resolvedCrewHomeCache;
      }
    } catch {
      // Try the publishable example before falling back to the standard home.
    }
  }
  resolvedCrewHomeCache = path.join(os.homedir(), DEFAULT_HOME_DIRNAME);
  resolvedCrewHomeEnv = envCacheKey;
  return resolvedCrewHomeCache;
}
