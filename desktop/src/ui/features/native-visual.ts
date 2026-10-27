import { state } from '../state';
import { interactiveOwnerKey } from './interactive-owner';

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
const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
const axisFormats = new Map<number, Intl.NumberFormat>();
const format = (n: number): string => numberFormat.format(n);
const axisFormat = (step: number, n: number): string => {
  const digits = Math.min(20, Math.max(0, 1 - Math.floor(Math.log10(step))));
  let formatter = axisFormats.get(digits);
  if (!formatter) {
    formatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits });
    axisFormats.set(digits, formatter);
  }
  return formatter.format(n);
};
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

type VisualParams = Record<string, string | number>;

function restoreVisualState(spec: VisualSpec, storageKey: string, signature: string): {
  params: VisualParams; hidden: Set<string>;
} {
  const params = Object.fromEntries((spec.parameters ?? []).map(p => [p.id, p.value]));
  const hidden = new Set<string>();
  let saved: { values?: Record<string, unknown>; hidden?: string[]; signature?: string } = {};
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw && raw.length <= 16000) saved = JSON.parse(raw);
  } catch { /* Defaults. */ }
  if (!saved || typeof saved !== 'object') saved = {};
  if (saved.signature !== signature) return { params, hidden };
  for (const p of spec.parameters ?? []) {
    const value = saved.values?.[p.id];
    if ((p.type === 'range' || p.type === 'number')
      ? numeric(value) && value >= p.min! && value <= p.max!
      : text(value, 1000) && (p.type !== 'select' || p.options!.includes(value))) {
      params[p.id] = value as string | number;
    }
  }
  if (Array.isArray(saved.hidden)) {
    for (const key of saved.hidden) if (spec.chart?.series.some(s => s.id === key)) hidden.add(key);
  }
  if (hidden.size === spec.chart?.series.length) hidden.clear();
  return { params, hidden };
}

