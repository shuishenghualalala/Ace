"""Gateway 辅助函数与常量（从 server.py 抽出）。

多数是无状态纯函数（鉴权、session_id 派生、状态帧/配置体构造、JSON 抽取、组队
提示模板）；少数（config_body / session_agent_label / with_session_agent_labels）
接受 crew 参数读取其配置/存储，但不持有任何运行时状态。
"""

from __future__ import annotations

import hmac
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from crew.agent.external.runtime_registry import resolve_runtime_display_badge
from crew.gateway.session_context import SessionSource, build_session_key
from crew.team.formation import (
    build_team_draft as build_team_draft,
    confirmed_formation_plan as confirmed_formation_plan,
    enrich_team_member_role as enrich_team_member_role,
    fallback_team_suggestion as fallback_team_suggestion,
    fast_team_suggestion as fast_team_suggestion,
    suggest_role_description as suggest_role_description,
)

if TYPE_CHECKING:
    from crew.app import CrewApp
    from crew.gateway.channel_manager import ChannelManager

# 静态前端目录（PyInstaller frozen 时从 _MEIPASS 取）
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    WEB_DIR = Path(sys._MEIPASS) / "web"
else:
    WEB_DIR = Path(__file__).resolve().parents[2] / "web"
DIST_DIR = WEB_DIR / "dist"

WS_PING_INTERVAL_S = 30.0
WS_RECEIVE_TIMEOUT_S = 90.0

