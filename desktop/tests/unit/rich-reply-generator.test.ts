import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

describe('rich reply generated validator freshness', () => {
  let root: string;
  let destination: string;
  let schema: string;
  let script: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'crew-validator-'));
    const desktop = join(root, 'desktop');
    mkdirSync(join(desktop, 'scripts'), { recursive: true });
    mkdirSync(join(desktop, 'src/ui/features/rich-reply'), { recursive: true });
    mkdirSync(join(root, 'crew/sites'), { recursive: true });
    symlinkSync(resolve('node_modules'), join(desktop, 'node_modules'), 'junction');
    script = join(desktop, 'scripts/generate-rich-reply-validator.mjs');
    destination = join(desktop, 'src/ui/features/rich-reply/validate.generated.js');
    schema = join(root, 'crew/sites/rich_reply.schema.json');
    copyFileSync(resolve('scripts/generate-rich-reply-validator.mjs'), script);
    copyFileSync(resolve('src/ui/features/rich-reply/validate.generated.js'), destination);
    copyFileSync(resolve('../crew/sites/rich_reply.schema.json'), schema);
  });

  afterEach(() => rmSync(root, { recursive: true, force: true }));

  const check = () => spawnSync(process.execPath, [script, '--check'], { encoding: 'utf8' });

  it.each(['LF', 'CRLF'])('accepts an unchanged validator checked out with %s line endings', (ending) => {
    const code = readFileSync(destination, 'utf8').replace(/\r\n/g, '\n');
    writeFileSync(destination, ending === 'CRLF' ? code.replace(/\n/g, '\r\n') : code);
    const result = check();
    expect(result.stderr).toBe('');
    expect(result.status).toBe(0);
  });

  it('still rejects a validator generated from an older schema', () => {
    const changed = JSON.parse(readFileSync(schema, 'utf8'));
    changed.title = 'Changed schema';
    writeFileSync(schema, JSON.stringify(changed));
    const result = check();
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('Rich reply validator is stale');
  });
});
