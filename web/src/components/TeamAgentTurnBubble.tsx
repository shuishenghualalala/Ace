import type { TeamMemberView, UiMessage } from "../types";
import { buildAgentTurnState } from "../lib/agentTurnState";
import { openLocalPath } from "../lib/localPath";
import AgentTurnBubble from "./AgentTurnBubble";
import MarkdownContent from "./MarkdownContent";

interface Props {
  message: UiMessage;
  isStreaming?: boolean;
  teamMembers?: TeamMemberView[];
  onRetryMention?: (message: UiMessage) => void;
  onCancelMention?: (message: UiMessage) => void;
}

function isCrewAgent(message: UiMessage): boolean {
  return String(message.agentId || "").trim() === "crew::builtin";
}

function resolveTeamCommunicationRole(message: UiMessage, fallback: string): string {
  const role = String(fallback || "").trim();
  const target = (message.mentionTo || [])
    .map((item) => String(item || "").trim())
    .find(Boolean);
  if (!target || !/^向\s+\S+/.test(role)) return role;
  const label = target === "crew::builtin" ? "Crew" : target;
  return role.replace(/^向\s+\S+/, `向 ${label}`);
}

function compactTeamRole(role: string): string {
  const normalized = String(role || "")
    .replace(/[\r\n]+/g, " ")
    .replace(/[`*_#>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return "";
  const beforeInternalSections = normalized.split(/(?:工作原则|团队协作关系|输出格式|工作安排|边界)\s*[-:：]?/i)[0]
    .replace(/^(职责|角色|职能)\s*[-:：]?\s*/i, "")
    .replace(/^[-*\d.、)\s]+/, "")
    .trim();
  const compact = beforeInternalSections || normalized;
  return compact.length > 48 ? `${compact.slice(0, 48).trimEnd()}…` : compact;
}

function resolveIdentity(message: UiMessage, teamMembers: TeamMemberView[] = []) {
  const member = (message.agentId ? teamMembers.find((item) => item.agentId === message.agentId) : undefined)
    || (message.agentName ? teamMembers.find((item) => item.name === message.agentName) : undefined)
    || (message.isLeader ? teamMembers.find((item) => item.isLeader) : undefined);
  const crewLogo = isCrewAgent(message) || member?.agentId === "crew::builtin";
  return {
    name: crewLogo ? "Crew" : (member?.name || message.agentName || "Agent"),
    badge: member?.displayBadge || "?",
    role: compactTeamRole(resolveTeamCommunicationRole(
      message,
      member?.isLeader ? "leader" : (message.isLeader ? "leader" : (message.agentRole || member?.role || "").trim()),
    )),
    tone: member?.tone ?? message.agentTone ?? 0,
    crewLogo,
  };
}

function artifactIcon(artifact: NonNullable<UiMessage["artifacts"]>[number]): string {
  const source = `${artifact.kind || ""} ${artifact.mime_type || ""} ${artifact.content_type || ""} ${artifact.path || ""} ${artifact.title || ""}`.toLowerCase();
  const ext = source.match(/\.([a-z0-9]+)(?:\?|#|\s|$)/)?.[1] || "";
  if (artifact.content_type === "inode/directory") return "DIR";
  if (artifact.kind === "image" || ["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return "IMG";
  if (artifact.kind === "html" || ["html", "htm"].includes(ext)) return "HTML";
  if (artifact.kind === "spreadsheet" || ["xlsx", "xls", "csv", "tsv"].includes(ext)) return "XLS";
  if (artifact.kind === "presentation" || ["pptx", "ppt"].includes(ext)) return "PPT";
  if (["docx", "doc"].includes(ext)) return "DOC";
  if (ext === "pdf") return "PDF";
  if (["md", "markdown"].includes(ext)) return "MD";
  if (["json", "yaml", "yml"].includes(ext)) return "DATA";
  if (artifact.kind === "text" || ["txt", "log"].includes(ext)) return "TXT";
  return "FILE";
}

function ArtifactCards({ artifacts }: { artifacts?: UiMessage["artifacts"] }) {
  const list = artifacts?.filter((item) => item.title || item.path) || [];
  if (list.length === 0) return null;
  return (
    <div className="team-artifacts" aria-label="产物">
      {list.map((artifact, index) => {
        const title = artifact.title || artifact.path || "产物";
        const body = artifact.summary || artifact.path || artifact.mime_type || artifact.content_type || "";
        const content = (
          <>
            <span className="team-artifact__icon" aria-hidden="true">{artifactIcon(artifact)}</span>
            <span className="team-artifact__body">
              <strong>{title}</strong>
              {body && <em>{body}</em>}
            </span>
          </>
        );
        return artifact.path ? (
          <button
            className="team-artifact"
            type="button"
            title={artifact.path}
            key={`${title}_${index}`}
            onClick={() => void openLocalPath(artifact.path || "")}
          >
            {content}
          </button>
        ) : (
          <div className="team-artifact" title={title} key={`${title}_${index}`}>{content}</div>
        );
      })}
    </div>
  );
}

function highlightMentionMarkdown(text: string): string {
  return String(text || "").replace(
    /(^|[\s([{（【,，。.!！?？;；:：])@([A-Za-z0-9_\-\u4e00-\u9fa5][A-Za-z0-9_\-\u4e00-\u9fa5:.：]*)/g,
    "$1**@$2**",
  );
}

const RETRYABLE_MENTION_STATUSES = new Set(["failed", "expired", "cancelled"]);
const ACTIVE_MENTION_STATUSES = new Set(["published", "waiting_reply", "queued", "delivered"]);

export default function TeamAgentTurnBubble({
  message,
  isStreaming = false,
  teamMembers,
  onRetryMention,
  onCancelMention,
}: Props) {
  const identity = resolveIdentity(message, teamMembers);
  const state = buildAgentTurnState([{
    ...message,
    text: highlightMentionMarkdown(message.text),
  }], isStreaming);
  const processText = (message.processText || "").trim();
  const isUserMentionAnswer = message.communicationKind === "user_mention_answer";
  const canRetryMention = isUserMentionAnswer
    && RETRYABLE_MENTION_STATUSES.has(String(message.communicationStatus || "").trim())
    && Boolean(message.communicationRequestText)
    && Boolean(onRetryMention);
  const canCancelMention = isUserMentionAnswer
    && ACTIVE_MENTION_STATUSES.has(String(message.communicationStatus || "").trim())
    && Boolean(onCancelMention);
  const isCollapsible = message.displayMode === "collapsible";
  const isPlanningProgress = message.eventType === "team_planning_progress";
  const afterContent = (
    <>
      <ArtifactCards artifacts={message.artifacts} />
      {processText && !isCollapsible && (
        <details className="team-internal__collapse team-internal__collapse--embedded">
          <summary>{message.collapsedTitle || "执行过程"}</summary>
          <div className="team-internal__process md-body">
            <MarkdownContent content={processText} />
          </div>
        </details>
      )}
      {(canRetryMention || canCancelMention) && (
        <div className="team-internal__communication-actions">
          {canRetryMention && (
            <button
              type="button"
              onClick={(event) => {
                event.currentTarget.disabled = true;
                onRetryMention?.(message);
              }}
            >
              重试
            </button>
          )}
          {canCancelMention && (
            <button
              type="button"
              onClick={(event) => {
                event.currentTarget.disabled = true;
                onCancelMention?.(message);
              }}
            >
              取消
            </button>
          )}
        </div>
      )}
    </>
  );
  const bubbleClassName = `team-internal__bubble team-internal__bubble--tone-${identity.tone}`
    + (identity.crewLogo ? " is-crew" : "")
    + (isPlanningProgress ? " is-planning" : "")
    + " md-body";

  return (
      <AgentTurnBubble
      identity={{ ...identity, teamLogo: isPlanningProgress }}
      state={state}
      isStreaming={isStreaming}
      id={`message-${message.id}`}
      className={`team-internal${isPlanningProgress ? " team-internal--planning" : ""}`}
      bubbleClassName={bubbleClassName}
      processClassName="team-internal__agent-process"
      collapsibleTitle={isCollapsible ? (message.collapsedTitle || "执行过程") : undefined}
      afterContent={afterContent}
    />
  );
}
