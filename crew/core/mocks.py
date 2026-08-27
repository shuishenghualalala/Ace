"""契约层自带的 Fake/Mock 实现。

目的：任何模块的同学不依赖别人的进度，也能用这些 Mock 把自己的模块单测跑绿。
例如开发 agent 内核时用 FakeProvider，不需要真实 LLM key。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import uuid

from crew.core.interfaces import LLMProvider, MemoryProvider, SessionStore, WorkspaceStore
from crew.core.types import ChatResponse, Message, StreamChunk


class FakeProvider(LLMProvider):
    """可编排的假 LLM。

    用法 1（脚本化，测工具循环）：
        FakeProvider(script=[ChatResponse(tool_calls=[...]), ChatResponse(text="done")])
        每次 chat() 依次弹出一个预设响应。
    用法 2（回声，测链路连通）：
        不传 script，直接把最后一条 user 消息回声为 final 文本。
    """

    def __init__(self, script: list[ChatResponse] | None = None) -> None:
        self._script = list(script or [])
        self.calls: list[list[Message]] = []  # 记录每次调用的 messages，便于断言
        self.stream_calls: list[list[Message]] = []  # 记录每次 stream_chat 调用

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.calls.append(list(messages))
        if self._script:
            return self._script.pop(0)
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return ChatResponse(text=f"[fake] 收到: {last_user}", finish_reason="stop")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls.append(list(messages))
        # 复用 chat() 的逻辑，把结果拆成字符逐个发出。
        # max_tokens 是 mock，忽略（不透传给 chat，避免子类 override 的 chat 无此参数报错）。
        resp = await self.chat(messages, tools)
        text = resp.text
        chunk_size = max(1, len(text) // 4) if text else 1
        for i in range(0, len(text), chunk_size):
            yield StreamChunk(delta_text=text[i : i + chunk_size])
        yield StreamChunk(
            delta_text="",
            done=True,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
            reasoning_content=resp.reasoning_content,
        )


class InMemorySessionStore(SessionStore):
    """内存会话存储。测试用，进程结束即丢。"""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], list[Message]] = {}
        self._ws: dict[tuple[str, str], str] = {}
        self._titles: dict[tuple[str, str], str] = {}
        self._fallback_titles: dict[tuple[str, str], str] = {}
        self._manual_titles: dict[tuple[str, str], bool] = {}
        self._status: dict[tuple[str, str], tuple[str, str]] = {}
        self._archived: dict[tuple[str, str], bool] = {}
        self._pinned: dict[tuple[str, str], bool] = {}

    @staticmethod
    def _key(session_id: str, owner_account_id: str = "") -> tuple[str, str]:
        return owner_account_id, session_id

    @staticmethod
    def _first_user_title(messages: list[Message]) -> str:
        """返回首条非空用户消息；不存在时使用与 SQLite store 一致的占位标题。"""
        return next((m.content for m in messages if m.role == "user" and m.content), "新会话")

    def load(self, session_id: str, owner_account_id: str = "") -> list[Message]:
        return list(self._data.get(self._key(session_id, owner_account_id), []))

    def load_child_sessions(
        self,
        session_id: str,
        *,
        owner_account_id: str,
    ) -> list[tuple[str, list[Message]]]:
        prefix = f"{session_id}::"
        return [
            (sid, list(messages))
            for (owner, sid), messages in sorted(self._data.items())
            if owner == owner_account_id and sid.startswith(prefix)
        ]

    def append(self, session_id: str, messages: list[Message], owner_account_id: str = "") -> None:
        self._data.setdefault(self._key(session_id, owner_account_id), []).extend(messages)

    def save(
        self,
        session_id: str,
        messages: list[Message],
        workspace_id: str = "default",
        owner_account_id: str = "",
        *,
        title_fallback: str | None = None,
    ) -> None:
        key = self._key(session_id, owner_account_id)
        self._data[key] = list(messages)
        # workspace_id 仅在首次创建时写入，后续 save 不覆盖（与 SQLiteSessionStore 对齐：
        # 会话归属在创建时确定，避免每轮回写把归属冲掉）。
        if key not in self._ws:
            self._ws[key] = workspace_id
        # 与 SQLite store 的 fallback_title 语义一致：None 使用首条用户消息，
        # 空串则保留为待后台摘要覆盖的占位标题。
        self._fallback_titles[key] = (
            title_fallback if title_fallback is not None else self._first_user_title(messages)
        )

    def clear(self, session_id: str, owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        self._data.pop(key, None)
        self._ws.pop(key, None)
        self._titles.pop(key, None)
        self._fallback_titles.pop(key, None)
        self._manual_titles.pop(key, None)
        self._status.pop(key, None)
        self._archived.pop(key, None)
        self._pinned.pop(key, None)

    def set_title(self, session_id: str, title: str, owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        if key in self._data:
            self._titles[key] = title

    def mark_title_manual(
        self,
        session_id: str,
        owner_account_id: str = "",
        manual: bool = True,
    ) -> None:
        """记录用户是否手动命名标题，与 SQLite store 的覆盖保护语义一致。"""
        key = self._key(session_id, owner_account_id)
        if key in self._data:
            self._manual_titles[key] = manual

    def set_archived(self, session_id: str, archived: bool, owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        if key in self._data:
            self._archived[key] = archived
            if archived:
                self._pinned[key] = False

    def set_pinned(self, session_id: str, pinned: bool, owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        if key in self._data:
            self._pinned[key] = pinned

    def set_status(self, session_id: str, status: str, error: str = "", owner_account_id: str = "") -> None:
        key = self._key(session_id, owner_account_id)
        self._data.setdefault(key, [])
        self._status[key] = (status, error)

    def touch_session(self, session_id: str, owner_account_id: str = "") -> None:
        """内存 store 无需保活，空实现。"""
        return None

    def expire_idle_sessions(self, idle_seconds: float, exclude_session_ids: set[str] | None = None) -> int:
        """内存 store 不过期，返回 0。"""
        return 0

    def list_sessions(
        self,
        workspace_id: str | None = None,
        owner_account_id: str = "",
        *,
        include_archived: bool = False,
        exclude_channel_sessions: bool = True,
    ) -> list[dict]:
        out: list[dict] = []
        for (owner, sid), msgs in self._data.items():
            if owner != owner_account_id:
                continue
            if "::" in sid:  # 排除 Team 内部子会话
                continue
            if (
                exclude_channel_sessions
                and sid.startswith("agent:main:")
                and not sid.startswith("agent:main:nearby:")
            ):
                continue
            key = self._key(sid, owner)
            archived = self._archived.get(key, False)
            if archived and not include_archived:
                continue
            wid = self._ws.get(key, "default")
            if workspace_id is not None and wid != workspace_id:
                continue
            if key in self._titles:
                title = self._titles[key]
            elif key in self._fallback_titles:
                title = self._fallback_titles[key]
            else:
                title = self._first_user_title(msgs)
            out.append(
                {
                    "session_id": sid,
                    "title": title[:40],
                    "message_count": len(msgs),
                    "updated_at": 0.0,
                    "created_at": 0.0,
                    "workspace_id": wid,
                    "last_status": self._status.get(key, ("", ""))[0],
                    "archived": archived,
                    "pinned": self._pinned.get(key, False),
                    "manual_title": self._manual_titles.get(key, False),
                }
            )
        # 置顶优先，再按更新时间倒序（内存 store updated_at 恒为 0，稳定排序）
        out.sort(key=lambda r: (not r["pinned"], -r["updated_at"]))
        return out

    def get_status(self, session_id: str, owner_account_id: str = "") -> tuple[str, str]:
        """测试辅助：取 (last_status, last_error)。"""
        return self._status.get(self._key(session_id, owner_account_id), ("", ""))

    def get_workspace_id(self, session_id: str, owner_account_id: str = "") -> str | None:
        """读取会话所属 workspace_id；不存在返回 None（对齐真实 SessionStore）。"""
        return self._ws.get(self._key(session_id, owner_account_id))

    def session_belongs_to(self, session_id: str, owner_account_id: str) -> bool:
        return self._key(session_id, owner_account_id) in self._data


class InMemoryWorkspaceStore(WorkspaceStore):
    """内存工作空间存储。测试用，内置工作空间查不到时自动补建。"""

    # 镜像 crew/state/workspace_store.py 的 BUILTIN_WORKSPACES（core 不反向依赖 state）。
    _BUILTIN_WORKSPACES = {
        "default": ("默认工作空间", False),
        "wiki": ("Wiki 知识库", True),
        "companion": ("同伴空间", False),
    }

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(workspace_id: str, owner_account_id: str = "") -> tuple[str, str]:
        return owner_account_id, workspace_id

    @classmethod
    def _builtin(cls, workspace_id: str) -> dict:
        name, hidden = cls._BUILTIN_WORKSPACES[workspace_id]
        return {"id": workspace_id, "name": name, "description": "", "instructions": "", "hidden": hidden}

    def _ensure_default(self, owner_account_id: str = "") -> None:
        self._data.setdefault(self._key("default", owner_account_id), self._builtin("default"))

    def create(
        self,
        name: str,
        description: str = "",
        instructions: str = "",
        root_path: str = "",
        owner_account_id: str = "",
    ) -> dict:
        self._ensure_default(owner_account_id)
        wid = f"ws_{uuid.uuid4().hex[:8]}"
        ws = {
            "id": wid,
            "name": name,
            "description": description,
            "instructions": instructions,
            "root_path": root_path,
            "hidden": False,
        }
        self._data[self._key(wid, owner_account_id)] = ws
        return dict(ws)

    def get(self, workspace_id: str, owner_account_id: str = "") -> dict:
        if workspace_id in self._BUILTIN_WORKSPACES:
            self._data.setdefault(self._key(workspace_id, owner_account_id), self._builtin(workspace_id))
        return dict(self._data[self._key(workspace_id, owner_account_id)])

    def list(self, owner_account_id: str = "") -> list[dict]:
        self._ensure_default(owner_account_id)
        return [dict(w) for (owner, _), w in self._data.items() if owner == owner_account_id]

    def update(self, workspace_id: str, owner_account_id: str = "", **fields) -> dict:
        key = self._key(workspace_id, owner_account_id)
        self._data[key].update({k: v for k, v in fields.items() if v is not None})
        return dict(self._data[key])

    def delete(self, workspace_id: str, owner_account_id: str = "") -> None:
        if workspace_id not in self._BUILTIN_WORKSPACES:
            self._data.pop(self._key(workspace_id, owner_account_id), None)


class NullMemory(MemoryProvider):
    """空记忆。prefetch 永远返回空串，write 不做事。"""

    async def prefetch(self, session_id: str, query: str) -> str:
        return ""

    async def write(self, session_id: str, messages: list[Message]) -> None:
        return None
