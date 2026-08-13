"""单个 Ace E2E case 执行器。

由 scripts/run_e2e_batch.py 以子进程方式调用，保证每个 case 拥有独立的
数据库、Crew Home、日志和工作区，互不污染。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crew.app import CrewApp, build_app
from crew.core.envelope import Envelope, ResponseChunk
from crew.state.config import load_config

OWNER = "local"


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _snapshot_workspace(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if root is None or not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            files[path.relative_to(root).as_posix()] = path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return files


def _setup_config(spec: dict[str, Any], case_dir: Path) -> Any:
    model_profile = str(spec.get("model_profile") or "default").strip()
    os.environ["CREW_MODEL_PROFILE"] = model_profile
    os.environ["CREW_HOME"] = str(case_dir / ".crew")

    cfg = load_config()
    # 开源版 README 的约定是 CREW_MODEL_API_KEY；若本地配置仍按默认
    # CREW_API_KEY 读取，测试环境自动补齐，避免 test runner 与项目文档脱节。
    if not cfg.has_llm_key and os.getenv("CREW_MODEL_API_KEY"):
        profile = cfg.model_profiles.get(cfg.active_model_id)
        if profile is not None:
            profile.api_key = os.environ["CREW_MODEL_API_KEY"]
            cfg.api_key = profile.api_key
    cfg.db_path = str(case_dir / "crew.db")
    cfg.memory_db_path = str(case_dir / "memory.db")
    cfg.crew_home = str(case_dir / ".crew")
    cfg.task_workspace_root = str(case_dir / "task_workspaces")
    cfg.log_file = str(case_dir / "crew.log")
    cfg.log_level = "INFO"
    cfg.llm_trace = True
    cfg.title_auto = False
    cfg.cron_enabled = False
    cfg.plugins_enabled = []
    cfg.mcp_servers = {}
    cfg.security_enabled = False
    cfg.sqlite_wal = True
    cfg.tasks_auto_background_after_seconds = 0.0
    cfg.max_iterations = min(int(cfg.max_iterations or 30), 30)
    cfg.subagent_max_iterations = min(int(cfg.subagent_max_iterations or 30), 30)
    cfg.subagent_timeout_seconds = min(float(cfg.subagent_timeout_seconds or 300), 300)
    cfg.tasks_subagent_execution_timeout_seconds = min(
        float(cfg.tasks_subagent_execution_timeout_seconds or 300), 300
    )
    cfg.stream_read_timeout = max(float(cfg.stream_read_timeout or 120), 180)
    cfg.tasks_agent_turn_execution_timeout_seconds = max(
        float(cfg.tasks_agent_turn_execution_timeout_seconds or 3600),
        float(spec.get("timeout_seconds") or 300),
    )
    if cfg.wiki is not None:
        cfg.wiki.ingest.auto_apply = True
    cfg.external_agents_enabled = True
    cfg.external_security_enabled = False
    return cfg


def _setup_workspace(
    app: CrewApp,
    spec: dict[str, Any],
    case_dir: Path,
    owner: str,
) -> Path:
    ws_spec = spec.get("workspace") or {}
    root = case_dir / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in (ws_spec.get("files") or {}).items():
        path = root / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    app.workspace_store.get("default", owner_account_id=owner)
    app.workspace_store.update(
        "default",
        owner_account_id=owner,
        root_path=str(root),
        instructions=str(ws_spec.get("instructions") or ""),
    )
    return root


def _setup_attachments(
    app: CrewApp,
    spec: dict[str, Any],
    owner: str,
) -> list[dict[str, Any]]:
    from crew.state.home import get_owner_runtime_home

    upload_dir = get_owner_runtime_home(owner) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(spec.get("attachments") or []):
        name = str(raw.get("name") or f"e2e_attachment_{index}.md")
        safe_name = Path(name).name
        path = upload_dir / safe_name
        path.write_text(str(raw.get("content") or ""), encoding="utf-8")
        result.append(
            {
                "name": safe_name,
                "path": str(path),
                "type": str(raw.get("type") or "text/markdown"),
            }
        )
    return result


def _setup_wiki(app: CrewApp, spec: dict[str, Any], owner: str) -> str:
    seed = spec.get("wiki_seed") or {}
    kb_id = str(seed.get("kb_id") or "e2e")
    store = app._wiki_store
    if kb_id == "default":
        store.init_kb(owner, kb_id)
    else:
        try:
            store.create_kb(kb_id, name=kb_id, owner_account_id=owner)
        except ValueError:
            store.init_kb(owner, kb_id)

    from crew.wiki.schemas import WikiPage

    for raw in seed.get("pages") or []:
        page = WikiPage(
            id="",
            page_type=str(raw.get("page_type") or "topic"),
            title=str(raw["title"]),
            content=str(raw.get("content") or ""),
            file_path="",
            summary=str(raw.get("summary") or ""),
            confidence=str(raw.get("confidence") or "high"),
            tags=[str(item) for item in (raw.get("tags") or [])],
        )
        store.save_page(page, owner_account_id=owner, kb_id=kb_id)
    return kb_id


def _setup_external(
    app: CrewApp,
    spec: dict[str, Any],
    owner: str,
) -> tuple[dict[str, Any] | None, str | None]:
    provider = str(spec.get("runtime_provider") or "").strip()
    if not provider:
        return None, "external case 缺少 runtime_provider"

    from crew.agent.external import detector, runtime_registry

    candidate = detector.scan_provider_runtime(runtime_registry.builtin_descriptor(provider))
    if candidate is None:
        return None, f"runtime {provider} 未安装"
    runtime = app.external_agents.upsert_runtime(candidate.as_dict())
    agent = app.external_agents.create_agent(
        owner_account_id=owner,
        name=f"E2E {provider}",
        runtime_id=runtime["id"],
        model=str(spec.get("runtime_model") or ""),
    )
    return {"executor": "external", "external_agent_id": agent["id"]}, None


async def _collect_chunks(
    app: CrewApp,
    envelope: Envelope,
    timeout: float,
) -> list[ResponseChunk]:
    chunks: list[ResponseChunk] = []

    async def _consume() -> None:
        async for chunk in app.handle(envelope):
            chunks.append(chunk)

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except TimeoutError:
        chunks.append(ResponseChunk.error(envelope.request_id, "总超时"))
    return chunks


def _assertions(
    spec: dict[str, Any],
    app: CrewApp,
    chunks: list[ResponseChunk],
    workspace_root: Path | None,
    owner: str,
    wiki_kb_id: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    error_messages = [
        str(chunk.body.get("message") or "")
        for chunk in chunks
        if chunk.kind == "error"
    ]
    if error_messages and not spec.get("allow_errors"):
        errors.append("收到 error 帧: " + " | ".join(error_messages[:5]))

    final_text = "".join(
        str(chunk.body.get("text") or "")
        for chunk in chunks
        if chunk.kind == "final"
    )
    used_tools = {
        str(chunk.body.get("name") or "")
        for chunk in chunks
        if chunk.kind == "tool" and str(chunk.body.get("phase") or "") == "start"
    }
    used_tools.discard("")

    expected_tools = spec.get("expected_tools") or []
    if expected_tools and not any(tool in used_tools for tool in expected_tools):
        errors.append(
            f"期望至少调用 {expected_tools}，实际调用 {sorted(used_tools)}"
        )

    expected_files = spec.get("expected_files") or []
    for item in expected_files:
        relative = str(item.get("path") or "")
        if not relative:
            continue
        target = (workspace_root / relative) if workspace_root is not None else None
        if target is None or not target.is_file():
            errors.append(f"期望工作区文件不存在: {relative}")
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        for keyword in item.get("contains") or []:
            if str(keyword) not in content:
                errors.append(
                    f"期望文件 {relative} 包含 {keyword!r}，实际内容前 200 字符: "
                    + content[:200].replace("\n", "\\n")
                )

    expected_keywords = spec.get("expected_final_keywords") or []
    if expected_keywords:
        if not final_text:
            errors.append("期望 final 文本，但未收到 final 帧")
        else:
            for keyword in expected_keywords:
                if str(keyword) not in final_text:
                    errors.append(
                        f"final 文本缺少关键词 {keyword!r}，实际前 300 字符: "
                        + final_text[:300].replace("\n", "\\n")
                    )

    if spec.get("expect_wiki_sources") or spec.get("expect_wiki_pages"):
        store = app._wiki_store
        source_count = len(store.list_raws(owner, kb_id=wiki_kb_id))
        page_count = len(store.list_all(owner, kb_id=wiki_kb_id))
        if spec.get("expect_wiki_sources") and source_count == 0:
            errors.append(f"期望 wiki 有 raw source，实际 0 个 (kb={wiki_kb_id})")
        if spec.get("expect_wiki_pages") and page_count == 0:
            errors.append(f"期望 wiki 有页面，实际 0 个 (kb={wiki_kb_id})")

    return not errors, errors


async def _run_case(spec: dict[str, Any], case_dir: Path) -> int:
    started = time.monotonic()
    result: dict[str, Any] = {
        "case_id": spec.get("case_id"),
        "category": spec.get("category"),
        "title": spec.get("title"),
        "status": "failed",
        "error": "",
        "duration_seconds": 0.0,
        "tools": [],
        "final_text": "",
    }
    workspace_root: Path | None = None
    attachments: list[dict[str, Any]] = []
    session_config: dict[str, Any] = dict(spec.get("session_config") or {})
    wiki_kb_id = str(session_config.get("wiki_kb_id") or "default")
    skip_reason: str | None = None
    app: CrewApp | None = None

    try:
        cfg = _setup_config(spec, case_dir)
        requires_llm = str(spec.get("mode") or "agent") != "external"
        if requires_llm and not cfg.has_llm_key:
            skip_reason = f"模型 {cfg.active_model_id} 未配置 API Key"
        else:
            import crew.state.logging as logging_mod

            logging_mod._CONFIGURED = False
            logging_mod._LLM_TRACE_ENABLED = False

            app = build_app(config=cfg, enable_team=True)
            await app.startup(start_cron=False)
            try:
                owner = OWNER

                if spec.get("setup") == "wiki_seed":
                    wiki_kb_id = _setup_wiki(app, spec, owner)
                    session_config["wiki_kb_id"] = wiki_kb_id

                if spec.get("runtime_provider"):
                    external_config, reason = _setup_external(app, spec, owner)
                    if reason:
                        skip_reason = reason
                    else:
                        session_config.update(external_config or {})

                if skip_reason is None:
                    if spec.get("attachments"):
                        attachments = _setup_attachments(app, spec, owner)

                    if spec.get("workspace"):
                        workspace_root = _setup_workspace(app, spec, case_dir, owner)
                        _write_json(
                            case_dir / "workspace-before.json",
                            _snapshot_workspace(workspace_root),
                        )
                        if spec.get("mode") == "external" and workspace_root is not None:
                            timeout_seconds = float(spec.get("timeout_seconds") or 300)
                            cfg.agent_acp_config = {
                                **cfg.agent_acp_config,
                                "timeout": max(120.0, timeout_seconds),
                                "cwd": str(workspace_root),
                            }

                    session_id = f"e2e_{spec.get('case_id') or 'case'}"
                    workspace_id = str(spec.get("workspace_id") or "default")
                    app.session_store.ensure_session(
                        session_id,
                        workspace_id=workspace_id,
                        owner_account_id=owner,
                    )
                    if session_config:
                        app.session_store.set_agent_config(
                            session_id,
                            session_config,
                            owner_account_id=owner,
                        )

                    envelope_mode = str(spec.get("mode") or "agent")
                    if envelope_mode == "external":
                        envelope_mode = "agent"
                    params = dict(spec.get("envelope_params") or {})
                    if session_config.get("wiki_kb_id"):
                        params["wiki_kb_id"] = session_config["wiki_kb_id"]
                    envelope = Envelope.of(
                        str(spec.get("prompt") or ""),
                        session_id=session_id,
                        user_id=owner,
                        workspace_id=workspace_id,
                        mode=envelope_mode,
                        params=params,
                        attachments=attachments,
                    )

                    timeout = float(spec.get("timeout_seconds") or 300)
                    chunks = await _collect_chunks(app, envelope, timeout=timeout)

                    transcript = [
                        {
                            "ts": round(chunk.ts, 3),
                            "kind": chunk.kind,
                            "sequence": chunk.sequence,
                            "is_final": chunk.is_final,
                            "body": chunk.body,
                        }
                        for chunk in chunks
                    ]
                    with (case_dir / "transcript.jsonl").open(
                        "w", encoding="utf-8"
                    ) as handle:
                        for item in transcript:
                            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

                    final_text = "".join(
                        str(chunk.body.get("text") or "")
                        for chunk in chunks
                        if chunk.kind == "final"
                    )
                    tools = sorted(
                        {
                            str(chunk.body.get("name") or "")
                            for chunk in chunks
                            if chunk.kind == "tool"
                            and str(chunk.body.get("phase") or "") == "start"
                        }
                    )
                    result["tools"] = tools
                    result["final_text"] = final_text[:2000]

                    passed, failures = _assertions(
                        spec,
                        app,
                        chunks,
                        workspace_root,
                        owner,
                        wiki_kb_id,
                    )
                    if passed:
                        result["status"] = "passed"
                    else:
                        result["status"] = "failed"
                        result["error"] = "；".join(failures)
                    if workspace_root is not None:
                        _write_json(
                            case_dir / "workspace-after.json",
                            _snapshot_workspace(workspace_root),
                        )
            finally:
                if app is not None:
                    await app.shutdown(timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        if skip_reason is None:
            result["status"] = "failed"
            result["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    if skip_reason is not None:
        result["status"] = "skipped"
        result["error"] = skip_reason

    result["duration_seconds"] = round(time.monotonic() - started, 2)
    _write_json(case_dir / "result.json", result)
    print(
        f"[{result['status'].upper()}] {spec.get('category')}/{spec.get('case_id')} "
        f"{result['duration_seconds']}s"
    )
    if result.get("error"):
        print(result["error"])
    return 0 if result["status"] == "passed" else (2 if result["status"] == "skipped" else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行单个 Ace E2E case")
    parser.add_argument("spec", help="case JSON 文件路径")
    parser.add_argument("case_dir", help="case 产物目录")
    args = parser.parse_args()

    case_dir = Path(args.case_dir).resolve()
    case_dir.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    _write_json(case_dir / "case.json", spec)

    return asyncio.run(_run_case(spec, case_dir))


if __name__ == "__main__":
    raise SystemExit(main())
