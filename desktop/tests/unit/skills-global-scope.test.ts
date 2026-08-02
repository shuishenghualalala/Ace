import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { skillInitial } from '../../src/ui/features/skills-page';

const source = readFileSync(
  new URL('../../src/ui/features/skills-page.ts', import.meta.url),
  'utf8',
);

describe('global Skill management copy', () => {
  it('states host-wide impact before install and uninstall', () => {
    expect(source).toContain('技能是本机全局共享能力');
    expect(source).toContain('安装结果对本机所有登录账号生效');
    expect(source).toContain('本机所有账号都将无法再通过');
    expect(source).toContain("confirmText: '全局安装'");
    expect(source).toContain("confirmText: '全局卸载'");
  });

  it('surfaces local skills with a local badge and symlink install note', () => {
    expect(source).toContain("source: 'local'");
    expect(source).toContain('skill-card-v3__badge--local');
    expect(source).toContain('~/.agents/skills');
    expect(source).toContain('将以软链方式安装');
  });

  it('creates a stable letter avatar from Chinese, English, numeric, and empty names', () => {
    expect(skillInitial('通用 PPT 模板助手')).toBe('通');
    expect(skillInitial('browser control')).toBe('B');
    expect(skillInitial('2026 PDF tools')).toBe('P');
    expect(skillInitial('  ')).toBe('S');
  });
});
