import { createIcon } from './icon';

export type TurnFileStatus =
  | 'added'
  | 'modified'
  | 'deleted'
  | 'renamed'
  | 'binary'
  | 'unavailable';

export interface TurnFileSummary {
  path: string;
  name: string;
  added: number;
  removed: number;
  status: TurnFileStatus;
}

const STATUS_LABELS: Record<TurnFileStatus, string> = {
  added: '新增',
  modified: '修改',
  deleted: '删除',
  renamed: '重命名',
  binary: '二进制',
  unavailable: '不可用',
};

function formatCount(value: number): string {
  return Math.abs(value).toLocaleString('en-US');
}

function splitPath(path: string): { dir: string; name: string } {
  const normalized = path.replace(/\\/g, '/');
  const slash = normalized.lastIndexOf('/');
  return slash < 0
    ? { dir: '', name: normalized }
    : { dir: normalized.slice(0, slash + 1), name: normalized.slice(slash + 1) };
}

function createFileItem(file: TurnFileSummary): HTMLLIElement {
  const item = document.createElement('li');
  item.className = 'msg__file-changes__item mw-turn-files__item';
  item.dataset.fileStatus = file.status;
  const path = splitPath(file.path);

  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'msg__file-changes__row mw-turn-files__row';
  open.dataset.fileChangesOpen = 'files';
  open.dataset.fileChangesPath = file.path;
  open.title = '在看板中查看改动';

  const pathElement = document.createElement('span');
  pathElement.className = 'msg__file-changes__path';
  pathElement.title = file.path;
  if (path.dir) {
    const directory = document.createElement('span');
    directory.className = 'msg__file-changes__dir';
    directory.textContent = path.dir;
    pathElement.appendChild(directory);
  }
  const name = document.createElement('span');
  name.className = 'msg__file-changes__name';
  name.textContent = path.name || file.name;
  pathElement.appendChild(name);

  const badges = document.createElement('span');
  badges.className = 'msg__file-changes__badges';
  const status = document.createElement('span');
  status.className = `msg__file-changes__badge msg__file-changes__badge--status msg__file-changes__badge--${file.status}`;
  status.textContent = STATUS_LABELS[file.status];
  badges.appendChild(status);
  if (file.added > 0) {
    const added = document.createElement('span');
    added.className = 'msg__file-changes__badge msg__file-changes__badge--add';
    added.textContent = `+${formatCount(file.added)}`;
    badges.appendChild(added);
  }
  if (file.removed > 0) {
    const removed = document.createElement('span');
    removed.className = 'msg__file-changes__badge msg__file-changes__badge--del';
    removed.textContent = `-${formatCount(file.removed)}`;
    badges.appendChild(removed);
  }
  open.append(pathElement, badges);

  const reveal = document.createElement('button');
  reveal.type = 'button';
  reveal.className = 'msg__file-changes__reveal';
  const cannotReveal = file.status === 'deleted' || file.status === 'unavailable';
  if (cannotReveal) {
    reveal.disabled = true;
    reveal.classList.add('is-disabled');
    reveal.title = file.status === 'deleted' ? '文件已删除' : '文件不可用';
    reveal.setAttribute('aria-label', reveal.title);
  } else {
    reveal.dataset.fileReveal = file.path;
    reveal.title = '打开方式';
    reveal.setAttribute('aria-label', `${path.name || file.name} 的打开方式`);
    reveal.setAttribute('aria-haspopup', 'menu');
    reveal.setAttribute('aria-expanded', 'false');
  }
  reveal.appendChild(createIcon('icon-folder', { size: 16 }));
  item.append(open, reveal);
  return item;
}

export function createTurnFilesView(
  files: TurnFileSummary[],
  previewCount = 3,
): HTMLElement | null {
  if (files.length === 0) return null;
  const totalAdded = files.reduce((total, file) => total + file.added, 0);
  const totalRemoved = files.reduce((total, file) => total + file.removed, 0);
  const firstPath = files[0]?.path ?? '';

  const card = document.createElement('section');
  card.className = 'msg__file-changes mw-turn-files';
  card.dataset.fileChangesCard = '1';
  card.setAttribute('aria-label', `已编辑 ${files.length} 个文件`);

  const header = document.createElement('header');
  header.className = 'msg__file-changes__head mw-turn-files__header';
  const lead = document.createElement('button');
  lead.type = 'button';
  lead.className = 'msg__file-changes__lead';
  lead.dataset.fileChangesOpen = 'files';
  if (firstPath) lead.dataset.fileChangesPath = firstPath;
  lead.title = '在看板中查看文件改动';
  lead.appendChild(createIcon('icon-file', { size: 18 }));
  const title = document.createElement('span');
  title.className = 'msg__file-changes__title';
  title.textContent = `已编辑 ${files.length} 个文件`;
  lead.appendChild(title);
  if (totalAdded > 0 || totalRemoved > 0) {
    const stats = document.createElement('span');
    stats.className = 'msg__file-changes__stats';
    if (totalAdded > 0) {
      const added = document.createElement('span');
      added.className = 'msg__file-changes__stat msg__file-changes__stat--add';
      added.textContent = `+${formatCount(totalAdded)}`;
      stats.appendChild(added);
    }
    if (totalRemoved > 0) {
      const removed = document.createElement('span');
      removed.className = 'msg__file-changes__stat msg__file-changes__stat--del';
      removed.textContent = `-${formatCount(totalRemoved)}`;
      stats.appendChild(removed);
    }
    lead.appendChild(stats);
  }

  const review = document.createElement('button');
  review.type = 'button';
  review.className = 'msg__file-changes__review';
  review.dataset.fileChangesOpen = 'files';
  if (firstPath) review.dataset.fileChangesPath = firstPath;
  review.textContent = '查看';
  review.title = '在看板中查看文件改动';
  header.append(lead, review);

  const list = document.createElement('ul');
  list.className = 'msg__file-changes__list mw-turn-files__list';
  list.append(...files.slice(0, previewCount).map(createFileItem));
  card.append(header, list);

  const remaining = files.slice(previewCount);
  if (remaining.length > 0) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'msg__file-changes__more-btn';
    more.textContent = `再显示 ${remaining.length} 个文件`;
    more.addEventListener('click', () => {
      list.append(...remaining.map(createFileItem));
      more.remove();
    }, { once: true });
    card.appendChild(more);
  }
  return card;
}
