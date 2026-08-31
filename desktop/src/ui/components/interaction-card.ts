import type { ToolCallInfo } from '../chat-render';
import { createTranscriptMarkdown } from './transcript';

type JsonObject = Record<string, unknown>;

interface InteractionOption {
  id: string;
  label: string;
  description?: string;
}

const submittedInteractions = new Set<string>();

function objectValue(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null;
}

function parseObject(value: string | undefined): JsonObject | null {
  if (!value) return null;
  try {
    return objectValue(JSON.parse(value));
  } catch {
    return null;
  }
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(textValue).filter(Boolean);
}

function normalizeOptions(value: unknown): InteractionOption[] {
  if (!Array.isArray(value)) return [];
  const options: InteractionOption[] = [];
  value.forEach((item, index) => {
    if (typeof item === 'string') {
      const label = item.trim();
      if (label) options.push({ id: String.fromCharCode(65 + index), label });
      return;
    }
    const raw = objectValue(item);
    if (!raw) return;
    const id = textValue(raw.id) || textValue(raw.value) || String.fromCharCode(65 + index);
    const label = textValue(raw.label) || textValue(raw.text);
    if (!label) return;
    const description = textValue(raw.description);
    options.push({ id, label, ...(description ? { description } : {}) });
  });
  return options.slice(0, 12);
}

