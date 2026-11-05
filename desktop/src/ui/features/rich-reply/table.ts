import { display, el } from './data';
import type { Dataset } from './types';
import type { TableView } from './state';
export function renderTable(d: Dataset, title: string, pageSize: number, view: TableView, save: () => void): {
    node: HTMLElement;
    update: (rows: Dataset['rows']) => void;
} {
    const node = el('section', 'rich-reply__table');
    node.setAttribute('aria-label', title);
    const search = el('input');
    search.type = 'search';
    search.maxLength = 200;
    search.placeholder = '筛选此表';
    search.setAttribute('aria-label', `${title}筛选`);
    search.value = view.search;
    const viewport = el('div', 'rich-reply__table-scroll'), table = el('table'), head = el('thead'), tr = el('tr'), body = el('tbody');
    const count = el('span', 'rich-reply__muted'), prev = el('button', '', '上一页'), next = el('button', '', '下一页');
    prev.type = next.type = 'button';
    const foot = el('div', 'rich-reply__actions');
    foot.append(prev, count, next);
    const headers: HTMLTableCellElement[] = [];
    let rows: Dataset['rows'] = [];
    const draw = (): void => {
        let selected = rows.filter(row => row.some(v => display(v).toLocaleLowerCase().includes(view.search.toLocaleLowerCase())));
        const ci = d.columns.findIndex(c => c.id === view.sort);
        if (ci >= 0)
            selected = [...selected].sort((a, b) => { const x = a[ci], y = b[ci]; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'zh-CN', { numeric: true })) * (view.descending ? -1 : 1); });
        const pages = Math.max(1, Math.ceil(selected.length / pageSize));
        view.page = Math.min(view.page, pages - 1);
        headers.forEach((h, i) => h.setAttribute('aria-sort', ci === i ? (view.descending ? 'descending' : 'ascending') : 'none'));
        body.replaceChildren();
        for (const row of selected.slice(view.page * pageSize, (view.page + 1) * pageSize)) {
            const line = el('tr');
            for (const cell of row)
                line.append(el('td', '', display(cell)));
            body.append(line);
        }
        if (!selected.length) {
            const line = el('tr'), cell = el('td', '', '无匹配数据');
            cell.colSpan = d.columns.length;
            line.append(cell);
            body.append(line);
        }
        count.textContent = `${selected.length} 条 · ${view.page + 1}/${pages} 页`;
        prev.disabled = view.page === 0;
        next.disabled = view.page >= pages - 1;
    };
    for (const c of d.columns) {
        const th = el('th'), button = el('button', '', c.label);
        button.type = 'button';
        button.setAttribute('aria-label', `按${c.label}排序`);
        button.addEventListener('click', () => { view.descending = view.sort === c.id ? !view.descending : false; view.sort = c.id; view.page = 0; draw(); save(); });
        th.append(button);
        tr.append(th);
        headers.push(th);
    }
    search.addEventListener('input', () => { view.search = search.value; view.page = 0; draw(); save(); });
    prev.addEventListener('click', () => { view.page--; draw(); save(); });
    next.addEventListener('click', () => { view.page++; draw(); save(); });
    head.append(tr);
    table.append(head, body);
    viewport.append(table);
    node.append(search, viewport, foot);
    return { node, update: data => { rows = data; draw(); } };
}
