import validate from './validate.generated.js';
import { finite, type Expr, type Parameter, type ReplyNode, type ReplySpec, type Value } from './types';
const validDate = (v: string): boolean => /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(Date.parse(v)) && new Date(v).toISOString().slice(0, 10) === v;
export function validValue(p: Parameter, v: unknown, submitted = false): v is Value {
    if (p.type === 'number' || p.type === 'range') {
        if (!finite(v) || !finite(p.min) || !finite(p.max) || !finite(p.step) || p.step <= 0 || v < p.min || v > p.max)
            return false;
        const ticks = (v - p.min) / p.step;
        return Math.abs(ticks - Math.round(ticks)) <= 1e-7 * Math.max(1, Math.abs(ticks));
    }
    if (p.type === 'checkbox')
        return typeof v === 'boolean' && (!submitted || !p.required || v);
    if (p.type === 'multiselect')
        return Array.isArray(v) && v.every(x => typeof x === 'string' && p.options?.includes(x)) && new Set(v).size === v.length && (!submitted || !p.required || !!v.length);
    if (typeof v !== 'string' || v.length > 1000)
        return false;
    if (!v)
        return !submitted || !p.required;
    if (p.type === 'select' || p.type === 'radio')
        return !!p.options?.includes(v);
    return p.type !== 'date' || validDate(v);
}
export function parseVisual(value: unknown): ReplySpec | null {
    try {
        if (new TextEncoder().encode(JSON.stringify(value)).length > 128000)
            return null;
        const bounded = (v: unknown, d = 0): boolean => d <= 32 && (!v || typeof v !== 'object' || Object.values(v).every(x => bounded(x, d + 1)));
        if (!bounded(value) || !validate(value))
            return null;
        const s = value as ReplySpec, ps = s.parameters ?? [], ds = s.datasets ?? [];
        const params = new Map(ps.map(p => [p.id, p])), datasets = new Map(ds.map(d => [d.id, d]));
        if (params.size !== ps.length || datasets.size !== ds.length)
            return null;
        for (const p of ps) {
            const numeric = p.type === 'number' || p.type === 'range', choice = ['select', 'radio', 'multiselect'].includes(p.type);
            if (numeric ? (!finite(p.min) || !finite(p.max) || !finite(p.step) || p.min >= p.max || p.step <= 0 || p.options !== undefined) : ['min', 'max', 'step'].some(k => k in p))
                return null;
            if (choice ? !p.options?.length : p.options !== undefined)
                return null;
            if (!validValue(p, p.value))
                return null;
        }
        const expression = (e: Expr, columns = new Set<string>(), depth = 0): boolean => {
            if (depth > 12)
                return false;
            if (typeof e === 'number')
                return finite(e);
            if ('param' in e)
                return ['number', 'range'].includes(params.get(e.param)?.type ?? '');
            if ('column' in e)
                return columns.has(e.column);
            return e.args.every(a => expression(a, columns, depth + 1));
        };
        for (const d of ds) {
            if (new Set(d.columns.map(c => c.id)).size !== d.columns.length)
                return null;
            const raw = d.columns.filter(c => c.expression === undefined), numbers = new Set(raw.filter(c => c.type === 'number').map(c => c.id));
            for (const c of d.columns) {
                if (c.expression !== undefined && (c.type !== 'number' || !expression(c.expression, numbers)))
                    return null;
                if (c.cumulative && c.type !== 'number')
                    return null;
                if (c.initial !== undefined && (!c.cumulative || !expression(c.initial, numbers)))
                    return null;
            }
            for (const row of d.rows)
                if (row.length !== raw.length || row.some((v, i) => raw[i].type === 'number' ? !finite(v) : raw[i].type === 'date' ? (typeof v !== 'string' || !validDate(v)) : typeof v !== 'string'))
                    return null;
        }
        const ids = new Set<string>(), fields = new Set<string>();
        const nodes = (items: ReplyNode[], depth = 0): boolean => {
            if (depth > 6)
                return false;
            for (const n of items) {
                if (ids.has(n.id) || ids.size >= 120)
                    return false;
                ids.add(n.id);
                if ('children' in n && !nodes(n.children, depth + 1))
                    return false;
                if (n.type === 'field') {
                    if (!params.has(n.parameter) || fields.has(n.parameter))
                        return false;
                    fields.add(n.parameter);
                }
                if (n.type === 'metric' && !expression(n.expression))
                    return false;
                if (n.type === 'table' || n.type === 'chart') {
                    const d = datasets.get(n.dataset);
                    if (!d)
                        return false;
                    if (n.type === 'chart') {
                        const x = d.columns.find(c => c.id === n.x);
                        if (!x || new Set(n.series.map(s => s.column)).size !== n.series.length || n.series.some(s => !d.columns.some(c => c.id === s.column && c.type === 'number')))
                            return false;
                        if (n.chartType === 'scatter' && x.type !== 'number')
                            return false;
                        if (['pie', 'donut'].includes(n.chartType) && n.series.length !== 1)
                            return false;
                    }
                }
                if (n.type === 'button' && n.action.type === 'set')
                    for (const [key, v] of Object.entries(n.action.values)) {
                        const p = params.get(key);
                        if (!p || !validValue(p, v))
                            return false;
                    }
            }
            return true;
        };
        return nodes(s.children) && fields.size === params.size ? s : null;
    }
    catch {
        return null;
    }
}
