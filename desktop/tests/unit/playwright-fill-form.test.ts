import { describe, expect, it, vi } from 'vitest';

import {
  ActionError,
  fillForm,
  type ActionContext,
  type FillFormField,
} from '../../src/main/browser/playwright-actions';

import type { Locator, Page } from '../../src/main/browser/playwright-compat';
import type { RefRecord } from '../../src/main/browser/playwright-snapshot';
import { attestedMaterial, materialState } from './helpers/playwright-material';

interface TargetSpec {
  selector: string;
  role: string;
  name: string;
  tag: string;
  inputType: string;
  actionKind: string;
  contentEditable?: boolean;
  option?: { label: string; value: string };
  optionMatches?: number;
  checked?: boolean;
  missingSecurity?: boolean;
  minimumDispatchTimeout?: number;
  onMutation?: () => void;
  mutationError?: Error;
  count?: number | (() => number);
}

function formFixture(specs: TargetSpec[]): {
  ctx: ActionContext;
  calls: string[];
  state: Map<string, string>;
  locators: Map<string, Locator>;
} {
  const calls: string[] = [];
  const state = new Map(specs.map(({ selector }) => [selector, `safe:${selector}`]));
  const hash = (value: string): string => `hash:${value}`;
  const locators = new Map<string, Locator>();
  const refs = new Map<string, RefRecord>();

  specs.forEach((spec, index) => {
    const ref = `@e${index + 1}`;
    const locator = {
      normalize: vi.fn(async () => {
        calls.push(`normalize:${spec.selector}`);
        return locator;
      }),
      count: vi.fn(async () => {
        calls.push(`count:${spec.selector}`);
        return typeof spec.count === 'function' ? spec.count() : (spec.count ?? 1);
      }),
      evaluate: vi.fn(async (
        _callback: unknown,
        argument?: { selectBy?: 'label' | 'value'; value?: string },
      ) => {
        if (argument?.selectBy) {
          calls.push(`option:${spec.selector}:${argument.selectBy}`);
          if (!spec.option) return 0;
          const candidate = argument.selectBy === 'label'
            ? spec.option.label
            : spec.option.value;
          return candidate === argument.value ? (spec.optionMatches ?? 1) : 0;
        }
        calls.push(`fingerprint:${spec.selector}`);
        return materialState(state.get(spec.selector) ?? '', {
          actionKind: spec.actionKind,
          accessibleRole: spec.role,
          accessibleName: spec.name,
          tag: spec.tag,
          inputType: spec.inputType,
          contentEditable: Boolean(spec.contentEditable),
        });
      }),
      ariaSnapshot: vi.fn(async () => {
        calls.push(`aria:${spec.selector}`);
        return `- ${spec.role} ${JSON.stringify(spec.name)}`;
      }),
      waitFor: vi.fn(async () => {
        calls.push(`visible:${spec.selector}`);
      }),
      isEnabled: vi.fn(async () => true),
      isEditable: vi.fn(async () => true),
      isChecked: vi.fn(async () => Boolean(spec.checked)),
      fill: vi.fn(async (_value: string, options?: { timeout?: number }) => {
        calls.push(`mutate:fill:${spec.selector}`);
        if ((options?.timeout ?? 0) < (spec.minimumDispatchTimeout ?? 0)) {
          throw new Error('simulated control needs a longer dispatch timeout');
        }
        spec.onMutation?.();
        if (spec.mutationError) throw spec.mutationError;
      }),
      selectOption: vi.fn(async (value: unknown) => {
        calls.push(`mutate:select:${spec.selector}:${JSON.stringify(value)}`);
        spec.onMutation?.();
        if (spec.mutationError) throw spec.mutationError;
        return [];
      }),
      setChecked: vi.fn(async (
        value: boolean,
        options?: { trial?: boolean },
      ) => {
        if (options?.trial) {
          calls.push(`trial:check:${spec.selector}:${String(value)}`);
          return;
        }
        calls.push(`mutate:check:${spec.selector}:${String(value)}`);
        spec.onMutation?.();
        if (spec.mutationError) throw spec.mutationError;
      }),
    } as unknown as Locator;
    locators.set(spec.selector, locator);
    refs.set(ref, {
      selector: spec.selector,
      playwrightRef: '',
      role: spec.role,
      name: spec.name,
      securityKey: `key:${index + 1}`,
      security: spec.missingSecurity
        ? ''
        : hash(attestedMaterial(state.get(spec.selector) ?? '', {
          tag: spec.tag,
          inputType: spec.inputType,
          contentEditable: Boolean(spec.contentEditable),
        })),
      navigation: '',
      downloadNavigation: '',
      action: '',
      actionKind: spec.actionKind,
      semanticRole: spec.role,
      semanticName: spec.name,
      documentBaseURI: 'https://example.test/',
      documentURL: 'https://example.test/',
      tag: spec.tag,
      inputType: spec.inputType,
      contentEditable: Boolean(spec.contentEditable),
      fieldTier: 'plain',
    });
  });

  const page = {
    locator: (selector: string) => {
      const locator = locators.get(selector);
      if (!locator) throw new Error(`unknown selector: ${selector}`);
      return locator;
    },
  } as unknown as Page;
  return {
    ctx: { page, refs, hash, timeoutMs: 10_000 },
    calls,
    state,
    locators,
  };
}

