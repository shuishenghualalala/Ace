import { Fragment } from "react";
import type { Skill } from "../types";

const COMMANDS = [
  { name: "/plan", detail: "进入计划模式" },
  { name: "/craft", detail: "回到普通对话" },
  { name: "/new", detail: "在当前工作空间新建对话" },
] as const;

type CommandName = (typeof COMMANDS)[number]["name"];
export type ComposerCommandActions = Partial<Record<CommandName, () => void>>;

type CommandItem = { kind: "command"; token: CommandName; detail: string };
type SkillItem = { kind: "skill"; token: string; detail: string };
export type ComposerSlashItem = CommandItem | SkillItem;

/** Complete a leading slash token, not an inline path or a submitted argument. */
export function composerSlashQuery(text: string): string | null {
  const token = text.trimStart();
  const match = /^\/(?:([\p{L}\p{N}_-]+(?:\/[\p{L}\p{N}_-]+)*\/?))?$/u.exec(token);
  return match?.[0] === token ? (match[1] ?? "").toLowerCase() : null;
}

export function composerSlashAction(text: string, actions: ComposerCommandActions): (() => void) | undefined {
  const token = text.trim().toLowerCase();
  const command = COMMANDS.find(({ name }) => name === token);
  return command && actions[command.name];
}

export function composerSlashItems(
  query: string, actions: ComposerCommandActions, skills: readonly Skill[],
): ComposerSlashItem[] {
  const commands = COMMANDS.filter(({ name }) => actions[name]);
  const reserved = new Set<string>(commands.map(({ name }) => name));
  return [
    ...commands
      .filter(({ name, detail }) => name.slice(1).startsWith(query) || detail.includes(query))
      .map(({ name, detail }): CommandItem => ({ kind: "command", token: name, detail })),
    ...skills
      .filter(({ slug, display_name, name }) =>
        !reserved.has(`/${slug.toLowerCase()}`) &&
        [slug, display_name, name].some((value) => value?.toLowerCase().includes(query)))
      .slice(0, 30)
      .map((skill): SkillItem => ({ kind: "skill", token: `/${skill.slug}`, detail: skill.display_name || skill.name })),
  ];
}

export function ComposerSlashMenu({ id, items, selected, onSelect }: {
  id: string;
  items: readonly ComposerSlashItem[];
  selected: number;
  onSelect: (item: ComposerSlashItem) => void;
}) {
  return (
    <div className="at-popover" id={id} role="listbox" aria-label="斜杠命令与技能">
      {items.map((item, index) => (
        <Fragment key={`${item.kind}:${item.token}`}>
          {item.kind !== items[index - 1]?.kind && (
            <div className="at-popover__section" aria-hidden="true">{item.kind === "command" ? "命令" : "技能"}</div>
          )}
          <button type="button" role="option" id={`${id}-${index}`} aria-selected={index === selected}
            aria-label={`${item.kind === "command" ? "命令" : "技能"} ${item.token} ${item.detail}`}
            className="at-popover__item" tabIndex={-1}
            onMouseDown={(event) => event.preventDefault()} onClick={() => onSelect(item)}>
            <span className="at-popover__display">{item.token}</span>
            <span className="at-popover__meta">{item.detail}</span>
          </button>
        </Fragment>
      ))}
    </div>
  );
}
