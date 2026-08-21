"""Keep every executable, I/O, browser, and IPC surface in a strict ledger."""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from scripts.generate_execution_surface_inventory import render_markdown

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs" / "security" / "execution-surface-inventory.json"
INVENTORY_SCHEMA = ROOT / "docs" / "security" / "execution-surface-inventory.schema.json"
INVENTORY_SUMMARY = ROOT / "docs" / "security" / "execution-surface-inventory.md"
PYTHON_ROOTS = (ROOT / "crew", ROOT / "optional-skills", ROOT / "plugins", ROOT / "scripts")
JAVASCRIPT_ROOTS = (
    ROOT / "crew",
    ROOT / "optional-skills",
    ROOT / "plugins",
    ROOT / "desktop" / "src",
    ROOT / "desktop" / "scripts",
)
RUST_ROOTS = (ROOT / "security-runtime" / "src",)
PYTHON_NETWORK_ROOTS = (ROOT / "crew", ROOT / "plugins")
PYTHON_FILE_ROOTS = (
    ROOT / "crew" / "browser",
    ROOT / "crew" / "plugins",
    ROOT / "crew" / "tools",
    ROOT / "crew" / "wiki",
    ROOT / "plugins",
)
JAVASCRIPT_PRIMITIVE_ROOTS = (ROOT / "desktop" / "src", ROOT / "web" / "src")
JAVASCRIPT_BROWSER_ROOTS = (ROOT / "desktop" / "src" / "main",)
GATEWAY_ROOT = ROOT / "crew" / "gateway"
IPC_CHANNELS = ROOT / "desktop" / "src" / "shared" / "ipc-channels.ts"
IPC_HANDLERS = ROOT / "desktop" / "src" / "main" / "index.ts"

# Primitive discovery is intentionally bounded to the current AST/regex patterns below;
# it is not a complete network or callsite inventory.

# This is the original process gate. Keep this list and its AST-based discovery intact.
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
    "crew/agent/external/acp_adapter.py",
    "crew/agent/external/cli_adapter.py",
    "crew/agent/external/codex_adapter.py",
    "crew/agent/external/detector.py",
    "crew/browser/manager.py",
    "crew/cron/scheduler.py",
    "crew/sites/manager.py",

    "crew/team/team_manager.py",
    "crew/tools/builtin.py",
    "crew/tools/managed_tools.py",
    "crew/tools/mcp_client.py",
    "crew/wiki/parser.py",
}
DESKTOP_PROCESS_FILES = {
    "desktop/scripts/check-security.mjs",
    "desktop/scripts/pw-contract.ts",
    "desktop/scripts/resolve-playwright-candidates.mjs",
    "desktop/src/main/gateway-instance-auth.ts",
    "desktop/src/main/index.ts",
    "desktop/src/main/open-with-service.ts",
    "desktop/src/main/security-setup.ts",
    "desktop/src/main/uninstall.ts",
    "desktop/src/main/update/update-installer.ts",
}