function appendText(parent: HTMLElement, className: string, text: string): HTMLElement {
  const element = document.createElement('div');
  element.className = className;
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

function activityLabel(activityType: string): string {
  const labels: Record<string, string> = {
    quiz: '知识测验',
    single_choice: '单项选择',
    interview: '模拟面试',
    recall: '主动回忆',
    teach_back: '复述练习',
    flashcard: '记忆卡',
  };
  return labels[activityType] || '学习练习';
}

function renderProgress(parent: HTMLElement, progress: JsonObject | null): void {
  if (!progress) return;
  const current = numberValue(progress.current);
  const total = numberValue(progress.total);
  if (current === null || total === null || current < 1 || total < current) return;
  const label = document.createElement('span');
  label.className = 'interaction-card__progress';
  label.textContent = `${current} / ${total}`;
  label.setAttribute('aria-label', `学习进度 ${current}/${total}`);
  parent.appendChild(label);
}

function renderEvidence(parent: HTMLElement, pageIds: string[]): void {
  if (pageIds.length === 0) return;
  const evidence = document.createElement('div');
  evidence.className = 'interaction-card__evidence';
  evidence.textContent = `依据 ${pageIds.length} 个 Wiki 页面`;
  evidence.title = pageIds.join('\n');
  parent.appendChild(evidence);
}

function renderActivityCard(tool: ToolCallInfo): HTMLElement | null {
  if (tool.status !== 'done') return null;
  const args = parseObject(tool.args);
  const result = parseObject(tool.result);
  if (!args || textValue(args.action) !== 'create' || !result) return null;
  const activity = objectValue(result.activity);
  if (!activity) return null;

  const activityId = textValue(activity.id);
  const prompt = textValue(activity.prompt) || textValue(args.prompt);
  if (!activityId || !prompt) return null;
  const activityType = textValue(activity.activity_type) || textValue(args.activity_type);
  const payload = objectValue(activity.public_payload) || objectValue(args.public_payload) || {};
  if (textValue(payload.schema) !== 'crew.interaction.v1') return null;
  const interaction = objectValue(payload.interaction) || payload;
  const options = normalizeOptions(interaction.options);
  const progress = objectValue(payload.progress);
  const title = textValue(payload.title) || activityLabel(activityType);
  const pageIds = stringList(activity.evidence_page_ids);

  const card = document.createElement('section');
  card.className = 'followup-card interaction-card interaction-card--activity';
  card.dataset.interactionId = activityId;
  card.setAttribute('aria-label', title);

  const heading = document.createElement('header');
  heading.className = 'interaction-card__heading';
  const headingCopy = document.createElement('div');
  headingCopy.className = 'interaction-card__heading-copy';
  appendText(headingCopy, 'followup-card__source', 'Wiki 学习');
  appendText(headingCopy, 'followup-card__title', title);
  heading.appendChild(headingCopy);
  renderProgress(heading, progress);
  card.appendChild(heading);

  const question = document.createElement('div');
  question.className = 'followup-card__question';
  question.appendChild(createTranscriptMarkdown(prompt, {
    className: 'followup-card__qtext interaction-card__markdown',
  }));

  if (options.length > 0) {
    const choices = document.createElement('div');
    choices.className = 'followup-card__options';
    choices.setAttribute('role', 'radiogroup');
    choices.setAttribute('aria-label', prompt);
    options.forEach((option) => {
      const optionRow = document.createElement('label');
      optionRow.className = 'followup-card__option';
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = `learning-${activityId}`;
      input.value = option.id;
      input.dataset.interactionOption = option.id;
      input.dataset.interactionId = activityId;
      input.setAttribute('aria-label', `${option.id}，${option.label}`);
      const optionKey = document.createElement('span');
      optionKey.className = 'followup-card__option-key';
      optionKey.setAttribute('aria-hidden', 'true');
      optionKey.textContent = option.id;
      const copy = document.createElement('span');
      copy.className = 'followup-card__option-copy';
      appendText(copy, 'interaction-card__option-label', option.label);
      if (option.description) {
        appendText(copy, 'followup-card__option-description', option.description);
      }
      optionRow.append(input, optionKey, copy);
      input.addEventListener('change', () => {
        input.dispatchEvent(new CustomEvent('crew:interaction-submit', {
          bubbles: true,
          detail: {
            interactionId: activityId,
            text: `我选择 ${option.id}：${option.label}`,
          },
        }));
      });
      choices.appendChild(optionRow);
    });
    question.appendChild(choices);
  } else {
    appendText(question, 'followup-card__subtitle interaction-card__hint', '在下方输入你的回答');
  }
  card.appendChild(question);

  renderEvidence(card, pageIds);
  return card;
}

function renderFeedbackList(parent: HTMLElement, title: string, items: string[], tone: string): void {
  if (items.length === 0) return;
  const section = document.createElement('section');
  section.className = `interaction-card__feedback interaction-card__feedback--${tone}`;
  appendText(section, 'interaction-card__feedback-title', title);
  const list = document.createElement('ul');
  items.slice(0, 4).forEach((item) => {
    const row = document.createElement('li');
    row.textContent = item;
    list.appendChild(row);
  });
  section.appendChild(list);
  parent.appendChild(section);
}

function renderAssessmentCard(tool: ToolCallInfo): HTMLElement | null {
  if (tool.name !== 'wiki_learning_assess' || tool.status !== 'done') return null;
  const result = parseObject(tool.result);
  if (!result) return null;
  const assessment = objectValue(result.assessment);
  if (!assessment) return null;
  const activityId = textValue(assessment.activity_id);
  const summary = textValue(assessment.summary);
  const score = numberValue(assessment.score);
  if (!activityId || !summary || score === null) return null;

  const card = document.createElement('section');
  card.className = 'followup-card interaction-card interaction-card--assessment';
  card.dataset.interactionCompletion = activityId;
  card.setAttribute('aria-label', '本题反馈');

  const header = document.createElement('header');
  header.className = 'interaction-card__heading';
  const headingCopy = document.createElement('div');
  headingCopy.className = 'interaction-card__heading-copy';
  appendText(headingCopy, 'followup-card__source', 'Wiki 学习');
  appendText(headingCopy, 'followup-card__title', '本题反馈');
  header.appendChild(headingCopy);
  const scoreBadge = document.createElement('span');
  scoreBadge.className = 'interaction-card__score';
  scoreBadge.textContent = `${Math.round(score * 100)}%`;
  scoreBadge.setAttribute('aria-label', `得分 ${Math.round(score * 100)}%`);
  header.appendChild(scoreBadge);
  card.appendChild(header);

  const summaryBlock = document.createElement('div');
  summaryBlock.className = 'followup-card__question';
  summaryBlock.appendChild(createTranscriptMarkdown(summary, {
    className: 'followup-card__qtext interaction-card__markdown',
  }));
  card.appendChild(summaryBlock);
  const meter = document.createElement('div');
  meter.className = 'interaction-card__meter';
  meter.setAttribute('role', 'progressbar');
  meter.setAttribute('aria-valuemin', '0');
  meter.setAttribute('aria-valuemax', '100');
  meter.setAttribute('aria-valuenow', String(Math.round(score * 100)));
  const fill = document.createElement('span');
  fill.dataset.level = String(Math.round(Math.max(0, Math.min(1, score)) * 10));
  meter.appendChild(fill);
  card.appendChild(meter);

  renderFeedbackList(card, '做得好的地方', stringList(assessment.strengths), 'positive');
  renderFeedbackList(card, '可以继续加强', stringList(assessment.gaps), 'growth');
  renderEvidence(card, stringList(assessment.evidence_page_ids));
  return card;
}

export function renderToolInteractionCard(tool: ToolCallInfo): HTMLElement | null {
  return renderActivityCard(tool) || renderAssessmentCard(tool);
}

export function markInteractionSubmitted(interactionId: string): void {
  if (interactionId) submittedInteractions.add(interactionId);
}

/** Keep only the latest unanswered choice card interactive after history reloads. */
export function syncInteractionCards(container: HTMLElement): void {
  const completed = new Set(
    Array.from(container.querySelectorAll<HTMLElement>('[data-interaction-completion]'))
      .map((item) => item.dataset.interactionCompletion || '')
      .filter(Boolean),
  );
  const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-interaction-id]'))
    .filter((item) => item.classList.contains('interaction-card'));
  const active = [...cards].reverse().find((card) => {
    const id = card.dataset.interactionId || '';
    return id && !completed.has(id) && !submittedInteractions.has(id);
  });
  cards.forEach((card) => {
    const id = card.dataset.interactionId || '';
    const disabled = card !== active || completed.has(id) || submittedInteractions.has(id);
    card.classList.toggle('interaction-card--disabled', disabled);
    card.querySelectorAll<HTMLInputElement>('[data-interaction-option]').forEach((input) => {
      input.disabled = disabled;
    });
  });
}
