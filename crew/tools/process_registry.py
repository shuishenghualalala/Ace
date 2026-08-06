"""后台进程注册表 —— 管理 terminal(background=true) 启动的进程。

对照 Hermes tools/process_registry.py 裁剪而来。Crew 是本地单机场景，故砍掉
Hermes 的 PTY 交互 / Docker·SSH·Modal sandbox 后端 / crash-recovery checkpoint /
gateway watcher 路由 / 跨 session 全局熔断器，只保留本地进程真正需要的核心：

  - 输出捕获（reader 线程 + 200KB 滚动缓冲，替代原来的 DEVNULL 丢弃）
  - 状态轮询 / 日志读取 / 阻塞等待 / 杀进程
  - watch_patterns：输出命中模式时排队通知（单 session 限流 + strike 自动降级）
  - notify_on_complete：进程退出时排队一次完成通知

通知投递复用 Crew 既有通路：reader 线程把事件写入 per-session 待通知队列，
app.handle() 每轮 drain_for_session() 取出 → envelope.params → runtime 拼进
<system-reminder>，与后台子 agent 通知保持一致。
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import crew as _crew_pkg
from crew.core.runctx import current_owner_account_id
from crew.tools.output_filters import strip_ansi
from crew.tools.registry import tool_error
from crew.security.launch import ProcessLaunch, serialize_profile, shell_argv

SessionKey = tuple[str, str]

# Trust root for the managed background bridge subprocess. The bridge must import
# ``crew`` only from the installed package location, never from the task ``cwd``:
# ``python -m crew.security.background_runner`` puts ``cwd`` on ``sys.path[0]`` and a
# workspace-dropped ``crew/security/background_runner.py`` (plus a fake
# ``crew/security/runtime_client.py``) would execute on the host *before* the native
# helper, classifier, or sandbox ever run (H-1). The launcher below runs under
# ``-I`` (no PYTHONPATH / user site) and rebuilds ``sys.path`` to drop ``cwd`` and
# prepend only this trusted root.
_CREW_TRUST_ROOT = str(Path(_crew_pkg.__file__).resolve().parent.parent)
_BACKGROUND_BRIDGE_LAUNCHER = (
    "import sys; "
    "sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
    f"sys.path.insert(0, {_CREW_TRUST_ROOT!r}); "
    "from crew.security.background_runner import main; "
    "raise SystemExit(main())"
)

# ---- 限制项 ----
MAX_OUTPUT_CHARS = 200_000      # 200KB 滚动输出缓冲
FINISHED_TTL_SECONDS = 1800     # 已结束进程保留 30 分钟
MAX_PROCESSES = 64              # 最大并发跟踪进程数（超出按最旧 LRU 淘汰）
MAX_PENDING_PER_SESSION = 20    # 每 session 待注入通知上限，防无限堆积

# ---- watch_patterns 限流（单 session）----
# 硬规则：每 WATCH_MIN_INTERVAL_SECONDS 至多发一条 watch 命中通知。冷却窗口内到达的
# 命中被丢弃并记一次 strike；连续 WATCH_STRIKE_LIMIT 个 strike 窗口后，永久禁用该
# session 的 watch_patterns 并降级为 notify_on_complete（进程真正退出时发一条）。
WATCH_MIN_INTERVAL_SECONDS = 15
WATCH_STRIKE_LIMIT = 3

_IS_WINDOWS = os.name == "nt"
_WINDOWS_PROCESS_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)


def terminate_process_tree(pid: int) -> None:
    """Best-effort terminate one process and its complete child process tree.

    Windows ``Popen.terminate()`` only stops the top-level process, so command
    chains such as PowerShell → Node → LibreOffice can otherwise leave orphaned
    children. POSIX processes are started in their own group and terminate as a
    group; Windows uses ``taskkill /T /F``.
    """
    if pid <= 0:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        return


def _checkpoint_path() -> Path:
    """崩溃恢复 checkpoint 文件路径（懒解析，随 CREW_HOME 变化）。"""
    from crew.state.home import get_crew_home

    return get_crew_home() / "processes.json"


def _owner_matches(session: "ProcessSession", owner_account_id: str = "") -> bool:
    owner = str(owner_account_id or "").strip()
    return not owner or str(session.owner_account_id or "") == owner


def _pid_alive(pid: int | None) -> bool:
    """best-effort 检测 host PID 是否存活。"""
    if not pid:
        return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)  # 信号 0：只探测存在性，不真正发信号
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限 → 仍算活着
    except OSError:
        return False


@dataclass
class ProcessSession:
    """一个被跟踪的后台进程及其输出缓冲。"""

    id: str
    command: str
    session_key: str = ""                         # 归属会话（用于通知路由）
    owner_account_id: str = ""
    pid: int | None = None
    process: subprocess.Popen | None = None
    cwd: str | None = None
    started_at: float = 0.0
    exited: bool = False
    exit_code: int | None = None
    output_buffer: str = ""
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False                        # 崩溃恢复认领的进程：可查状态/可 kill，但无输出历史
    notify_on_complete: bool = False
    watch_patterns: list[str] = field(default_factory=list)
    # watch 限流状态（单 session）
    _watch_disabled: bool = field(default=False, repr=False)
    _watch_suppressed: int = field(default=0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _heartbeat_thread: threading.Thread | None = field(default=None, repr=False)
    _secret_values: tuple[str, ...] = field(default=(), repr=False)
    task_id: str = ""
    output_ref: str = ""


class ProcessRegistry:
    """运行中 / 已结束后台进程的内存注册表。线程安全。"""

    def __init__(self) -> None:
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()
        # 待注入通知：(owner_account_id, session_key) -> [event, ...]。reader 线程写入，handle() 取出。
        self._pending: dict[SessionKey, list[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._task_runtime: Any = None

    def configure_task_runtime(self, runtime: Any) -> None:
        """Attach the unified task runtime without making tools import app."""
        self._task_runtime = runtime

    # ----- 通知队列 -----

    @staticmethod
    def _key(session_key: str, owner_account_id: str = "") -> SessionKey:
        return owner_account_id or "", session_key

    def _enqueue(self, session: ProcessSession, event: dict[str, Any]) -> None:
        with self._pending_lock:
            queue = self._pending.setdefault(self._key(session.session_key, session.owner_account_id), [])
            queue.append(event)
            if len(queue) > MAX_PENDING_PER_SESSION:
                del queue[:-MAX_PENDING_PER_SESSION]

    def drain_for_session(self, session_key: str, owner_account_id: str = "") -> list[dict[str, Any]]:
        """弹出某 session 的全部待通知事件（线程安全）。"""
        with self._pending_lock:
            return self._pending.pop(self._key(session_key, owner_account_id), []) or []

    # ----- 启动 -----

    @staticmethod
    def _child_env(
        owner_account_id: str,
        *,
        managed: bool = False,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build the explicit child environment and identify injected secrets."""
        from crew.state.home import managed_runtime_env_overrides, runtime_env_overrides
        from crew.tools.redact import sensitive_env_values

        build_env = managed_runtime_env_overrides if managed else runtime_env_overrides
        values = build_env(owner_account_id=owner_account_id)
        values["PYTHONUNBUFFERED"] = "1"
        return values, tuple(sensitive_env_values(values))

    def spawn_local(
        self,
        command: str,
        *,
        cwd: str | None = None,
        session_key: str = "",
        owner_account_id: str = "",
        watch_patterns: list[str] | None = None,
        notify_on_complete: bool = False,
        task_id: str = "",
        output_ref: str = "",
    ) -> ProcessSession:
        """本地后台启动一条命令，立即返回（非阻塞）。

        输出由 daemon reader 线程读入滚动缓冲，可经 poll/log/wait 取回。
        """
        child_env, secret_values = self._child_env(owner_account_id)
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            session_key=session_key,
            owner_account_id=owner_account_id,
            cwd=cwd or os.getcwd(),
            started_at=time.time(),
            notify_on_complete=notify_on_complete,
            watch_patterns=list(watch_patterns or []),
            task_id=task_id,
            output_ref=output_ref,
            _secret_values=secret_values,
        )

        env = dict(os.environ)
        env.update(child_env)
        popen_args: list[str] | str = command
        shell = True
        if _IS_WINDOWS:
            # Use PowerShell on Windows so commands run in the same shell family
            # as the desktop/runtime environment; cmd.exe breaks common commands
            # such as `pwd` and gives misleading cwd behavior.
            #
            # stdout 编码三件套必须一起做：进程级 [Console]::OutputEncoding、管道级
            # $OutputEncoding、控制台代码页 chcp 65001。只设 Out-File 默认编码不影响
            # stdout——中文 Windows 默认 CP936，Popen(encoding="utf-8", errors="replace")
            # 会把 CP936 字节当非法字节替换为 U+FFFD，浏览器渲染为 ���。
            command = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                "chcp 65001 | Out-Null; "
                "$PSDefaultParameterValues['Out-File:Encoding']='utf8NoBOM'; "
                "Remove-Item Alias:pwd -Force -ErrorAction SilentlyContinue; "
                "function global:pwd { (Get-Location).Path }; "
                f"{command}"
            )
            powershell_exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            popen_args = [
                powershell_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ]
            shell = False
        proc = subprocess.Popen(
            popen_args,
            shell=shell,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=session.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,  # 自成进程组，便于整树 kill
            # Windows 进程组仍共享父控制台；taskkill /T /F 可能把控制信号回传给
            # Gateway。后台 shell 无交互控制台需求，创建无窗口独立进程组才能安全整树终止。
            creationflags=_WINDOWS_PROCESS_FLAGS if _IS_WINDOWS else 0,
        )
        session.process = proc
        session.pid = proc.pid

        with self._lock:
            self._prune_if_needed()
            self._running[session.id] = session

        reader = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            daemon=True,
            name=f"proc-reader-{session.id}",
        )
        session._reader_thread = reader
        reader.start()
        if self._task_runtime is not None and task_id:
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(session,),
                daemon=True,
                name=f"proc-heartbeat-{session.id}",
            )
            session._heartbeat_thread = heartbeat
            heartbeat.start()

        self._write_checkpoint()
        return session

    def spawn_security(
        self,
        command: str,
        *,
        launch: ProcessLaunch,
        cwd: str | None = None,
        **session_options: Any,
    ) -> ProcessSession:
        """Start through the host-owned security decision; managed never uses a user argv."""
        if not launch.managed:
            return self.spawn_local(command, cwd=cwd, **session_options)
        owner_account_id = str(session_options.get("owner_account_id") or "")
        child_env, secret_values = self._child_env(
            owner_account_id,
            managed=True,
        )
        profile = serialize_profile(launch.profile)
        readable_roots = [
            entry["root"]
            for entry in profile["filesystem"]
            if entry["access"] == "read" and entry["escalatable"]
        ]
        for root in launch.trusted_readable_roots:
            value = str(root)
            if value not in readable_roots:
                readable_roots.append(value)
        payload = {
            "version": 1,
            "helper_argv": list(launch.helper_argv),
            "command": list(shell_argv(command)),
            "cwd": str(Path(cwd or os.getcwd()).resolve(strict=True)),
            "writable_roots": [
                entry["root"] for entry in profile["filesystem"] if entry["access"] == "read_write"
            ],
            "readable_roots": readable_roots,
            "denied_roots": [
                entry["root"] for entry in profile["filesystem"] if entry["access"] == "deny"
            ],
            "network_rules": profile["network_entries"],
            "allow_local_binding": profile["allow_local_binding"],
            "env_overrides": child_env,
        }
        return self._spawn_managed_bridge(
            command,
            payload,
            cwd=cwd,
            secret_values=secret_values,
            **session_options,
        )

    def _spawn_managed_bridge(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        cwd: str | None,
        session_key: str = "",
        owner_account_id: str = "",
        watch_patterns: list[str] | None = None,
        notify_on_complete: bool = False,
        task_id: str = "",
        output_ref: str = "",
        secret_values: tuple[str, ...] = (),
    ) -> ProcessSession:
        """Launch only the fixed Ace bridge; command is sent through stdin."""
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}", command=command, session_key=session_key,
            owner_account_id=owner_account_id, cwd=cwd or os.getcwd(), started_at=time.time(),
            notify_on_complete=notify_on_complete, watch_patterns=list(watch_patterns or []),
            task_id=task_id, output_ref=output_ref, _secret_values=secret_values,
        )
        flags = _WINDOWS_PROCESS_FLAGS if _IS_WINDOWS else 0
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _BACKGROUND_BRIDGE_LAUNCHER],
            shell=False, text=True, encoding="utf-8", errors="replace", cwd=session.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            preexec_fn=None if _IS_WINDOWS else os.setsid, creationflags=flags,
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        proc.stdin.close()
        session.process = proc
        session.pid = proc.pid
        with self._lock:
            self._prune_if_needed()
            self._running[session.id] = session
        reader = threading.Thread(target=self._reader_loop, args=(session,), daemon=True, name=f"proc-reader-{session.id}")
        session._reader_thread = reader
        reader.start()
        self._write_checkpoint()
        return session

    def _heartbeat_loop(self, session: ProcessSession) -> None:
        interval = float(getattr(self._task_runtime, "heartbeat_interval", 10.0))
        while not session.exited:
            time.sleep(max(0.1, interval))
            if session.exited:
                return
            try:
                self._task_runtime.heartbeat(session.task_id)
            except Exception:
                return

    # ----- reader 线程 -----

    @staticmethod
    def _redact_private_output(
        output: str,
        secret_values: tuple[str, ...],
        *,
        truncated: bool,
        max_output_chars: int,
    ) -> str:
        """Expose only complete, redacted output after a secret-bearing child exits."""
        from crew.tools.redact import redact_secret_values, redact_sensitive_text

        if truncated:
            # The rolling buffer can begin inside a secret. Drop one maximum-secret
            # width, then move past any complete secret crossing that cut.
            start = max(len(value) for value in secret_values) - 1
            while True:
                advanced = start
                for value in secret_values:
                    offset = output.find(value)
                    while offset >= 0:
                        end = offset + len(value)
                        if offset < start < end:
                            advanced = max(advanced, end)
                        offset = output.find(value, offset + 1)
                if advanced == start:
                    break
                start = advanced
            output = output[start:]
        redacted = redact_secret_values(output, secret_values)
        return redact_sensitive_text(redacted, force=True)[-max_output_chars:]

    def _publish_output(self, session: ProcessSession, chunk: str) -> None:
        """Publish already-safe output to every observer of a process session."""
        with session._lock:
            session.output_buffer += chunk
            if len(session.output_buffer) > session.max_output_chars:
                session.output_buffer = session.output_buffer[-session.max_output_chars:]
            output_chars = len(session.output_buffer)
            output_tail = strip_ansi(session.output_buffer[-1000:])
        if session.output_ref:
            try:
                path = Path(session.output_ref)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as output_file:
                    output_file.write(chunk)
            except OSError:
                pass
        if self._task_runtime is not None and session.task_id:
            try:
                self._task_runtime.touch_activity(
                    session.task_id,
                    {
                        "pid": session.pid,
                        "output_chars": output_chars,
                        "output_tail": output_tail,
                    },
                )
            except Exception:
                pass
        self._check_watch_patterns(session, chunk)

    def _reader_loop(self, session: ProcessSession) -> None:
        """daemon 线程：持续读取 Popen stdout 到滚动缓冲。

        用 buffer.read1(4096) + 增量 UTF-8 解码器，而不是文本模式 read(4096)——后者会
        阻塞到填满 4096 字符或 EOF，小增量输出（如 `for i ...; do echo $i; sleep`）期间
        reader 一直阻塞、output_buffer 不增长，前台 terminal 的 onProgress 进度流就只能
        在进程退出时一次性吐全部（对齐 OCC Stage5 onProgress 需要 line-level 实时增量）。
        read1 有数据即返回，增量解码器正确拼接跨 chunk 的多字节字符（含 emoji）。
        """
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        private_output = ""
        private_output_truncated = False
        private_limit = session.max_output_chars
        if session._secret_values:
            private_limit += max(len(value) for value in session._secret_values) - 1
        try:
            while True:
                # buffer 在 text-mode Popen 上仍可用（底层 BufferedReader）；非 text 模式直接 read1
                buf = getattr(session.process.stdout, "buffer", None)
                if buf is not None:
                    raw = buf.read1(4096)
                else:  # 兜底：非 text 模式（理论不会走到，Popen 用了 text=True）
                    raw = session.process.stdout.read1(4096)
                if not raw:
                    # 输入未就绪也可能返回空，需检查 EOF
                    if session.process.stdout.at_eof() if hasattr(session.process.stdout, "at_eof") else True:
                        break
                    # 部分 BufferedReader.read1 在无数据时返回 b'' 表示 EOF
                    try:
                        if session.process.stdout.closed:
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    # 真正的 EOF：再读一次确认
                    if not raw:
                        break
                    continue
                chunk = decoder.decode(raw)
                if not chunk:
                    continue  # 多字节字符跨 chunk，等下一片再输出
                if session._secret_values:
                    private_output += chunk
                    if len(private_output) > private_limit:
                        private_output = private_output[-private_limit:]
                        private_output_truncated = True
                else:
                    self._publish_output(session, chunk)
        except Exception:  # noqa: BLE001 - reader 异常按读取结束处理
            pass
        finally:
            try:
                # flush 增量解码器尾部（无尾字节，防御性）
                tail = decoder.decode(b"", final=True)
                if tail:
                    if session._secret_values:
                        private_output += tail
                        if len(private_output) > private_limit:
                            private_output = private_output[-private_limit:]
                            private_output_truncated = True
                    else:
                        self._publish_output(session, tail)
            except Exception:  # noqa: BLE001
                pass
            if session._secret_values:
                safe_output = self._redact_private_output(
                    private_output,
                    session._secret_values,
                    truncated=private_output_truncated,
                    max_output_chars=session.max_output_chars,
                )
                if safe_output:
                    self._publish_output(session, safe_output)
            try:
                session.process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            session.exited = True
            session.exit_code = session.process.returncode
            self._move_to_finished(session)

    def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        """扫描新输出中的 watch_patterns 并按限流排队通知。

        单 session 限流：每 WATCH_MIN_INTERVAL_SECONDS 至多发一条。冷却窗口内的命中
        丢弃并对该窗口记一次 strike；连续 WATCH_STRIKE_LIMIT 个 strike 后禁用 watch
        并降级为 notify_on_complete。
        """
        if not session.watch_patterns or session._watch_disabled or session.exited:
            return

        matched_lines: list[str] = []
        matched_pattern: str | None = None
        for line in new_text.splitlines():
            for pat in session.watch_patterns:
                if pat in line:
                    matched_lines.append(line.rstrip())
                    if matched_pattern is None:
                        matched_pattern = pat
                    break
        if not matched_lines:
            return

        now = time.time()
        should_disable = False
        with session._lock:
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                # 冷却窗口内：丢弃 + 记 strike（每窗口仅记一次）
                session._watch_suppressed += len(matched_lines)
                if not session._watch_strike_candidate:
                    session._watch_strike_candidate = True
                    session._watch_consecutive_strikes += 1
                    if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                        session._watch_disabled = True
                        session.notify_on_complete = True
                        should_disable = True
                emit = False
                suppressed = 0
            else:
                # 冷却已过：上一个窗口若无丢弃则重置 strike 计数
                if session._watch_cooldown_until and not session._watch_strike_candidate:
                    session._watch_consecutive_strikes = 0
                session._watch_strike_candidate = False
                session._watch_cooldown_until = now + WATCH_MIN_INTERVAL_SECONDS
                suppressed = session._watch_suppressed
                session._watch_suppressed = 0
                emit = True

        if not emit:
            if should_disable:
                self._enqueue(session, {
                    "type": "watch_disabled",
                    "session_id": session.id,
                    "command": session.command,
                    "message": (
                        f"进程 {session.id} 的 watch_patterns 已禁用："
                        f"连续 {WATCH_STRIKE_LIMIT} 个限流窗口触发（最小间隔 "
                        f"{WATCH_MIN_INTERVAL_SECONDS}s）。已降级为 notify_on_complete，"
                        f"进程退出时会发一条完成通知。"
                    ),
                })
            return

        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"
        self._enqueue(session, {
            "type": "watch_match",
            "session_id": session.id,
            "command": session.command,
            "pattern": matched_pattern,
            "output": output,
            "suppressed": suppressed,
        })

    def _move_to_finished(self, session: ProcessSession) -> None:
        """把 session 从 running 移到 finished（幂等：重复调用不重复发通知）。"""
        with self._lock:
            was_running = self._running.pop(session.id, None) is not None
            self._finished[session.id] = session
        self._write_checkpoint()

        if was_running and session.notify_on_complete:
            output_tail = strip_ansi(session.output_buffer[-2000:]) if session.output_buffer else ""
            self._enqueue(session, {
                "type": "completion",
                "session_id": session.id,
                "command": session.command,
                "exit_code": session.exit_code,
                "output": output_tail,
            })
        if was_running and self._task_runtime is not None and session.task_id:
            try:
                status = "completed" if session.exit_code == 0 else "failed"
                self._task_runtime.finish(
                    session.task_id,
                    owner_account_id=session.owner_account_id,
                    status=status,
                    result=strip_ansi(session.output_buffer[-2000:]),
                    error="" if status == "completed" else f"进程退出码 {session.exit_code}",
                    progress={
                        "pid": session.pid,
                        "exit_code": session.exit_code,
                        "output_chars": len(session.output_buffer),
                        "output_tail": strip_ansi(session.output_buffer[-1000:]),
                    },
                )
            except Exception:
                pass

    # ----- 查询 -----

    def get(self, session_id: str, owner_account_id: str = "") -> ProcessSession | None:
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        session = self._refresh_detached(session)
        if session is None or not _owner_matches(session, owner_account_id):
            return None
        return session

    def _refresh_detached(self, session: ProcessSession | None) -> ProcessSession | None:
        """崩溃恢复认领的进程没有 Popen 句柄，靠 host PID 存活检测判定是否已退出。"""
        if session is None or session.exited or not session.detached:
            return session
        if _pid_alive(session.pid):
            return session
        with session._lock:
            if session.exited:
                return session
            session.exited = True
            session.exit_code = None  # 无句柄可 wait，真实退出码不可得
        self._move_to_finished(session)
        return session

    def poll(self, session_id: str, owner_account_id: str = "") -> dict[str, Any]:
        """查看状态 + 最近输出预览。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        with session._lock:
            preview = strip_ansi(session.output_buffer[-1000:]) if session.output_buffer else ""
        result: dict[str, Any] = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "task_id": session.task_id,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        if session.detached:
            result["detached"] = True
            result["note"] = "进程为重启后认领，无输出历史，仅能查状态/kill"
        return result

    def read_log(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 200,
        *,
        owner_account_id: str = "",
    ) -> dict[str, Any]:
        """读取完整输出日志（按行分页）。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        with session._lock:
            full_output = strip_ansi(session.output_buffer)
        lines = full_output.splitlines()
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset:offset + limit]
        result: dict[str, Any] = {
            "session_id": session.id,
            "task_id": session.task_id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": len(lines),
            "showing": f"{len(selected)} lines",
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        return result

    def wait(self, session_id: str, timeout: int | None = None, *, owner_account_id: str = "") -> dict[str, Any]:
        """阻塞直到进程退出或超时。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        effective_timeout = float(timeout) if timeout and timeout > 0 else 180.0
        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            if session.exited:
                return {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "output": strip_ansi(session.output_buffer[-2000:]),
                }
            time.sleep(0.5)
        return {
            "status": "timeout",
            "output": strip_ansi(session.output_buffer[-1000:]),
            "timeout_note": f"已等待 {int(effective_timeout)}s，进程仍在运行",
        }

    def kill_process(self, session_id: str, owner_account_id: str = "") -> dict[str, Any]:
        """杀掉一个后台进程（含子进程树）。"""
        session = self.get(session_id, owner_account_id=owner_account_id)
        if session is None:
            return {"status": "not_found", "error": f"无此进程: {session_id}"}
        if session.exited:
            return {"status": "already_exited", "exit_code": session.exit_code}
        try:
            if session.process is not None:
                if _IS_WINDOWS:
                    terminate_process_tree(session.process.pid)
                else:
                    try:
                        os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        session.process.kill()
            elif session.detached and session.pid:
                # 崩溃恢复认领的进程：无 Popen 句柄，按 host PID 杀
                if not _pid_alive(session.pid):
                    session.exited = True
                    session.exit_code = None
                    self._move_to_finished(session)
                    return {"status": "already_exited", "exit_code": None}
                try:
                    if _IS_WINDOWS:
                        terminate_process_tree(session.pid)
                    else:
                        os.killpg(os.getpgid(session.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(session.pid, signal.SIGTERM)
                    except OSError:
                        pass
            session.exited = True
            session.exit_code = -15  # SIGTERM
            self._move_to_finished(session)
            return {"status": "killed", "session_id": session.id}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

    def list_sessions(self, owner_account_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())
        all_sessions = [
            refreshed
            for s in all_sessions
            if (refreshed := self._refresh_detached(s)) is not None
            and _owner_matches(refreshed, owner_account_id)
        ]
        result = []
        for s in all_sessions:
            entry: dict[str, Any] = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": s.output_buffer[-200:] if s.output_buffer else "",
            }
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    def count_running(self) -> int:
        try:
            return len(self._running)
        except Exception:  # noqa: BLE001
            return 0

    # ----- 清理 -----

    def _prune_if_needed(self) -> None:
        """淘汰过期 / 超量的已结束 session。须持有 self._lock。"""
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest]

    # ----- 崩溃恢复 checkpoint -----

    def _write_checkpoint(self) -> None:
        """把运行中（未退出）进程的元数据原子写入 checkpoint 文件。失败静默。"""
        try:
            with self._lock:
                entries = [
                    {
                        "session_id": s.id,
                        "command": s.command,
                        "pid": s.pid,
                        "cwd": s.cwd,
                        "started_at": s.started_at,
                        "session_key": s.session_key,
                        "owner_account_id": s.owner_account_id,
                        "notify_on_complete": s.notify_on_complete,
                        "watch_patterns": s.watch_patterns,
                        "task_id": s.task_id,
                        "output_ref": s.output_ref,
                    }
                    for s in self._running.values()
                    if not s.exited and s.pid
                ]
            path = _checkpoint_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)  # 原子替换，避免崩溃时读到半截文件
        except Exception:  # noqa: BLE001 - checkpoint 失败不影响主流程
            pass

    def recover_from_checkpoint(self) -> int:
        """启动时读取 checkpoint，按 host PID 存活检测重新认领后台进程。

        返回认领为 detached 的进程数。已死进程跳过（无句柄取不到退出码）。
        """
        path = _checkpoint_path()
        if not path.exists():
            return 0
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0

        recovered = 0
        for entry in entries:
            pid = entry.get("pid")
            if not pid or not _pid_alive(pid):
                continue
            session = ProcessSession(
                id=entry.get("session_id") or f"proc_{uuid.uuid4().hex[:12]}",
                command=entry.get("command", "unknown"),
                session_key=entry.get("session_key", ""),
                owner_account_id=entry.get("owner_account_id", ""),
                pid=pid,
                cwd=entry.get("cwd"),
                started_at=entry.get("started_at", time.time()),
                detached=True,
                notify_on_complete=entry.get("notify_on_complete", False),
                watch_patterns=entry.get("watch_patterns", []),
                task_id=entry.get("task_id", ""),
                output_ref=entry.get("output_ref", ""),
            )
            with self._lock:
                self._running[session.id] = session
            recovered += 1
        self._write_checkpoint()
        return recovered


# 模块级单例
process_registry = ProcessRegistry()


def format_process_notification(evt: dict[str, Any]) -> str | None:
    """把一条进程通知事件格式化为可注入上下文的文本。"""
    evt_type = evt.get("type", "completion")
    sid = evt.get("session_id", "unknown")
    cmd = evt.get("command", "unknown")

    if evt_type == "watch_disabled":
        return f"[重要] {evt.get('message', '')}"

    if evt_type == "watch_match":
        pat = evt.get("pattern", "?")
        out = evt.get("output", "")
        sup = evt.get("suppressed", 0)
        text = (
            f"[重要] 后台进程 {sid} 命中 watch 模式「{pat}」。\n"
            f"命令: {cmd}\n命中输出:\n{out}"
        )
        if sup:
            text += f"\n（另有 {sup} 条更早的命中被限流丢弃）"
        return text

    exit_code = evt.get("exit_code", "?")
    out = evt.get("output", "")
    return (
        f"[重要] 后台进程 {sid} 已结束（退出码 {exit_code}）。\n"
        f"命令: {cmd}\n输出:\n{out}"
    )


# ---------------------------------------------------------------------------
# process 工具：schema + handler
# ---------------------------------------------------------------------------

PROCESS_SCHEMA = {
    "name": "process",
    "description": (
        "管理 terminal(background=true) 启动的后台进程。actions: "
        "'list'（列出全部）、'poll'（查状态+最新输出）、'log'（完整输出，支持分页）、"
        "'wait'（阻塞至结束或超时）、'kill'（终止）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "log", "wait", "kill"],
                "description": "对后台进程执行的操作",
            },
            "session_id": {
                "type": "string",
                "description": "后台进程的 session_id（来自 terminal 后台返回）。除 list 外均必填。",
            },
            "timeout": {
                "type": "integer",
                "description": "wait 操作最多阻塞秒数，超时返回部分输出",
                "minimum": 1,
            },
            "offset": {
                "type": "integer",
                "description": "log 操作的行偏移（默认取最后 200 行）",
            },
            "limit": {
                "type": "integer",
                "description": "log 操作最多返回行数",
                "minimum": 1,
            },
        },
        "required": ["action"],
    },
}


def _handle_process(args: dict[str, Any]) -> str:
    import json

    action = args.get("action", "")
    session_id = str(args.get("session_id", "")) if args.get("session_id") is not None else ""
    owner_account_id = str(current_owner_account_id.get() or "").strip()

    if action == "list":
        return json.dumps(
            {"processes": process_registry.list_sessions(owner_account_id=owner_account_id)},
            ensure_ascii=False,
        )
    if action not in {"poll", "log", "wait", "kill"}:
        return tool_error(f"未知 action: {action}。可用: list, poll, log, wait, kill")
    if not session_id:
        return tool_error(f"{action} 需要 session_id")
    if action == "poll":
        return json.dumps(
            process_registry.poll(session_id, owner_account_id=owner_account_id),
            ensure_ascii=False,
        )
    if action == "log":
        return json.dumps(
            process_registry.read_log(
                session_id,
                offset=args.get("offset", 0),
                limit=args.get("limit", 200),
                owner_account_id=owner_account_id,
            ),
            ensure_ascii=False,
        )
    if action == "wait":
        return json.dumps(
            process_registry.wait(
                session_id,
                timeout=args.get("timeout"),
                owner_account_id=owner_account_id,
            ),
            ensure_ascii=False,
        )
    # kill
    return json.dumps(
        process_registry.kill_process(session_id, owner_account_id=owner_account_id),
        ensure_ascii=False,
    )


def register_process_tool(registry: Any) -> None:
    """注册 process 工具到给定 registry。"""
    registry.register(
        name="process",
        toolset="terminal",
        schema=PROCESS_SCHEMA,
        handler=_handle_process,
        emoji="⚙️",
        display_name="管理进程",
        ui_label_template="进程 {action}",
        always_load=True,
        search_hint="process background job poll log wait kill list terminal",
    )
