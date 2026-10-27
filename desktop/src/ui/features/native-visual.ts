import type { ChatMessage } from '../chat-render';
import { state } from '../state';

type Expr = number | { param: string } | { x: true } | { op: string; args: [Expr, Expr] };
interface Parameter { id: string; label: string; type: 'range' | 'number' | 'text' | 'select'; value: number | string; min?: number; max?: number; step?: number; unit?: string; options?: string[] }
interface Series { id: string; label: string; values?: number[]; expression?: Expr; cumulative?: boolean; initial?: Expr }
export interface VisualSpec {
  title: string; description?: string; parameters?: Parameter[];
  chart?: { type: 'line' | 'bar'; x: number[]; xLabel: string; yLabel: string; series: Series[]; showTotals?: boolean };
  action?: { label: string; instruction: string };
}
export interface NativeVisual { key: string; sessionId: string; spec: VisualSpec }
const ops = ['add', 'sub', 'mul', 'div', 'pow', 'min', 'max'];
const numeric = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 1e12;
const text = (v: unknown, max = 200): v is string => typeof v === 'string' && v.length > 0 && v.length <= max;
const id = (v: unknown): v is string => text(v, 40) && /^[a-zA-Z][a-zA-Z0-9_]*$/.test(v) && !['constructor', 'prototype', '__proto__'].includes(v);
const object = (v: unknown, keys: string[]): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).every(k => keys.includes(k));

/** Validate history and tool results as untrusted data, never executable code. */
export function parseVisual(value: unknown): VisualSpec | null {
  try {
    if (new TextEncoder().encode(JSON.stringify(value)).length > 64000 || !object(value, ['title', 'description', 'parameters', 'chart', 'action']) || !text(value.title)) return null;
    if (value.description !== undefined && !text(value.description, 1000)) return null;
    const params = value.parameters ?? [];
    if (!Array.isArray(params) || params.length > 8) return null;
    const ids = new Set<string>(); const numbers = new Set<string>();
    for (const p of params) {
      if (!object(p, ['id', 'label', 'type', 'value', 'min', 'max', 'step', 'unit', 'options']) || !id(p.id) || ids.has(p.id) || !text(p.label) || !['range', 'number', 'text', 'select'].includes(String(p.type))) return null;
      ids.add(p.id);
      if (p.unit !== undefined && !text(p.unit, 30)) return null;
      if (p.type === 'range' || p.type === 'number') {
        if (!numeric(p.value) || !numeric(p.min) || !numeric(p.max) || !numeric(p.step) || p.step <= 0 || p.min >= p.max || p.value < p.min || p.value > p.max || p.options !== undefined) return null;
        numbers.add(p.id);
      } else {
        if (!text(p.value, 1000) || ['min', 'max', 'step'].some(k => p[k] !== undefined)) return null;
        if (p.type === 'select') {
          if (!Array.isArray(p.options) || !p.options.length || p.options.length > 20 || !p.options.every(v => text(v)) || new Set(p.options).size !== p.options.length || !p.options.includes(p.value)) return null;
        } else if (p.options !== undefined) return null;
      }
    }
    const expr = (e: unknown, depth = 0): boolean => {
      if (depth > 12) return false;
      if (numeric(e)) return true;
      if (object(e, ['param']) && typeof e.param === 'string') return numbers.has(e.param);
      if (object(e, ['x']) && e.x === true) return true;
      return object(e, ['op', 'args']) && ops.includes(String(e.op)) && Array.isArray(e.args) && e.args.length === 2 && e.args.every(a => expr(a, depth + 1));
    };
    if (value.chart !== undefined) {
      const c = value.chart;
      if (!object(c, ['type', 'x', 'xLabel', 'yLabel', 'series', 'showTotals']) || !['line', 'bar'].includes(String(c.type)) || !text(c.xLabel) || !text(c.yLabel) || !Array.isArray(c.x) || !c.x.length || c.x.length > 120 || !c.x.every(numeric)) return null;
      const xs = c.x as number[];
      if (!xs.every((x, i) => i === 0 || x > xs[i - 1]) || (c.showTotals !== undefined && typeof c.showTotals !== 'boolean')) return null;
      if (!Array.isArray(c.series) || !c.series.length || c.series.length > 4) return null;
      const seriesIds = new Set<string>();
      for (const s of c.series) {
        if (!object(s, ['id', 'label', 'values', 'expression', 'cumulative', 'initial']) || !id(s.id) || seriesIds.has(s.id) || !text(s.label) || (s.values !== undefined) === (s.expression !== undefined)) return null;
        seriesIds.add(s.id);
        if (s.values !== undefined && (!Array.isArray(s.values) || s.values.length !== xs.length || !s.values.every(numeric))) return null;
        if (s.expression !== undefined && !expr(s.expression)) return null;
        if (s.cumulative !== undefined && typeof s.cumulative !== 'boolean') return null;
        if (s.initial !== undefined && (s.cumulative !== true || !expr(s.initial))) return null;
      }
    }
    if (value.action !== undefined && (!object(value.action, ['label', 'instruction']) || !text(value.action.label, 60) || !text(value.action.instruction, 1000) || !params.length)) return null;
    if (!value.chart && !(params.length && value.action)) return null;
    return value as unknown as VisualSpec;
  } catch { return null; }
}

