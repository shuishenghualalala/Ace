// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import fixture from '../../../tests/fixtures/native-visual.json';
import invalidFixtures from '../../../tests/fixtures/native-visual-invalid.json';
import { bindNativeVisuals, parseVisual, renderNativeVisual, type NativeVisual } from '../../src/ui/features/native-visual';
import { calculateDataset, evaluate } from '../../src/ui/features/rich-reply/data';
import { chartOption, hydrateCharts, renderChart } from '../../src/ui/features/rich-reply/charts';
import { replyState } from '../../src/ui/features/rich-reply/state';
import type { ChartNode, ReplySpec } from '../../src/ui/features/rich-reply/types';
import { inlineSurfaces } from '../../src/ui/features/inline-surfaces';
import { state } from '../../src/ui/state';
const chart = vi.hoisted(() => ({ resize: vi.fn(), setOption: vi.fn(), clear: vi.fn(), dispose: vi.fn(), on: vi.fn() }));
vi.mock('echarts/core', () => ({ init: vi.fn(() => chart), use: vi.fn() }));
const spec = parseVisual(fixture)!;
const visual: NativeVisual = { key: 'native:s:call', sessionId: 's', spec };
const send = vi.fn(async (_s: string, _m: string) => true);
function mount(v = visual) { const root = renderNativeVisual(v); document.body.append(root); return root; }
function change(root: HTMLElement, name: string, value: string | boolean) { const input = root.querySelector<HTMLInputElement>(`[name="${name}"]`)!; if (typeof value === 'boolean')
    input.checked = value;
else
    input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); }