_PYTHON_NETWORK_CALLS = {
    "aiohttp.ClientSession": "aiohttp.ClientSession",
    "httpx.AsyncClient": "httpx.AsyncClient",
    "httpx.Client": "httpx.Client",
    "httpx.delete": "httpx.request",
    "httpx.get": "httpx.request",
    "httpx.patch": "httpx.request",
    "httpx.post": "httpx.request",
    "httpx.put": "httpx.request",
    "httpx.request": "httpx.request",
    "httpx2.AsyncClient": "httpx2.AsyncClient",
    "requests.delete": "requests.request",
    "requests.get": "requests.request",
    "requests.patch": "requests.request",
    "requests.post": "requests.request",
    "requests.put": "requests.request",
    "requests.request": "requests.request",
    "requests.Session": "requests.Session",
    "socket.create_connection": "socket.create_connection",
    "urllib.request.urlopen": "urllib.request.urlopen",
}
_PYTHON_NETWORK_REFERENCES = {
    "httpx.AsyncClient": "httpx.AsyncClient",
    "httpx.Client": "httpx.Client",
    "httpx2.AsyncClient": "httpx2.AsyncClient",
    "requests.Session": "requests.Session",
    "socket.getaddrinfo": "socket.getaddrinfo",
    "socket.socket": "socket.socket",
}
_PYTHON_FILE_METHODS = {
    "open": "Path.open",
    "read_bytes": "Path.read_bytes",
    "read_text": "Path.read_text",
    "write_bytes": "Path.write_bytes",
    "write_text": "Path.write_text",
}
_RUST_PROCESS_PATTERNS = (
    r"\bCommand::new\s*\(",
    r"\bCreateProcess(?:AsUserW|WithLogonW)\s*\(",
    r"\blibc::fork\s*\(",
)
_RUST_NETWORK_PATTERNS = {
    "TcpListener::bind": r"\bTcpListener::bind\s*\(",
    "TcpStream::connect": r"\bTcpStream::connect\s*\(",
    "TcpStream::connect_timeout": r"\bTcpStream::connect_timeout\s*\(",
    "UdpSocket": r"\bUdpSocket::(?:bind|connect)\s*\(",
    "reqwest": r"\breqwest::",
}
_RUST_FILE_PATTERNS = {
    "fs.read": r"\b(?:std::)?fs::(?:read|read_to_string)\s*\(",
    "fs.write": r"\b(?:std::)?fs::write\s*\(",
    "fs.mutate": r"\b(?:std::)?fs::(?:copy|rename|remove_file|create_dir|create_dir_all)\s*\(",
    "File.open": r"\bFile::open\s*\(",
    "File.create": r"\bFile::create\s*\(",
    "OpenOptions": r"\bOpenOptions::new\s*\(",
}
_JAVASCRIPT_NETWORK_PATTERNS = {
    "axios": r"(?<![\w.])axios(?:\.(?:delete|get|patch|post|put|request))?\s*\(",
    "fetch": r"(?<![\w.])fetch\s*\(",
    "WebSocket": r"\bnew\s+WebSocket\s*\(",
    "net.connect": r"\bnet\.(?:connect|createConnection)\s*\(",
    "http.get": r"\bhttp\.get\s*\(",
    "http.request": r"\bhttp\.request\s*\(",
    "https.get": r"\bhttps\.get\s*\(",
    "https.request": r"\bhttps\.request\s*\(",
    "undici.request": r"\bundici\.request\s*\(",
}
_JAVASCRIPT_BROWSER_PATTERNS = {
    "debugger.sendCommand": r"\bdebugger\.sendCommand\s*\(",
    "downloadURL": r"\.downloadURL\s*\(",
    "executeJavaScript": r"\.executeJavaScript\s*\(",
    "setDownloadPath": r"\.setDownloadPath\s*\(",
    "setFileInputFiles": r"\.setFileInputFiles\s*\(",
    "setPermissionCheckHandler": r"\.setPermissionCheckHandler\s*\(",
    "setPermissionRequestHandler": r"\.setPermissionRequestHandler\s*\(",
    "setWindowOpenHandler": r"\.setWindowOpenHandler\s*\(",
    "will-download": r"\.on\s*\(\s*['\"]will-download['\"]",
    "will-navigate": r"\.on\s*\(\s*['\"]will-navigate['\"]",
}
_JAVASCRIPT_IPC_PATTERNS = {
    "ipcMain.handle": r"\bipcMain\.handle\s*\(",
    "ipcMain.handle.bind": r"\bipcMain\.handle\.bind\s*\(",
    "ipcMain.on": r"\bipcMain\.on\s*\(",
    "ipcMain.on.bind": r"\bipcMain\.on\.bind\s*\(",
    "ipcMain.once": r"\bipcMain\.once\s*\(",
}
_FS_FUNCTIONS = {
    "copyFile",
    "createReadStream",
    "createWriteStream",
    "open",
    "readFile",
    "readFileSync",
    "rename",
    "writeFile",
    "writeFileSync",
}


@dataclass(frozen=True, order=True)
class PrimitiveRef:
    kind: str
    path: str
    primitive: str


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _inventory() -> dict[str, Any]:
    return _load_json(INVENTORY)


def _inventory_paths(category: str) -> set[str]:
    return {
        item["path"]
        for item in _inventory()["surfaces"]
        if category in item["categories"]
    }


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
                if (
                    "tests" in path.parts
                    or "node_modules" in path.parts
                    or path.name.endswith(".d.ts")
                    # The contract bundle is a gitignored derivative of the
                    # reviewed TypeScript source and must not become a second
                    # inventory record after the Electron contract runs.
                    or path.relative_to(ROOT).as_posix() == "desktop/scripts/pw-contract.mjs"
                ):
                    continue
                text = path.read_text(encoding="utf-8-sig")
                if re.search(r"(?:from\s+|require\()['\"](?:node:)?child_process['\"]", text):
                    found.add(path.relative_to(ROOT).as_posix())
    return found


