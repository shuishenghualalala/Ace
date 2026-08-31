import { describe, expect, it, vi } from 'vitest';
import { WorkspaceRegistry, createDesktopWorkspaceRegistry } from '../../src/ui/features/workspace-registry';

describe('WorkspaceRegistry', () => {
  it('registers the four Desktop workspace domains without centralizing their data', () => {
    const registry = createDesktopWorkspaceRegistry();
    expect(registry.list().map((module) => module.kind)).toEqual([
      'conversation', 'external', 'wiki', 'companion', 'project',
    ]);
    expect(registry.navigation('assistant').map((item) => item.id)).toEqual(['chat', 'agents', 'wiki', 'nearby']);
  });

  it('supports pluggable navigation and deterministic disposal', () => {
    const registry = new WorkspaceRegistry();
    const dispose = vi.fn();
    registry.register({
      id: 'custom',
      kind: 'conversation',
      label: '自定义',
      navigation: { id: 'skills', label: '自定义入口', icon: 'process-skill', productModes: ['assistant'], order: 1 },
      dispose,
    });

    expect(registry.navigation('assistant')[0]?.label).toBe('自定义入口');
    expect(registry.unregister('custom')).toBe(true);
    expect(dispose).toHaveBeenCalledOnce();
    expect(registry.unregister('custom')).toBe(false);
  });

  it('rejects duplicate module ids', () => {
    const registry = new WorkspaceRegistry();
    registry.register({ id: 'duplicate', kind: 'wiki', label: 'Wiki' });
    expect(() => registry.register({ id: 'duplicate', kind: 'wiki', label: 'Wiki' })).toThrow(/already registered/);
  });
});