function drawNativeChart(
  spec: VisualSpec,
  values: number[][],
  hidden: Set<string>,
  selected: number,
  plot: HTMLElement,
  summaries: HTMLElement,
  details: HTMLElement,
  showDetail: (index: number) => void,
): void {
  plot.replaceChildren(); summaries.replaceChildren(); details.textContent = '';
  if (!spec.chart) return;
  const chart = spec.chart;
  const visible = chart.series.map((series, index) => ({ series, index }))
    .filter(({ series }) => !hidden.has(series.id));
  const all = visible.flatMap(({ index }) => values[index]);
  if (!all.length) return;
  const lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  const rawStep = (hi - lo || 1) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = ([1, 2, 2.5, 5, 10].find(n => n * magnitude >= rawStep) ?? 10) * magnitude;
  const min = Math.floor(lo / step) * step;
  const max = hi === lo ? min + step * 4 : Math.ceil(hi / step) * step;
  const formatAxis = (n: number): string => axisFormat(step, n);
  const width = Math.max(300, Math.round(plot.getBoundingClientRect().width || 640));
  const height = 280;
  const left = Math.min(126, Math.max(62,
    Math.max(formatAxis(min).length, formatAxis(max).length) * 7 + 12));
  const right = width - 18, top = 28, bottom = height - 40;
  const x = (i: number): number => chart.type === 'bar'
    ? left + (i + .5) * (right - left) / chart.x.length
    : chart.x.length === 1 ? (left + right) / 2
      : left + (chart.x[i] - chart.x[0]) / (chart.x.at(-1)! - chart.x[0]) * (right - left);
  const y = (v: number): number => bottom - (v - min) / (max - min) * (bottom - top);
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': `${spec.title}，${chart.xLabel}，${chart.yLabel}`, tabindex: 0,
  });
  svg.append(svgNode('text', { x: left, y: 15, class: 'native-visual__axis' }, chart.yLabel));
  for (let tick = 0; tick <= Math.round((max - min) / step); tick++) {
    const value = min + step * tick;
    svg.append(svgNode('line', { x1: left, x2: right, y1: y(value), y2: y(value), class: 'native-visual__grid' }));
    svg.append(svgNode('text', { x: left - 8, y: y(value) + 4, 'text-anchor': 'end', class: 'native-visual__axis' }, formatAxis(value)));
  }
  svg.append(svgNode('line', { x1: left, x2: right, y1: y(0), y2: y(0), class: 'native-visual__zero' }));
  const tickStep = Math.max(1, Math.ceil((chart.x.length - 1) / (width < 450 ? 3 : 7)));
  chart.x.forEach((value, index) => {
    if (index % tickStep === 0 || index === chart.x.length - 1) {
      svg.append(svgNode('text', { x: x(index), y: bottom + 18, 'text-anchor': 'middle', class: 'native-visual__axis' }, format(value)));
    }
  });
  svg.append(svgNode('text', { x: (left + right) / 2, y: height - 2, 'text-anchor': 'middle', class: 'native-visual__axis' }, chart.xLabel));
  visible.forEach(({ series, index }, position) => {
    const color = `var(--mw-chart-${index + 1})`;
    if (chart.type === 'line') {
      const path = values[index].map((value, i) => `${i ? 'L' : 'M'}${x(i)},${y(value)}`).join(' ');
      svg.append(svgNode('path', { d: path, fill: 'none', stroke: color, 'stroke-width': 2.5 }));
      values[index].forEach((value, i) => {
        if (chart.x.length <= 24 || i === chart.x.length - 1) {
          svg.append(svgNode('circle', { cx: x(i), cy: y(value), r: 3, fill: color }));
        }
      });
    } else {
      const groupWidth = (right - left) / chart.x.length * .78;
      const barWidth = groupWidth / visible.length;
      values[index].forEach((value, i) => svg.append(svgNode('rect', {
        x: x(i) - groupWidth / 2 + position * barWidth,
        y: Math.min(y(0), y(value)), width: Math.max(.5, barWidth - 1),
        height: Math.abs(y(value) - y(0)), fill: color,
      })));
    }
    if (chart.showTotals) {
      const summary = el('div', 'native-visual__summary');
      summary.append(el('span', '', series.label), el('strong', '', format(values[index].at(-1)!)));
      summaries.append(summary);
    }
  });
  const guide = svgNode('line', { y1: top, y2: bottom, class: 'native-visual__guide', visibility: 'hidden' });
  svg.append(guide);
  const inspect = (index: number): void => {
    showDetail(index);
    guide.setAttribute('x1', String(x(index))); guide.setAttribute('x2', String(x(index)));
    guide.setAttribute('visibility', 'visible');
  };
  svg.addEventListener('pointermove', (event) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const point = (event.clientX - rect.left) / rect.width * width;
    let nearest = 0;
    chart.x.forEach((_, index) => {
      if (Math.abs(x(index) - point) < Math.abs(x(nearest) - point)) nearest = index;
    });
    inspect(nearest);
  });
  svg.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const current = selected < 0 ? chart.x.length - 1 : selected;
    inspect(Math.max(0, Math.min(chart.x.length - 1, current + (event.key === 'ArrowRight' ? 1 : -1))));
  });
  plot.append(svg);
  showDetail(selected >= 0 && selected < chart.x.length ? selected : chart.x.length - 1);
}

function bindNativeControls(
  spec: VisualSpec,
  params: VisualParams,
  form: HTMLFormElement,
  status: HTMLElement,
  identity: string,
  refresh: () => boolean,
  save: () => void,
): void {
  for (const parameter of spec.parameters ?? []) {
    const label = el('label', 'native-visual__control');
    const header = el('span', 'native-visual__control-label', parameter.label);
    const output = el('output');
    if (parameter.type === 'range') {
      output.textContent = `${format(Number(params[parameter.id]))}${parameter.unit ? ` ${parameter.unit}` : ''}`;
      header.append(output);
    } else if (parameter.unit) header.append(el('span', '', parameter.unit));
    const input = parameter.type === 'select' ? el('select') : el('input');
    input.name = parameter.id; input.setAttribute('aria-label', parameter.label); input.required = true;
    if (input instanceof HTMLSelectElement) {
      for (const option of parameter.options!) {
        const node = el('option', '', option); node.value = option; input.append(node);
      }
    } else {
      input.type = parameter.type === 'range' ? 'range' : parameter.type === 'number' ? 'number' : 'text';
      if (parameter.type === 'range' || parameter.type === 'number') {
        input.min = String(parameter.min); input.max = String(parameter.max); input.step = String(parameter.step);
      } else input.maxLength = 1000;
    }
    input.value = String(params[parameter.id]);
    input.addEventListener('input', () => {
      if (identity !== interactiveOwnerKey()) return;
      const value = parameter.type === 'number' || parameter.type === 'range' ? Number(input.value) : input.value;
      if (input.value !== '' && input.checkValidity()) params[parameter.id] = value;
      if (parameter.type === 'range') {
        output.textContent = `${format(Number(value))}${parameter.unit ? ` ${parameter.unit}` : ''}`;
      }
      status.textContent = '';
      if (!refresh()) save();
    });
    label.append(header, input); form.append(label);
  }
}

