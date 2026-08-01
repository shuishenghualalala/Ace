export default function AgentAvatarLogo() {
  return (
    <svg className="msg-agent-logo" width="25" height="25" viewBox="0 0 32 32" aria-hidden="true">
      <path
        className="msg-agent-logo__head"
        d="M6.6 19.1c0-5.5 3.7-8.4 9.7-8.4 6 0 9.4 3.2 9.4 8.4v2.4c0 4.2-3.2 6.5-9.5 6.5-6.4 0-9.6-2.3-9.6-6.5v-2.4Z"
      />
      <g className="msg-agent-logo__hat" transform="rotate(-13 12.4 8.6)">
        <path d="M8.9 3.8h6.6a1.1 1.1 0 0 1 1.1 1.1v5.6H7.8V4.9a1.1 1.1 0 0 1 1.1-1.1Z" />
        <path d="M5.8 10.5h14.1" />
      </g>
      <path className="msg-agent-logo__hair" d="M13.2 10.2c3.7 2.7 8.1 4.7 13.2 4.5" />
      <path className="msg-agent-logo__hair" d="M15 10.5c3.4 2.7 6.5 4.5 10.8 5.7" />
      <path className="msg-agent-logo__hair msg-agent-logo__hair--tip" d="M18.2 12c2.4 1.9 5.3 3.5 8.7 4.4" />
      <path className="msg-agent-logo__eye" d="M12.7 17.1v3.1" />
      <path className="msg-agent-logo__eye" d="M19.4 17.1v3.1" />
    </svg>
  );
}