describe('Playwright typed batch form', () => {
  it('resolves and acts on each exact target in caller order', async () => {
    const fixture = formFixture([
      {
        selector: '#title',
        role: 'textbox',
        name: 'Title',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
      },
      {
        selector: '#bio',
        role: 'generic',
        name: '',
        tag: 'div',
        inputType: '',
        actionKind: 'input',
        contentEditable: true,
      },
      {
        selector: '#country',
        role: 'combobox',
        name: 'Country',
        tag: 'select',
        inputType: '',
        actionKind: 'select',
        option: { label: 'China', value: 'cn' },
      },
      {
        selector: '#terms',
        role: 'checkbox',
        name: 'Terms',
        tag: 'input',
        inputType: 'checkbox',
        actionKind: 'toggle',
      },
      {
        selector: '#volume',
        role: 'slider',
        name: 'Volume',
        tag: 'input',
        inputType: 'range',
        actionKind: 'input',
        missingSecurity: true,
      },
    ]);
    const fields: FillFormField[] = [
      { type: 'textbox', ref: '@e1', value: 'private-title' },
      { type: 'textbox', ref: '@e2', value: 'private-bio' },
      {
        type: 'combobox',
        ref: '@e3',
        value: 'China',
        selectBy: 'label',
      },
      { type: 'checkbox', ref: '@e4', value: true },
      { type: 'slider', ref: '@e5', value: '75' },
    ];

    await expect(fillForm(fixture.ctx, fields)).resolves.toEqual({ completedCount: 5 });

    expect(fixture.calls.filter((call) => call.startsWith('mutate:'))).toEqual([
      'mutate:fill:#title',
      'mutate:fill:#bio',
      'mutate:select:#country:{"label":"China"}',
      'mutate:check:#terms:true',
      'mutate:fill:#volume',
    ]);
    expect(fixture.calls).not.toContain(expect.stringContaining('normalize:'));
    expect(fixture.calls).not.toContain(expect.stringContaining('fingerprint:'));
    expect(fixture.calls).not.toContain(expect.stringContaining('visible:'));
    expect(fixture.calls).not.toContain(expect.stringContaining('trial:'));
    expect(fixture.calls).toContain('mutate:select:#country:{"label":"China"}');
    expect(fixture.calls).toContain('mutate:fill:#volume');
    expect(fixture.calls).not.toContain(expect.stringContaining('press'));
  });

  it('reports prior completion when Playwright strict resolution rejects a later field', async () => {
    const fixture = formFixture([
      {
        selector: '#first',
        role: 'textbox',
        name: 'First',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
      },
      {
        selector: '#country',
        role: 'combobox',
        name: 'Country',
        tag: 'select',
        inputType: '',
        actionKind: 'select',
        option: { label: 'China', value: 'cn' },
        mutationError: new Error(
          'locator.selectOption: Error: strict mode violation: locator resolved to 0 elements',
        ),
      },
    ]);

    const error = await fillForm(fixture.ctx, [
      { type: 'textbox', ref: '@e1', value: 'must-not-run' },
      { type: 'combobox', ref: '@e2', value: 'missing', selectBy: 'value' },
    ]).catch((reason) => reason);

    expect(error).toMatchObject({
      code: 'stale_ref',
      completedCount: 1,
      partial: true,
      uncertain: false,
    });
    expect(fixture.calls).toContain('mutate:fill:#first');
    // The official Locator API was invoked, but Playwright proved strict
    // resolution failed before any select/input/change dispatch.
    expect(fixture.calls).toContain('mutate:select:#country:{"value":"missing"}');
    expect(fixture.ctx.refs.size).toBe(2);
  });

  it('resolves persisted selectors in order so one field can reveal the next', async () => {
    let dependentVisible = false;
    const fixture = formFixture([
      {
        selector: '#kind',
        role: 'combobox',
        name: 'Kind',
        tag: 'select',
        inputType: '',
        actionKind: 'select',
        onMutation: () => {
          dependentVisible = true;
        },
      },
      {
        selector: '#dependent',
        role: 'textbox',
        name: 'Dependent detail',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
        count: () => dependentVisible ? 1 : 0,
      },
    ]);

    await expect(fillForm(fixture.ctx, [
      { type: 'combobox', selector: '#kind', value: 'custom', selectBy: 'value' },
      { type: 'textbox', selector: '#dependent', value: 'now available' },
    ])).resolves.toEqual({ completedCount: 2 });

    expect(fixture.calls.filter((call) => call.startsWith('mutate:'))).toEqual([
      'mutate:select:#kind:{"value":"custom"}',
      'mutate:fill:#dependent',
    ]);
  });

  it('reports confirmed completion count without echoing private values or field identity', async () => {
    const second: TargetSpec = {
      selector: '#secret-second',
      role: 'textbox',
      name: 'Private account identifier',
      tag: 'input',
      inputType: 'text',
      actionKind: 'input',
    };
    const fixture = formFixture([
      {
        selector: '#first',
        role: 'textbox',
        name: 'First',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
        onMutation: () => {
          second.mutationError = new Error('renderer detached during second fill');
        },
      },
      second,
    ]);

    const error = await fillForm(fixture.ctx, [
      { type: 'textbox', ref: '@e1', value: 'first-private-value' },
      { type: 'textbox', ref: '@e2', value: 'second-private-value' },
    ]).catch((reason) => reason as ActionError);

    expect(error).toMatchObject({
      code: 'input_uncertain',
      completedCount: 1,
      phase: 'dispatching',
      partial: true,
      uncertain: true,
    });
    expect(error.message).toContain('1/2');
    expect(error.message).not.toContain('first-private-value');
    expect(error.message).not.toContain('second-private-value');
    expect(error.message).not.toContain('Private account identifier');
    expect(error.message).not.toContain('#secret-second');
  });

  it('marks a failing current dispatch uncertain and excludes it from completedCount', async () => {
    const fixture = formFixture([
      {
        selector: '#unstable',
        role: 'textbox',
        name: 'Unstable',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
        mutationError: new Error('Timeout after input event'),
      },
    ]);

    const error = await fillForm(fixture.ctx, [
      { type: 'textbox', ref: '@e1', value: 'private' },
    ]).catch((reason) => reason as ActionError);

    expect(error).toMatchObject({
      code: 'input_uncertain',
      completedCount: 0,
      phase: 'dispatching',
      partial: true,
      uncertain: true,
    });
  });

  it('delegates radio uncheck semantics to Playwright setChecked without a trial pass', async () => {
    const fixture = formFixture([
      {
        selector: '#name',
        role: 'textbox',
        name: 'Name',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
      },
      {
        selector: '#choice',
        role: 'radio',
        name: 'Choice',
        tag: 'input',
        inputType: 'radio',
        actionKind: 'toggle',
        checked: true,
      },
    ]);

    await expect(fillForm(fixture.ctx, [
      { type: 'textbox', ref: '@e1', value: 'must-not-run' },
      { type: 'radio', ref: '@e2', value: false },
    ])).resolves.toEqual({ completedCount: 2 });
    expect(fixture.calls).toContain('mutate:fill:#name');
    expect(fixture.calls).toContain('mutate:check:#choice:false');
    expect(fixture.calls).not.toContain(expect.stringContaining('trial:'));
    expect(fixture.ctx.refs.size).toBe(2);
  });

  it('allows duplicate option labels and leaves deterministic choice to Playwright', async () => {
    const fixture = formFixture([
      {
        selector: '#duplicate-labels',
        role: 'combobox',
        name: 'Region',
        tag: 'select',
        inputType: '',
        actionKind: 'select',
        option: { label: 'Same label', value: 'first' },
        optionMatches: 2,
      },
    ]);

    await expect(fillForm(fixture.ctx, [{
      type: 'combobox',
      ref: '@e1',
      value: 'Same label',
      selectBy: 'label',
    }])).resolves.toEqual({ completedCount: 1 });
    expect(fixture.calls).toContain(
      'mutate:select:#duplicate-labels:{"label":"Same label"}',
    );
  });

  it('preserves an empty option value instead of treating it as missing', async () => {
    const fixture = formFixture([
      {
        selector: '#empty-option',
        role: 'combobox',
        name: 'Optional region',
        tag: 'select',
        inputType: '',
        actionKind: 'select',
        option: { label: 'None', value: '' },
      },
    ]);

    await expect(fillForm(fixture.ctx, [{
      type: 'combobox',
      ref: '@e1',
      value: '',
      selectBy: 'value',
    }])).resolves.toEqual({ completedCount: 1 });

    expect(fixture.calls).toContain(
      'mutate:select:#empty-option:{"value":""}',
    );
  });

  it('gives a control more than the old two-second dispatch window', async () => {
    const fixture = formFixture([
      {
        selector: '#slow-control',
        role: 'textbox',
        name: 'Slow control',
        tag: 'input',
        inputType: 'text',
        actionKind: 'input',
        minimumDispatchTimeout: 3_000,
      },
    ]);

    await expect(fillForm(fixture.ctx, [
      { type: 'textbox', ref: '@e1', value: 'eventually accepted' },
    ])).resolves.toEqual({ completedCount: 1 });
    expect(fixture.locators.get('#slow-control')?.fill).toHaveBeenCalledWith(
      'eventually accepted',
      { timeout: 10_000 },
    );
  });
});
