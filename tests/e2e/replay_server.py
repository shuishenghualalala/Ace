"""Keyless OpenAI-compatible replay server for assembled backend tests.

The server serves recorded assistant responses in order. The application keeps
its real provider, HTTP client, tool registry, prompt building, persistence and
gateway path; only the nondeterministic model boundary is replaced.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ReplayServer:
    """Serve one recorded response per chat completion request."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ace-replay-server",
            daemon=True,
        )

    @classmethod
    def from_jsonl(cls, path: Path) -> "ReplayServer":
        responses = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return cls(responses)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._responses)

    def start(self) -> "ReplayServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ReplayServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _next_response(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._responses:
                return None
            return self._responses.pop(0)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                return

            def do_POST(self):  # noqa: N802 - stdlib signature
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    self._json_error(400, "invalid JSON request")
                    return
                response = server._next_response()
                if response is None:
                    self._json_error(500, "no recorded model response remains")
                    return
                if bool(payload.get("stream")):
                    self._stream(response)
                else:
                    self._completion(response)

            def _completion(self, response: dict[str, Any]) -> None:
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": str(response.get("text") or ""),
                }
                tool_calls = response.get("tool_calls") or []
                if tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": str(call.get("id") or f"call_replay_{index}"),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name") or ""),
                                "arguments": json.dumps(
                                    call.get("arguments") or {}, ensure_ascii=False,
                                ),
                            },
                        }
                        for index, call in enumerate(tool_calls)
                    ]
                self._send_json(200, {
                    "id": "chatcmpl-replay",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "ace-replay",
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": response.get("finish_reason") or (
                            "tool_calls" if tool_calls else "stop"
                        ),
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })

            def _stream(self, response: dict[str, Any]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                base = {
                    "id": "chatcmpl-replay",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "ace-replay",
                }
                self._event({**base, "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }]})
                text = str(response.get("text") or "")
                if text:
                    self._event({**base, "choices": [{
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }]})
                for index, call in enumerate(response.get("tool_calls") or []):
                    self._event({**base, "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{
                            "index": index,
                            "id": str(call.get("id") or f"call_replay_{index}"),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name") or ""),
                                "arguments": json.dumps(
                                    call.get("arguments") or {}, ensure_ascii=False,
                                ),
                            },
                        }]},
                        "finish_reason": None,
                    }]})
                finished = response.get("finish_reason") or (
                    "tool_calls" if response.get("tool_calls") else "stop"
                )
                self._event({**base, "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finished,
                }]})
                self._event({
                    **base,
                    "choices": [],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

            def _event(self, payload: dict[str, Any]) -> None:
                self.wfile.write(
                    ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json_error(self, status: int, message: str) -> None:
                self._send_json(status, {"error": {"message": message, "type": "replay_error"}})

        return Handler
