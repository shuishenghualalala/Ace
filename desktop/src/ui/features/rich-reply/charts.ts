import { init, use, type EChartsType, type EChartsCoreOption } from 'echarts/core';
import { LineChart, BarChart, PieChart, ScatterChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, AriaComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import { display, el } from './data';
import type { ChartNode, Dataset } from './types';
use([LineChart, BarChart, PieChart, ScatterChart, GridComponent, TooltipComponent, AriaComponent, SVGRenderer]);
export function chartOption(n: ChartNode, d: Dataset, rows: Dataset['rows'], hidden: string[], colors: string[], textColor: string, gridColor: string): EChartsCoreOption {
    const xi = d.columns.findIndex(c => c.id === n.x), pie = ['pie', 'donut'].includes(n.chartType);
    const series = n.series.filter(s => !hidden.includes(s.column)).map(s => {
        const yi = d.columns.findIndex(c => c.id === s.column), name = s.label ?? d.columns[yi].label;
        if (pie) {
            if (rows.some(r => Number(r[yi]) < 0) || !rows.some(r => Number(r[yi]) > 0))
                throw Error('饼图需要非负数据，且至少一个值大于零');
            return { name, type: 'pie', radius: n.chartType === 'donut' ? ['40%', '68%'] : '68%', label: { color: textColor }, data: rows.map(r => ({ name: String(r[xi]), value: r[yi] })) };
        }
        const color = colors[n.series.indexOf(s) % colors.length];
        return { name, itemStyle: { color }, lineStyle: { color }, type: n.chartType === 'scatter' ? 'scatter' : ['bar', 'stackedBar'].includes(n.chartType) ? 'bar' : 'line', ...(n.chartType.startsWith('stacked') ? { stack: 'total' } : {}), ...(['area', 'stackedArea'].includes(n.chartType) ? { areaStyle: { opacity: .25 } } : {}), symbolSize: 7, showSymbol: rows.length <= 30, data: rows.map(r => [r[xi], r[yi]]) };
    });
    const visible = n.series.filter(s => !hidden.includes(s.column));
    const summary = rows.slice(0, 10).map(row => `${d.columns[xi].label} ${display(row[xi])}：${visible.map(s => {
        const yi = d.columns.findIndex(c => c.id === s.column);
        return `${s.label ?? d.columns[yi].label} ${display(row[yi])}`;
    }).join('，')}`).join('；');
    const description = `${n.title ?? '图表'}，共 ${rows.length} 条数据${rows.length > 10 ? '，以下为前 10 条' : ''}。${summary}。左右方向键查看数据。`;
    return { animation: false, color: colors, aria: { enabled: true, label: { description } }, textStyle: { color: textColor }, tooltip: { trigger: pie || n.chartType === 'scatter' ? 'item' : 'axis', renderMode: 'richText', confine: true }, ...(!pie ? { grid: { left: 60, right: 24, top: 35, bottom: 50, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' }, xAxis: { type: d.columns[xi].type === 'number' ? 'value' : d.columns[xi].type === 'date' ? 'time' : 'category', name: n.xLabel ?? d.columns[xi].label, nameLocation: 'middle', nameGap: 30, axisLabel: { color: textColor }, axisLine: { lineStyle: { color: gridColor } } }, yAxis: { type: 'value', name: n.yLabel ?? '', axisLabel: { color: textColor }, splitLine: { lineStyle: { color: gridColor } } } } : {}), series };
}
interface ChartMount {
    node: HTMLElement;
    mounted?: boolean;
    draw: () => void;
    dispose: () => void;
}
const mounts = new Set<ChartMount>();
let resize: ResizeObserver | undefined, theme: MutationObserver | undefined;
export function hydrateCharts(): void {
    for (const mount of mounts)
        if (!mount.node.isConnected) {
            resize?.unobserve(mount.node);
            mount.dispose();
            mounts.delete(mount);
        }
    resize ??= typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(entries => { for (const entry of entries)
        for (const m of mounts)
            if (m.node === entry.target)
                m.draw(); });
    if (!theme && typeof MutationObserver !== 'undefined') {
        theme = new MutationObserver(() => { for (const m of mounts)
            m.draw(); });
        theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
    }
    for (const mount of mounts)
        if (!mount.mounted) {
            mount.mounted = true;
            resize?.observe(mount.node);
            mount.draw();
        }
}
export function renderChart(n: ChartNode, d: Dataset, hidden: string[], save: () => void): {
    node: HTMLElement;
    update: (rows: Dataset['rows']) => void;
} {
    const node = el('section', 'rich-reply__chart'), legend = el('div', 'rich-reply__legend'), plot = el('div', 'rich-reply__plot'), detail = el('div', 'rich-reply__muted');
    plot.tabIndex = 0;
    plot.setAttribute('aria-label', `${n.title ?? '图表'}，左右方向键查看数据`);
    detail.setAttribute('aria-live', 'polite');
    let chart: EChartsType | undefined, rows: Dataset['rows'] = [], index = 0, error = '';
    const inspect = (): void => { const row = rows[index]; detail.textContent = error || (row ? d.columns.map((c, i) => `${c.label} ${display(row[i])}`).join(' · ') : '暂无数据'); };
    const draw = (): void => {
        if (!node.isConnected || !rows.length)
            return;
        const style = getComputedStyle(node), color = style.getPropertyValue('--mw-text-secondary').trim() || '#666', grid = style.getPropertyValue('--mw-chart-grid').trim() || '#ddd';
        const colors = Array.from({ length: 8 }, (_, i) => style.getPropertyValue(`--mw-chart-${i + 1}`).trim() || ['#4385cf', '#48a86d', '#9474ce', '#d17b32', '#0f6772', '#9f3f5b', '#765300', '#4b57a3'][i]);
        try {
            const option = chartOption(n, d, rows, hidden, colors, color, grid);
            if (!chart) {
                chart = init(plot, undefined, { renderer: 'svg', width: plot.clientWidth || 640, height: 300 });
                chart.on('mouseover', (event: unknown) => { const e = event as {
                    dataIndex?: number;
                }; if (typeof e.dataIndex === 'number') {
                    index = e.dataIndex;
                    inspect();
                } });
            }
            chart.resize({ width: plot.clientWidth || 640, height: 300 });
            chart.setOption(option, { notMerge: true });
            error = '';
        }
        catch (cause) {
            chart?.clear();
            error = cause instanceof Error ? cause.message : '无法绘制图表';
        }
        inspect();
    };
    for (const s of n.series) {
        const label = s.label ?? d.columns.find(c => c.id === s.column)!.label, button = el('button', '', label);
        button.type = 'button';
        button.setAttribute('aria-pressed', String(!hidden.includes(s.column)));
        button.addEventListener('click', () => { const i = hidden.indexOf(s.column); if (i >= 0)
            hidden.splice(i, 1);
        else if (hidden.length < n.series.length - 1)
            hidden.push(s.column); button.setAttribute('aria-pressed', String(!hidden.includes(s.column))); draw(); save(); });
        legend.append(button);
    }
    plot.addEventListener('keydown', e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        index = Math.max(0, Math.min(rows.length - 1, index + (e.key === 'ArrowRight' ? 1 : -1)));
        inspect();
    } });
    node.append(legend, plot, detail);
    mounts.add({ node, draw, dispose: () => chart?.dispose() });
    return { node, update: data => { rows = data; index = Math.min(index, Math.max(0, rows.length - 1)); if (!rows.length)
            chart?.clear(); draw(); inspect(); } };
}
