"""per-session 增量 Token 计量（ADR-0042 D6）。

锚点 = 最近一次真实 Provider usage；锚后 delta 按保守启发式（chars 分层估算）
累加并 max(0) 钳制——压缩使视图收缩时计量不回落，直到下一次真实 usage 重锚定。
接口三值 {exact|estimated|none}：有锚点 exact（真实 usage + 锚后增量）、
无锚点 estimated（纯启发式）、空视图 none。精确/估算/零值从不混淆。

usage 快照以 meter_checkpoint 事件追加进 session_events，跨重启经
checkpoint_loader 重锚定；锚定时刻的视图估算（baseline_estimate）随快照
持久化，delta 始终以同一基准累加。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from crew.agent.compact.tokens import estimate_tokens
from crew.core.types import Message

MeterKind = Literal["exact", "estimated", "none"]

CheckpointLoader = Callable[[str, str], dict[str, Any] | None]
# loader(session_id, owner_account_id) -> 最新 meter_checkpoint payload


@dataclass(frozen=True)
class MeterAnchor:
    prompt_tokens: int
    source: str
    fingerprint: str
    baseline_estimate: int
    recorded_at: float


@dataclass(frozen=True)
class TokenMeasurement:
    kind: MeterKind
    tokens: int


class TokenMeter:
    """per-session 增量计量：锚点 + 锚后保守 delta。"""

    def __init__(self, checkpoint_loader: CheckpointLoader | None = None) -> None:
        self._loader = checkpoint_loader
        self._anchors: dict[tuple[str, str], MeterAnchor] = {}

    @staticmethod
    def _key(session_id: str, owner_account_id: str) -> tuple[str, str]:
        return owner_account_id, session_id

    def _anchor(self, key: tuple[str, str]) -> MeterAnchor | None:
        anchor = self._anchors.get(key)
        if anchor is None and self._loader is not None:
            payload = self._loader(key[1], key[0])
            if isinstance(payload, dict) and isinstance(payload.get("prompt_tokens"), int):
                prompt_tokens = int(payload["prompt_tokens"])
                anchor = MeterAnchor(
                    prompt_tokens=prompt_tokens,
                    source=str(payload.get("source") or "provider"),
                    fingerprint=str(payload.get("fingerprint") or ""),
                    baseline_estimate=int(payload.get("baseline_estimate") or prompt_tokens),
                    recorded_at=float(payload.get("recorded_at") or 0),
                )
                self._anchors[key] = anchor
        return anchor

    def record_usage(
        self,
        session_id: str,
        owner_account_id: str,
        *,
        prompt_tokens: int,
        source: str,
        fingerprint: str,
        view_estimate: int,
    ) -> bool:
        """记录真实 usage 为新锚点，返回锚点链是否无缝延续。

        复用条件：请求信封一致（fingerprint 相同）且 usage ≥ 旧锚点启发式估价。
        不满足（压缩改了视图等）也算新锚点——真实数据优先，仅标记为 rebased。
        """
        key = self._key(session_id, owner_account_id)
        previous = self._anchors.get(key)
        continuous = (
            previous is not None
            and previous.fingerprint == fingerprint
            and prompt_tokens >= previous.prompt_tokens
            + max(0, view_estimate - previous.baseline_estimate)
        )
        self._anchors[key] = MeterAnchor(
            prompt_tokens=max(0, int(prompt_tokens)),
            source=source,
            fingerprint=fingerprint,
            baseline_estimate=max(0, int(view_estimate)),
            recorded_at=time.time(),
        )
        return continuous

    def measure(
        self, session_id: str, owner_account_id: str, messages: list[Message]
    ) -> TokenMeasurement:
        if not messages:
            return TokenMeasurement("none", 0)
        key = self._key(session_id, owner_account_id)
        anchor = self._anchor(key)
        if anchor is None:
            return TokenMeasurement("estimated", estimate_tokens(messages))
        current = estimate_tokens(messages)
        delta = max(0, current - anchor.baseline_estimate)
        return TokenMeasurement("exact", anchor.prompt_tokens + delta)

    def invalidate(self, session_id: str, owner_account_id: str) -> None:
        self._anchors.pop(self._key(session_id, owner_account_id), None)
