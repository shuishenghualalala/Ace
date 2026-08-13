import type { TeamMemberView, UiMessage } from "../types";
import AgentAvatarLogo from "./AgentAvatarLogo";
import MarkdownContent from "./MarkdownContent";
import MessageActions from "./MessageActions";
import TeamAgentTurnBubble from "./TeamAgentTurnBubble";
import ToolCallCard from "./ToolCallCard";
import ThinkingBlock from "./ThinkingBlock";

interface Props {
  msg: UiMessage;
  isStreaming?: boolean;
  canEdit?: boolean;
  onEdit?: (msg: UiMessage) => void;
  teamMembers?: TeamMemberView[];
  /** 传入后消息正文中的 [[Wiki 页面名]] 渲染为可点击引用链接（Wiki 问答场景）。 */
  onWikiLink?: (title: string) => void;
}

export default function MessageItem({ msg, isStreaming = false, canEdit = false, onEdit, teamMembers, onWikiLink }: Props) {
  switch (msg.role) {
    case "user": {
      const images = msg.attachments?.filter((a) => a.type === "image");
      return (
        <div className="msg user" id={`message-${msg.id}`} data-message-id={msg.id}>
          <div className="msg__bubble">
            <MessageActions text={msg.text} canEdit={canEdit} onEdit={() => onEdit?.(msg)} />
            {msg.text}
            {images && images.length > 0 && (
              <div className="msg__images">
                {images.map((a) => (
                  <img
                    key={a.id}
                    src={a.previewUrl || (a.content?.startsWith("data:") ? a.content : "")}
                    alt={a.name}
                    title={a.name}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      );
    }
    case "team_internal":
      return <TeamAgentTurnBubble message={msg} isStreaming={isStreaming} teamMembers={teamMembers} />;
    case "assistant": {
      const hasThinking = Boolean(msg.thinking);
      const hasToolCalls = msg.toolCalls && msg.toolCalls.length > 0;
      return (
        <div className="msg">
          <div className="msg__avatar bot">
            <AgentAvatarLogo />
          </div>
          <div className="msg__body">
            <div className="msg__name">Crew</div>
            {hasThinking && <ThinkingBlock content={msg.thinking!} />}
            {hasToolCalls && (
              <div className="msg__tools">
                {msg.toolCalls!.map((tc) => (
                  <ToolCallCard key={tc.toolCallId} tool={tc} />
                ))}
              </div>
            )}
            {msg.text && (
              <div className="msg__text md-body">
                <MarkdownContent content={msg.text} onWikiLink={onWikiLink} />
              </div>
            )}
          </div>
        </div>
      );
    }
    case "tool":
      return <div className="line-tool">{msg.text}</div>;
    case "status":
      return <div className="line-status">{msg.text}</div>;
    case "error":
      return <div className="line-error">{msg.text}</div>;
    default:
      return null;
  }
}
