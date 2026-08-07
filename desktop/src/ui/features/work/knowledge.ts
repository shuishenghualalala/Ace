/**
 * Work 知识：用户可查看、创建与编辑的个人知识（Wiki 沉淀）。
 */

import { backendApi, workApi } from '../../backend-client';
import type { WorkKnowledgePage } from '../../backend-client';
import { openConfirmDialog } from '../../components/overlays';
import { workStore, loadWorkKnowledge } from '../../stores/work-store';

/** 渲染个人知识列表。 */
export function renderPersonalKnowledge(
  container: HTMLElement,
  pages: WorkKnowledgePage[],
  onSelect?: (page: WorkKnowledgePage) => void,
): void {
  container.className = 'mw-work-knowledge__personal';
  container.innerHTML = '';
  if (pages.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mw-work-knowledge__empty';
    empty.textContent = '暂无个人知识（确认摘要和成果会自动沉淀）';
    container.append(empty);
    return;
  }
  for (const page of pages) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'mw-work-knowledge__card';
    card.dataset.pageType = page.page_type;
    const title = document.createElement('span');
    title.className = 'mw-work-knowledge__title';
    title.textContent = page.title;
    const summary = document.createElement('span');
    summary.className = 'mw-work-knowledge__summary';
    summary.textContent = page.summary || '';
    card.append(title, summary);
    card.addEventListener('click', () => onSelect?.(page));
    container.append(card);
  }
}

