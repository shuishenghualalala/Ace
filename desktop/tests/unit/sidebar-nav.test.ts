import { describe, expect, it } from 'vitest';
import { resolveShellNavigation } from '../../src/ui/features/sidebar-nav';

describe('resolveShellNavigation', () => {
  it('preserves the existing Skills entry in assistant mode', () => {
    const navigation = resolveShellNavigation('assistant', { agents: 'available' });

    expect(navigation.map((item) => item.id)).toContain('skills');
  });

  it('exposes the Security entry in assistant mode', () => {
    const navigation = resolveShellNavigation('assistant', { agents: 'available' });

    expect(navigation.map((item) => item.id)).toContain('security');
  });

  it('exposes Security as a standalone page and removes Audit', () => {
    const ids = resolveShellNavigation('assistant', { agents: 'available' }).map((item) => item.id);

    expect(ids).not.toContain('audit');
  });
});
