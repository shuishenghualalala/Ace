"""把两个仿真站点挂到一个本地 HTTP server 上。

零依赖（只用标准库）：这套 fixture 既要能在 pytest 里秒起秒停，也要能被人
单独拉起来对着内置浏览器做端到端验证，不值得为此引入 web 框架。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

from . import feed, ticket
from ._html import page
from ._state import MockState, Response

MAX_BODY_BYTES = 64 * 1024

_INDEX = page(
    "仿真站点",
    """
<h1>Crew 录制功能 · 本地仿真站点</h1>
<ul>
  <li><a href="/ticket/list">站点 A · 内网工单</a>（需登录，含审批按钮与注入工单）</li>
  <li><a href="/feed/">站点 B · 内容站</a>（免登录，同文档分类切换、加载更多、站内搜索）</li>
</ul>
""",
)


def _single(values: dict[str, list[str]]) -> dict[str, str]:
    """query/form 一律取首值。仿真站点没有多值参数的场景。"""
    return {key: items[0] for key, items in values.items() if items}


class _Handler(BaseHTTPRequestHandler):
    # 由 serve() 在子类上绑定
    state: MockState

    protocol_version = "HTTP/1.1"
    server_version = "CrewMockSites/1.0"

    def log_message(self, *args: object) -> None:  # noqa: D102 - 保持测试输出干净
        pass

    def _cookies(self) -> dict[str, str]:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return {key: morsel.value for key, morsel in jar.items()}

    def _form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0:
            return {}
        # 仿真站点不接受大 body：越界直接当空表单，避免测试里一个笔误把内存吃满。
        if length > MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return _single(parse_qs(raw, keep_blank_values=True))

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        # /ticket 与 /feed 的根路径要保留尾斜杠语义，单独还原
        if parts.path in {"/ticket/", "/feed/"}:
            path = parts.path
        query = _single(parse_qs(parts.query, keep_blank_values=True))
        form = self._form() if method == "POST" else {}
        cookies = self._cookies()

        if path == "/":
            self._send(Response(body=_INDEX))
            return

        for site in (ticket, feed):
            response = site.handle(self.state, method, path, query, form, cookies)
            if response is not None:
                self._send(response)
                return

        self._send(Response(status=404, body=page("未找到", "<h1>404</h1>")))

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        # 仿真站点必须每次都返回最新内容：榜单顺序、漂移开关都是「同一 URL 内容会变」，
        # 被缓存住会让测试看到上一次的页面。
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("POST")


def build_server(port: int = 0) -> tuple[ThreadingHTTPServer, MockState]:
    """创建（但不启动）一个仿真站点 server。port=0 表示由系统分配。"""
    state = MockState()
    handler = type("_BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server, state


@contextmanager
def serve(port: int = 0) -> Iterator[tuple[str, MockState]]:
    """在后台线程里跑仿真站点，yield `(base_url, state)`。"""
    server, state = build_server(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