def _desktop_process_files() -> set[str]:
    return {path for path in _javascript_process_files() if path.startswith("desktop/")}


ELECTRON_PROCESS_LEDGER = ROOT / "docs" / "security" / "electron-process-callsites.json"
_JS_PROCESS_CALL_RE = re.compile(
    r"(?<![.\w])\b(spawn|spawnSync|exec|execFile|execFileSync|execSync|fork)\s*\("
)


def _electron_process_callsites(
    files: Iterable[str] | None = None,
) -> dict[str, int]:
    """Enumerate every Desktop child_process call by file, function, and ordinal.

    This is a deliberately line-oriented heuristic over the curated
    DESKTOP_PROCESS_FILES set, not a full TypeScript parser. Comment-only lines
    are skipped; the existing check-security.mjs gate separately rejects shell
    string commands. A newly added call shifts ordinals, so the ledger comparison
    below fails until the callsite is reviewed and registered.
    """
    found: dict[str, int] = {}
    for rel in sorted(files or DESKTOP_PROCESS_FILES):
        # check-security.mjs only names child_process patterns inside its own
        # gate regexes; it imports fs/path and launches no real child process.
        if rel.endswith("check-security.mjs"):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        ordinal = 0
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "*", "/*")):
                continue
            for match in _JS_PROCESS_CALL_RE.finditer(line):
                ordinal += 1
                found[f"{rel}#{match.group(1)}:{ordinal}"] = line_no
    return found


def _source_files(roots: Iterable[Path], patterns: Iterable[str]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else [
            path for pattern in patterns for path in root.rglob(pattern)
        ]
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            if (
                "tests" in path.parts
                or "node_modules" in path.parts
                or "site-packages" in path.parts
                or path.name.endswith(".d.ts")
            ):
                continue
            yield path


def _dotted_name(
    node: ast.expr,
    modules: dict[str, str],
    functions: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Name):
        return functions.get(node.id, modules.get(node.id, node.id))
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value, modules, functions)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _python_primitives(
    repository_root: Path,
    roots: Iterable[Path],
    *,
    include_network: bool,
    include_file: bool,
) -> set[PrimitiveRef]:
    found: set[PrimitiveRef] = set()
    for path in _source_files(roots, ("*.py",)):
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

        relative = path.relative_to(repository_root).as_posix()
        for node in ast.walk(tree):
            if include_network and isinstance(node, ast.Attribute):
                name = _dotted_name(node, modules, functions)
                if name in _PYTHON_NETWORK_REFERENCES:
                    found.add(
                        PrimitiveRef(
                            "python-network",
                            relative,
                            _PYTHON_NETWORK_REFERENCES[name],
                        )
                    )
            if not isinstance(node, ast.Call):
                continue
            name = _dotted_name(node.func, modules, functions)
            if include_network:
                if name in _PYTHON_NETWORK_CALLS:
                    found.add(
                        PrimitiveRef("python-network", relative, _PYTHON_NETWORK_CALLS[name])
                    )
                elif name and name.endswith(".OutboundHttpClient"):
                    found.add(PrimitiveRef("python-network", relative, "OutboundHttpClient"))
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "ClientSession"
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "__import__"
                    and node.func.value.args
                    and isinstance(node.func.value.args[0], ast.Constant)
                    and node.func.value.args[0].value == "aiohttp"
                ):
                    found.add(
                        PrimitiveRef("python-network", relative, "aiohttp.ClientSession")
                    )
            if include_file:
                if isinstance(node.func, ast.Name) and node.func.id == "open":
                    found.add(PrimitiveRef("python-file", relative, "builtins.open"))
                elif name == "os.open":
                    found.add(PrimitiveRef("python-file", relative, "os.open"))
                elif name == "os.replace":
                    found.add(PrimitiveRef("python-file", relative, "os.replace"))
                elif isinstance(node.func, ast.Attribute) and node.func.attr in _PYTHON_FILE_METHODS:
                    found.add(
                        PrimitiveRef(
                            "python-file",
                            relative,
                            _PYTHON_FILE_METHODS[node.func.attr],
                        )
                    )
    return found