# 前端未构建时的占位页
NOT_BUILT_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>Crew</title><style>body{font-family:system-ui;max-width:640px;margin:80px auto;padding:0 24px;color:#111}
code{background:#f5f5f5;padding:2px 6px;border-radius:4px}</style></head>
<body><h1>Crew 前端尚未构建</h1>
<p>请任选其一启动前端：</p>
<ul><li>开发模式：<code>cd web &amp;&amp; npm install &amp;&amp; npm run dev</code>，访问 <code>http://localhost:5173</code></li>
<li>构建托管：<code>cd web &amp;&amp; npm run build</code>，再刷新本页（:8000 单端口）</li></ul></body></html>"""

EXTERNAL_AGENTS_DISABLED_BODY = {
    "ok": False,
    "code": "external_agents_disabled",
    "error": "外部智能体功能已在配置中关闭",
}


class ExternalAgentsDisabledError(RuntimeError):
    """外部智能体产品能力关闭时的统一 Gateway 业务异常。"""


def require_external_agents_enabled(crew: Any) -> None:
    """后端最终防线：只有配置明确开启时才允许外部 Runtime/Agent/Team 操作。"""
    if not getattr(crew.config, "external_agents_enabled", False):
        raise ExternalAgentsDisabledError(EXTERNAL_AGENTS_DISABLED_BODY["error"])


# ---------------------------------------------------------------------------
# 鉴权 / session_id / 平台清单
# ---------------------------------------------------------------------------

def ws_token_ok(request_headers: dict[str, str], query_token: str | None, expected: str) -> bool:
    """WS 鉴权：未配置 token 则放行；否则校验 Authorization 或 query token。"""
    if not expected:
        return True
    auth = request_headers.get("authorization") or request_headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    else:
        token = (query_token or "").strip()
    # 常量时间比较，避免计时侧信道泄露 expected 的前缀（用于 鉴权基线）。
    return hmac.compare_digest(token, expected)


def resolve_session_id(data: dict[str, Any], *, platform: str = "web") -> str:
    """缺省 session_id 时用 build_session_key 派生，替代硬编码 'web'。"""
    explicit = data.get("session_id")
    if explicit:
        return str(explicit)
    source = SessionSource(
        platform=platform,
        chat_id=str(data.get("chat_id") or "default"),
        chat_type=str(data.get("chat_type") or "dm"),
        user_id=data.get("user_id"),
    )
    return build_session_key(source)


def connected_platforms(channel_manager: ChannelManager) -> list[str]:
    names = ["local", "web"]
    for row in channel_manager.status():
        if row.get("running"):
            names.append(str(row["name"]))
    return list(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# 出站帧构造（WS 状态帧，消重用）
# ---------------------------------------------------------------------------

def status_frame(session_id: str, message: str) -> dict[str, Any]:
    """构造 WS 状态帧 dict（WS 控制动作 / plan 模式多处复用）。"""
    return {
        "kind": "status",
        "body": {"message": message, "control": True},
        "is_final": False,
        "sequence": 0,
        "session_id": session_id,
    }


def config_body(
    crew: CrewApp,
    *,
    owner_account_id: str = "",
    include_builtin_profiles: bool = True,
    is_gateway_admin: bool = False,
) -> dict[str, Any]:
    """构造前端配置响应体（GET /api/config、POST /api/config/model、CRUD 共用）。"""
    profiles = [
        profile.public_dict()
        for profile in crew.owner_visible_model_profiles(
            owner_account_id,
            include_builtin_profiles=include_builtin_profiles,
        )
    ]
    active = crew.owner_default_model_profile(owner_account_id)
    return {
        "model": active.model,
        "has_key": active.has_key,
        "base_url": active.base_url,
        "active_model_id": active.id,
        "default_model_id": active.id,
        "models": crew.owner_public_model_options(owner_account_id),
        "model_profiles": profiles,
        "is_gateway_admin": is_gateway_admin,
        "wiki": {
            "enabled": crew.config.wiki.enabled,
        },
        "external_agents": {
            "enabled": bool(getattr(crew.config, "external_agents_enabled", True)),
        },
        "security": {
            "enabled": bool(getattr(crew.config, "security_enabled", False)),
            "default_mode": (
                "request_approval"
                if getattr(crew.config, "security_enabled", False)
                else "full_access"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 智能组队提示模板（LLM 不可用时的回退）
# ---------------------------------------------------------------------------

def role_markdown(role_name: str, agent: dict, workflow: str, description: str, is_leader: bool) -> str:
    relation = (
        "把团队目标拆成阶段任务，明确每个成员的输入、输出、截止点；收集成员结果后做一致性检查和最终整合。"
        if is_leader
        else "接收 Leader 的任务说明，按职责完成专业产出；遇到阻塞及时回传风险、依赖和建议下一动作。"
    )
    duty = (
        f"理解团队目标「{description or workflow or '未填写'}」，制定可持续推进的协作节奏，分配任务并验收结果。"
        if is_leader
        else f"结合 {agent.get('name') or agent.get('provider') or '该智能体'} 的运行时能力，完成与团队目标相关的专业子任务。"
    )
    return "\n".join([
        f"### {role_name}",
        "",
        "#### 工作原则",
        "- 目标清晰：所有动作都服务团队描述和当前工作流。",
        "- 小步推进：复杂任务拆成可验收的阶段成果。",
        "- 可交接：每次输出都说明当前成果、风险和下一步。",
        "",
        "#### 职责",
        f"- {duty}",
        "",
        "#### 团队协作关系",
        f"- {relation}",
        "",
        "#### 输出格式",
        "- 当前成果：已经完成的结论、文件、代码或分析。",
        "- 下一负责人：下一步应由 Leader 或具体成员继续。",
        "- 下一动作：明确可执行的下一步。",
        "- 风险/阻塞：缺少的信息、依赖、权限或失败原因。",
        "",
        "#### 工作安排",
        "- 启动：确认目标、约束、交付物和优先级。",
        "- 执行：按角色完成子任务，并保留可复核的过程信息。",
        "- 汇总：由 Leader 对齐口径、检查遗漏并形成最终输出。",
    ])


def extract_json_object(text: str) -> dict | None:
    """从可能含前后噪声的文本中抽取首个 JSON 对象。"""
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 会话 agent 标签（左边栏展示 executor 归属）
# ---------------------------------------------------------------------------

def session_agent_binding(config: dict | None) -> dict[str, str]:
    """Project the persisted session config into a stable UI binding kind."""
    value = config if isinstance(config, dict) else {}
    executor = str(value.get("executor") or "builtin").strip().lower()
    external = value.get("external") if isinstance(value.get("external"), dict) else {}
    acp = value.get("acp") if isinstance(value.get("acp"), dict) else {}
    team = value.get("team") if isinstance(value.get("team"), dict) else {}

    external_team_id = str(team.get("external_team_id") or "").strip()
    if executor == "team" and external_team_id:
        return {"kind": "external_team", "id": external_team_id}

    external_agent_id = str(
        value.get("external_agent_id")
        or external.get("external_agent_id")
        or acp.get("external_agent_id")
        or ""
    ).strip()
    if executor in {"external", "acp"} and external_agent_id:
        return {"kind": "external_agent", "id": external_agent_id}

    if executor == "client":
        return {"kind": "client", "id": ""}
    return {"kind": "builtin", "id": ""}


def session_agent_label(
    crew: CrewApp,
    session_id: str,
    *,
    owner_account_id: str = "",
) -> dict[str, str]:
    getter = getattr(crew.session_store, "get_agent_config", None)
    config = getter(session_id, owner_account_id=owner_account_id) if callable(getter) else None
    executor = str((config or {}).get("executor") or "builtin").lower()
    if bool((config or {}).get("inspiration_creation") or (config or {}).get("site_creation")):
        return {"name": "灵感", "provider": "sites", "display_badge": "◇"}
    if executor == "team":
        team_cfg = (config or {}).get("team") if isinstance((config or {}).get("team"), dict) else {}
        external_team_id = str(team_cfg.get("external_team_id") or "").strip()
        if external_team_id and crew.external_agents is not None:
            try:
                team = crew.external_agents.get_team(
                    external_team_id,
                    owner_account_id=owner_account_id,
                )
                return {
                    "name": str(team.get("name") or "Team"),
                    "provider": "team",
                    "display_badge": "T",
                }
            except KeyError:
                return {"name": "Team", "provider": "team", "display_badge": "T"}
        return {"name": "Team", "provider": "team", "display_badge": "T"}
    if executor in {"external", "acp"}:
        external = (config or {}).get("external") if isinstance((config or {}).get("external"), dict) else {}
        acp = (config or {}).get("acp") if isinstance((config or {}).get("acp"), dict) else {}
        selected_model = str(external.get("model") or acp.get("model") or "").strip()
        agent_id = str(
            external.get("external_agent_id")
            or acp.get("external_agent_id")
            or (config or {}).get("external_agent_id")
            or ""
        ).strip()
        if agent_id and crew.external_agents is not None:
            try:
                agent, runtime = crew.external_agents.agent_with_runtime(
                    agent_id,
                    owner_account_id=owner_account_id,
                )
                metadata = (
                    runtime.get("metadata")
                    if isinstance(runtime.get("metadata"), dict)
                    else {}
                )
                return {
                    "name": str(agent.get("name") or agent.get("provider") or "External"),
                    "provider": str(agent.get("provider") or "external"),
                    "display_badge": resolve_runtime_display_badge(
                        provider=str(runtime.get("provider") or agent.get("provider") or ""),
                        metadata=metadata,
                    ),
                    "model": str(
                        selected_model
                        or agent.get("model")
                        or agent.get("provider")
                        or agent.get("name")
                        or "External"
                    ),
                }
            except KeyError:
                return {"name": "External", "provider": "external", "display_badge": "?"}
        return {"name": "External", "provider": "external", "display_badge": "?"}
    if executor == "client":
        return {"name": "Client", "provider": "client", "display_badge": "C"}
    return {"name": "Crew", "provider": "crew", "display_badge": "M"}


def with_session_agent_labels(
    crew: CrewApp,
    sessions: list[dict],
    *,
    owner_account_id: str = "",
) -> list[dict]:
    """为会话列表合并 agent 展示标签与会话级模型绑定。"""
    from crew.state.session_model import read_binding

    getter = getattr(crew.session_store, "get_agent_config", None)
    enriched: list[dict] = []
    owner_profiles = getattr(crew, "owner_model_profiles", None)
    profiles = (
        owner_profiles(owner_account_id)
        if callable(owner_profiles)
        else crew.config.owner_model_profiles(owner_account_id)
    )
    owner_default_id = getattr(crew.config, "owner_default_model_id", None)
    fallback_model_id = (
        owner_default_id(owner_account_id)
        if callable(owner_default_id)
        else crew.config.active_model_id
    )
    for session in sessions:
        sid = str(session.get("session_id") or "")
        stored = getter(sid, owner_account_id=owner_account_id) if callable(getter) and sid else None
        binding = read_binding(
            stored,
            crew.config,
            profiles,
            fallback_model_id=fallback_model_id,
        )
        enriched.append(
            {
                **session,
                "agent_label": session_agent_label(
                    crew,
                    sid,
                    owner_account_id=owner_account_id,
                ),
                "agent_binding": session_agent_binding(stored),
                "model_profile_id": binding["model_profile_id"],
                "pending_model_profile_id": binding.get("pending_model_profile_id"),
                "model_label": binding["model_label"],
            }
        )
    return enriched
