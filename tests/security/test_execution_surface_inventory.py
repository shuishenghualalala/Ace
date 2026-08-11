"""Keep every process-capable source visible to the security migration."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs" / "security" / "execution-surface-inventory.md"
PYTHON_ROOTS = (ROOT / "crew", ROOT / "optional-skills", ROOT / "plugins", ROOT / "scripts")
JAVASCRIPT_ROOTS = (
    ROOT / "crew",
    ROOT / "optional-skills",
    ROOT / "plugins",
    ROOT / "desktop" / "src",
    ROOT / "desktop" / "scripts",
)
PROCESS_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.system",
    "pexpect.spawn",
    "pty.fork",
    "pty.spawn",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "subprocess.run",
}
INDIRECT_SURFACES = {
    "crew/browser/manager.py",
    "crew/cron/scheduler.py",
    "crew/sites/manager.py",
    "crew/skills/html-to-pdf/scripts/convert.cjs",
    "crew/team/team_manager.py",
    "crew/tools/builtin.py",
    "crew/tools/mcp_client.py",
    "crew/wiki/parser.py",
}
DESKTOP_PROCESS_FILES = {
    "desktop/scripts/check-security.mjs",
    "desktop/scripts/resolve-playwright-candidates.mjs",
    "desktop/src/main/index.ts",
    "desktop/src/main/open-with-service.ts",
    "desktop/src/main/security-setup.ts",
    "desktop/src/main/uninstall.ts",
}


def _inventory_paths() -> set[str]:
    text = INVENTORY.read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([^`]+)` \|", text, flags=re.MULTILINE))


def _qualified_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    modules: dict[str, str] = {}
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                modules[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                functions[item.asname or item.name] = f"{node.module}.{item.name}"

    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            name = functions.get(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            module = modules.get(node.func.value.id, node.func.value.id)
            name = f"{module}.{node.func.attr}"
        else:
            name = None
        if name in PROCESS_CALLS:
            calls.add(name)
    return calls


def _python_process_files() -> set[str]:
    found: set[str] = set()
    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "site-packages" in path.parts:
                continue
            if _qualified_calls(path):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def _javascript_process_files() -> set[str]:
    found: set[str] = set()
    for root in JAVASCRIPT_ROOTS:
        for pattern in ("*.ts", "*.tsx", "*.js", "*.mjs", "*.cjs"):
            for path in root.rglob(pattern):
                if "node_modules" in path.parts or path.name.endswith(".d.ts"):
                    continue
                text = path.read_text(encoding="utf-8-sig")
                if re.search(r"(?:from\s+|require\()['\"](?:node:)?child_process['\"]", text):
                    found.add(path.relative_to(ROOT).as_posix())
    return found


def _desktop_process_files() -> set[str]:
    return {path for path in _javascript_process_files() if path.startswith("desktop/")}


def test_all_process_capable_sources_are_classified() -> None:
    registered = _inventory_paths()
    discovered = _python_process_files() | _javascript_process_files()

    assert discovered <= registered, f"未登记的外部执行面: {sorted(discovered - registered)}"
    assert INDIRECT_SURFACES <= registered, f"未登记的间接执行面: {sorted(INDIRECT_SURFACES - registered)}"
    assert _desktop_process_files() == DESKTOP_PROCESS_FILES

    # 直接执行面条目不得陈旧：登记的直接文件集合必须与源码扫描结果一致
    direct_entries = {
        path
        for path in registered
        if path.endswith((".py", ".ts", ".tsx", ".js", ".mjs", ".cjs"))
        and path not in INDIRECT_SURFACES
    }
    assert direct_entries == discovered
