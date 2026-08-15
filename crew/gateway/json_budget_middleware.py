"""ASGI middleware for bounded Gateway JSON structure scanning."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from crew.gateway.json_budget import (
    JSONBudgetLimits,
    JSONStructureBudget,
    JSONStructureBudgetExceeded,
    JSONStructureInvalid,
)


class GatewayJSONStructureBudgetMiddleware:
    """Scan JSON bodies before route parsing while leaving other transports alone."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_depth: int = 32,
        max_nodes: int = 100_000,
        max_object_keys: int = 10_000,
        max_array_items: int = 10_000,
        max_string_bytes: int = 4 * 1024 * 1024,
        max_number_chars: int = 128,
        max_number_digits: int | None = None,
        max_body_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.app = app
        self.limits = JSONBudgetLimits(
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_object_keys=max_object_keys,
            max_array_items=max_array_items,
            max_string_bytes=max_string_bytes,
            max_number_chars=(
                max_number_chars
                if max_number_digits is None
                else max_number_digits
            ),
        )
        self.max_body_bytes = max(1, int(max_body_bytes))

    @staticmethod
    def _is_json(scope: Scope) -> bool:
        if scope.get("method", "GET").upper() in {"GET", "HEAD"}:
            return False
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-type":
                media_type = value.split(b";", 1)[0].strip().lower()
                return media_type == b"application/json" or media_type.endswith(b"+json")
        return False

    @staticmethod
    async def _reject(send: Send, *, status: int, error: str) -> None:
        body = (f'{{"ok":false,"error":"{error}"}}').encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._is_json(scope):
            await self.app(scope, receive, send)
            return

        scanner = JSONStructureBudget(self.limits)
        messages: list[Message] = []
        total = 0
        invalid = False
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            body = message.get("body") or b""
            total += len(body)
            if total > self.max_body_bytes:
                await self._reject(send, status=413, error="JSON 请求体超过安全上限")
                return
            if not invalid:
                try:
                    scanner.feed(body)
                except JSONStructureBudgetExceeded:
                    await self._reject(send, status=413, error="JSON 请求结构超过安全上限")
                    return
                except JSONStructureInvalid:
                    # Preserve FastAPI's existing malformed-JSON response.
                    invalid = True
            if not message.get("more_body", False):
                break

        if not invalid and messages and messages[-1].get("type") == "http.request":
            try:
                scanner.finish()
            except JSONStructureBudgetExceeded:
                await self._reject(send, status=413, error="JSON 请求结构超过安全上限")
                return
            except JSONStructureInvalid:
                pass

        position = 0

        async def replay_receive() -> Message:
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)
