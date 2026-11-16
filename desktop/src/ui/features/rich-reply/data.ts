import { finite, type Dataset, type Expr, type Values } from './types';
export function evaluate(e: Expr, params: Values, row: Record<string, unknown> = {}): number {
    let n: number;
    if (typeof e === 'number')
        n = e;
    else if ('param' in e)
        n = Number(params[e.param]);
    else if ('column' in e)
        n = Number(row[e.column]);
    else {
        const a = evaluate(e.args[0], params, row), b = evaluate(e.args[1], params, row);
        switch (e.op) {
            case 'add':
                n = a + b;
                break;
            case 'sub':
                n = a - b;
                break;
            case 'mul':
                n = a * b;
                break;
            case 'div':
                n = b === 0 ? NaN : a / b;
                break;
            case 'pow':
                n = a ** b;
                break;
            case 'min':
                n = Math.min(a, b);
                break;
            case 'max':
                n = Math.max(a, b);
                break;
        }
    }
    if (!finite(n))
        throw Error('计算超出范围，请调整参数');
    return n;
}
export function calculateDataset(d: Dataset, params: Values): (string | number | boolean)[][] {
    const raw = d.columns.filter(c => c.expression === undefined), totals = new Map<string, number>();
    return d.rows.map(row => {
        const source = Object.fromEntries(raw.map((c, i) => [c.id, row[i]]));
        return d.columns.map(c => {
            let value = c.expression === undefined ? source[c.id] : evaluate(c.expression, params, source);
            if (c.cumulative) {
                value = (totals.get(c.id) ?? evaluate(c.initial ?? 0, params, source)) + Number(value);
                if (!finite(value))
                    throw Error('累计值超出范围');
                totals.set(c.id, value);
            }
            return value;
        });
    });
}
export const display = (v: unknown): string => typeof v === 'number' ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(v) : Array.isArray(v) ? v.join('、') : typeof v === 'boolean' ? (v ? '是' : '否') : String(v ?? '');
export const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = ''): HTMLElementTagNameMap[K] => { const node = document.createElement(tag); node.className = cls; node.textContent = text; return node; };
