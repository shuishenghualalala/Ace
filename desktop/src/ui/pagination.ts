/**
 * 通用分页条：左侧范围摘要 + 每页条数，右侧页码与跳转。
 * 视觉对齐工作台列表底部分页（图四风格）。
 */

import { escapeHtml } from './state';

export interface PaginationModel {
  page: number;
  pageSize: number;
  total: number;
}

export interface PaginationOptions {
  /** DOM id 前缀，用于区分同页多个分页实例 */
  id: string;
  pageSizeChoices?: number[];
}

export function paginate<T>(items: T[], page: number, pageSize: number): T[] {
  const safeSize = Math.max(1, pageSize);
  const totalPages = Math.max(1, Math.ceil(items.length / safeSize));
  const safePage = Math.min(Math.max(1, page), totalPages);
  const start = (safePage - 1) * safeSize;
  return items.slice(start, start + safeSize);
}

export function totalPages(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / Math.max(1, pageSize)));
}

/** 生成带省略号的页码序列，如 [1, '…', 4, 5, 6, '…', 20] */
export function pageNumbers(current: number, total: number): Array<number | '…'> {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: Array<number | '…'> = [1];
  const left = Math.max(2, current - 2);
  const right = Math.min(total - 1, current + 2);
  if (left > 2) pages.push('…');
  for (let p = left; p <= right; p += 1) pages.push(p);
  if (right < total - 1) pages.push('…');
  pages.push(total);
  return pages;
}

/**
 * 渲染分页 HTML。`model.page` 为 1-based 当前页。
 */
export function renderPagination(model: PaginationModel, opts: PaginationOptions): string {
  const choices = opts.pageSizeChoices ?? [10, 20, 50];
  const pages = totalPages(model.total, model.pageSize);
  const page = Math.min(Math.max(1, model.page), pages);
  const from = model.total === 0 ? 0 : (page - 1) * model.pageSize + 1;
  const to = model.total === 0 ? 0 : Math.min(model.total, page * model.pageSize);
  const nums = pageNumbers(page, pages);
  const prefix = opts.id;

  const sizeOptions = choices
    .map(
      (n) =>
        `<option value="${n}"${n === model.pageSize ? ' selected' : ''}>${n}</option>`,
    )
    .join('');

  const numBtns = nums
    .map((n) => {
      if (n === '…') {
        return `<span class="mm-pager__ellipsis" aria-hidden="true">…</span>`;
      }
      const active = n === page ? ' is-active' : '';
      return `<button type="button" class="mm-pager__num${active}" data-${prefix}-page="${n}">${n}</button>`;
    })
    .join('');

  return `
    <footer class="mm-pager" data-pager-id="${escapeHtml(prefix)}">
      <div class="mm-pager__meta">
        <span class="mm-pager__summary">显示 ${from} 至 ${to} 共 ${model.total} 条结果</span>
        <label class="mm-pager__size">
          <span>每页:</span>
          <select class="mm-pager__select" data-${prefix}-size aria-label="每页条数">${sizeOptions}</select>
        </label>
      </div>
      <div class="mm-pager__nav" role="navigation" aria-label="分页">
        <button type="button" class="mm-pager__btn" data-${prefix}-page="prev"${page <= 1 ? ' disabled' : ''} aria-label="上一页">&lt;</button>
        ${numBtns}
        <button type="button" class="mm-pager__btn" data-${prefix}-page="next"${page >= pages ? ' disabled' : ''} aria-label="下一页">&gt;</button>
        <label class="mm-pager__jump">
          <span>跳转</span>
          <input type="number" class="mm-pager__jump-input" data-${prefix}-jump min="1" max="${pages}" placeholder="${page}" aria-label="跳转到页码" />
        </label>
      </div>
    </footer>
  `;
}

export interface PaginationBindHandlers {
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

/**
 * 绑定分页事件。需在 render 后调用；`id` 与 renderPagination 一致。
 */
export function bindPagination(id: string, handlers: PaginationBindHandlers): void {
  const root = document.querySelector(`[data-pager-id="${id}"]`);
  if (!root) return;

  root.querySelectorAll<HTMLElement>(`[data-${id}-page]`).forEach((btn) => {
    btn.addEventListener('click', () => {
      const raw = btn.getAttribute(`data-${id}-page`);
      if (!raw || btn.hasAttribute('disabled')) return;
      const current = Number(root.querySelector('.mm-pager__num.is-active')?.textContent ?? '1');
      const max = Number(root.querySelector('.mm-pager__jump-input')?.getAttribute('max') ?? '1');
      if (raw === 'prev') handlers.onPageChange(Math.max(1, current - 1));
      else if (raw === 'next') handlers.onPageChange(Math.min(max, current + 1));
      else handlers.onPageChange(Number(raw));
    });
  });

  const sizeSel = root.querySelector<HTMLSelectElement>(`[data-${id}-size]`);
  sizeSel?.addEventListener('change', () => {
    handlers.onPageSizeChange(Number(sizeSel.value) || 10);
  });

  const jump = root.querySelector<HTMLInputElement>(`[data-${id}-jump]`);
  jump?.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const max = Number(jump.max) || 1;
    const v = Math.min(max, Math.max(1, Number(jump.value) || 1));
    handlers.onPageChange(v);
    jump.value = '';
  });
}