def _javascript_fs_aliases(text: str) -> tuple[set[str], dict[str, str]]:
    module_aliases: set[str] = set()
    function_aliases: dict[str, str] = {}
    import_pattern = re.compile(
        r"import\s+(?P<body>[\s\S]*?)\s+from\s+['\"](?:node:)?fs(?:/promises)?['\"]"
    )
    for match in import_pattern.finditer(text):
        body = match.group("body").strip()
        if body.startswith("{") and body.endswith("}"):
            for part in body[1:-1].split(","):
                pieces = part.strip().split()
                if not pieces:
                    continue
                original = pieces[0]
                alias = pieces[-1]
                if original in _FS_FUNCTIONS:
                    function_aliases[alias] = f"fs.{original}"
        else:
            alias_match = re.search(r"(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)$", body)
            if alias_match:
                module_aliases.add(alias_match.group(1))
    for match in re.finditer(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(['\"](?:node:)?fs['\"]\)",
        text,
    ):
        module_aliases.add(match.group(1))
    return module_aliases, function_aliases


def _javascript_primitives(
    repository_root: Path,
    roots: Iterable[Path],
    *,
    include_network: bool,
    include_file: bool,
    include_browser: bool,
    include_ipc: bool = False,
) -> set[PrimitiveRef]:
    found: set[PrimitiveRef] = set()
    patterns = ("*.ts", "*.tsx", "*.js", "*.mjs", "*.cjs")
    for path in _source_files(roots, patterns):
        text = path.read_text(encoding="utf-8-sig")
        relative = path.relative_to(repository_root).as_posix()
        if include_network:
            for primitive, pattern in _JAVASCRIPT_NETWORK_PATTERNS.items():
                if re.search(pattern, text):
                    found.add(PrimitiveRef("javascript-network", relative, primitive))
        if include_browser:
            for primitive, pattern in _JAVASCRIPT_BROWSER_PATTERNS.items():
                if re.search(pattern, text):
                    found.add(PrimitiveRef("javascript-browser", relative, primitive))
        if include_ipc:
            for primitive, pattern in _JAVASCRIPT_IPC_PATTERNS.items():
                if re.search(pattern, text):
                    found.add(PrimitiveRef("javascript-ipc", relative, primitive))
        if include_file:
            module_aliases, function_aliases = _javascript_fs_aliases(text)
            for alias in module_aliases:
                for function in _FS_FUNCTIONS:
                    if re.search(
                        rf"\b{re.escape(alias)}(?:\.promises)?\.{function}\s*\(",
                        text,
                    ):
                        found.add(PrimitiveRef("javascript-file", relative, f"fs.{function}"))
            for alias, primitive in function_aliases.items():
                if re.search(rf"\b{re.escape(alias)}\s*\(", text):
                    found.add(PrimitiveRef("javascript-file", relative, primitive))
    return found


def _rust_production_source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").split("#[cfg(test)]", 1)[0]


def _rust_process_files(repository_root: Path = ROOT) -> set[str]:
    found: set[str] = set()
    roots = (repository_root / "security-runtime" / "src",)
    for path in _source_files(roots, ("*.rs",)):
        text = _rust_production_source(path)
        if any(re.search(pattern, text) for pattern in _RUST_PROCESS_PATTERNS):
            found.add(path.relative_to(repository_root).as_posix())
    return found


def _rust_primitives(repository_root: Path, roots: Iterable[Path]) -> set[PrimitiveRef]:
    found: set[PrimitiveRef] = set()
    for path in _source_files(roots, ("*.rs",)):
        text = _rust_production_source(path)
        relative = path.relative_to(repository_root).as_posix()
        for primitive, pattern in _RUST_NETWORK_PATTERNS.items():
            if re.search(pattern, text):
                found.add(PrimitiveRef("rust-network", relative, primitive))
        for primitive, pattern in _RUST_FILE_PATTERNS.items():
            if re.search(pattern, text):
                found.add(PrimitiveRef("rust-file", relative, primitive))
    return found


def _gateway_routes(repository_root: Path = ROOT) -> dict[str, set[str]]:
    routes: dict[str, set[str]] = {}
    root = repository_root / "crew" / "gateway"
    for path in _source_files((root,), ("*.py",)):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        discovered: set[str] = set()
        for node in ast.walk(tree):
            decorators = (
                node.decorator_list
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else ()
            )
            for decorator in decorators:
                if (
                    not isinstance(decorator, ast.Call)
                    or not isinstance(decorator.func, ast.Attribute)
                    or decorator.func.attr
                    not in {
                        "api_route",
                        "delete",
                        "get",
                        "head",
                        "options",
                        "patch",
                        "post",
                        "put",
                        "trace",
                        "websocket",
                    }
                    or not decorator.args
                    or not isinstance(decorator.args[0], ast.Constant)
                    or not isinstance(decorator.args[0].value, str)
                ):
                    continue
                discovered.add(f"{decorator.func.attr.upper()} {decorator.args[0].value}")
        if discovered:
            routes[path.relative_to(repository_root).as_posix()] = discovered
    return routes


def _ipc_routes(path: Path = IPC_CHANNELS) -> set[str]:
    text = path.read_text(encoding="utf-8-sig")
    groups = {
        "IPC_INVOKE_CHANNELS": "invoke",
        "IPC_MAIN_TO_RENDERER_EVENT_CHANNELS": "main-event",
        "IPC_RENDERER_TO_MAIN_EVENT_CHANNELS": "renderer-event",
    }
    routes: set[str] = set()
    for constant, prefix in groups.items():
        match = re.search(
            rf"export\s+const\s+{constant}\s*=\s*\[(?P<body>[\s\S]*?)\]\s+as\s+const",
            text,
        )
        assert match, f"missing closed IPC channel registry: {constant}"
        routes.update(
            f"{prefix}:{channel}"
            for channel in re.findall(r"['\"]([^'\"]+)['\"]", match.group("body"))
        )
    return routes


def _ipc_handlers(path: Path = IPC_HANDLERS) -> set[str]:
    text = path.read_text(encoding="utf-8-sig")
    return set(re.findall(r"\btrustedHandle\(\s*['\"]([^'\"]+)['\"]", text))


def _registered_primitives(inventory: dict[str, Any]) -> set[PrimitiveRef]:
    return {
        PrimitiveRef(ref["kind"], ref["path"], ref["primitive"])
        for item in inventory["surfaces"]
        for ref in item["primitive_refs"]
    }


def _registered_routes(inventory: dict[str, Any], category: str) -> dict[str, set[str]]:
    registered: dict[str, set[str]] = {}
    for item in inventory["surfaces"]:
        if category not in item["categories"]:
            continue
        for reference in item["covered_routes"]:
            registered.setdefault(reference["path"], set()).add(reference["route"])
    return registered


def _schema_errors(inventory: dict[str, Any]) -> list[str]:
    schema = _load_json(INVENTORY_SCHEMA)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(inventory), key=lambda item: list(item.path))
    ]


