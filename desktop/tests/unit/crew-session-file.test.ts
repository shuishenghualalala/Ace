import { describe, expect, it } from 'vitest';
import path from 'node:path';
import { resolveTaskWorkspaceRoot } from '../../src/main/crew-session-file';

describe('resolveTaskWorkspaceRoot', () => {
  const home = path.resolve('/tmp/ace-custom-home');
  const config = [
    'other:',
    '  task_workspace_root: ignored',
    'runtime:',
    '  crew_home: .Crew',
    '  task_workspace_root: custom/tasks # stored beneath Crew Home',
    'agent:',
    '  task_workspace_root: also-ignored',
  ].join('\n');

  it('uses runtime.task_workspace_root relative to the active Gateway home', () => {
    expect(resolveTaskWorkspaceRoot(home, config, '')).toBe(path.join(home, 'custom', 'tasks'));
  });

  it('gives the environment override precedence and resolves relative to Crew Home', () => {
    expect(resolveTaskWorkspaceRoot(home, config, 'override/tasks')).toBe(path.join(home, 'override', 'tasks'));
  });

  it('defaults to the active Gateway home', () => {
    expect(resolveTaskWorkspaceRoot(home, '', '')).toBe(path.join(home, 'task_workspaces'));
  });
});
