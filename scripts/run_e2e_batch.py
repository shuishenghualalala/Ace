"""Ace 后端 E2E 批量入口。

读取 tests/e2e/scenarios.yaml，每个 case 使用独立子进程运行，并产出
HTML 报告、每 case 的 crew.log / llm.jsonl / transcript.jsonl 和工作区快照。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS = ROOT / "tests" / "e2e" / "scenarios.yaml"
CASE_RUNNER = ROOT / "tests" / "e2e" / "_run_case.py"
_PYTHON_CACHE: str | None = None


def _resolve_python() -> str:
    """优先使用仓库 .venv，保证 Python >= 3.11 且能 import crew。"""
    global _PYTHON_CACHE
    if _PYTHON_CACHE:
        return _PYTHON_CACHE
    candidates = [sys.executable]
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(
        [
            ROOT / ".venv" / "bin" / "python",
            ROOT / ".venv" / "bin" / "python3",
            ROOT / ".venv" / "Scripts" / "python.exe",
        ]
    )
    checked: list[str] = []
    for candidate in candidates:
        executable = str(candidate)
        if executable in checked:
            continue
        checked.append(executable)
        try:
            proc = subprocess.run(
                [
                    executable,
                    "-c",
                    "import sys; assert sys.version_info[:2] >= (3, 11); import crew",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                cwd=str(ROOT),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            _PYTHON_CACHE = executable
            return executable
    raise RuntimeError(
        "没有找到 Python 3.11+ 解释器。请先激活仓库环境："
        "source .venv/bin/activate（Windows: .venv\\Scripts\\Activate.ps1）"
    )


def _load_scenarios(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"场景文件必须是 YAML 对象: {path}")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for category, cases in raw.items():
        if not isinstance(cases, dict):
            continue
        result[str(category)] = {
            str(case_id): dict(case)
            for case_id, case in cases.items()
            if isinstance(case, dict)
        }
    return result


def _flat_cases(
    scenarios: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for category, items in scenarios.items():
        for case_id, spec in items.items():
            cases.append(
                {
                    "category": category,
                    "case_id": case_id,
                    **spec,
                }
            )
    return cases


def _filter_cases(
    cases: list[dict[str, Any]],
    categories: list[str],
    case_ids: list[str],
) -> list[dict[str, Any]]:
    category_set = {item for item in categories if item}
    id_set = {item for item in case_ids if item}
    result = []
    for case in cases:
        if category_set and case["category"] not in category_set:
            continue
        if id_set and case["case_id"] not in id_set:
            continue
        result.append(case)
    return result


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _run_one(
    case: dict[str, Any],
    case_dir: Path,
    profile: str,
) -> dict[str, Any]:
    spec = dict(case)
    spec["model_profile"] = profile or str(case.get("model_profile") or "default")
    spec_file = case_dir / "case.json"
    _write_json(spec_file, spec)

    env = os.environ.copy()
    env["CREW_MODEL_PROFILE"] = spec["model_profile"]
    env["PYTHONUNBUFFERED"] = "1"

    timeout = float(case.get("timeout_seconds") or 300) + 60
    python = _resolve_python()
    try:
        proc = subprocess.run(
            [python, str(CASE_RUNNER), str(spec_file), str(case_dir)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        runner_log = (proc.stdout or "") + (proc.stderr or "")
        (case_dir / "runner.log").write_text(runner_log, encoding="utf-8")
        result_file = case_dir / "result.json"
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            result = {
                "case_id": case["case_id"],
                "category": case["category"],
                "title": case.get("title", ""),
                "status": "failed",
                "error": f"执行器未产出 result.json，退出码 {proc.returncode}",
                "duration_seconds": 0.0,
                "tools": [],
                "final_text": "",
            }
            _write_json(result_file, result)
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.stdout:
            partial += str(exc.stdout)
        if exc.stderr:
            partial += str(exc.stderr)
        (case_dir / "runner.log").write_text(
            partial + "\n[runner timeout]\n",
            encoding="utf-8",
        )
        result = {
            "case_id": case["case_id"],
            "category": case["category"],
            "title": case.get("title", ""),
            "status": "failed",
            "error": f"case 执行超过 {timeout:.0f}s",
            "duration_seconds": timeout,
            "tools": [],
            "final_text": "",
        }
        _write_json(case_dir / "result.json", result)
    return result


def _render_report(
    report_dir: Path,
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for item in results:
        counts[item.get("status")] = counts.get(item.get("status"), 0) + 1

    rows: list[str] = []
    for case, result in zip(cases, results):
        rel_dir = f"./{case['category']}/{case['case_id']}"
        status = str(result.get("status") or "failed")
        duration = f"{float(result.get('duration_seconds') or 0):.1f}s"
        error = str(result.get("error") or "")
        if len(error) > 220:
            error = error[:220] + " ..."
        tools = ", ".join(result.get("tools") or []) or "-"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(case['category'] + '/' + case['case_id'])}</code></td>"
            f"<td>{html.escape(str(case.get('title') or ''))}</td>"
            f"<td class=\"{status}\">{status.upper()}</td>"
            f"<td>{duration}</td>"
            f"<td>{html.escape(tools)}</td>"
            f"<td>{html.escape(error)}</td>"
            "<td>"
            f"<a href=\"{rel_dir}/result.json\">result</a> · "
            f"<a href=\"{rel_dir}/transcript.jsonl\">transcript</a> · "
            f"<a href=\"{rel_dir}/llm.jsonl\">llm</a> · "
            f"<a href=\"{rel_dir}/crew.log\">log</a> · "
            f"<a href=\"{rel_dir}/workspace-before.json\">before</a> · "
            f"<a href=\"{rel_dir}/workspace-after.json\">after</a>"
            "</td>"
            "</tr>"
        )

    title = "Ace 后端 E2E 批量测试报告"
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 32px auto; max-width: 1200px; padding: 0 20px; color: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left;
              vertical-align: top; font-size: 14px; }}
    th {{ background: #f3f4f6; }}
    .passed {{ color: #166534; font-weight: 600; }}
    .failed {{ color: #b91c1c; font-weight: 600; }}
    .skipped {{ color: #92400e; font-weight: 600; }}
    code {{ background: #f3f4f6; padding: 1px 4px; border-radius: 4px; }}
    a {{ color: #111827; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p>生成时间: {datetime.now(UTC).isoformat(timespec='seconds')}</p>
  <p>通过 {counts['passed']} / 失败 {counts['failed']} / 跳过 {counts['skipped']} / 总数 {len(results)}</p>
  <table>
    <thead><tr><th>Case</th><th>标题</th><th>状态</th><th>耗时</th><th>工具</th><th>错误</th><th>产物</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
    (report_dir / "index.html").write_text(html_text, encoding="utf-8")
    _write_json(report_dir / "summary.json", {"cases": results})


def main() -> int:
    parser = argparse.ArgumentParser(description="批量运行 Ace 后端 E2E 场景")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--category", default="", help="逗号分隔，例如 complex_tasks,wiki")
    parser.add_argument("--case", default="", help="逗号分隔的 case id")
    parser.add_argument("--profile", default="", help="覆盖 CREW_MODEL_PROFILE")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT
        / "build"
        / "e2e"
        / datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
    )
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help="有 skip 的 case 时也以非零退出",
    )
    args = parser.parse_args()

    scenarios = _load_scenarios(args.scenarios)
    cases = _filter_cases(
        _flat_cases(scenarios),
        [item for item in args.category.split(",") if item.strip()],
        [item for item in args.case.split(",") if item.strip()],
    )
    if not cases:
        print("没有匹配的 E2E case", file=sys.stderr)
        return 1

    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for case in cases:
        case_dir = report_dir / case["category"] / case["case_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        result = _run_one(case, case_dir, args.profile)
        elapsed = time.monotonic() - started
        label = f"[{result.get('status', 'failed').upper()}] {case['category']}/{case['case_id']}"
        print(f"{label} {elapsed:.1f}s")
        if result.get("error"):
            print(f"  {result['error'][:400]}")
        results.append(result)

    _render_report(report_dir, cases, results)
    print(f"\n报告: {report_dir / 'index.html'}")

    failed = any(item.get("status") == "failed" for item in results)
    skipped = any(item.get("status") == "skipped" for item in results)
    if failed or (args.fail_on_skip and skipped):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