def _expiry_errors(
    inventory: dict[str, Any], today: date | None = None
) -> list[str]:
    today = today or datetime.now(UTC).date()
    errors = []
    for item in inventory["surfaces"]:
        for field in ("review_deadline", "exception_expiry"):
            value = item.get(field)
            if value is not None and date.fromisoformat(value) < today:
                errors.append(f"{item['id']} {field} expired on {value}")
    return errors


def test_inventory_schema_references_and_expiry_are_strict() -> None:
    inventory = _inventory()
    assert _schema_errors(inventory) == []

    ids = [item["id"] for item in inventory["surfaces"]]
    assert len(ids) == len(set(ids)), "execution-surface IDs must be unique"
    locators = [
        (item["path"], item["symbol_or_route"]) for item in inventory["surfaces"]
    ]
    assert len(locators) == len(set(locators)), "execution-surface locators must be unique"

    primitive_refs = [
        PrimitiveRef(reference["kind"], reference["path"], reference["primitive"])
        for item in inventory["surfaces"]
        for reference in item["primitive_refs"]
    ]
    assert len(primitive_refs) == len(set(primitive_refs)), (
        "reviewed primitive references must have exactly one owning record"
    )
    route_refs = [
        (reference["path"], reference["route"])
        for item in inventory["surfaces"]
        for reference in item["covered_routes"]
    ]
    assert len(route_refs) == len(set(route_refs)), (
        "route and IPC references must have exactly one owning record"
    )

    for item in inventory["surfaces"]:
        assert (ROOT / item["path"]).exists(), f"{item['id']} has stale source path"
        for reference in [*item["tests"], *item["evidence"], *item["artifact_refs"]]:
            assert (ROOT / reference["path"]).exists(), (
                f"{item['id']} has stale test/evidence/artifact path: {reference['path']}"
            )
        for reference in [*item["primitive_refs"], *item["covered_routes"]]:
            assert (ROOT / reference["path"]).exists(), (
                f"{item['id']} has stale discovered path: {reference['path']}"
            )
    assert _expiry_errors(inventory) == []


