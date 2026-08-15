import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { GatewayWsProtocolIdentity } from '../../src/shared/gateway-ws-protocol';
import {
  GatewayFetchArgs,
  SecurityDecisionArgs,
  ShellOpenPathArgs,
  UpdateDownloadArgs,
} from '../../src/shared/ipc-schemas';

interface ParserCorpus {
  schema_version: number;
  campaign: {
    seed: number;
    ci_cases: number;
    max_cases: number;
    max_generated_input_bytes: number;
  };
  url: {
    desktop_gateway_invalid: string[];
  };
  path: {
    ipc_valid: string[];
    ipc_invalid: unknown[];
  };
  command: {
    bash_must_ask: string[];
    powershell_must_ask: string[];
    host_execution_templates: {
      powershell: string;
    };
  };
}

const corpus = JSON.parse(readFileSync(
  new URL('../../../tests/security/test_012_parser_corpus.json', import.meta.url),
  'utf8',
)) as ParserCorpus;
const ABSOLUTE_MAX_CASES = 2048;
const ABSOLUTE_MAX_GENERATED_INPUT_BYTES = 4096;

if (
  corpus.schema_version !== 1
  || corpus.campaign.ci_cases < 1
  || corpus.campaign.ci_cases > corpus.campaign.max_cases
  || corpus.campaign.max_cases > ABSOLUTE_MAX_CASES
  || corpus.campaign.max_generated_input_bytes < 1
  || corpus.campaign.max_generated_input_bytes > ABSOLUTE_MAX_GENERATED_INPUT_BYTES
) {
  throw new Error('unsafe TEST-012 corpus resource bounds');
}

function campaignSeed(): number {
  const raw = Number.parseInt(process.env.ACE_TEST012_SEED || '', 10);
  return Number.isSafeInteger(raw) ? raw : corpus.campaign.seed;
}

function campaignCases(): number {
  const raw = Number.parseInt(process.env.ACE_TEST012_CASES || '', 10);
  const requested = Number.isSafeInteger(raw) ? raw : corpus.campaign.ci_cases;
  return Math.max(
    corpus.campaign.ci_cases,
    Math.min(requested, corpus.campaign.max_cases),
  );
}

function deterministicRandom(seed: number): () => number {
  let state = seed >>> 0 || 1;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return state >>> 0;
  };
}

