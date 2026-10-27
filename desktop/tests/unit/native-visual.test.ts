// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import fixture from '../../../tests/fixtures/native-visual.json';
import invalidFixtures from '../../../tests/fixtures/native-visual-invalid.json';
import { bindNativeVisuals, calculate, evaluate, parseVisual, renderNativeVisual, type NativeVisual } from '../../src/ui/features/native-visual';
import { inlineSurfaces } from '../../src/ui/features/inline-surfaces';
import { state } from '../../src/ui/state';
const spec = parseVisual(fixture)!;
const visual: NativeVisual = { key: 'native:s:call', sessionId: 's', spec };
const send = vi.fn(async (_sid: string, _message: string) => true);
function mount(v = visual) { const root = renderNativeVisual(v); document.body.append(root); return root; }
function change(root: HTMLElement, name: string, value: string) { const input = root.querySelector<HTMLInputElement>(`[name="${name}"]`)!; input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); }
beforeEach(() => { document.body.replaceChildren(); localStorage.clear(); send.mockClear(); state.isLoggedIn = true; state.userInfo = { uid: 'a' }; state.activeSessionId = 's'; bindNativeVisuals(send); });
describe('native visual protocol and rendering', () => {
  it('evaluates shared scenarios and bounds invalid arithmetic', () => {
    expect(spec).not.toBeNull();
    expect(calculate(spec, { cost: 7000, income: 12000, initial: 3000 }).map(s => Math.round(s.at(-1)!))).toEqual([21000, 83304, 197657]);
    expect(() => evaluate({ op: 'div', args: [1, 0] }, {}, 1)).toThrow();
    expect(() => evaluate({ op: 'pow', args: [10, 100] }, {}, 1)).toThrow();
  });
  it('rejects shared invalid protocol examples and unbounded input', () => {
    for (const { path, value } of invalidFixtures) {
      const next = structuredClone(fixture);
      let target = next as Record<string, unknown>;
      for (const key of path.slice(0, -1)) target = target[key] as Record<string, unknown>;
      target[path.at(-1)!] = value;
      expect(parseVisual(next)).toBeNull();
    }
    expect(parseVisual({ ...fixture, parameters: Array(9).fill(fixture.parameters[0]) })).toBeNull();
  });
  it('renders no iframe, restores values and isolates accounts and components', () => {
    const root = mount(); change(root, 'income', '12000'); change(root, 'cost', '7000');
    expect(root.querySelector('iframe')).toBeNull(); expect(root.textContent).toContain('83,304');
    expect(send).not.toHaveBeenCalled(); root.remove();
    const restored = mount(); expect(restored.querySelector<HTMLInputElement>('[name="cost"]')!.value).toBe('7000');
    expect(restored.textContent).toContain('83,304');
    expect(mount({ ...visual, key: 'other' }).querySelector<HTMLInputElement>('[name="cost"]')!.value).toBe('6000');
    state.userInfo = { uid: 'b' }; expect(mount().querySelector<HTMLInputElement>('[name="cost"]')!.value).toBe('6000');
  });
  it('does not persist or submit without a stable user identity', () => {
    state.userInfo = { staffName: 'missing id' };
    const root = mount();
    change(root, 'cost', '7000');
    root.querySelector<HTMLButtonElement>('.native-visual__action')!.click();
    expect(localStorage.length).toBe(0);
    expect(send).not.toHaveBeenCalled();
  });
  it('validates, submits readable values once, and rejects cross-session actions', async () => {
    const root = mount(); const action = root.querySelector<HTMLButtonElement>('.native-visual__action')!;
    change(root, 'income', '12000'); change(root, 'cost', '7000');
    change(root, 'initial', '-100'); action.click(); expect(send).not.toHaveBeenCalled(); expect(root.querySelector('svg')).toBeNull();
    change(root, 'initial', '3000'); action.click(); action.click();
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(send.mock.calls[0][1]).toContain('月成本（cost）：7000 元');
    expect(send.mock.calls[0][1]).not.toContain('"parameters"');
    await vi.waitFor(() => expect(action.disabled).toBe(true));
    change(root, 'cost', '7100'); state.activeSessionId = 'elsewhere'; action.click(); expect(send).toHaveBeenCalledTimes(1);
  });
  it('keeps static charts free of controls/actions and supports keyboard and legend inspection', () => {
    const root = mount({ ...visual, spec: { title: '比较', chart: { type: 'bar', x: [1, 2], xLabel: '期', yLabel: '件', series: [{ id: 'a', label: 'A', values: [1, 2] }, { id: 'b', label: 'B', values: [3, 4] }] } } });
    expect(root.querySelector('input')).toBeNull(); expect(root.querySelector('.native-visual__action')).toBeNull();
    root.querySelector('svg')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
    expect(root.querySelector('.native-visual__detail')!.textContent).toContain('A 1');
    root.querySelector<HTMLButtonElement>('.native-visual__legend-item')!.click();
    expect(root.querySelector('.native-visual__detail')!.textContent).not.toContain('A 1');
  });
  it('renders text/select form values safely and never sends automatically', async () => {
    const form = parseVisual({ title: '<img src=x>', parameters: [{ id: 'name', label: '姓名', type: 'text', value: '甲' }, { id: 'mode', label: '方案', type: 'select', value: 'A', options: ['A', 'B'] }], action: { label: '比较', instruction: '比较该方案' } })!;
    const root = mount({ ...visual, spec: form }); expect(root.querySelector('img')).toBeNull();
    change(root, 'name', '<script>bad()</script>'); expect(send).not.toHaveBeenCalled();
    root.querySelector<HTMLButtonElement>('.native-visual__action')!.click();
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1)); expect(send.mock.calls[0][1]).toContain('方案（mode）：A');
  });
  it('only consumes trusted tool envelopes for the rendered session and deduplicates live/history copies', () => {
    const message = { id: 'm', role: 'assistant' as const, content: '', timestamp: 1, toolCalls: [{ toolCallId: 't', name: 'Widget', status: 'done' as const, startedAt: 0, result: JSON.stringify({ ok: true, surface: { version: 2, mode: 'native', display: 'inline', status: 'ready', sessionId: 's', spec } }) }] };
    expect(inlineSurfaces([message, message], 's').visuals).toHaveLength(1);
    expect(inlineSurfaces([message], 'other').visuals).toHaveLength(0);
    message.toolCalls[0].name = 'terminal';
    expect(inlineSurfaces([message], 's').visuals).toHaveLength(0);
  });
});