export function nativeVisuals(messages: ChatMessage[], sessionId: string): NativeVisual[] {
  const found = new Map<string, NativeVisual>();
  for (const m of messages) for (const t of m.toolCalls ?? []) {
    if (t.name !== 'Widget' || !t.result) continue;
    try {
      const result = JSON.parse(t.result); const s = result.surface;
      if (result.ok !== true || s?.version !== 2 || s.mode !== 'native' || s.display !== 'inline' || s.status !== 'ready' || s.sessionId !== sessionId) continue;
      const spec = parseVisual(s.spec);
      if (spec) { const key = `native:${sessionId}:${t.toolCallId}`; found.set(key, { key, sessionId, spec }); }
    } catch { /* Ordinary tool output. */ }
  }
  return [...found.values()];
}

export function evaluate(e: Expr, parameters: Record<string, string | number>, x: number): number {
  let n: number;
  if (typeof e === 'number') n = e;
  else if ('param' in e) n = Number(parameters[e.param]);
  else if ('x' in e) n = x;
  else {
    const a = evaluate(e.args[0], parameters, x), b = evaluate(e.args[1], parameters, x);
    switch (e.op) {
      case 'add': n = a + b; break; case 'sub': n = a - b; break; case 'mul': n = a * b; break;
      case 'div': n = b === 0 ? NaN : a / b; break; case 'pow': n = a ** b; break;
      case 'min': n = Math.min(a, b); break; case 'max': n = Math.max(a, b); break;
      default: throw new Error('不支持的计算');
    }
  }
  if (!numeric(n)) throw new Error('计算超出范围，请调整参数');
  return n;
}
export function calculate(spec: VisualSpec, params: Record<string, string | number>): number[][] {
  const c = spec.chart;
  if (!c) return [];
  return c.series.map(s => {
    let total = evaluate(s.initial ?? 0, params, c.x[0]);
    return c.x.map((x, i) => {
      const n = s.values ? s.values[i] : evaluate(s.expression!, params, x);
      total = s.cumulative ? total + n : n;
      if (!numeric(total)) throw new Error('计算超出范围，请调整参数');
      return total;
    });
  });
}

let submit: (sessionId: string, text: string) => Promise<boolean> = async () => false;
export function bindNativeVisuals(handler: typeof submit): void { submit = handler; }
const owner = (): string => state.isLoggedIn && state.userInfo ? JSON.stringify([state.userInfo.staffUid, state.userInfo.uid, state.userInfo.pid, state.userInfo.staffCode]) : '';
const format = (n: number): string => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(n);
const fingerprint = (value: string): string => {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
  return `${value.length}:${hash >>> 0}`;
};
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, className = '', content = ''): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag); node.className = className; node.textContent = content; return node;
};
const svgNode = (tag: string, attributes: Record<string, string | number>, content?: string): SVGElement => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (content !== undefined) node.textContent = content;
  return node;
};