function randomText(next: () => number, maximum: number): string {
  const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}[],:;|&$`\'"/\\ \t\r\n';
  const length = next() % (maximum + 1);
  let value = '';
  for (let index = 0; index < length; index += 1) {
    value += alphabet[next() % alphabet.length];
  }
  return value;
}

describe('TEST-012 deterministic parser property gate', () => {
  it('bounds frame and JSON serialization without accepting invalid envelopes', () => {
    expect(corpus.schema_version).toBe(1);
    const next = deterministicRandom(campaignSeed() ^ 0x4652414d);
    let nonce = 0;
    const identity = new GatewayWsProtocolIdentity(
      () => `test012-${String(++nonce).padStart(16, '0')}`,
    );
    const maximumPayload = corpus.campaign.max_generated_input_bytes - 512;

    for (let caseIndex = 0; caseIndex < campaignCases(); caseIndex += 1) {
      const payload = {
        kind: 'pong',
        padding: randomText(next, maximumPayload),
        protocol_version: 99,
        client_sequence: -1,
        nonce: 'caller-controlled',
      };
      const frame = identity.frame(payload);
      const encoded = JSON.stringify(frame);
      expect(Buffer.byteLength(encoded, 'utf8')).toBeLessThanOrEqual(
        corpus.campaign.max_generated_input_bytes,
      );
      expect(frame.protocol_version).toBe(1);
      expect(frame.client_sequence).toBe(caseIndex + 1);
      expect(frame.nonce).toBe(`test012-${String(caseIndex + 1).padStart(16, '0')}`);
      expect(JSON.parse(encoded)).toEqual(frame);
    }

    for (const invalid of [null, [], 'frame', 7, true]) {
      expect(() => identity.frame(invalid as object)).toThrow(
        'invalid Gateway WebSocket protocol frame',
      );
    }
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(() => identity.encode(cyclic)).toThrow();

    const decision = {
      workspaceId: 'workspace',
      sessionId: 'session',
      taskId: 'task',
      requestId: 'request',
      decision: 'once',
    };
    const cyclicPermissions: Record<string, unknown> = {};
    cyclicPermissions.self = cyclicPermissions;
    expect(SecurityDecisionArgs.parse({
      ...decision,
      permissions: cyclicPermissions,
    }).ok).toBe(false);
    expect(SecurityDecisionArgs.parse({
      ...decision,
      permissions: { value: BigInt(1) },
    }).ok).toBe(false);
    expect(SecurityDecisionArgs.parse({
      ...decision,
      permissions: { value: 'x'.repeat(33 * 1024) },
    }).ok).toBe(false);
  });

  it('rejects generated URL confusion without network access', () => {
    const next = deterministicRandom(campaignSeed() ^ 0x55524c);
    const forbiddenFetch = vi.fn(() => {
      throw new Error('parser attempted network access');
    });
    vi.stubGlobal('fetch', forbiddenFetch);
    try {
      expect(GatewayFetchArgs.parse({
        url: 'http://127.0.0.1:8000/api/sessions',
      }).ok).toBe(true);
      for (const url of corpus.url.desktop_gateway_invalid) {
        expect(GatewayFetchArgs.parse({ url }).ok, url).toBe(false);
      }

      for (let caseIndex = 0; caseIndex < campaignCases(); caseIndex += 1) {
        const label = `case-${caseIndex}-${next().toString(16)}`;
        const hostile = [
          `https://127.0.0.1:8000/api/${label}`,
          `http://${label}.example/api/sessions`,
          `http://user:password@127.0.0.1:8000/api/${label}`,
          `http://127.0.0.1:8000/not-api/${label}`,
          `file:///api/${label}`,
          `http://127.0.0.1:8000/api/${label}#fragment`,
        ][next() % 6];
        expect(Buffer.byteLength(hostile, 'utf8')).toBeLessThanOrEqual(
          corpus.campaign.max_generated_input_bytes,
        );
        expect(GatewayFetchArgs.parse({ url: hostile }).ok, hostile).toBe(false);
      }

      for (const url of [
        'http://updates.example.test/update.exe',
        'https://user:password@updates.example.test/update.exe',
        'https://updates.example.test/update.exe#fragment',
        'file:///tmp/update.exe',
      ]) {
        expect(UpdateDownloadArgs.parse({ url }).ok, url).toBe(false);
      }
      expect(forbiddenFetch).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('bounds path and command inputs without executing command text', () => {
    const next = deterministicRandom(campaignSeed() ^ 0x50415448);

    for (const path of corpus.path.ipc_valid) {
      expect(ShellOpenPathArgs.parse({ path }).ok, path).toBe(true);
    }
    for (const path of corpus.path.ipc_invalid) {
      expect(ShellOpenPathArgs.parse({ path }).ok, String(path)).toBe(false);
    }
    for (let caseIndex = 0; caseIndex < campaignCases(); caseIndex += 1) {
      const invalidPath = [
        `/tmp/case-${caseIndex}\0escape`,
        `/${'x'.repeat(4097)}`,
        next(),
        [caseIndex],
      ][next() % 4];
      expect(ShellOpenPathArgs.parse({ path: invalidPath }).ok).toBe(false);
    }

    const temporary = mkdtempSync(join(tmpdir(), 'ace-test012-'));
    const marker = join(temporary, 'parser-must-not-execute');
    try {
      const commands = [
        ...corpus.command.bash_must_ask,
        ...corpus.command.powershell_must_ask,
        corpus.command.host_execution_templates.powershell.replace('{marker}', marker),
      ];
      const base = {
        workspaceId: 'workspace',
        sessionId: 'session',
        taskId: 'task',
        requestId: 'request',
        decision: 'once',
      };
      for (const command of commands) {
        const parsed = SecurityDecisionArgs.parse({
          ...base,
          alwaysArgvPrefix: ['parser-only-shell', '-Command', command],
        });
        expect(parsed.ok, command).toBe(true);
      }

      for (let caseIndex = 0; caseIndex < campaignCases(); caseIndex += 1) {
        const invalidToken: unknown = [
          '',
          `bad\0token-${caseIndex}`,
          `bad\ncontrol-${caseIndex}`,
          'x'.repeat(4097),
          next(),
        ][next() % 5];
        expect(SecurityDecisionArgs.parse({
          ...base,
          alwaysArgvPrefix: ['parser-only-shell', invalidToken],
        }).ok).toBe(false);
      }
      expect(existsSync(marker)).toBe(false);
    } finally {
      rmSync(temporary, { recursive: true, force: true });
    }
  });
});
