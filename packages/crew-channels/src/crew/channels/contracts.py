"""Explicit runtime contract for Gateway channel adapters.

The canonical base class lives in ``crew.core.interfaces.Channel`` so that the
kernel can reference channels without depending on the product package.  This
module adds a runtime-checkable Protocol that captures the same duck-typing
surface used by ``ChannelManager`` and ``DeliveryRouter``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from crew.core.interfaces import MessageHandler


@runtime_checkable
class Channel(Protocol):
    """One inbound channel adapter owned by one Feature Generation.

    Implementations may inherit from ``crew.core.interfaces.Channel``; that ABC
    already declares the same surface.  This Protocol is the consumer-facing
    boundary used inside the Channels Feature.
    """

    name: str

    async def start(self, handler: MessageHandler) -> None: ...

    async def stop(self) -> None: ...

    def bind_app(self, app: Any) -> None: ...

    async def send_to_target(
        self,
        target: str,
        text: str,
        origin: Any | None = None,
    ) -> bool: ...

    def status_detail(self) -> dict[str, Any]: ...

    def apply_config(self, config: Any) -> None: ...
