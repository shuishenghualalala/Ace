#!/usr/bin/env python3
"""Real desktop gateway acceptance. Creates a new KB/session; never mocks calls.

Start the development desktop with the reviewer roster from the example config.
Set REVIEW_GATEWAY_TOKEN if the gateway requires bearer authentication.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import websockets

from run_project_review import GOAL


async def run(base: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:8]
    sid, kb = f"review-desktop-{suffix}", f"fictional-review-{suffix}"
    token = os.environ.get("REVIEW_GATEWAY_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    metadata = {"session_id": sid, "kb_id": kb, "started_at": time.time(),
                "mode": "real_gateway_team", "fictional": True}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=120) as client:
        async def call(method: str, path: str, payload: dict):
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("ok") is False:
                raise RuntimeError(str(data))
            return data
        await call("POST", "/api/wiki/kbs", {"kb_id": kb, "name": "三项目虚构预审｜真实模型验收"})
        manifest = []
        for file in sorted((Path(__file__).resolve().parents[1] / "examples/project-review/materials").glob("*.md")):
            result = await call("POST", "/api/wiki/capture", {"kb_id": kb, "title": file.name,
                "content": file.read_text()})
            manifest.append({"file": file.name, "capture": result})
        (output / "sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        await call("POST", f"/api/session/{sid}/ensure", {"title": "三项目预审｜真实 Kimi 协作", "workspace_id": "default"})
        await call("PUT", f"/api/session/{sid}/agent-config", {"executor": "team", "team": {}})
        (output / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        print(f"session={sid} kb={kb}", flush=True)
        try:
            async with websockets.connect(base.replace("http", "ws", 1) + "/ws",
                    additional_headers=headers, max_size=16 * 1024 * 1024) as socket:
                await socket.send(json.dumps({"action": "subscribe", "sessions": [sid]}))
                await socket.send(json.dumps({"query": GOAL + "\n原始来源在所选知识库中，已授权只读本轮快照。每份独立审阅控制在1200字以内。",
                    "session_id": sid, "request_id": "review-" + suffix, "mode": "team",
                    "external_wiki_read_kb_id": kb, "collaboration_contract": "project_review_v1",
                    "team_execution_profile": {"requested_mode": "standard"}}, ensure_ascii=False))
                with (output / "events.jsonl").open("w") as events:
                    async with asyncio.timeout(1800):
                        async for raw in socket:
                            frame = json.loads(raw)
                            events.write(json.dumps(frame, ensure_ascii=False) + "\n")
                            events.flush()
                            kind, body = frame.get("kind"), frame.get("body") or {}
                            if kind in {"status", "error", "final", "followup_question"}:
                                print(kind, json.dumps(body, ensure_ascii=False)[:500], flush=True)
                            if kind == "ping":
                                await socket.send(json.dumps({"action": "pong"}))
                            if kind == "final" and frame.get("session_id", sid) == sid:
                                (output / "final.md").write_text(str(body.get("text") or ""))
                                break
                            if kind in {"error", "followup_question"} and frame.get("session_id", sid) == sid:
                                metadata["attention"] = body
                                break
            result = await client.get(f"/api/collaboration/sessions/{sid}/executions")
            result.raise_for_status()
            executions = result.json()["executions"]
            (output / "executions.json").write_text(json.dumps(executions, ensure_ascii=False, indent=2))
            metadata["states"] = [t["state"] for t in executions]
            metadata["success"] = bool(executions) and all(t["state"] == "completed" for t in executions)
            if not metadata["success"]:
                raise RuntimeError("真实网关尚未完成交付，检查规划、事件和执行记录")
        except Exception as exc:
            metadata["error"] = str(exc)
            raise
        finally:
            metadata["elapsed_seconds"] = time.time() - metadata["started_at"]
            (output / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:28180")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.base_url.rstrip("/"), args.output.resolve()))