def test_intentionally_missing_required_field_is_rejected() -> None:
    inventory = copy.deepcopy(_inventory())
    del inventory["surfaces"][0]["final_enforcement_point"]
    errors = _schema_errors(inventory)
    assert any("'final_enforcement_point' is a required property" in error for error in errors)


def test_artifact_refs_and_review_deadline_are_required() -> None:
    for field in ("artifact_refs", "review_deadline"):
        inventory = copy.deepcopy(_inventory())
        del inventory["surfaces"][0][field]
        errors = _schema_errors(inventory)
        assert any(f"'{field}' is a required property" in error for error in errors)


def test_review_deadline_must_be_an_iso_date() -> None:
    inventory = copy.deepcopy(_inventory())
    inventory["surfaces"][0]["review_deadline"] = "not-a-date"
    errors = _schema_errors(inventory)
    assert any("review_deadline" in error and "date" in error for error in errors)


def test_expired_review_or_exception_deadline_is_rejected() -> None:
    inventory = copy.deepcopy(_inventory())
    inventory["surfaces"][0]["review_deadline"] = "2000-01-01"
    assert _expiry_errors(inventory)

    inventory["surfaces"][0]["status"] = "tracked-exception"
    inventory["surfaces"][0]["exception_expiry"] = "2000-01-01"
    assert any("exception_expiry" in error for error in _expiry_errors(inventory))


def test_tracked_exception_requires_expiry() -> None:
    inventory = copy.deepcopy(_inventory())
    inventory["surfaces"][0]["status"] = "tracked-exception"
    inventory["surfaces"][0]["exception_expiry"] = None
    assert _schema_errors(inventory)


def test_all_process_capable_sources_are_classified() -> None:
    discovered = _python_process_files() | _javascript_process_files()
    direct_entries = _inventory_paths("process-direct") - _rust_process_files()
    indirect_entries = _inventory_paths("process-indirect")

    assert discovered == direct_entries, (
        f"process inventory drift; unregistered={sorted(discovered - direct_entries)}, "
        f"stale={sorted(direct_entries - discovered)}"
    )
    assert indirect_entries == INDIRECT_SURFACES, (
        f"indirect process inventory drift; unregistered={sorted(INDIRECT_SURFACES - indirect_entries)}, "
        f"stale={sorted(indirect_entries - INDIRECT_SURFACES)}"
    )
    assert INDIRECT_SURFACES.isdisjoint(discovered), (
        f"间接执行面重新引入了直接进程调用: {sorted(INDIRECT_SURFACES & discovered)}"
    )
    assert _desktop_process_files() == DESKTOP_PROCESS_FILES
    assert _inventory_paths("process-direct") & _rust_process_files() == _rust_process_files()


def test_electron_process_callsites_are_registered_with_metadata() -> None:
    discovered = _electron_process_callsites()
    ledger = json.loads(ELECTRON_PROCESS_LEDGER.read_text(encoding="utf-8"))
    assert ledger.get("schema") == "ace.electron-process-callsites.v1"
    entries = ledger.get("entries")
    assert isinstance(entries, list) and entries
    registered = {entry["key"]: entry for entry in entries}
    assert len(registered) == len(entries), "electron callsite keys must be unique"
    assert set(discovered) == set(registered), (
        "electron callsite drift; unregistered="
        f"{sorted(set(discovered) - set(registered))}, "
        f"stale={sorted(set(registered) - set(discovered))}"
    )
    for key, entry in registered.items():
        assert entry["file"] == key.split("#", 1)[0]
        for field in (
            "executable",
            "argv_kind",
            "cwd",
            "environment",
            "identity",
            "security_type",
            "owner",
            "test",
            "review_deadline",
        ):
            value = entry.get(field)
            assert isinstance(value, str) and value.strip(), f"{key} missing {field}"


def test_new_electron_process_callsite_is_discovered() -> None:
    with TemporaryDirectory(prefix=".inventory-electron-", dir=ROOT) as directory:
        root = Path(directory)
        source = root / "main.ts"
        source.write_text(
            "import { spawn } from 'child_process';\nspawn(executable, args);\n",
            encoding="utf-8",
        )
        discovered = _electron_process_callsites(
            [source.relative_to(ROOT).as_posix()]
        )
        assert discovered
        assert next(iter(discovered)).endswith("#spawn:1")


