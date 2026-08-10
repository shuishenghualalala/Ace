import { describe, expect, it } from 'vitest';
import { resolveShellNavigation } from '../../src/ui/features/sidebar-nav';

describe('resolveShellNavigation', () => {
  it('assistant mode keeps Skills, exposes Inspiration/Security and removes Audit', () => {
    const navigation = resolveShellNavigation('assistant', { agents: 'available' });
    const ids = navigation.map((item) => item.id);

    expect(ids).toContain('skills');
    expect(navigation).toContainEqual(expect.objectContaining({
      id: 'sites',
      label: '灵感',
      featureState: 'available',
    }));
    expect(ids).toContain('security');
    expect(ids).not.toContain('audit');
  });
});
