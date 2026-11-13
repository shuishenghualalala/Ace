import type { MsgRole, UiMessage } from "../types";

export type ChatTurn =
  | { kind: "user"; message: UiMessage }
  | { kind: "agent"; messages: UiMessage[]; turnId: string };

export function isAgentRole(role: MsgRole): boolean {
  // Team 内部消息由 MessageItem → TeamAgentTurnBubble 单独渲染，保留成员身份与过程卡片。
  return role === "assistant" || role === "status" || role === "error";
}

/** 把扁平消息列表按「用户回合 / 连续 agent 回合」分组，对齐 Desktop renderChat 逻辑。 */
export function groupMessagesIntoTurns(messages: UiMessage[]): ChatTurn[] {
  const turns: ChatTurn[] = [];
  let i = 0;
  while (i < messages.length) {
    const msg = messages[i];
    if (!isAgentRole(msg.role)) {
      turns.push({ kind: "user", message: msg });
      i += 1;
      continue;
    }
    let j = i + 1;
    while (j < messages.length && isAgentRole(messages[j].role)) j += 1;
    turns.push({ kind: "agent", messages: messages.slice(i, j), turnId: msg.id });
    i = j;
  }
  return turns;
}

export function lastAgentTurnIndex(turns: ChatTurn[]): number {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (turns[i].kind === "agent") return i;
  }
  return -1;
}
