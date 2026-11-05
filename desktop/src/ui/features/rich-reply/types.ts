export type Value = number | string | boolean | string[];
export type Values = Record<string, Value>;
export type Expr = number | {
    param: string;
} | {
    column: string;
} | {
    op: 'add' | 'sub' | 'mul' | 'div' | 'pow' | 'min' | 'max';
    args: [
        Expr,
        Expr
    ];
};
export interface Parameter {
    id: string;
    label: string;
    type: 'range' | 'number' | 'text' | 'textarea' | 'select' | 'radio' | 'multiselect' | 'checkbox' | 'date';
    value: Value;
    required?: boolean;
    min?: number;
    max?: number;
    step?: number;
    unit?: string;
    options?: string[];
}
export interface Column {
    id: string;
    label: string;
    type: 'number' | 'string' | 'date';
    expression?: Expr;
    cumulative?: boolean;
    initial?: Expr;
}
export interface Dataset {
    id: string;
    columns: Column[];
    rows: (string | number | boolean)[][];
}
export type Action = {
    type: 'submit';
    instruction: string;
} | {
    type: 'set';
    values: Values;
} | {
    type: 'reset';
};
export type Layout = {
    id: string;
    type: 'row' | 'column' | 'card';
    title?: string;
    children: ReplyNode[];
};
export interface ChartNode {
    id: string;
    type: 'chart';
    title?: string;
    dataset: string;
    chartType: 'line' | 'bar' | 'area' | 'stackedBar' | 'stackedArea' | 'scatter' | 'pie' | 'donut';
    x: string;
    xLabel?: string;
    yLabel?: string;
    series: {
        column: string;
        label?: string;
    }[];
}
export type ReplyNode = Layout | ChartNode | {
    id: string;
    type: 'text';
    text: string;
} | {
    id: string;
    type: 'badge';
    text: string;
    tone?: 'neutral' | 'success' | 'warning' | 'danger';
} | {
    id: string;
    type: 'image';
    src: string;
    alt: string;
} | {
    id: string;
    type: 'metric';
    label: string;
    expression: Expr;
    unit?: string;
} | {
    id: string;
    type: 'field';
    parameter: string;
} | {
    id: string;
    type: 'button';
    label: string;
    action: Action;
} | {
    id: string;
    type: 'table';
    dataset: string;
    title?: string;
    pageSize?: number;
};
export interface ReplySpec {
    version: 3;
    id: string;
    revision: number;
    title: string;
    description?: string;
    parameters?: Parameter[];
    datasets?: Dataset[];
    children: ReplyNode[];
}
export interface NativeVisual {
    key: string;
    sessionId: string;
    spec: ReplySpec;
    readOnly?: boolean;
}
export const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 1e12;
export const nodesOf = (nodes: ReplyNode[]): ReplyNode[] => nodes.flatMap(n => [n, ...('children' in n ? nodesOf(n.children) : [])]);
