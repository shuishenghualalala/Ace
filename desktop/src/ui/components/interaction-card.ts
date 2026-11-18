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

function dispatchInteractionSubmit(card: HTMLElement, interactionId: string, text: string): void {
  const answer = text.trim();
  if (!answer || card.classList.contains('interaction-card--disabled')) return;
  card.dispatchEvent(new CustomEvent('crew:interaction-submit', {
    bubbles: true,
    detail: { interactionId, text: answer },
  }));
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
    const answerInput = document.createElement('textarea');
    answerInput.className = 'interaction-card__answer-input';
    answerInput.rows = 4;
    answerInput.placeholder = '在下方输入你的回答…';
    answerInput.dataset.interactionAnswer = '1';
    answerInput.dataset.interactionId = activityId;
    answerInput.setAttribute('aria-label', '你的回答');

    const answerActions = document.createElement('div');
    answerActions.className = 'interaction-card__answer-actions';
    const submitButton = document.createElement('button');
    submitButton.type = 'button';
    submitButton.className = 'interaction-card__answer-submit';
    submitButton.dataset.interactionSubmit = '1';
    submitButton.dataset.interactionId = activityId;
    submitButton.textContent = '提交回答';
    submitButton.disabled = true;
    const submitAnswer = (): void => {
      dispatchInteractionSubmit(card, activityId, answerInput.value);
    };
    answerInput.addEventListener('input', () => {
      submitButton.disabled = !answerInput.value.trim();
    });
    answerInput.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        if (!submitButton.disabled) submitAnswer();
      }
    });
    submitButton.addEventListener('click', submitAnswer);
    answerActions.appendChild(submitButton);
    question.append(answerInput);
    card.appendChild(question);
    card.appendChild(answerActions);
    renderEvidence(card, pageIds);
    return card;
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

interface ActivityControls {
  id: string;
  card: HTMLElement;
  controls: NodeListOf<HTMLInputElement | HTMLTextAreaElement | HTMLButtonElement>;
}

interface InteractionUnit {
  activities: ActivityControls[];
  completions: string[];
}

const ACTIVITY_SELECTOR = '.interaction-card[data-interaction-id]';
const COMPLETION_SELECTOR = '[data-interaction-completion]';
const CONTROL_SELECTOR = '[data-interaction-option], [data-interaction-answer], [data-interaction-submit]';

function unitElements(unit: HTMLElement, selector: string): HTMLElement[] {
  const children = Array.from(unit.querySelectorAll<HTMLElement>(selector));
  if (unit.matches(selector)) children.unshift(unit);
  return children;
}

function setActivityDisabled(activity: ActivityControls, disabled: boolean): void {
  activity.card.classList.toggle('interaction-card--disabled', disabled);
  for (const control of activity.controls) control.disabled = disabled;
}

/** Index only interaction cards/completions in the added transcript units. */
export class InteractionCardIndex {
  private readonly units = new Map<HTMLElement, InteractionUnit>();
  private readonly activities = new Set<ActivityControls>();
  private readonly completions = new Map<string, number>();
  private active: ActivityControls | undefined;

  constructor(private readonly submitted: Set<string>) {}

  update(added: readonly HTMLElement[], removed: readonly HTMLElement[], reordered = false): void {
    let touched = reordered && this.activities.size > 1;
    for (const unit of removed) {
      const previous = this.units.get(unit);
      if (!previous) continue;
      this.units.delete(unit);
      for (const activity of previous.activities) this.activities.delete(activity);
      for (const id of previous.completions) {
        const count = this.completions.get(id) ?? 0;
        if (count > 1) this.completions.set(id, count - 1);
        else this.completions.delete(id);
      }
      touched = true;
    }
    for (const unit of added) {
      if (this.units.has(unit)) continue;
      const activities = unitElements(unit, ACTIVITY_SELECTOR).map((card): ActivityControls => ({
        card,
        id: card.dataset.interactionId ?? '',
        controls: card.querySelectorAll(CONTROL_SELECTOR),
      }));
      const completions = unitElements(unit, COMPLETION_SELECTOR)
        .map((card) => card.dataset.interactionCompletion ?? '')
        .filter(Boolean);
      if (!activities.length && !completions.length) continue;
      this.units.set(unit, { activities, completions });
      for (const id of completions) this.completions.set(id, (this.completions.get(id) ?? 0) + 1);
      for (const activity of activities) {
        this.activities.add(activity);
        setActivityDisabled(activity, true);
      }
      touched = true;
    }
    if (touched || (this.active && this.submitted.has(this.active.id))) this.refreshActive();
  }

  submit(interactionId: string, origin: Element): boolean {
    if (this.active?.id !== interactionId || !this.active.card.contains(origin)) return false;
    if (this.submitted.has(interactionId)) {
      this.refreshActive();
      return false;
    }
    this.submitted.add(interactionId);
    this.refreshActive();
    return true;
  }

  deactivate(): void {
    if (this.active) setActivityDisabled(this.active, true);
    this.active = undefined;
  }

  private refreshActive(): void {
    let next: ActivityControls | undefined;
    for (const activity of this.activities) {
      if (!activity.id || this.completions.has(activity.id) || this.submitted.has(activity.id)) continue;
      if (!next || (next.card.compareDocumentPosition(activity.card) & Node.DOCUMENT_POSITION_FOLLOWING)) {
        next = activity;
      }
    }
    if (this.active === next) return;
    if (this.active) setActivityDisabled(this.active, true);
    if (next) setActivityDisabled(next, false);
    this.active = next;
  }
}

/** Standalone callers can initialize the same policy from a complete container. */
export function syncInteractionCards(container: HTMLElement): void {
  new InteractionCardIndex(submittedInteractions).update([container], []);
}