async function renderPersonalKnowledgeDetail(
  container: HTMLElement,
  page: WorkKnowledgePage,
  rerender: () => Promise<void>,
): Promise<void> {
  container.className = 'mw-work-knowledge__detail';
  container.textContent = '正在打开知识…';
  try {
    const response = await backendApi.wikiPage(page.id);
    const title = document.createElement('input');
    const content = document.createElement('textarea');
    const actions = document.createElement('div');
    const save = document.createElement('button');
    const remove = document.createElement('button');
    const feedback = document.createElement('p');
    title.value = response.page.title;
    title.setAttribute('aria-label', '编辑知识标题');
    content.value = response.page.content ?? '';
    content.setAttribute('aria-label', '编辑知识内容');
    actions.className = 'mw-work-knowledge__detail-actions';
    save.type = 'button';
    save.textContent = '保存修改';
    remove.type = 'button';
    remove.className = 'mw-work-knowledge__delete';
    remove.textContent = '删除';
    feedback.className = 'mw-work-knowledge__feedback';
    feedback.setAttribute('aria-live', 'polite');
    save.addEventListener('click', () => {
      save.disabled = true;
      feedback.textContent = '正在保存…';
      void backendApi.wikiUpdatePage(page.id, {
        title: title.value.trim(),
        content: content.value,
      }).then(rerender).catch((error) => {
        save.disabled = false;
        feedback.textContent = `保存失败：${error instanceof Error ? error.message : String(error)}`;
      });
    });
    remove.addEventListener('click', () => {
      openConfirmDialog({
        trigger: remove,
        title: '删除个人知识',
        object: response.page.title,
        consequence: '该知识将从个人知识库移除，后续办公对话无法再引用。',
        confirmLabel: '删除',
        onConfirm: () => {
          void backendApi.wikiDeletePage(page.id).then(rerender).catch((error) => {
            feedback.textContent = `删除失败：${error instanceof Error ? error.message : String(error)}`;
          });
        },
      });
    });
    actions.append(remove, save);
    container.replaceChildren(title, content, actions, feedback);
  } catch (error) {
    container.textContent = `打开失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

/** 渲染完整的个人知识页面。 */
export async function renderKnowledgePage(container: HTMLElement): Promise<void> {
  container.className = 'mw-work-knowledge';
  container.innerHTML = '';
  const feedback = document.createElement('p');
  feedback.className = 'mw-work-knowledge__feedback';
  feedback.textContent = '正在加载知识库…';
  feedback.setAttribute('aria-live', 'polite');
  container.append(feedback);
  workStore.set({ error: null });
  await loadWorkKnowledge();
  const state = workStore.get();
  feedback.textContent = state.error ? `加载失败：${state.error}` : '';
  if (state.error) feedback.dataset.state = 'error';
  const toolbar = document.createElement('div');
  const newKnowledge = document.createElement('button');
  const layout = document.createElement('div');
  const lists = document.createElement('div');
  const detail = document.createElement('section');
  toolbar.className = 'mw-work-knowledge__toolbar';
  newKnowledge.type = 'button';
  newKnowledge.className = 'mw-work-knowledge__new';
  newKnowledge.textContent = '新建知识';
  toolbar.append(feedback, newKnowledge);
  layout.className = 'mw-work-knowledge__layout';
  lists.className = 'mw-work-knowledge__lists';
  detail.className = 'mw-work-knowledge__detail';
  detail.innerHTML = `
    <h2>知识详情</h2>
    <p class="mw-work-knowledge__summary">选择一条个人知识查看或编辑。</p>
  `;
  const showCreateForm = (): void => {
    const heading = document.createElement('h2');
    const hint = document.createElement('p');
    const create = document.createElement('form');
    const createTitle = document.createElement('input');
    const createContent = document.createElement('textarea');
    const actions = document.createElement('div');
    const cancel = document.createElement('button');
    const createButton = document.createElement('button');
    heading.textContent = '新建个人知识';
    hint.className = 'mw-work-knowledge__summary';
    hint.textContent = '保存已经确认的结论、方法或工作成果，后续对话可直接引用。';
    create.className = 'mw-work-knowledge__create';
    createTitle.required = true;
    createTitle.placeholder = '知识标题';
    createTitle.setAttribute('aria-label', '知识标题');
    createContent.required = true;
    createContent.placeholder = '确认后沉淀的内容';
    createContent.setAttribute('aria-label', '知识内容');
    actions.className = 'mw-work-knowledge__detail-actions';
    cancel.type = 'button';
    cancel.textContent = '取消';
    createButton.type = 'submit';
    createButton.textContent = '保存到个人知识';
    actions.append(cancel, createButton);
    create.append(createTitle, createContent, actions);
    cancel.addEventListener('click', () => {
      detail.innerHTML = `
        <h2>知识详情</h2>
        <p class="mw-work-knowledge__summary">选择一条个人知识查看或编辑。</p>
      `;
      newKnowledge.focus();
    });
    create.addEventListener('submit', (event) => {
      event.preventDefault();
      createButton.disabled = true;
      feedback.removeAttribute('data-state');
      feedback.textContent = '正在保存…';
      void workApi.savePersonalKnowledge({
        title: createTitle.value.trim(),
        content: createContent.value.trim(),
      })
        .then(() => renderKnowledgePage(container))
        .catch((error) => {
          createButton.disabled = false;
          feedback.dataset.state = 'error';
          feedback.textContent = `保存失败：${error instanceof Error ? error.message : String(error)}`;
        });
    });
    detail.replaceChildren(heading, hint, create);
    createTitle.focus();
  };
  newKnowledge.addEventListener('click', showCreateForm);
  const personal = document.createElement('section');
  personal.className = 'mw-work-knowledge__section';
  const personalHeader = document.createElement('h2');
  personalHeader.textContent = '个人知识';
  personal.append(personalHeader);
  const personalList = document.createElement('div');
  renderPersonalKnowledge(personalList, state.personalKnowledge, (page) => {
    void renderPersonalKnowledgeDetail(detail, page, () => renderKnowledgePage(container));
  });
  personal.append(personalList);
  lists.append(personal);
  layout.append(lists, detail);
  container.replaceChildren(toolbar, layout);

  const contextSearch = document.querySelector<HTMLInputElement>(
    '.mw-work-knowledge-context__search',
  );
  const contextScopes = [
    ...document.querySelectorAll<HTMLButtonElement>('[data-knowledge-scope]'),
  ];
  const applyContext = (): void => {
    const query = contextSearch?.value.trim().toLowerCase() ?? '';
    const scope = contextScopes.find((button) => button.getAttribute('aria-current') === 'page')
      ?.dataset.knowledgeScope ?? 'personal';
    personal.hidden = scope !== 'personal';
    detail.hidden = false;
    newKnowledge.hidden = false;
    layout.dataset.scope = scope;
    for (const card of lists.querySelectorAll<HTMLElement>('.mw-work-knowledge__card')) {
      card.hidden = Boolean(query && !card.textContent?.toLowerCase().includes(query));
    }
  };
  if (contextSearch) contextSearch.oninput = applyContext;
  for (const button of contextScopes) {
    button.onclick = () => {
      for (const candidate of contextScopes) candidate.removeAttribute('aria-current');
      button.setAttribute('aria-current', 'page');
      applyContext();
    };
  }
  applyContext();
}
