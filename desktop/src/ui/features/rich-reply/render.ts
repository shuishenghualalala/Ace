import { imageDisplayUrl } from '../../tool-screenshot';
import { state } from '../../state';
import { interactiveOwnerKey } from '../interactive-owner';
import { el, display, evaluate, calculateDataset } from './data';
import { renderField } from './fields';
import { renderChart, hydrateCharts } from './charts';
import { renderTable } from './table';
import { validValue } from './protocol';
import { replyState } from './state';
import type { NativeVisual, ReplyNode, Dataset, Action } from './types';
let submit: (sessionId: string, text: string) => Promise<boolean> = async () => false;
export function bindNativeVisuals(handler: typeof submit): void { submit = handler; }
export function hydrateNativeVisuals(_container: HTMLElement): void { hydrateCharts(); }
export function renderNativeVisual(visual: NativeVisual): HTMLElement {
    const { spec } = visual, { data, save, owner } = replyState(visual), params = spec.parameters ?? [];
    const root = el('section', 'native-visual rich-reply');
    root.setAttribute('aria-label', spec.title);
    root.dataset.replyId = spec.id;
    root.dataset.revision = String(spec.revision);
    root.append(el('h3', 'rich-reply__title', spec.title));
    if (spec.description)
        root.append(el('p', 'rich-reply__muted', spec.description));
    const form = el('form', 'rich-reply__body');
    form.noValidate = true;
    form.addEventListener('submit', e => e.preventDefault());
    const error = el('p', 'rich-reply__error');
    error.setAttribute('role', 'alert');
    const status = el('p', 'rich-reply__muted');
    status.setAttribute('role', 'status');
    const history = el('details', 'rich-reply__history'), historyList = el('ol');
    history.append(el('summary', '', `操作记录 · 修订 ${spec.revision}`), historyList);
    let pending = false, invalid = false;
    const buttons: {
        node: HTMLButtonElement;
        id: string;
        action: Action;
    }[] = [], syncs: (() => void)[] = [], updates: ((datasets: Map<string, Dataset['rows']>) => void)[] = [];
    const allowed = (): boolean => !!owner && owner === interactiveOwnerKey() && state.activeSessionId === visual.sessionId && root.isConnected && !visual.readOnly;
    const saveState = (): void => { if (!visual.readOnly && !save())
        status.textContent = '输入未能保存到此设备'; };
    const audit = (text: string): void => { data.events.push(`${new Date().toLocaleTimeString('zh-CN')} ${text}`); data.events = data.events.slice(-20); drawHistory(); saveState(); };
    const drawHistory = (): void => { historyList.replaceChildren(...data.events.map(s => el('li', '', s))); history.hidden = !data.events.length; };
    const token = (id: string): string => JSON.stringify([spec.revision, id, data.values]);
    const updateButtons = (): void => { for (const b of buttons)
        b.node.disabled = !!visual.readOnly || pending || (b.action.type === 'submit' && (invalid || data.sent.includes(token(b.id)))); };
    const refresh = (): void => {
        invalid = params.some(p => !validValue(p, data.values[p.id]));
        error.textContent = invalid ? '请检查输入范围和格式' : '';
        const datasets = new Map<string, Dataset['rows']>();
        try {
            if (invalid)
                throw Error(error.textContent);
            for (const d of spec.datasets ?? [])
                datasets.set(d.id, calculateDataset(d, data.values));
            for (const update of updates)
                update(datasets);
        }
        catch (cause) {
            invalid = true;
            error.textContent = cause instanceof Error ? cause.message : '无法计算';
            for (const update of updates)
                try {
                    update(new Map());
                }
                catch { /* Clear invalid outputs. */ }
        }
        updateButtons();
    };
    const changed = (): void => { if (!allowed())
        return; status.textContent = ''; refresh(); saveState(); };
    const execute = (id: string, label: string, action: Action): void => {
        if (!allowed() || pending)
            return;
        if (action.type === 'set' || action.type === 'reset') {
            const values = action.type === 'set' ? action.values : Object.fromEntries(params.map(p => [p.id, p.value]));
            for (const [key, value] of Object.entries(values))
                data.values[key] = structuredClone(value);
            syncs.forEach(fn => fn());
            refresh();
            audit(label);
            status.textContent = '已更新';
            return;
        }
        refresh();
        const missing = params.filter(p => !validValue(p, data.values[p.id], true));
        if (invalid || missing.length) {
            error.textContent = missing.length ? `请填写或确认：${missing.map(p => p.label).join('、')}` : error.textContent;
            return;
        }
        const snapshot = token(id);
        if (data.sent.includes(snapshot))
            return;
        const lines = params.map(p => `${p.label}：${display(data.values[p.id])}${p.unit ? ` ${p.unit}` : ''}`);
        const message = `${action.instruction}\n\n「${spec.title}」· ${label}\n${lines.join('\n')}`;
        pending = true;
        status.textContent = '正在提交…';
        updateButtons();
        void Promise.resolve().then(() => allowed() ? submit(visual.sessionId, message) : false).then(sent => {
            if (owner !== interactiveOwnerKey())
                return;
            if (sent) {
                data.sent.push(snapshot);
                data.sent = data.sent.slice(-20);
                audit(`已提交「${label}」`);
            }
            status.textContent = sent ? '已提交' : '提交失败，请重试';
        }).catch(() => { if (owner === interactiveOwnerKey())
            status.textContent = '提交失败，请重试'; }).finally(() => { pending = false; updateButtons(); });
    };
    const render = (n: ReplyNode): HTMLElement => {
        let node: HTMLElement;
        if ('children' in n) {
            node = el(n.type === 'card' ? 'section' : 'div', `rich-reply__${n.type}`);
            if (n.title)
                node.append(el('h4', '', n.title));
            for (const child of n.children)
                node.append(render(child));
        }
        else
            switch (n.type) {
                case 'text':
                    node = el('p', 'rich-reply__text', n.text);
                    break;
                case 'badge':
                    node = el('span', `rich-reply__badge rich-reply__badge--${n.tone ?? 'neutral'}`, n.text);
                    break;
                case 'image': {
                    const image = el('img', 'rich-reply__image');
                    image.src = imageDisplayUrl(n.src);
                    image.alt = n.alt;
                    image.loading = 'lazy';
                    image.referrerPolicy = 'no-referrer';
                    image.addEventListener('error', () => { const fallback = el('span', 'rich-reply__muted', `图片未能加载：${n.alt}`); image.replaceWith(fallback); }, { once: true });
                    node = image;
                    break;
                }
                case 'metric': {
                    node = el('div', 'rich-reply__metric');
                    const value = el('strong');
                    node.append(el('span', 'rich-reply__muted', n.label), value);
                    updates.push(ds => { value.textContent = !invalid && (ds.size || (spec.datasets ?? []).length === 0) ? `${display(evaluate(n.expression, data.values))}${n.unit ?? ''}` : '—'; });
                    break;
                }
                case 'field': {
                    const field = renderField(params.find(p => p.id === n.parameter)!, data.values, changed, allowed);
                    node = field.node;
                    syncs.push(field.sync);
                    if (visual.readOnly)
                        (node as HTMLFieldSetElement).disabled = true;
                    break;
                }
                case 'button': {
                    const button = el('button', 'rich-reply__button', n.label);
                    button.type = 'button';
                    button.dataset.action = n.action.type;
                    button.addEventListener('click', () => execute(n.id, n.label, n.action));
                    buttons.push({ node: button, id: n.id, action: n.action });
                    node = button;
                    break;
                }
                case 'chart': {
                    const d = spec.datasets!.find(d => d.id === n.dataset)!;
                    const hidden = data.hidden[n.id] ??= [];
                    hidden.splice(0, hidden.length, ...hidden.filter(id => n.series.some(s => s.column === id)));
                    if (hidden.length >= n.series.length)
                        hidden.length = 0;
                    const chart = renderChart(n, d, hidden, saveState);
                    node = el('div');
                    if (n.title)
                        node.append(el('h4', '', n.title));
                    node.append(chart.node);
                    updates.push(ds => chart.update(ds.get(n.dataset) ?? []));
                    break;
                }
                case 'table': {
                    const d = spec.datasets!.find(d => d.id === n.dataset)!, title = n.title ?? '数据明细';
                    const view = data.views[n.id] ??= { search: '', sort: '', descending: false, page: 0 };
                    const table = renderTable(d, title, n.pageSize ?? 10, view, saveState);
                    node = el('div');
                    node.append(el('h4', '', title), table.node);
                    updates.push(ds => table.update(ds.get(n.dataset) ?? []));
                    break;
                }
            }
        node.dataset.nodeId = n.id;
        return node;
    };
    for (const node of spec.children)
        form.append(render(node));
    if (visual.readOnly)
        status.textContent = `历史快照 · 修订 ${spec.revision}`;
    root.append(form, error, status, history);
    refresh();
    drawHistory();
    return root;
}