function bindNativeLegend(
  spec: VisualSpec,
  legend: HTMLElement,
  hidden: Set<string>,
  draw: () => void,
  save: () => void,
): void {
  for (const [index, series] of (spec.chart?.series ?? []).entries()) {
    const button = el('button', 'native-visual__legend-item', series.label);
    button.type = 'button';
    button.style.setProperty('--series-color', `var(--mw-chart-${index + 1})`);
    button.setAttribute('aria-pressed', String(!hidden.has(series.id)));
    button.addEventListener('click', () => {
      if (hidden.has(series.id)) hidden.delete(series.id);
      else if (hidden.size < spec.chart!.series.length - 1) hidden.add(series.id);
      button.setAttribute('aria-pressed', String(!hidden.has(series.id)));
      draw(); save();
    });
    legend.append(button);
  }
}

export function renderNativeVisual(visual: NativeVisual): HTMLElement {
  const { spec } = visual;
  const identity = interactiveOwnerKey();
  const storageKey = `ace.native.v1:${identity}:${visual.key}`;
  const signature = fingerprint(JSON.stringify(spec));
  const { params, hidden } = restoreVisualState(spec, storageKey, signature);
  const root = el('section', 'native-visual'); root.setAttribute('aria-label', spec.title);
  root.append(el('h3', 'native-visual__title', spec.title));
  if (spec.description) root.append(el('p', 'native-visual__description', spec.description));
  const form = el('form', 'native-visual__controls');
  form.addEventListener('submit', event => event.preventDefault());
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
    if (!identity || identity !== interactiveOwnerKey()) return;
    try {
      const data = JSON.stringify({ values: params, hidden: [...hidden], signature });
      if (new TextEncoder().encode(data).length > 16000) throw new Error('state too large');
      localStorage.setItem(storageKey, data);
    } catch { status.textContent = '参数未能保存到此设备'; }
  };
  const updateAction = (): void => {
    if (action) action.disabled = pending || invalid || lastSent === current();
  };
  const showDetail = (index: number): void => {
    if (!spec.chart || !values.length) return;
    selected = index;
    const detail = spec.chart.series.map((series, i) => ({ series, i }))
      .filter(({ series }) => !hidden.has(series.id))
      .map(({ series, i }) => `${series.label} ${format(values[i][index])}`).join(' · ');
    details.textContent = `${spec.chart.xLabel} ${format(spec.chart.x[index])} · ${detail}（${spec.chart.yLabel}）`;
  };
  const draw = (): void => {
    if (invalid) {
      plot.replaceChildren(); summaries.replaceChildren(); details.textContent = '';
      return;
    }
    drawNativeChart(spec, values, hidden, selected, plot, summaries, details, showDetail);
  };
  const refresh = (): boolean => {
    invalid = !form.checkValidity(); error.textContent = invalid ? '请检查输入范围和格式' : '';
    if (!invalid) try {
      values = calculate(spec, params);
    } catch (cause) {
      invalid = true; error.textContent = cause instanceof Error ? cause.message : '无法计算';
    }
    draw(); updateAction();
    return invalid;
  };
  bindNativeControls(spec, params, form, status, identity, refresh, save);
  bindNativeLegend(spec, legend, hidden, draw, save);
  root.append(form, summaries, legend, plot, details, error);
  if (action) {
    action.type = 'button';
    action.addEventListener('click', () => {
      if (pending || !root.isConnected || !identity || identity !== interactiveOwnerKey()
        || state.activeSessionId !== visual.sessionId) return;
      refresh();
      if (invalid || !form.reportValidity() || lastSent === current()) return;
      const snapshot = current(); pending = true; status.textContent = '正在提交…'; updateAction();
      const lines = (spec.parameters ?? []).map(parameter =>
        `${parameter.label}（${parameter.id}）：${String(params[parameter.id])}${parameter.unit ? ` ${parameter.unit}` : ''}`);
      const message = `${spec.action!.instruction}\n\n「${spec.title}」当前参数：\n${lines.join('\n')}`;
      void Promise.resolve().then(() => submit(visual.sessionId, message)).then(sent => {
        if (sent) lastSent = snapshot;
        status.textContent = sent ? '已提交' : '提交失败，请重试';
      }).catch(() => {
        status.textContent = '提交失败，请重试';
      }).finally(() => { pending = false; updateAction(); });
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
