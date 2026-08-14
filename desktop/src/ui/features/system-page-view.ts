import { createIcon, type IconId } from '../components/icon';
import { setRuntimeStyle } from '../components/runtime-style';

export type SystemPageStatus = 'loading' | 'ready' | 'partial' | 'offline' | 'error';

export interface SystemPageView {
  element: HTMLElement;
  setStatus(status: SystemPageStatus, message: string): void;
}

function kpi(label: string, valueId: string, iconId: string, icon: IconId): HTMLElement {
  const card = document.createElement('article');
  const symbol = document.createElement('span');
  const copy = document.createElement('span');
  const labelElement = document.createElement('span');
  const value = document.createElement('strong');
  card.className = 'system-page__kpi';
  symbol.className = 'system-page__kpi-icon';
  symbol.id = iconId;
  symbol.append(createIcon(icon, { size: 20 }));
  copy.className = 'system-page__kpi-copy';
  labelElement.className = 'system-page__kpi-label';
  labelElement.textContent = label;
  value.className = 'system-page__kpi-value';
  value.id = valueId;
  value.textContent = '—';
  copy.append(labelElement, value);
  card.append(symbol, copy);
  return card;
}

function resourceRow(label: string, barId: string, valueId: string): HTMLElement {
  const row = document.createElement('div');
  const track = document.createElement('span');
  const fill = document.createElement('span');
  const value = document.createElement('span');
  row.className = 'system-page__resource';
  track.className = 'system-page__resource-track';
  fill.className = 'system-page__resource-fill';
  fill.id = barId;
  setRuntimeStyle(fill, 'width', '0%');
  value.className = 'system-page__resource-value';
  value.id = valueId;
  value.textContent = '—';
  track.append(fill);
  row.append(
    Object.assign(document.createElement('span'), {
      className: 'system-page__resource-label',
      textContent: label,
    }),
    track,
    value,
  );
  return row;
}

export function createSystemPageView(options: { onRefresh(): void }): SystemPageView {
  const element = document.createElement('section');
  const header = document.createElement('header');
  const copy = document.createElement('div');
  const title = document.createElement('h1');
  const description = document.createElement('p');
  const headerActions = document.createElement('div');
  const refresh = document.createElement('button');
  const status = document.createElement('div');
  const kpis = document.createElement('div');
  const cards = document.createElement('div');
  const resources = document.createElement('section');
  const resourcesHeader = document.createElement('header');
  const resourcesHeading = document.createElement('div');
  const resourcesBody = document.createElement('div');
  const services = document.createElement('section');
  const servicesHeader = document.createElement('header');
  const servicesTable = document.createElement('div');

  element.className = 'page-shell page-shell--system system-page';
  element.dataset.systemPage = '';
  header.className = 'page-header page-header--hub';
  copy.className = 'page-header__copy';
  title.className = 'page-header__title';
  title.textContent = '系统总览';
  description.className = 'page-header__desc';
  description.textContent = '查看运行状态、资源占用、会话负载与平台服务。日志和使用统计在设置中统一管理。';
  headerActions.className = 'page-header__actions';
  refresh.id = 'sys-resources-refresh';
  refresh.type = 'button';
  refresh.className = 'mw-button mw-button--secondary mw-button--default';
  refresh.setAttribute('aria-label', '立即刷新');
  refresh.append(createIcon('icon-refresh', { size: 16 }), document.createTextNode('刷新'));
  refresh.addEventListener('click', options.onRefresh);
  copy.append(title, description);
  headerActions.append(refresh);
  header.append(copy, headerActions);

  status.id = 'sys-overview-state';
  status.className = 'system-page__state';
  status.setAttribute('role', 'status');

  kpis.className = 'system-page__kpis';
  kpis.append(
    kpi('运行时长', 'sys-kpi-uptime', 'sys-kpi-uptime-icon', 'process-clock'),
    kpi('活跃 / 并发上限', 'sys-kpi-cpu', 'sys-kpi-concurrency-icon', 'status-running'),
    kpi('累计 Token', 'sys-kpi-memory', 'sys-kpi-token-icon', 'process-memory'),
    kpi('会话 / 运行中', 'sys-kpi-tasks', 'sys-kpi-session-icon', 'icon-task'),
  );

  cards.className = 'system-page__cards';
  resources.id = 'sys-resources';
  resources.className = 'system-page__card';
  resourcesHeader.className = 'system-page__card-header';
  resourcesHeading.append(
    Object.assign(document.createElement('h2'), {
      className: 'system-page__card-title',
      textContent: '资源占用',
    }),
    Object.assign(document.createElement('span'), {
      id: 'sys-refresh-stamp',
      className: 'system-page__card-meta',
      textContent: '暂无数据',
    }),
  );
  resourcesHeader.append(resourcesHeading);
  resourcesBody.className = 'system-page__resources';
  resourcesBody.append(
    Object.assign(document.createElement('div'), {
      id: 'sys-resources-error',
      className: 'system-page__inline-error',
      hidden: true,
    }),
    Object.assign(document.createElement('div'), {
      id: 'sys-process-meta',
      className: 'system-page__process',
      hidden: true,
    }),
    resourceRow('CPU', 'sys-bar-cpu', 'sys-bar-cpu-val'),
    resourceRow('内存', 'sys-bar-mem', 'sys-bar-mem-val'),
    resourceRow('磁盘', 'sys-bar-disk', 'sys-bar-disk-val'),
    resourceRow('网络', 'sys-bar-net', 'sys-bar-net-val'),
  );
  resources.append(resourcesHeader, resourcesBody);

  services.className = 'system-page__card';
  servicesHeader.className = 'system-page__card-header';
  servicesHeader.append(
    Object.assign(document.createElement('h2'), {
      className: 'system-page__card-title',
      textContent: '平台服务',
    }),
    Object.assign(document.createElement('span'), {
      id: 'sys-services-hint',
      className: 'system-page__card-meta',
      textContent: '—',
    }),
  );
  servicesTable.id = 'sys-services-table';
  servicesTable.className = 'system-page__services';
  servicesTable.textContent = '暂无服务状态';
  services.append(servicesHeader, servicesTable);
  cards.append(resources, services);
  element.append(header, status, kpis, cards);

  return {
    element,
    setStatus(nextStatus, message) {
      const hidden = nextStatus === 'ready' || !message;
      status.dataset.state = nextStatus;
      status.hidden = hidden;
      status.textContent = hidden ? '' : message;
      refresh.disabled = nextStatus === 'loading';
      refresh.setAttribute('aria-busy', String(nextStatus === 'loading'));
    },
  };
}