export function renderNativeVisual(visual: NativeVisual): HTMLElement {
  const { spec } = visual; const identity = owner();
  const storageKey = `ace.native.v1:${identity}:${visual.key}`;
  const params: Record<string, string | number> = Object.fromEntries((spec.parameters ?? []).map(p => [p.id, p.value]));
  const hidden = new Set<string>();
  let saved: { values?: Record<string, unknown>; hidden?: string[]; signature?: string } = {};
  const signature = fingerprint(JSON.stringify(spec));
  try { const raw = localStorage.getItem(storageKey); if (raw && raw.length <= 16000) saved = JSON.parse(raw); } catch { /* Defaults. */ }
  if (!saved || typeof saved !== 'object') saved = {};
  if (saved.signature === signature) {
    for (const p of spec.parameters ?? []) {
      const v = saved.values?.[p.id];
      if ((p.type === 'range' || p.type === 'number') ? numeric(v) && v >= p.min! && v <= p.max! : text(v, 1000) && (p.type !== 'select' || p.options!.includes(v))) params[p.id] = v as string | number;
    }
    if (Array.isArray(saved.hidden)) for (const key of saved.hidden) if (spec.chart?.series.some(s => s.id === key)) hidden.add(key);
    if (hidden.size === spec.chart?.series.length) hidden.clear();
  }
  const root = el('section', 'native-visual'); root.setAttribute('aria-label', spec.title);
  root.append(el('h3', 'native-visual__title', spec.title));
  if (spec.description) root.append(el('p', 'native-visual__description', spec.description));
  const form = el('form', 'native-visual__controls'); form.addEventListener('submit', e => e.preventDefault());
  const summaries = el('div', 'native-visual__summaries');
  const plot = el('div', 'native-visual__plot');
  const legend = el('div', 'native-visual__legend');
  const details = el('div', 'native-visual__detail'); details.setAttribute('aria-live', 'polite');
  const error = el('p', 'native-visual__error'); error.setAttribute('role', 'alert');
  const status = el('span', 'native-visual__status'); status.setAttribute('role', 'status');
  const action = spec.action ? el('button', 'native-visual__action', spec.action.label) : null;
  let pending = false, lastSent = '', values: number[][] = [], selected = -1, invalid = false;
  const current = (): string => JSON.stringify(params);
  const save = (): void => {
    if (!identity || identity !== owner()) return;
    try { const data = JSON.stringify({ values: params, hidden: [...hidden], signature });
      if (new TextEncoder().encode(data).length > 16000) throw new Error('state too large');
      localStorage.setItem(storageKey, data);
    } catch { status.textContent = '参数未能保存到此设备'; }
  };
  const updateAction = (): void => { if (action) action.disabled = pending || invalid || lastSent === current(); };
  const showDetail = (index: number): void => {
    if (!spec.chart || !values.length) return;
    selected = index;
    details.textContent = `${spec.chart.xLabel} ${format(spec.chart.x[index])} · ${spec.chart.series.filter(s => !hidden.has(s.id)).map(s => `${s.label} ${format(values[spec.chart!.series.indexOf(s)][index])}`).join(' · ')}（${spec.chart.yLabel}）`;
  };
  function draw(): void {
    plot.replaceChildren(); summaries.replaceChildren(); details.textContent = '';
    if (!spec.chart || invalid) return;
    const c = spec.chart;
    const visible = c.series.map((s, i) => ({ s, i })).filter(({ s }) => !hidden.has(s.id));
    const all = visible.flatMap(({ i }) => values[i]);
    if (!all.length) return;
    const lo = Math.min(0, ...all), hi = Math.max(0, ...all); const span = hi - lo || 1;
    const rawStep = span / 4, magnitude = 10 ** Math.floor(Math.log10(rawStep));
    const step = ([1, 2, 2.5, 5, 10].find(n => n * magnitude >= rawStep) ?? 10) * magnitude;
    const min = Math.floor(lo / step) * step, max = hi === lo ? min + step * 4 : Math.ceil(hi / step) * step;
    const axisFormat = (n: number): string => new Intl.NumberFormat('zh-CN', {
      maximumFractionDigits: Math.min(20, Math.max(0, 1 - Math.floor(Math.log10(step)))),
    }).format(n);
    const width = Math.max(300, Math.round(plot.getBoundingClientRect().width || 640)); const height = 280;
    const left = Math.min(126, Math.max(62, Math.max(axisFormat(min).length, axisFormat(max).length) * 7 + 12));
    const right = width - 18, top = 28, bottom = height - 40;
    const x = (i: number): number => c.type === 'bar' ? left + (i + .5) * (right - left) / c.x.length : c.x.length === 1 ? (left + right) / 2 : left + (c.x[i] - c.x[0]) / (c.x.at(-1)! - c.x[0]) * (right - left);
    const y = (v: number): number => bottom - (v - min) / (max - min) * (bottom - top);
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${spec.title}，${c.xLabel}，${c.yLabel}`, tabindex: 0 });
    svg.append(svgNode('text', { x: left, y: 15, class: 'native-visual__axis' }, c.yLabel));
    for (let tick = 0; tick <= Math.round((max - min) / step); tick++) {
      const v = min + step * tick;
      svg.append(svgNode('line', { x1: left, x2: right, y1: y(v), y2: y(v), class: 'native-visual__grid' }));
      svg.append(svgNode('text', { x: left - 8, y: y(v) + 4, 'text-anchor': 'end', class: 'native-visual__axis' }, axisFormat(v)));
    }
    svg.append(svgNode('line', { x1: left, x2: right, y1: y(0), y2: y(0), class: 'native-visual__zero' }));
    const tickStep = Math.max(1, Math.ceil((c.x.length - 1) / (width < 450 ? 3 : 7)));
    c.x.forEach((v, i) => { if (i % tickStep === 0 || i === c.x.length - 1) svg.append(svgNode('text', { x: x(i), y: bottom + 18, 'text-anchor': 'middle', class: 'native-visual__axis' }, format(v))); });
    svg.append(svgNode('text', { x: (left + right) / 2, y: height - 2, 'text-anchor': 'middle', class: 'native-visual__axis' }, c.xLabel));
    visible.forEach(({ s, i }, position) => {
      const color = `var(--mw-chart-${i + 1})`;
      if (c.type === 'line') {
        svg.append(svgNode('path', { d: values[i].map((v, j) => `${j ? 'L' : 'M'}${x(j)},${y(v)}`).join(' '), fill: 'none', stroke: color, 'stroke-width': 2.5 }));
        values[i].forEach((v, j) => { if (c.x.length <= 24 || j === c.x.length - 1) svg.append(svgNode('circle', { cx: x(j), cy: y(v), r: 3, fill: color })); });
      } else {
        const groupWidth = (right - left) / c.x.length * .78; const barWidth = groupWidth / visible.length;
        values[i].forEach((v, j) => svg.append(svgNode('rect', { x: x(j) - groupWidth / 2 + position * barWidth, y: Math.min(y(0), y(v)), width: Math.max(.5, barWidth - 1), height: Math.abs(y(v) - y(0)), fill: color })));
      }
      if (c.showTotals) { const summary = el('div', 'native-visual__summary'); summary.append(el('span', '', s.label), el('strong', '', format(values[i].at(-1)!))); summaries.append(summary); }
    });
    const guide = svgNode('line', { y1: top, y2: bottom, class: 'native-visual__guide', visibility: 'hidden' }); svg.append(guide);
    const inspect = (i: number): void => { showDetail(i); guide.setAttribute('x1', String(x(i))); guide.setAttribute('x2', String(x(i))); guide.setAttribute('visibility', 'visible'); };
    svg.addEventListener('pointermove', (event) => { const e = event as PointerEvent; const rect = svg.getBoundingClientRect(); if (!rect.width) return;
      const point = (e.clientX - rect.left) / rect.width * width;
      let nearest = 0; c.x.forEach((_, i) => { if (Math.abs(x(i) - point) < Math.abs(x(nearest) - point)) nearest = i; }); inspect(nearest);
    });
    svg.addEventListener('keydown', (event) => { const e = event as KeyboardEvent; if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return; e.preventDefault(); inspect(Math.max(0, Math.min(c.x.length - 1, (selected < 0 ? c.x.length - 1 : selected) + (e.key === 'ArrowRight' ? 1 : -1)))); });
    plot.append(svg); showDetail(selected >= 0 && selected < c.x.length ? selected : c.x.length - 1);
  }
  function refresh(): void {
    invalid = !form.checkValidity(); error.textContent = invalid ? '请检查输入范围和格式' : '';
    if (!invalid) try { values = calculate(spec, params); } catch (e) { invalid = true; error.textContent = e instanceof Error ? e.message : '无法计算'; }
    draw(); updateAction();
  }
  for (const p of spec.parameters ?? []) {
    const label = el('label', 'native-visual__control'); const header = el('span', 'native-visual__control-label', p.label);
    const output = el('output');
    if (p.type === 'range') { output.textContent = `${format(Number(params[p.id]))}${p.unit ? ` ${p.unit}` : ''}`; header.append(output); }
    else if (p.unit) header.append(el('span', '', p.unit));
    const input = p.type === 'select' ? el('select') : el('input');
    input.name = p.id; input.setAttribute('aria-label', p.label); input.required = true;
    if (input instanceof HTMLSelectElement) for (const option of p.options!) { const opt = el('option', '', option); opt.value = option; input.append(opt); }
    else { input.type = p.type === 'range' ? 'range' : p.type === 'number' ? 'number' : 'text';
      if (p.type === 'range' || p.type === 'number') { input.min = String(p.min); input.max = String(p.max); input.step = String(p.step); }
      else input.maxLength = 1000;
    }
    input.value = String(params[p.id]);
    input.addEventListener('input', () => {
      if (identity !== owner()) return;
      const v = p.type === 'number' || p.type === 'range' ? Number(input.value) : input.value;
      if (input.value !== '' && input.checkValidity()) params[p.id] = v;
      if (p.type === 'range') output.textContent = `${format(Number(v))}${p.unit ? ` ${p.unit}` : ''}`;
      status.textContent = ''; refresh(); if (!invalid) save();
    });
    label.append(header, input); form.append(label);
  }
  for (const s of spec.chart?.series ?? []) {
    const button = el('button', 'native-visual__legend-item', s.label); button.type = 'button'; button.style.setProperty('--series-color', `var(--mw-chart-${spec.chart!.series.indexOf(s) + 1})`);
    button.setAttribute('aria-pressed', String(!hidden.has(s.id)));
    button.addEventListener('click', () => { if (hidden.has(s.id)) hidden.delete(s.id); else if (hidden.size < spec.chart!.series.length - 1) hidden.add(s.id);
      button.setAttribute('aria-pressed', String(!hidden.has(s.id))); draw(); save(); }); legend.append(button);
  }
  root.append(form, summaries, legend, plot, details, error);
  if (action) {
    action.type = 'button';
    action.addEventListener('click', () => {
      if (pending || !root.isConnected || !identity || identity !== owner() || state.activeSessionId !== visual.sessionId) return;
      refresh(); if (invalid || !form.reportValidity() || lastSent === current()) return;
      const snapshot = current(); pending = true; status.textContent = '正在提交…'; updateAction();
      const lines = (spec.parameters ?? []).map(p => `${p.label}（${p.id}）：${String(params[p.id])}${p.unit ? ` ${p.unit}` : ''}`);
      const message = `${spec.action!.instruction}\n\n「${spec.title}」当前参数：\n${lines.join('\n')}`;
      void Promise.resolve().then(() => submit(visual.sessionId, message)).then(sent => {
        if (sent) lastSent = snapshot; status.textContent = sent ? '已提交' : '提交失败，请重试';
      }).catch(() => { status.textContent = '提交失败，请重试'; }).finally(() => { pending = false; updateAction(); });
    });
    const footer = el('div', 'native-visual__footer'); footer.append(action, status); root.append(footer);
  } else root.append(status);
  refresh();
  // Mounted nodes keep their controls while surrounding text streams. Resize observers
  // are shared below and released when the renderer removes a visual.
  resizeCallbacks.set(root, draw);
  return root;
}

const resizeCallbacks = new WeakMap<Element, () => void>();
const observed = new Set<Element>();
let observer: ResizeObserver | undefined;
export function hydrateNativeVisuals(container: HTMLElement): void {
  if (typeof ResizeObserver === 'undefined') return;
  observer ??= new ResizeObserver(entries => { for (const entry of entries) resizeCallbacks.get(entry.target)?.(); });
  for (const node of observed) if (!node.isConnected) { observer.unobserve(node); observed.delete(node); }
  for (const node of container.querySelectorAll('.native-visual')) if (!observed.has(node)) { observed.add(node); observer.observe(node); resizeCallbacks.get(node)?.(); }
}
