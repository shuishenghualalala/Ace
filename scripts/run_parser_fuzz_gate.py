"""Run the bounded TEST-012 parser campaigns with per-language time limits.

The gate intentionally uses only repository test runners and deterministic
generators. It never invokes a shell and writes failure logs under the ignored
``test-results`` tree so CI can upload an exact reproducer artifact.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "security" / "test_012_parser_corpus.json"
ARTIFACT_DIR = ROOT / "test-results" / "test-012"
ABSOLUTE_MAX_CASES = 2048
ABSOLUTE_MAX_GENERATED_INPUT_BYTES = 4096


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    name: str
    argv: tuple[str, ...]
    returncode: int
    elapsed_seconds: float
    timed_out: bool
    log_path: str


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"TEST-012 requires {name} on PATH")
    return executable


def _timeout_seconds() -> int:
    try:
        requested = int(os.environ.get("ACE_TEST012_COMMAND_TIMEOUT_SECONDS", "300"))
    except ValueError:
        requested = 300
    return max(60, min(requested, 900))


def _validated_campaign(corpus: dict[str, object]) -> tuple[str, str, dict[str, int]]:
    if corpus.get("schema_version") != 1 or not isinstance(corpus.get("campaign"), dict):
        raise RuntimeError("TEST-012 corpus schema is unsupported")
    raw_campaign = corpus["campaign"]
    campaign: dict[str, int] = {}
    for field in (
        "seed",
        "ci_cases",
        "scheduled_cases",
        "max_cases",
        "max_generated_input_bytes",
    ):
        value = raw_campaign.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"TEST-012 campaign.{field} must be an integer")
        campaign[field] = value
    if not (
        1 <= campaign["ci_cases"] <= campaign["scheduled_cases"]
        <= campaign["max_cases"] <= ABSOLUTE_MAX_CASES
    ):
        raise RuntimeError("TEST-012 campaign case bounds are invalid")
    if not (
        1
        <= campaign["max_generated_input_bytes"]
        <= ABSOLUTE_MAX_GENERATED_INPUT_BYTES
    ):
        raise RuntimeError("TEST-012 generated input bound is unsafe")

    try:
        requested_seed = int(os.environ.get("ACE_TEST012_SEED", campaign["seed"]))
        requested_cases = int(os.environ.get("ACE_TEST012_CASES", campaign["ci_cases"]))
    except ValueError as exc:
        raise RuntimeError("TEST-012 seed and case overrides must be integers") from exc
    if not 1 <= requested_seed <= 0xFFFF_FFFF:
        raise RuntimeError("TEST-012 seed must fit an unsigned 32-bit integer")
    bounded_cases = max(
        campaign["ci_cases"],
        min(requested_cases, campaign["max_cases"]),
    )
    return str(requested_seed), str(bounded_cases), campaign


def _display_command(argv: tuple[str, ...]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _console_safe(value: str) -> str:
    encoding = sys.stdout.encoding or "utf-8"
    return value.encode(encoding, "backslashreplace").decode(encoding)


def _run(command: GateCommand, *, timeout: int, environment: dict[str, str]) -> GateResult:
    started = time.monotonic()
    timed_out = False
    returncode = 0
    try:
        completed = subprocess.run(
            command.argv,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
        output = completed.stdout
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        captured = exc.stdout or ""
        output = captured if isinstance(captured, str) else captured.decode("utf-8", "replace")
        output += f"\nTEST-012 command timed out after {timeout} seconds\n"

    elapsed = time.monotonic() - started
    log_path = ARTIFACT_DIR / f"{command.name}.log"
    log_path.write_text(
        f"$ {_display_command(command.argv)}\n\n{output}",
        encoding="utf-8",
    )
    print(f"\n[{command.name}] {_display_command(command.argv)}")
    safe_output = _console_safe(output)
    print(safe_output, end="" if safe_output.endswith("\n") else "\n")
    print(f"[{command.name}] exit={returncode} elapsed={elapsed:.2f}s")
    return GateResult(
        name=command.name,
        argv=command.argv,
        returncode=returncode,
        elapsed_seconds=round(elapsed, 3),
        timed_out=timed_out,
        log_path=str(log_path.relative_to(ROOT)).replace("\\", "/"),
    )


def main() -> int:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    seed, cases, campaign = _validated_campaign(corpus)

    if ARTIFACT_DIR.exists():
        shutil.rmtree(ARTIFACT_DIR)
    ARTIFACT_DIR.mkdir(parents=True)

    cargo = _required_executable("cargo")
    npm = _required_executable("npm")
    commands = (
        GateCommand(
            "rust",
            (
                cargo,
                "test",
                "--manifest-path",
                "security-runtime/Cargo.toml",
                "--test",
                "parser_fuzz_gate",
                "--locked",
            ),
        ),
        GateCommand(
            "python",
            (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/security/test_parser_fuzz_gate.py",
            ),
        ),
        GateCommand(
            "typescript",
            (
                npm,
                "--prefix",
                "desktop",
                "test",
                "--",
                "--run",
                "tests/unit/parser-fuzz-gate.test.ts",
            ),
        ),
    )
    environment = dict(os.environ)
    environment.update(
        {
            "ACE_TEST012_SEED": seed,
            "ACE_TEST012_CASES": cases,
            "PYTHONHASHSEED": "0",
        }
    )
    timeout = _timeout_seconds()
    results = [
        _run(command, timeout=timeout, environment=environment)
        for command in commands
    ]
    failed = [result for result in results if result.returncode != 0]
    summary = {
        "schema_version": 1,
        "seed": seed,
        "cases": cases,
        "max_generated_input_bytes": campaign["max_generated_input_bytes"],
        "command_timeout_seconds": timeout,
        "results": [
            {
                "name": result.name,
                "command": _display_command(result.argv),
                "returncode": result.returncode,
                "elapsed_seconds": result.elapsed_seconds,
                "timed_out": result.timed_out,
                "log_path": result.log_path,
            }
            for result in results
        ],
    }
    (ARTIFACT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if failed:
        shutil.copy2(CORPUS, ARTIFACT_DIR / CORPUS.name)
        (ARTIFACT_DIR / "PROMOTE-TO-CORPUS.txt").write_text(
            "Minimize the failing input shown in the language log, add it to "
            f"{CORPUS.relative_to(ROOT)}, and rerun the command recorded in summary.json.\n",
            encoding="utf-8",
        )
        print(
            "TEST-012 failed: "
            + ", ".join(result.name for result in failed)
            + f". Reproducer logs: {ARTIFACT_DIR}"
        )
        return 1

    print(
        "TEST-012 passed for Rust, Python, and TypeScript "
        f"(seed={seed}, cases={cases}, max_input={campaign['max_generated_input_bytes']} bytes)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
