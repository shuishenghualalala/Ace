import { describe, expect, it, vi } from 'vitest';
import { createExternalLinkBridge } from '../../src/main/external-link-bridge';

describe('validated external link bridge', () => {
  it('uses the same registered capability for IPC and host navigation', async () => {
    const bridge = createExternalLinkBridge();
    const open = vi.fn().mockResolvedValue(undefined);
    const handler = bridge.register(open);
    await handler({}, { url: 'https://example.com/ipc' });
    await bridge.openUrl('https://example.com/navigation');
    expect(open.mock.calls).toEqual([
      ['https://example.com/ipc'], ['https://example.com/navigation'],
    ]);
  });

  it.each(['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,test', 'not a URL'])(
    'rejects %s through either entry point', async (url) => {
      const bridge = createExternalLinkBridge();
      const open = vi.fn().mockResolvedValue(undefined);
      const handler = bridge.register(open);
      await expect(handler({}, { url })).rejects.toThrow('IPC_ARG_VALIDATION_FAILED');
      await expect(bridge.openUrl(url)).rejects.toThrow('IPC_ARG_VALIDATION_FAILED');
      expect(open).not.toHaveBeenCalled();
    },
  );

  it('does not open anything before the IPC capability has been registered', async () => {
    await expect(createExternalLinkBridge().openUrl('https://example.com')).rejects.toThrow('not registered');
  });
});