def test_reviewed_network_file_and_browser_primitives_are_exact() -> None:
    inventory = _inventory()
    discovered = set()
    discovered.update(
        _python_primitives(
            ROOT,
            PYTHON_NETWORK_ROOTS,
            include_network=True,
            include_file=False,
        )
    )
    discovered.update(
        _python_primitives(
            ROOT,
            PYTHON_FILE_ROOTS,
            include_network=False,
            include_file=True,
        )
    )
    discovered.update(
        _javascript_primitives(
            ROOT,
            JAVASCRIPT_PRIMITIVE_ROOTS,
            include_network=True,
            include_file=True,
            include_browser=False,
        )
    )
    discovered.update(
        _javascript_primitives(
            ROOT,
            JAVASCRIPT_BROWSER_ROOTS,
            include_network=False,
            include_file=False,
            include_browser=True,
            include_ipc=True,
        )
    )
    discovered.update(_rust_primitives(ROOT, RUST_ROOTS))
    registered = _registered_primitives(inventory)
    assert discovered == registered, (
        f"primitive inventory drift; unregistered={sorted(discovered - registered)}, "
        f"stale={sorted(registered - discovered)}"
    )


def test_new_network_primitive_is_discovered() -> None:
    with TemporaryDirectory(prefix=".inventory-network-", dir=ROOT) as directory:
        root = Path(directory)
        source = root / "network_client.py"
        source.write_text("import httpx\nclient = httpx.AsyncClient()\n", encoding="utf-8")
        new_ref = PrimitiveRef("python-network", "network_client.py", "httpx.AsyncClient")
        discovered = _python_primitives(
            root,
            (root,),
            include_network=True,
            include_file=False,
        )
        assert discovered - _registered_primitives(_inventory()) == {new_ref}


def test_new_structured_file_primitive_is_discovered() -> None:
    with TemporaryDirectory(prefix=".inventory-file-", dir=ROOT) as directory:
        root = Path(directory)
        source = root / "mutation.py"
        source.write_text(
            "from pathlib import Path\nPath('payload').write_text('hostile')\n",
            encoding="utf-8",
        )
        new_ref = PrimitiveRef("python-file", "mutation.py", "Path.write_text")
        discovered = _python_primitives(
            root,
            (root,),
            include_network=False,
            include_file=True,
        )
        assert discovered - _registered_primitives(_inventory()) == {new_ref}


def test_new_browser_primitive_is_discovered() -> None:
    with TemporaryDirectory(prefix=".inventory-browser-", dir=ROOT) as directory:
        root = Path(directory)
        source = root / "browser.ts"
        source.write_text(
            "await view.webContents.debugger.sendCommand('Runtime.evaluate', {});\n",
            encoding="utf-8",
        )
        new_ref = PrimitiveRef(
            "javascript-browser", "browser.ts", "debugger.sendCommand"
        )
        discovered = _javascript_primitives(
            root,
            (root,),
            include_network=False,
            include_file=False,
            include_browser=True,
        )
        assert discovered - _registered_primitives(_inventory()) == {new_ref}


def test_gateway_routes_and_ipc_channels_are_exactly_registered() -> None:
    inventory = _inventory()
    discovered_routes = _gateway_routes()
    registered_routes = _registered_routes(inventory, "gateway-route")
    assert discovered_routes == registered_routes, (
        f"Gateway route inventory drift; unregistered="
        f"{sorted(set(discovered_routes) - set(registered_routes))}, "
        f"stale={sorted(set(registered_routes) - set(discovered_routes))}"
    )

    invoke_routes = {route.removeprefix("invoke:") for route in _ipc_routes() if route.startswith("invoke:")}
    assert _ipc_handlers() == invoke_routes
    registered_ipc = _registered_routes(inventory, "gateway-ipc")
    expected_ipc_path = IPC_CHANNELS.relative_to(ROOT).as_posix()
    assert registered_ipc == {expected_ipc_path: _ipc_routes()}


def test_human_inventory_is_generated_from_machine_ledger() -> None:
    rendered = render_markdown(_inventory())
    assert "- Artifact references: " in rendered
    assert "- Review deadline: `" in rendered
    assert INVENTORY_SUMMARY.read_text(encoding="utf-8") == rendered
