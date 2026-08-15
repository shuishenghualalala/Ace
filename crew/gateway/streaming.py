"""Fail-closed boundaries for gateway HTTP streams."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.types import Send

log = logging.getLogger("gateway.streaming")


@dataclass(frozen=True)
class StreamLimits:
    max_chunk_bytes: int = 256 * 1024
    max_input_bytes: int = 4 * 1024 * 1024
    max_output_bytes: int = 4 * 1024 * 1024
    idle_timeout_s: float = 30.0
    absolute_timeout_s: float = 300.0
    send_timeout_s: float = 10.0


DEFAULT_STREAM_LIMITS = StreamLimits()


class _StreamBoundaryError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _StreamDisconnected(_StreamBoundaryError):
    def __init__(self) -> None:
        super().__init__("client_disconnected")


class _StreamTimeout(_StreamBoundaryError):
    def __init__(self) -> None:
        super().__init__("timeout")


class _StreamLimit(_StreamBoundaryError):
    def __init__(self) -> None:
        super().__init__("limit")


class StreamBudget:
    """Counts input and output bytes for one response-scoped stream."""

    def __init__(self, limits: StreamLimits) -> None:
        self.limits = limits
        self.started_at: float | None = None
        self.input_bytes = 0
        self.input_chunks = 0
        self.output_bytes = 0
        self.output_chunks = 0

    def start(self) -> None:
        if self.started_at is None:
            self.started_at = asyncio.get_running_loop().time()

    def _account(self, value: Any, *, output: bool) -> bytes:
        data = _to_bytes(value)
        if len(data) > self.limits.max_chunk_bytes:
            raise _StreamLimit()
        if output:
            total = self.output_bytes + len(data)
            if total > self.limits.max_output_bytes:
                raise _StreamLimit()
            self.output_bytes = total
            self.output_chunks += 1
        else:
            total = self.input_bytes + len(data)
            if total > self.limits.max_input_bytes:
                raise _StreamLimit()
            self.input_bytes = total
            self.input_chunks += 1
        return data

    def account_input(self, value: Any) -> bytes:
        return self._account(value, output=False)

    def account_output(self, value: Any) -> bytes:
        return self._account(value, output=True)

    def remaining(self) -> float:
        self.start()
        assert self.started_at is not None
        return self.limits.absolute_timeout_s - (
            asyncio.get_running_loop().time() - self.started_at
        )


def _to_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    delta = getattr(value, "delta_text", None)
    if isinstance(delta, str):
        return delta.encode("utf-8")
    return str(value).encode("utf-8")


async def _close(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - cleanup must not leak provider details
        log.warning("stream upstream cleanup failed")


async def _check_disconnected(request: Any) -> None:
    checker = getattr(request, "is_disconnected", None)
    if not callable(checker):
        return
    try:
        if await checker():
            raise _StreamDisconnected()
    except _StreamDisconnected:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - inability to verify is fail-closed
        raise _StreamDisconnected() from None


async def _next(
    iterator: Any,
    *,
    request: Any,
    budget: StreamBudget,
) -> Any:
    await _check_disconnected(request)
    remaining = budget.remaining()
    timeout = min(budget.limits.idle_timeout_s, remaining)
    if timeout <= 0:
        raise _StreamTimeout()
    try:
        return await asyncio.wait_for(anext(iterator), timeout)
    except StopAsyncIteration:
        raise
    except TimeoutError:
        raise _StreamTimeout() from None


async def bounded_input(
    source: AsyncIterable[Any],
    *,
    request: Any,
    limits: StreamLimits = DEFAULT_STREAM_LIMITS,
    budget: StreamBudget | None = None,
) -> AsyncIterator[Any]:
    """Bound provider/parser input while retaining the provider chunk type."""
    budget = budget or StreamBudget(limits)
    iterator = source.__aiter__()
    try:
        while True:
            item = await _next(iterator, request=request, budget=budget)
            budget.account_input(item)
            yield item
    except StopAsyncIteration:
        return
    finally:
        await _close(iterator)


def bounded_stream(
    source: AsyncIterable[Any],
    *,
    request: Any,
    limits: StreamLimits = DEFAULT_STREAM_LIMITS,
    budget: StreamBudget | None = None,
    error_event: Callable[[str], bytes | str] | None = None,
) -> AsyncIterator[bytes]:
    budget = budget or StreamBudget(limits)

    async def stream() -> AsyncIterator[bytes]:
        iterator = source.__aiter__()
        try:
            while True:
                try:
                    item = await _next(iterator, request=request, budget=budget)
                    data = budget.account_output(item)
                except StopAsyncIteration:
                    return
                except _StreamDisconnected:
                    return
                except _StreamBoundaryError as exc:
                    if error_event is not None:
                        try:
                            yield budget.account_output(error_event(exc.reason))
                        except _StreamBoundaryError:
                            pass
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - upstream details stay server-side
                    log.warning("stream upstream failed")
                    if error_event is not None:
                        try:
                            yield budget.account_output(error_event("stream_failed"))
                        except _StreamBoundaryError:
                            pass
                    return
                yield data
        finally:
            await _close(iterator)

    return stream()


class BoundedStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        budget: StreamBudget,
        send_timeout_s: float,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        self._budget = budget
        self._send_timeout_s = send_timeout_s

    async def stream_response(self, send: Send) -> None:
        async def send_with_deadline(message: dict[str, Any]) -> None:
            remaining = self._budget.remaining()
            timeout = min(self._send_timeout_s, remaining)
            if timeout <= 0:
                raise TimeoutError("stream absolute deadline exceeded")
            await asyncio.wait_for(send(message), timeout)

        try:
            await send_with_deadline({
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            })
            async for chunk in self.body_iterator:
                await send_with_deadline({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                })
            await send_with_deadline({
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            })
        except BaseException:
            await _close(self.body_iterator)
            raise


def bounded_streaming_response(
    request: Any,
    source: AsyncIterable[Any],
    *,
    media_type: str,
    limits: StreamLimits = DEFAULT_STREAM_LIMITS,
    budget: StreamBudget | None = None,
    error_event: Callable[[str], bytes | str] | None = None,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    background: BackgroundTask | None = None,
) -> BoundedStreamingResponse:
    budget = budget or StreamBudget(limits)
    return BoundedStreamingResponse(
        bounded_stream(
            source,
            request=request,
            limits=limits,
            budget=budget,
            error_event=error_event,
        ),
        budget=budget,
        send_timeout_s=limits.send_timeout_s,
        status_code=status_code,
        headers=headers,
        media_type=media_type,
        background=background,
    )
