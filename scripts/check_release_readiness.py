"""Fail a release when external-service or supplier acceptance evidence is incomplete."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "docs" / "release-gates-2026-07-14.json"
_VERSION_RE = re.compile(r"^\s*version:\s*([^\s#]+)", re.MULTILINE)
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request", "head"}


def _load_policy(root: Path) -> dict[str, Any]:
    """Load the reviewable release policy without consulting runtime credentials."""
    path = root / "docs" / POLICY_PATH.name
    return json.loads(path.read_text(encoding="utf-8"))


def _skill_version(skill_dir: Path) -> str:
    """Read the declared Skill version used to identify a supplier artifact."""
    match = _VERSION_RE.search((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def _requests_without_timeout(path: Path) -> list[int]:
    """Return source lines where direct requests calls have no finite timeout keyword."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _HTTP_METHODS:
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name) or owner.id != "requests":
            continue
        if not any(keyword.arg == "timeout" for keyword in node.keywords):
            missing.append(node.lineno)
    return sorted(missing)


def _vlm_credential_violations(path: Path) -> list[str]:
    """Detect the legacy paths that the accepted VLM credential contract forbids."""
    source = path.read_text(encoding="utf-8")
    violations: list[str] = []
    if ".openclaw" in source:
        violations.append("legacy OpenClaw path")
    if 'Path("CREW_ENV_FILE")' in source or "Path('CREW_ENV_FILE')" in source:
        violations.append("literal CREW_ENV_FILE path")
    if 'parent.parent.parent / ".env"' in source or "parent.parent.parent / '.env'" in source:
        violations.append("repository-relative .env path")
    return violations


def collect_gate_results(root: Path = ROOT) -> list[dict[str, Any]]:
    """Evaluate release gates and return credential-free, machine-readable results."""
    policy = _load_policy(root)
    gates = {gate["id"]: gate for gate in policy["gates"]}
    results: list[dict[str, Any]] = []

    shared = gates["shared-deployment-https"]
    results.append(
        {
            "id": shared["id"],
            "status": shared["status"],
            "reason": "HTTPS endpoint, certificate and interface acceptance evidence is pending",
        }
    )

    cm_dir = root / "crew" / "skills" / "cm-cloud-manage"
    cm_version = _skill_version(cm_dir)
    missing_timeouts: list[str] = []
    for source_path in sorted((cm_dir / "scripts").glob("*.py")):
        missing_timeouts.extend(
            f"{source_path.name}:{line}" for line in _requests_without_timeout(source_path)
        )
    cm_policy = gates["cm-cloud-manage-timeouts"]
    results.append(
        {
            "id": cm_policy["id"],
            "status": cm_policy["status"],
            "version": cm_version,
            "blocked_scopes": cm_policy["blocked_scopes"],
            "reason": "supplier timeout acceptance is pending",
            "evidence": {"requests_without_timeout": missing_timeouts},
        }
    )

    vlm_policy = gates["vlm-credential-contract"]
    vlm_evidence: dict[str, Any] = {}
    for slug in ("image-understanding", "video-understanding"):
        skill_dir = root / "crew" / "skills" / slug
        source_path = skill_dir / "scripts" / ("image_understand.py" if slug.startswith("image") else "video_understand.py")
        vlm_evidence[slug] = {
            "version": _skill_version(skill_dir),
            "violations": _vlm_credential_violations(source_path),
        }
    results.append(
        {
            "id": vlm_policy["id"],
            "status": vlm_policy["status"],
            "blocked_scopes": vlm_policy["blocked_scopes"],
            "reason": "supplier credential-contract acceptance is pending",
            "evidence": vlm_evidence,
        }
    )
    native_policy = gates["native-security-matrix"]
    native_evidence: dict[str, Any] = {}
    native_ready = True
    # Evidence must be bound to this commit so a JSON file copied out of another run
    # cannot unlock the gate. CI attestation (GitHub artifact attestation / OIDC) is
    # the real fix; until that exists we at least require commit+repository+workflow_run
    # fields and match the commit to the current HEAD, so hand-written evidence is rejected.
    head_commit = _current_head_commit(root)
    for platform_name in ("linux", "windows"):
        configured = os.environ.get(
            f"ACE_SECURITY_{platform_name.upper()}_EVIDENCE", ""
        ).strip()
        evidence_path = Path(configured) if configured else None
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path else {}
        except (OSError, json.JSONDecodeError):
            evidence = {}
        passed = (
            evidence.get("status") == "passed"
            and evidence.get("real_runner") is True
            and bool(evidence.get("workflow_run"))
            and bool(evidence.get("repository"))
            and bool(evidence.get("artifact_sha256"))
            and _evidence_commit_matches(evidence, head_commit)
        )
        native_ready = native_ready and passed
        native_evidence[platform_name] = {
            "passed": passed,
            "workflow_run": str(evidence.get("workflow_run", ""))[:500],
            "commit": str(evidence.get("commit", ""))[:40],
            "repository": str(evidence.get("repository", ""))[:120],
        }
    package_path_value = os.environ.get("ACE_SECURITY_PACKAGE_EVIDENCE", "").strip()
    package_path = Path(package_path_value) if package_path_value else None
    try:
        package_evidence = (
            json.loads(package_path.read_text(encoding="utf-8")) if package_path else {}
        )
    except (OSError, json.JSONDecodeError):
        package_evidence = {}
    package_ready = (
        package_evidence.get("status") == "passed"
        and package_evidence.get("package_signature_verified") is True
        and package_evidence.get("desktop_walkthrough") is True
        and package_evidence.get("cargo_lock_verified") is True
        and _evidence_commit_matches(package_evidence, head_commit)
    )
    native_ready = native_ready and package_ready
    native_evidence["package"] = {"passed": package_ready}
    results.append(
        {
            "id": native_policy["id"],
            "status": "ready" if native_ready else native_policy["status"],
            "blocked_scopes": native_policy["blocked_scopes"],
            "reason": "real Windows and Linux native security evidence bound to this commit is required",
            "evidence": native_evidence,
        }
    )
    return results


def _current_head_commit(root: Path) -> str:
    """Return the short HEAD commit sha, or empty string if git is unavailable."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _evidence_commit_matches(evidence: dict[str, Any], head_commit: str) -> bool:
    """Evidence must name the same commit as the current HEAD (full or short sha)."""
    if not head_commit:
        return False
    commit = str(evidence.get("commit", "")).strip()
    if not commit:
        return False
    return head_commit == commit or head_commit.startswith(commit) or commit.startswith(head_commit)


def main(argv: list[str] | None = None) -> int:
    """Print gate results and return non-zero while any required gate is blocked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    results = collect_gate_results()
    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{result['status'].upper():7} {result['id']}: {result['reason']}")
    return 1 if any(result["status"] != "ready" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