function action(root: HTMLElement, id: string) { root.querySelector<HTMLButtonElement>(`[data-node-id="${id}"]`)!.click(); }
beforeEach(() => { document.body.replaceChildren(); localStorage.clear(); send.mockClear(); state.isLoggedIn = true; state.userInfo = { uid: 'a' }; state.activeSessionId = 's'; bindNativeVisuals(send); });
describe('v3 rich replies', () => {
    it('shares validation fixtures with the backend and bounds arithmetic', () => {
        expect(spec).not.toBeNull();
        for (const item of invalidFixtures) {
            const next = structuredClone(fixture);
            let target = next as unknown as Record<string, unknown>;
            for (const key of item.path.slice(0, -1))
                target = target[key] as Record<string, unknown>;
            target[item.path.at(-1)!] = item.value;
            expect(parseVisual(next), item.name).toBeNull();
        }
        const rows = calculateDataset(spec.datasets![0], { income: 12000, cost: 7000 });
        expect(rows.at(-1)).toEqual([12, 57000]);
        expect(() => evaluate({ op: 'div', args: [1, 0] }, {})).toThrow();
        expect(() => evaluate({ op: 'pow', args: [10, 100] }, {})).toThrow();
    });
    it('updates cards, metrics and tables locally, restores state, and isolates identities', () => {
        const root = mount();
        action(root, 'pick2');
        expect(root.querySelector<HTMLInputElement>('[name="income"]')!.value).toBe('12000');
        expect(root.textContent).toContain('6,000元');
        expect(root.querySelector('iframe')).toBeNull();
        expect(send).not.toHaveBeenCalled();
        root.remove();
        expect(mount().querySelector<HTMLInputElement>('[name="income"]')!.value).toBe('12000');
        state.userInfo = { uid: 'b' };
        expect(mount().querySelector<HTMLInputElement>('[name="income"]')!.value).toBe('8000');
    });
    it('validates required date/checkbox, retries failure and prevents duplicate sends after remount', async () => {
        const root = mount();
        action(root, 'submit');
        expect(send).not.toHaveBeenCalled();
        expect(root.textContent).toContain('开始日期、确认假设');
        change(root, 'start', '2026-11-01');
        change(root, 'confirm', true);
        change(root, 'notes', '<script>unsafe()</script>');
        send.mockResolvedValueOnce(false);
        action(root, 'submit');
        await vi.waitFor(() => expect(root.textContent).toContain('提交失败'));
        action(root, 'submit');
        action(root, 'submit');
        await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(2));
        await vi.waitFor(() => expect(root.textContent).toContain('已提交「'));
        expect(send.mock.calls[1][1]).toContain('开始日期：2026-11-01');
        expect(root.querySelector('script')).toBeNull();
        root.remove();
        const restored = mount();
        action(restored, 'submit');
        expect(send).toHaveBeenCalledTimes(2);
    });
    it('rejects stale, detached, logged-out and cross-session actions', () => {
        const root = mount();
        state.activeSessionId = 'other';
        action(root, 'pick2');
        expect(root.querySelector<HTMLInputElement>('[name="income"]')!.value).toBe('8000');
        state.activeSessionId = 's';
        state.userInfo = { uid: 'other' };
        action(root, 'submit');
        expect(send).not.toHaveBeenCalled();
        root.remove();
        action(root, 'pick2');
        expect(mount({ ...visual, readOnly: true }).querySelector<HTMLButtonElement>('[data-node-id="pick2"]')!.disabled).toBe(true);
    });
    it('sorts numerically, filters and pages only the selected table and restores view state', () => {
        const root = mount();
        const search = root.querySelector<HTMLInputElement>('input[type="search"]')!;
        const table = root.querySelector('table')!;
        expect(table.querySelectorAll('tbody tr')).toHaveLength(5);
        const sort = root.querySelector<HTMLButtonElement>('[aria-label="按累计结余排序"]')!;
        sort.click();
        sort.click();
        expect(table.querySelector('tbody tr')!.textContent).toBe('1221,000');
        search.value = '21,000';
        search.dispatchEvent(new Event('input'));
        expect(table.querySelectorAll('tbody tr')).toHaveLength(1);
        expect(send).not.toHaveBeenCalled();
        root.remove();
        expect(mount().querySelector<HTMLInputElement>('input[type="search"]')!.value).toBe('21,000');
    });
    it('maps all chart types without exposing executable library options', () => {
        const d = spec.datasets![0], rows = calculateDataset(d, { income: 8000, cost: 6000 });
        for (const type of ['line', 'bar', 'area', 'stackedBar', 'stackedArea', 'scatter', 'pie', 'donut'] as ChartNode['chartType'][]) {
            const n: ChartNode = { id: 'c', type: 'chart', dataset: d.id, chartType: type, x: 'month', series: [{ column: 'net' }] };
            const safeRows = rows.map(r => [r[0], Math.abs(Number(r[1]))]);
            const option = chartOption(n, d, safeRows, [], ['red'], 'black', 'gray');
            expect(option.animation).toBe(false);
            expect((option.tooltip as {
                renderMode: string;
            }).renderMode).toBe('richText');
            expect(option.series).toHaveLength(1);
        }
    });
    it('only accepts trusted envelopes and updates the highest revision of the intended component', () => {
        const msg = (call: string, s: ReplySpec) => ({ id: call, role: 'assistant' as const, content: '', timestamp: 1, toolCalls: [{ toolCallId: call, name: 'Widget', status: 'done' as const, startedAt: 0, result: JSON.stringify({ ok: true, surface: { version: 3, mode: 'native', display: 'inline', status: 'ready', sessionId: 's', spec: s } }) }] });
        const a = msg('a', spec), b = msg('b', { ...spec, revision: 2, title: '更新' });
        expect(inlineSurfaces([a, b, a], 's').visuals).toHaveLength(1);
        expect(inlineSurfaces([a, b, a], 's').visuals[0].spec.title).toBe('更新');
        expect(inlineSurfaces([a], 'other').visuals).toHaveLength(0);
        a.toolCalls[0].name = 'terminal';
        expect(inlineSurfaces([a], 's').visuals).toHaveLength(0);
    });
    it('keeps chart colors stable when a series is hidden and infers category/date axes', () => {
        const d = spec.datasets![0];
        const n: ChartNode = { id: 'c', type: 'chart', dataset: d.id, chartType: 'line', x: 'month', series: [{ column: 'month' }, { column: 'net' }] };
        const option = chartOption(n, d, [[1, 2]], ['month'], ['red', 'blue'], 'black', 'gray');
        expect(option.series).toEqual([expect.objectContaining({ itemStyle: { color: 'blue' } })]);
        for (const [type, expected] of [['string', 'category'], ['date', 'time']] as const) {
            const dataset = { ...d, columns: d.columns.map((c, i) => i === 0 ? { ...c, type } : c) };
            const dateOption = chartOption(n, dataset, [['2026-11-01', 2]], [], ['red'], 'black', 'gray');
            expect(dateOption.xAxis).toMatchObject({ type: expected });
            expect(dateOption.aria).toMatchObject({ label: { description: expect.stringContaining('2026-11-01') } });
            expect(JSON.stringify(dateOption.aria)).not.toContain('NaN');
        }
    });
    it('does not redraw mounted charts on unrelated streaming hydration and disposes detached charts', () => {
        hydrateCharts();
        const d = spec.datasets![0];
        const n: ChartNode = { id: 'c', type: 'chart', dataset: d.id, chartType: 'area', x: 'month', series: [{ column: 'net' }] };
        const rendered = renderChart(n, d, [], () => {});
        rendered.update([[1, 2000]]);
        document.body.append(rendered.node);
        hydrateCharts();
        const draws = chart.setOption.mock.calls.length;
        hydrateCharts();
        hydrateCharts();
        expect(chart.setOption.mock.calls.length).toBe(draws);
        rendered.update([[1, 5000]]);
        expect(chart.setOption.mock.calls.length).toBe(draws + 1);
        const disposals = chart.dispose.mock.calls.length;
        rendered.node.remove();
        hydrateCharts();
        expect(chart.dispose.mock.calls.length).toBe(disposals + 1);
    });
    it('stores valid IDs that coincide with Object prototype property names', () => {
        const saved = replyState(visual);
        expect(saved.data.views.toString).toBeUndefined();
        expect(saved.data.hidden.valueOf).toBeUndefined();
        saved.data.views.toString = { search: '12', sort: 'month', descending: true, page: 0 };
        saved.data.hidden.valueOf = ['net'];
        expect(saved.save()).toBe(true);
        expect(replyState(visual).data.views.toString).toEqual(saved.data.views.toString);
        expect(replyState(visual).data.hidden.valueOf).toEqual(['net']);
    });
});
