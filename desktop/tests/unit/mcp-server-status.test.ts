import { describe, expect, it } from 'vitest';
import { mcpStatusText, mcpStatusChipClass, mcpTransportLabel } from '../../src/ui/features/settings-mcp';
import type { McpServerRow } from '../../src/ui/backend-client';

function row(over: Partial<McpServerRow>): McpServerRow {
  return {
    name: 'echo',
    transport: 'stdio',
    connected: false,
    error: '',
    tools: [],
    config: {},
    ...over,
  };
}

describe('mcpStatusText', () => {
  it('shows failure message when error present', () => {
    expect(mcpStatusText(row({ error: 'connection refused' }))).toBe('失败：connection refused');
  });

  it('shows connected when connected and no error', () => {
    expect(mcpStatusText(row({ connected: true }))).toBe('已连接');
  });

  it('error takes precedence over connected', () => {
    expect(mcpStatusText(row({ connected: true, error: 'boom' }))).toBe('失败：boom');
  });

  it('shows disconnected when neither', () => {
    expect(mcpStatusText(row({}))).toBe('未连接');
  });
});

describe('mcpStatusChipClass', () => {
  it('error → is-error', () => {
    expect(mcpStatusChipClass(row({ error: 'x' }))).toBe('is-error');
  });

  it('connected → is-online', () => {
    expect(mcpStatusChipClass(row({ connected: true }))).toBe('is-online');
  });

  it('otherwise → is-configured', () => {
    expect(mcpStatusChipClass(row({}))).toBe('is-configured');
  });
});

describe('mcpTransportLabel', () => {
  it('maps known transports to Chinese labels', () => {
    expect(mcpTransportLabel('stdio')).toBe('本地(stdio)');
    expect(mcpTransportLabel('http')).toBe('HTTP');
    expect(mcpTransportLabel('sse')).toBe('SSE');
    expect(mcpTransportLabel('unknown')).toBe('未知');
  });
});
