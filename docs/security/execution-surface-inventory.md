# Ace execution-surface inventory

<!-- GENERATED FILE: edit docs/security/execution-surface-inventory.json, then run
     python scripts/generate_execution_surface_inventory.py -->

The JSON ledger is the canonical security migration and release-gate input. Registration does
not itself claim that a surface is sandboxed: `status`, the final enforcement point, and the
fail-closed behavior state the current boundary. Model output and remote content remain
untrusted even when a surface is listed.
Primitive references are bounded review aids: tests cover the current Python network/file,
JavaScript network/file/browser/IPC, and Rust network/file patterns; they do not claim
complete network or callsite discovery.

- Schema version: `1`
- Inventory ID: `ACE-EXECUTION-SURFACES`
- Surface records: `64`
- Category counts: `browser`=8, `cdp`=2, `cua`=3, `dev-only`=8, `download`=13, `gateway-ipc`=6, `gateway-route`=1, `installer`=6, `mcp`=5, `network`=10, `plugin`=6, `process-direct`=37, `process-indirect`=12, `runtime-boundary`=14, `skill`=15, `structured-file-read`=13, `structured-file-write`=14, `updater`=6, `upload`=7

| ID | Categories | Locator | Owner | Status | Final enforcement |
|---|---|---|---|---|---|
| `ACE-SURF-BROWSER-HOST` | `browser`, `cdp`, `upload`, `download` | `desktop/src/main/browser-host.ts::BrowserHost reviewed Electron and CDP wrappers` | Browser Security maintainers | `enforced` | desktop/src/main/browser-host.ts permission, navigation, download, upload, and CDP dispatch guards |
| `ACE-SURF-CUA-SETUP` | `cua`, `installer`, `download`, `mcp` | `crew/tools/cua_setup.py::setup_cua_driver and daemon lifecycle` | CUA integration maintainers | `host-fixed` | crew/tools/cua_setup.py digest verification, restricted environment, install transaction, and daemon registry |
| `ACE-SURF-FILE-DESKTOP-WRAPPERS` | `structured-file-read`, `structured-file-write`, `upload`, `download`, `gateway-ipc` | `desktop/src/main/index.ts::reviewed Node filesystem calls behind trustedHandle and private host services` | Desktop Host maintainers | `enforced` | Desktop trustedHandle schemas, crew-file protocol identity checks, BrowserHost staging, and update/install file verifiers |
| `ACE-SURF-FILE-PYTHON-WRAPPERS` | `structured-file-read`, `structured-file-write`, `upload`, `download`, `plugin`, `skill` | `crew/tools/file_utils.py::identity-checked Python file wrappers and exact reviewed callers` | File Capability maintainers | `enforced` | crew/tools/file_utils.py verified descriptor reads and pinned-parent atomic replacement, with owner-private boundaries in Browser, Plugin, and Wiki callers |
| `ACE-SURF-FILE-RUNTIME-WRAPPERS` | `structured-file-read`, `structured-file-write`, `runtime-boundary` | `security-runtime/src::reviewed Rust runtime file and state primitives` | Native Runtime maintainers | `enforced` | security-runtime platform modules and protected state implementations |
| `ACE-SURF-GATEWAY-IPC` | `gateway-ipc`, `browser`, `upload`, `download`, `updater`, `installer` | `desktop/src/shared/ipc-channels.ts::closed renderer-to-main invoke and event channel registries` | Desktop IPC maintainers | `enforced` | desktop/src/main/index.ts::trustedHandle with desktop/src/shared/ipc-schemas.ts parsers |
| `ACE-SURF-GATEWAY-ROUTES` | `gateway-route`, `browser`, `upload`, `download`, `plugin`, `skill`, `cua`, `mcp` | `crew/gateway::all literal FastAPI HTTP and WebSocket decorators` | Gateway Security maintainers | `enforced` | crew/gateway/app.py authentication middleware plus route-local owner, role, capability, quota, and protocol guards; crew/gateway/context.py enforces cross-process owner upload store and request attachment-byte quotas; crew/gateway/app.py durably audits authorization denials |
| `ACE-SURF-NETWORK-DESKTOP-WRAPPERS` | `network`, `gateway-ipc`, `browser`, `updater`, `download` | `desktop/src/main/index.ts::reviewed fetch, WebSocket, and HTTPS request boundaries` | Desktop Network maintainers | `enforced` | Desktop URL/origin allowlists, Gateway instance authentication, update URL policy, and BrowserHost proxy requirements |
| `ACE-SURF-NETWORK-PLATFORM-PLUGINS` | `network`, `plugin` | `plugins/platforms::Feishu and Weixin fixed platform HTTP and long-poll clients` | Platform Integration maintainers | `host-fixed` | Bundled platform adapter endpoint construction, TLS connector, bounded request timeout, response validation, and Gateway owner lifecycle |
| `ACE-SURF-NETWORK-PYTHON-WRAPPERS` | `network`, `mcp`, `plugin`, `skill`, `cua`, `download` | `crew/security/outbound.py::OutboundHttpClient, policy adapters, provider clients, and exact reviewed socket users` | Network Policy maintainers | `enforced` | crew/security/outbound.py connection plans plus caller-specific origin, redirect, proxy, and transfer enforcement |
| `ACE-SURF-NETWORK-RUNTIME-WRAPPERS` | `network`, `runtime-boundary` | `security-runtime/src/network::native pinned connector and managed proxy listeners` | Native Network Runtime maintainers | `enforced` | security-runtime/src/network/connector.rs and platform proxy routing modules |
| `ACE-SURF-NETWORK-WEB-CLIENT` | `network`, `gateway-ipc` | `web/src::same-origin Gateway REST and WebSocket client` | Web Gateway Client maintainers | `enforced` | Gateway HTTP/WebSocket authentication, strict route schemas, owner binding, sequence and replay checks, and server-side capability enforcement |
| `ACE-SURF-PLUGIN-EXECUTION` | `plugin`, `skill`, `structured-file-read`, `structured-file-write`, `download` | `crew/plugins/manager.py::plugin discovery, install, capability registration, enable, disable, and uninstall` | Plugin Runtime maintainers | `host-fixed` | crew/plugins/discovery.py and crew/plugins/security.py provenance checks before crew/plugins/manager.py registration |
| `ACE-SURF-PROC-DIRECT-DESKTOP-CHECK` | `process-direct`, `dev-only` | `desktop/scripts/check-security.mjs::build-time Electron security configuration checks` | Release Security maintainers | `dev-only` | desktop/scripts/check-security.mjs fixed checks and release workflow |
| `ACE-SURF-PROC-DIRECT-DESKTOP-GATEWAY-AUTH` | `process-direct`, `gateway-ipc` | `desktop/src/main/gateway-instance-auth.ts::Windows Gateway instance-key ACL creation and verification helper` | Desktop Identity maintainers | `host-fixed` | gateway-instance-auth.ts fixed absolute PowerShell argv and post-create protected DACL verification |
| `ACE-SURF-PROC-DIRECT-DESKTOP-MAIN` | `process-direct`, `gateway-ipc`, `updater`, `browser` | `desktop/src/main/index.ts::Gateway spawn/restart, Desktop lifecycle, Browser bridge, and update orchestration` | Desktop Host maintainers | `host-fixed` | Desktop trustedHandle, process environment allowlist, Gateway instance proof, BrowserHost, and update installer boundary |
| `ACE-SURF-PROC-DIRECT-DESKTOP-OPEN-WITH` | `process-direct`, `structured-file-read` | `desktop/src/main/open-with-service.ts::user-directed registered-application discovery and open` | Desktop Host maintainers | `host-fixed` | open-with-service.ts application allowlist, argv separation, bounded discovery, and tree cleanup |
| `ACE-SURF-PROC-DIRECT-DESKTOP-PLAYWRIGHT-PROBE` | `process-direct`, `dev-only`, `browser` | `desktop/scripts/resolve-playwright-candidates.mjs::build/test Playwright browser candidate probe` | Desktop Browser build maintainers | `dev-only` | resolve-playwright-candidates.mjs fixed executable probes and bounded output |
| `ACE-SURF-PROC-DIRECT-DESKTOP-PW-CONTRACT` | `process-direct`, `dev-only` | `desktop/scripts/pw-contract.ts::deferred Electron contract profile cleanup helper` | Desktop Browser build maintainers | `dev-only` | pw-contract.ts deferTempRootCleanup fixed detached cleanup command |
| `ACE-SURF-PROC-DIRECT-DESKTOP-SECURITY-SETUP` | `process-direct`, `installer` | `desktop/src/main/security-setup.ts::Windows one-time elevated native runtime security setup` | Desktop Installer maintainers | `host-fixed` | security-setup.ts fixed absolute runtime argv and encoded RunAs PowerShell request |
| `ACE-SURF-PROC-DIRECT-DESKTOP-UNINSTALL` | `process-direct`, `installer`, `structured-file-write` | `desktop/src/main/uninstall.ts::signed product uninstall cleanup` | Desktop Installer maintainers | `host-fixed` | uninstall.ts fixed cleanup scripts, product-root validation, and OS-specific uninstall lifecycle |
| `ACE-SURF-PROC-DIRECT-DESKTOP-UPDATE-INSTALLER` | `process-direct`, `updater`, `installer` | `desktop/src/main/update/update-installer.ts::verified package installer launch` | Desktop Update maintainers | `host-fixed` | update-installer.ts descriptor revalidation and fixed platform installer helper or verified Windows EXE launch |
| `ACE-SURF-PROC-DIRECT-DEV-NPM-AUDIT` | `process-direct`, `dev-only` | `scripts/audit_runtime_npm.py::release-time production npm dependency audit` | Release Security maintainers | `dev-only` | audit_runtime_npm.py lockfile enumeration, bounded fixed subprocess, and release workflow |
| `ACE-SURF-PROC-DIRECT-DEV-PARSER-FUZZ` | `process-direct`, `dev-only` | `scripts/run_parser_fuzz_gate.py::bounded Python, TypeScript, and Rust parser fuzz gate` | Release Security maintainers | `dev-only` | run_parser_fuzz_gate.py corpus budgets, fixed commands, per-command timeouts, and ignored reproduction output root |
| `ACE-SURF-PROC-DIRECT-DEV-READINESS` | `process-direct`, `dev-only` | `scripts/check_release_readiness.py::commit-bound release readiness check` | Release Security maintainers | `dev-only` | check_release_readiness.py commit, cleanliness, evidence, and fixed git checks |
| `ACE-SURF-PROC-DIRECT-DEV-RUNTIME-EVIDENCE` | `process-direct`, `dev-only`, `structured-file-write` | `scripts/write_security_runtime_evidence.py::commit- and runner-bound native runtime evidence writer` | Release Security maintainers | `dev-only` | write_security_runtime_evidence.py fixed git checks and schema-bound evidence output |
| `ACE-SURF-PROC-DIRECT-DEV-SBOM` | `process-direct`, `dev-only` | `scripts/generate_security_sbom.py::deterministic tracked-lockfile SBOM generation` | Release Security maintainers | `dev-only` | generate_security_sbom.py tracked-file filter, fixed git argv, bounded timeout, and deterministic CycloneDX writer |
| `ACE-SURF-PROC-DIRECT-EXTERNAL-LIFECYCLE` | `process-direct`, `runtime-boundary` | `crew/agent/external/process_lifecycle.py::validated external process launch, probe, wait, cancel, and tree cleanup` | External Agent maintainers | `enforced` | crew/agent/external/process_lifecycle.py launch validation and OS process-tree lifecycle |
| `ACE-SURF-PROC-DIRECT-FILE-TOOLS` | `process-direct`, `structured-file-read`, `structured-file-write` | `crew/tools/file_tools.py::file_read, file_write, glob, grep, and fixed disabled-mode ripgrep` | File Capability maintainers | `enforced` | crew/tools/security_guard.py authorization plus crew/tools/file_utils.py identity-checked I/O; fixed ripgrep is disabled in managed mode |
| `ACE-SURF-PROC-DIRECT-LAUNCH` | `process-direct`, `runtime-boundary` | `crew/security/launch.py::execute_captured and launch routing` | Execution Broker maintainers | `enforced` | crew/security/launch.py mode routing and crew/security/runtime_client.py native helper protocol |
| `ACE-SURF-PROC-DIRECT-PROCESS-REGISTRY` | `process-direct`, `runtime-boundary`, `structured-file-write` | `crew/tools/process_registry.py::background process spawn, bridge, checkpoint, recovery, and cleanup` | Execution Broker maintainers | `enforced` | crew/tools/process_registry.py disabled/managed routing, bridge protocol, identity checks, and process-tree cleanup |
| `ACE-SURF-PROC-DIRECT-RUNTIME-CLIENT` | `process-direct`, `runtime-boundary` | `crew/security/runtime_client.py::NativeRuntimeClient helper spawn and authenticated protocol` | Execution Broker maintainers | `enforced` | crew/security/runtime_client.py helper identity verification and versioned nonce-bound stdin/stdout protocol |
| `ACE-SURF-PROC-DIRECT-RUST-BWRAP-SOURCE` | `process-direct`, `runtime-boundary` | `security-runtime/src/linux/bwrap_source.rs::bubblewrap source and identity probe` | Linux Native Runtime maintainers | `enforced` | bwrap_source.rs candidate validation before Linux sandbox plan construction |
| `ACE-SURF-PROC-DIRECT-RUST-LINUX` | `process-direct`, `runtime-boundary` | `security-runtime/src/linux/mod.rs::Linux sandbox command launch` | Linux Native Runtime maintainers | `enforced` | linux/mod.rs fixed sandbox invocation and lifecycle limits |
| `ACE-SURF-PROC-DIRECT-RUST-LINUX-PROXY` | `process-direct`, `runtime-boundary`, `network` | `security-runtime/src/linux/proxy_routing.rs::Linux proxy namespace bridge and helper fork` | Linux Native Runtime maintainers | `enforced` | proxy_routing.rs fixed namespace bridge, descriptor ownership, and child lifecycle |
| `ACE-SURF-PROC-DIRECT-RUST-MACOS` | `process-direct`, `runtime-boundary`, `network` | `security-runtime/src/macos/mod.rs::macOS sandbox-exec launch and readiness probe` | macOS Native Runtime maintainers | `enforced` | macos/mod.rs fixed sandbox-exec invocation, profile application, readiness probe, and process-group lifecycle |
| `ACE-SURF-PROC-DIRECT-RUST-SECCOMP` | `process-direct`, `runtime-boundary` | `security-runtime/src/linux/seccomp.rs::Linux seccomp launcher exec` | Linux Native Runtime maintainers | `enforced` | linux/seccomp.rs policy load and exec replacement |
| `ACE-SURF-PROC-DIRECT-RUST-SHELL` | `process-direct`, `runtime-boundary` | `security-runtime/src/shell.rs::fixed shell discovery and classification probes` | Native Runtime maintainers | `enforced` | shell.rs absolute candidate validation, fixed argv, minimal environment, timeout, and output limits |
| `ACE-SURF-PROC-DIRECT-RUST-WINDOWS` | `process-direct`, `runtime-boundary` | `security-runtime/src/windows/process.rs::Windows restricted-token process creation` | Windows Native Runtime maintainers | `enforced` | windows/process.rs CreateProcessAsUserW boundary with handle list, environment block, private Desktop ACL, suspended assignment, and Job object; windows/users.rs hides generated accounts in Winlogon UserList |
| `ACE-SURF-PROC-DIRECT-SECURITY-LIFECYCLE` | `process-direct`, `runtime-boundary` | `crew/security/process_lifecycle.py::captured execution process-tree cleanup` | Execution Broker maintainers | `enforced` | crew/security/process_lifecycle.py process-group or verified System32 taskkill cleanup |
| `ACE-SURF-PROC-DIRECT-SKILL-DOCX-REDLINING` | `process-direct`, `skill` | `crew/skills/docx/scripts/office/validators/redlining.py::DOCX redlining validator child tools` | Document Skill maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed validator argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-EVAL-VIEWER` | `process-direct`, `skill` | `crew/skills/skill-creator/eval-viewer/generate_review.py::skill evaluation review generator child processes` | Skill Creator maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed evaluation argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-HTML-PDF` | `process-direct`, `skill`, `browser`, `cdp`, `structured-file-read`, `structured-file-write` | `crew/skills/html-to-pdf/scripts/convert.cjs::sandboxed Chromium HTML-to-PDF converter` | Document Skill maintainers | `sandbox-descendant` | convert.cjs Chromium sandbox verification, request/navigation denial, resource budgets, private profile, and atomic publication |
| `ACE-SURF-PROC-DIRECT-SKILL-IMPROVE` | `process-direct`, `skill` | `crew/skills/skill-creator/scripts/improve_description.py::skill-description improvement evaluator` | Skill Creator maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed evaluator argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-MARKDOWN-PDF` | `process-direct`, `skill` | `crew/skills/md-to-pdf/scripts/md2pdf.py::Markdown-to-PDF child converter` | Document Skill maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed converter argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-PDF-CONVERTER` | `process-direct`, `skill` | `crew/skills/pdf/scripts/md2pdf/md2pdf_convert.py::PDF skill Markdown conversion child` | Document Skill maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed converter argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-RUN-EVAL` | `process-direct`, `skill` | `crew/skills/skill-creator/scripts/run_eval.py::skill evaluation case runner` | Skill Creator maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus bounded evaluator argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-XLSX-RECALC` | `process-direct`, `skill` | `crew/skills/xlsx/scripts/recalc.py::spreadsheet recalculation child process` | Spreadsheet Skill maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed recalculation argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-XLSX-REDLINING` | `process-direct`, `skill` | `crew/skills/xlsx/scripts/office/validators/redlining.py::spreadsheet redlining validator child tools` | Spreadsheet Skill maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox plus fixed validator argv and sandbox filesystem roots |
| `ACE-SURF-PROC-DIRECT-SKILL-XLSX-SOFFICE` | `process-direct`, `skill` | `crew/skills/xlsx/scripts/office/soffice.py::LibreOffice lifecycle and local UNO bridge` | Spreadsheet Skill maintainers | `sandbox-descendant` | Parent ProcessLaunch native sandbox, private profile, fixed LibreOffice argv, and local descendant bridge |
| `ACE-SURF-PROC-INDIRECT-ACP` | `process-indirect` | `crew/agent/external/acp_adapter.py::ACP bidirectional stdio adapter` | External Agent maintainers | `fail-closed-unavailable` | crew/agent/external/process_lifecycle.py launch validation and snapshot consumption |
| `ACE-SURF-PROC-INDIRECT-BROWSER` | `process-indirect`, `browser`, `network`, `upload`, `download`, `structured-file-read`, `structured-file-write` | `crew/browser/manager.py::model-reachable BrowserManager commands` | Browser Security maintainers | `enforced` | BrowserManager authorization and owner binding plus Desktop BrowserHost policy proxy and file staging |
| `ACE-SURF-PROC-INDIRECT-CLI` | `process-indirect` | `crew/agent/external/cli_adapter.py::external CLI conversation and stdio adapter` | External Agent maintainers | `fail-closed-unavailable` | crew/agent/external/process_lifecycle.py launch snapshot and bounded probe authority |
| `ACE-SURF-PROC-INDIRECT-CODEX` | `process-indirect` | `crew/agent/external/codex_adapter.py::Codex app-server bidirectional stdio adapter` | External Agent maintainers | `fail-closed-unavailable` | crew/agent/external/process_lifecycle.py launch validation and snapshot consumption |
| `ACE-SURF-PROC-INDIRECT-CRON` | `process-indirect` | `crew/cron/scheduler.py::scheduled agent and tool trigger` | Cron Runtime maintainers | `enforced` | Invoked agent/tool SecurityService and ProcessLaunch boundary |
| `ACE-SURF-PROC-INDIRECT-DETECTOR` | `process-indirect` | `crew/agent/external/detector.py::external runtime candidate discovery and fixed probes` | External Agent maintainers | `host-fixed` | crew/agent/external/process_lifecycle.py bounded control-plane probe |
| `ACE-SURF-PROC-INDIRECT-MANAGED-TOOLS` | `process-indirect`, `download`, `updater` | `crew/tools/managed_tools.py::ripgrep acquisition, verification, extraction, and fixed version probe` | Managed Tool maintainers | `host-fixed` | crew/tools/managed_tools.py digest, archive, path, atomic install, and fixed argv checks |
| `ACE-SURF-PROC-INDIRECT-MCP` | `process-indirect`, `mcp`, `network` | `crew/tools/mcp_client.py::outbound MCP stdio, HTTP, and SSE connection manager` | MCP Runtime maintainers | `enforced` | crew/tools/mcp_client.py transport gate, command integrity, minimal environment, outbound policy, queue, and lifecycle manager |
| `ACE-SURF-PROC-INDIRECT-SITES` | `process-indirect`, `structured-file-read`, `structured-file-write` | `crew/sites/manager.py::site build, preview, publish, export, and automation commands` | Sites Runtime maintainers | `enforced` | crew/sites/manager.py managed ProcessLaunch and owner-scoped site roots |
| `ACE-SURF-PROC-INDIRECT-TEAM` | `process-indirect`, `mcp` | `crew/team/team_manager.py::teammate task and shared tool dispatch` | Team Runtime maintainers | `enforced` | crew/team/workspace_guard.py plus each invoked tool security boundary |
| `ACE-SURF-PROC-INDIRECT-TERMINAL` | `process-indirect`, `structured-file-read`, `structured-file-write` | `crew/tools/builtin.py::terminal foreground/background and structured patch entrypoints` | Execution Broker maintainers | `enforced` | crew/security/launch.py and crew/tools/process_registry.py ProcessLaunch routing; file mutations use crew/tools/file_utils.py |
| `ACE-SURF-PROC-INDIRECT-WIKI` | `process-indirect`, `structured-file-read`, `structured-file-write`, `upload`, `download` | `crew/wiki/parser.py::Wiki ingest parser and delegated legacy Office conversion` | Wiki Security maintainers | `enforced` | Wiki upload/capture boundaries, archive budgets, parser limits, and brokered LibreOffice conversion |
| `ACE-SURF-SKILL-ACTIVATION` | `skill`, `plugin`, `structured-file-read` | `crew/agent/skills.py::skill discovery and activation into managed tool execution` | Skill Runtime maintainers | `sandbox-descendant` | crew/agent/skills.py path resolution and crew/tools/builtin.py ProcessLaunch boundary |
| `ACE-SURF-UPDATE-PIPELINE` | `updater`, `installer`, `network`, `download`, `structured-file-read`, `structured-file-write` | `desktop/src/main/update::signed update discovery, bounded download, file verification, and installer handoff` | Desktop Update maintainers | `enforced` | desktop/src/main/update/download-controller.ts, update-integrity.ts, update-file-security.ts, and update-installer.ts |

## Control details

### ACE-SURF-BROWSER-HOST

- Locator: `desktop/src/main/browser-host.ts::BrowserHost reviewed Electron and CDP wrappers`
- Trust source: Authenticated owner/task-bound Gateway commands and an isolated Electron BrowserView session
- Fail closed: Unknown CDP methods, missing owner or task bindings, unsafe navigation, denied permissions, and invalid artifact paths are rejected
- Lifecycle/revocation owner: BrowserManager owner/session lifecycle and Desktop BrowserHost cleanup
- Tests: `BROWSER-HOST-BOUNDARY` — `desktop/tests/unit/browser-host.test.ts`
- Evidence: `node-test` — `desktop/tests/unit/browser-host.test.ts`
- Artifact references: `node-test` — `desktop/tests/unit/browser-host.test.ts`
- Reviewed primitive references: `javascript-browser:desktop/src/main/browser-host.ts:debugger.sendCommand`, `javascript-browser:desktop/src/main/browser-host.ts:executeJavaScript`, `javascript-browser:desktop/src/main/browser-host.ts:setPermissionCheckHandler`, `javascript-browser:desktop/src/main/browser-host.ts:setPermissionRequestHandler`, `javascript-browser:desktop/src/main/browser-host.ts:setWindowOpenHandler`, `javascript-browser:desktop/src/main/browser-host.ts:will-download`, `javascript-browser:desktop/src/main/browser-host.ts:will-navigate`, `javascript-browser:desktop/src/main/browser/electron-cdp-transport.ts:debugger.sendCommand`, `javascript-browser:desktop/src/main/host-authority-dialog.ts:setWindowOpenHandler`, `javascript-browser:desktop/src/main/host-authority-dialog.ts:will-navigate`, `javascript-browser:desktop/src/main/index.ts:setWindowOpenHandler`, `javascript-browser:desktop/src/main/index.ts:will-navigate`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Electron session hook, CDP command wrapper, upload source, download sink, or BrowserView navigation path

### ACE-SURF-CUA-SETUP

- Locator: `crew/tools/cua_setup.py::setup_cua_driver and daemon lifecycle`
- Trust source: Authenticated administrator action with Desktop instance proof and pinned installer metadata
- Fail closed: Missing admin authority, unsupported platform, digest mismatch, download failure, or rollback failure leaves the CUA driver unavailable
- Lifecycle/revocation owner: Gateway CUA setup task registry and CUA daemon lifecycle
- Tests: `CUA-AUTH-BOUNDARY` — `tests/gateway/test_cua_setup_authorization.py`<br>`CUA-LIFECYCLE` — `tests/test_cua_setup.py`
- Evidence: `pytest` — `tests/gateway/test_cua_setup_authorization.py`
- Artifact references: `pytest` — `tests/gateway/test_cua_setup_authorization.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Installer URL, digest, daemon argv, platform launcher, MCP registration, or setup route changes

### ACE-SURF-FILE-DESKTOP-WRAPPERS

- Locator: `desktop/src/main/index.ts::reviewed Node filesystem calls behind trustedHandle and private host services`
- Trust source: Trusted sender validation, strict IPC schemas, user-selected paths, signed product artifacts, and owner-private Browser state
- Fail closed: Untrusted senders, malformed paths, non-owned files, links, oversized payloads, identity drift, and failed atomic writes are rejected
- Lifecycle/revocation owner: Desktop window/session lifecycle and owner-scoped Gateway or Browser cleanup
- Tests: `ELECTRON-IPC-FILE` — `tests/security/test_electron_ipc_contract.py`<br>`DESKTOP-IPC-SCHEMA` — `desktop/tests/unit/ipc-schemas.test.ts`
- Evidence: `node-test` — `desktop/tests/unit/ipc-schemas.test.ts`
- Artifact references: `node-test` — `desktop/tests/unit/ipc-schemas.test.ts`
- Reviewed primitive references: `javascript-file:desktop/src/main/app-version.ts:fs.readFileSync`, `javascript-file:desktop/src/main/browser-host.ts:fs.copyFile`, `javascript-file:desktop/src/main/browser-host.ts:fs.open`, `javascript-file:desktop/src/main/browser-host.ts:fs.rename`, `javascript-file:desktop/src/main/crew-file-protocol.ts:fs.open`, `javascript-file:desktop/src/main/crew-session-file.ts:fs.readFileSync`, `javascript-file:desktop/src/main/desktop-prefs.ts:fs.readFileSync`, `javascript-file:desktop/src/main/desktop-prefs.ts:fs.writeFileSync`, `javascript-file:desktop/src/main/index.ts:fs.copyFile`, `javascript-file:desktop/src/main/index.ts:fs.createWriteStream`, `javascript-file:desktop/src/main/index.ts:fs.open`, `javascript-file:desktop/src/main/index.ts:fs.readFile`, `javascript-file:desktop/src/main/index.ts:fs.readFileSync`, `javascript-file:desktop/src/main/open-with-service.ts:fs.readFile`, `javascript-file:desktop/src/main/selected-file-authority.ts:fs.open`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Node fs primitive, renderer path argument, upload staging path, artifact publication, or product-state file

### ACE-SURF-FILE-PYTHON-WRAPPERS

- Locator: `crew/tools/file_utils.py::identity-checked Python file wrappers and exact reviewed callers`
- Trust source: Canonical owner/workspace capability, pinned parent identity, owned snapshots, or trusted product lifecycle input
- Fail closed: Traversal, link or reparse objects, identity changes, oversized or special files, unauthorized roots, and failed atomic publication are rejected
- Lifecycle/revocation owner: SecurityService capability lifecycle plus owning Browser, Plugin, Tool, or Wiki session
- Tests: `FILE-POLICY` — `tests/security/test_file_policy.py`<br>`FILE-RACES` — `tests/security/test_file_races.py`
- Evidence: `pytest` — `tests/security/test_file_races.py`
- Artifact references: `pytest` — `tests/security/test_file_races.py`
- Reviewed primitive references: `python-file:crew/browser/manager.py:Path.open`, `python-file:crew/browser/manager.py:os.open`, `python-file:crew/plugins/manager.py:Path.open`, `python-file:crew/plugins/manager.py:Path.write_text`, `python-file:crew/plugins/security.py:Path.open`, `python-file:crew/plugins/security.py:Path.read_bytes`, `python-file:crew/plugins/security.py:Path.read_text`, `python-file:crew/tools/cua_setup.py:Path.write_bytes`, `python-file:crew/tools/cua_setup.py:Path.write_text`, `python-file:crew/tools/file_utils.py:os.open`, `python-file:crew/tools/file_utils.py:os.replace`, `python-file:crew/tools/managed_tools.py:Path.open`, `python-file:crew/tools/pipeline.py:Path.write_text`, `python-file:crew/tools/process_registry.py:os.open`, `python-file:crew/tools/skills_tools.py:Path.read_text`, `python-file:crew/wiki/archive_security.py:Path.open`, `python-file:crew/wiki/compiler.py:Path.read_text`, `python-file:crew/wiki/compiler.py:Path.write_text`, `python-file:crew/wiki/manager.py:Path.read_text`, `python-file:crew/wiki/manager.py:Path.write_text`, `python-file:crew/wiki/parser.py:Path.open`, `python-file:crew/wiki/prompts.py:Path.read_text`, `python-file:crew/wiki/seed.py:Path.open`, `python-file:crew/wiki/seed.py:Path.read_text`, `python-file:crew/wiki/seed.py:Path.write_text`, `python-file:crew/wiki/store/_filesystem.py:Path.open`, `python-file:crew/wiki/store/_filesystem.py:Path.read_bytes`, `python-file:crew/wiki/store/_filesystem.py:Path.read_text`, `python-file:crew/wiki/store/_filesystem.py:Path.write_text`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Python open/read/write primitive under crew/browser, crew/plugins, crew/tools, or crew/wiki

### ACE-SURF-FILE-RUNTIME-WRAPPERS

- Locator: `security-runtime/src::reviewed Rust runtime file and state primitives`
- Trust source: Signed authorization protocol, fixed runtime configuration, protected state roots, and platform-reported identities
- Fail closed: Unexpected object types, unsafe ownership or permissions, identity drift, state corruption, and unsupported platform operations abort execution
- Lifecycle/revocation owner: Native runtime session and parent broker process
- Tests: `RUNTIME-FILE-BOUNDARY` — `tests/security/test_native_runtime_contract.py`
- Evidence: `cargo-test` — `security-runtime/tests/protocol.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/protocol.rs`
- Reviewed primitive references: `rust-file:security-runtime/src/linux/bwrap_source.rs:File.open`, `rust-file:security-runtime/src/linux/proxy_routing.rs:fs.mutate`, `rust-file:security-runtime/src/linux/proxy_routing.rs:fs.read`, `rust-file:security-runtime/src/linux/seccomp.rs:fs.read`, `rust-file:security-runtime/src/linux/wsl.rs:fs.read`, `rust-file:security-runtime/src/macos/mod.rs:fs.mutate`, `rust-file:security-runtime/src/windows/acl.rs:fs.mutate`, `rust-file:security-runtime/src/windows/identity.rs:fs.mutate`, `rust-file:security-runtime/src/windows/path.rs:File.open`, `rust-file:security-runtime/src/windows/process.rs:File.open`, `rust-file:security-runtime/src/windows/readiness.rs:fs.mutate`, `rust-file:security-runtime/src/windows/state.rs:File.open`, `rust-file:security-runtime/src/windows/state.rs:OpenOptions`, `rust-file:security-runtime/src/windows/state.rs:fs.mutate`, `rust-file:security-runtime/src/windows/state.rs:fs.read`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Rust fs, File, or OpenOptions primitive in security-runtime/src

### ACE-SURF-GATEWAY-IPC

- Locator: `desktop/src/shared/ipc-channels.ts::closed renderer-to-main invoke and event channel registries`
- Trust source: Sandboxed context-isolated preload and trusted sender/origin validation
- Fail closed: Unknown channels, untrusted senders, invalid schemas, oversized payloads, and missing Gateway or Browser identity proof are rejected before dispatch
- Lifecycle/revocation owner: Desktop window lifecycle, Gateway socket owner, Browser session owner, and update controller
- Tests: `ELECTRON-IPC-CONTRACT` — `tests/security/test_electron_ipc_contract.py`<br>`IPC-SCHEMAS` — `desktop/tests/unit/ipc-schemas.test.ts`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: `javascript-ipc:desktop/src/main/index.ts:ipcMain.handle.bind`, `javascript-ipc:desktop/src/main/index.ts:ipcMain.on.bind`
- Covered routes/channels: `desktop/src/shared/ipc-channels.ts:invoke:app:get-auto-launch-enabled`, `desktop/src/shared/ipc-channels.ts:invoke:app:get-close-behavior`, `desktop/src/shared/ipc-channels.ts:invoke:app:get-system-locale`, `desktop/src/shared/ipc-channels.ts:invoke:app:get-version`, `desktop/src/shared/ipc-channels.ts:invoke:app:quit`, `desktop/src/shared/ipc-channels.ts:invoke:app:renderer-initial-state-ready`, `desktop/src/shared/ipc-channels.ts:invoke:app:set-auto-launch-enabled`, `desktop/src/shared/ipc-channels.ts:invoke:app:set-close-behavior`, `desktop/src/shared/ipc-channels.ts:invoke:auth:get-state`, `desktop/src/shared/ipc-channels.ts:invoke:auth:heartbeat`, `desktop/src/shared/ipc-channels.ts:invoke:auth:login`, `desktop/src/shared/ipc-channels.ts:invoke:auth:logout`, `desktop/src/shared/ipc-channels.ts:invoke:auth:send-code`, `desktop/src/shared/ipc-channels.ts:invoke:browser-view:get-navigation`, `desktop/src/shared/ipc-channels.ts:invoke:browser-view:hide`, `desktop/src/shared/ipc-channels.ts:invoke:browser-view:set-panel`, `desktop/src/shared/ipc-channels.ts:invoke:browser-ws:close`, `desktop/src/shared/ipc-channels.ts:invoke:browser-ws:connect`, `desktop/src/shared/ipc-channels.ts:invoke:clipboard:writeImage`, `desktop/src/shared/ipc-channels.ts:invoke:dialog:saveLocalExport`, `desktop/src/shared/ipc-channels.ts:invoke:dialog:selectFile`, `desktop/src/shared/ipc-channels.ts:invoke:dialog:selectFolder`, `desktop/src/shared/ipc-channels.ts:invoke:feedback:cancel`, `desktop/src/shared/ipc-channels.ts:invoke:feedback:image`, `desktop/src/shared/ipc-channels.ts:invoke:feedback:list`, `desktop/src/shared/ipc-channels.ts:invoke:feedback:preview`, `desktop/src/shared/ipc-channels.ts:invoke:feedback:submit`, `desktop/src/shared/ipc-channels.ts:invoke:gateway-ws:close`, `desktop/src/shared/ipc-channels.ts:invoke:gateway-ws:connect`, `desktop/src/shared/ipc-channels.ts:invoke:gateway-ws:send`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:ensure`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:fetch`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:get-status`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:retry`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:stream-cancel`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:stream-start`, `desktop/src/shared/ipc-channels.ts:invoke:gateway:upload`, `desktop/src/shared/ipc-channels.ts:invoke:image:showItemInFolder`, `desktop/src/shared/ipc-channels.ts:invoke:inspiration:close-window`, `desktop/src/shared/ipc-channels.ts:invoke:inspiration:open-window`, `desktop/src/shared/ipc-channels.ts:invoke:inspiration:window-state`, `desktop/src/shared/ipc-channels.ts:invoke:security:audit`, `desktop/src/shared/ipc-channels.ts:invoke:security:audit-export`, `desktop/src/shared/ipc-channels.ts:invoke:security:audit-purge`, `desktop/src/shared/ipc-channels.ts:invoke:security:alerts`, `desktop/src/shared/ipc-channels.ts:invoke:security:alert-isolate`, `desktop/src/shared/ipc-channels.ts:invoke:security:alert-revoke`, `desktop/src/shared/ipc-channels.ts:invoke:security:alert-resolve`, `desktop/src/shared/ipc-channels.ts:invoke:security:capabilities`, `desktop/src/shared/ipc-channels.ts:invoke:security:decide`, `desktop/src/shared/ipc-channels.ts:invoke:security:delete-rule`, `desktop/src/shared/ipc-channels.ts:invoke:security:enable-uac`, `desktop/src/shared/ipc-channels.ts:invoke:security:get-strict-security`, `desktop/src/shared/ipc-channels.ts:invoke:security:pending`, `desktop/src/shared/ipc-channels.ts:invoke:security:rules`, `desktop/src/shared/ipc-channels.ts:invoke:security:set-mode`, `desktop/src/shared/ipc-channels.ts:invoke:security:set-rule`, `desktop/src/shared/ipc-channels.ts:invoke:security:set-strict-security`, `desktop/src/shared/ipc-channels.ts:invoke:security:setup`, `desktop/src/shared/ipc-channels.ts:invoke:security:uac-status`, `desktop/src/shared/ipc-channels.ts:invoke:shell:listOpenApplications`, `desktop/src/shared/ipc-channels.ts:invoke:shell:openExternal`, `desktop/src/shared/ipc-channels.ts:invoke:shell:openPath`, `desktop/src/shared/ipc-channels.ts:invoke:shell:openPathWith`, `desktop/src/shared/ipc-channels.ts:invoke:shell:pathExists`, `desktop/src/shared/ipc-channels.ts:invoke:shell:readFileBase64`, `desktop/src/shared/ipc-channels.ts:invoke:shell:readTextFile`, `desktop/src/shared/ipc-channels.ts:invoke:shell:showItemInFolder`, `desktop/src/shared/ipc-channels.ts:invoke:shell:writeFileBase64`, `desktop/src/shared/ipc-channels.ts:invoke:shell:writeTextFile`, `desktop/src/shared/ipc-channels.ts:invoke:update:get-state`, `desktop/src/shared/ipc-channels.ts:invoke:update:install-package`, `desktop/src/shared/ipc-channels.ts:invoke:update:pause`, `desktop/src/shared/ipc-channels.ts:invoke:update:resume`, `desktop/src/shared/ipc-channels.ts:invoke:update:retry`, `desktop/src/shared/ipc-channels.ts:invoke:update:start-download`, `desktop/src/shared/ipc-channels.ts:invoke:wiki:openSourceFile`, `desktop/src/shared/ipc-channels.ts:invoke:window:close`, `desktop/src/shared/ipc-channels.ts:invoke:window:isMaximized`, `desktop/src/shared/ipc-channels.ts:invoke:window:maximize`, `desktop/src/shared/ipc-channels.ts:invoke:window:minimize`, `desktop/src/shared/ipc-channels.ts:invoke:workspace:directoryInfo`, `desktop/src/shared/ipc-channels.ts:main-event:auth:session-state`, `desktop/src/shared/ipc-channels.ts:main-event:backend:status`, `desktop/src/shared/ipc-channels.ts:main-event:backend:suppress-overlay`, `desktop/src/shared/ipc-channels.ts:main-event:browser-view:interaction-requested`, `desktop/src/shared/ipc-channels.ts:main-event:browser-view:layout-invalidated`, `desktop/src/shared/ipc-channels.ts:main-event:browser-view:load-failed`, `desktop/src/shared/ipc-channels.ts:main-event:browser-view:navigation-changed`, `desktop/src/shared/ipc-channels.ts:main-event:browser-ws:event`, `desktop/src/shared/ipc-channels.ts:main-event:gateway-ws:event`, `desktop/src/shared/ipc-channels.ts:main-event:gateway:stream-event`, `desktop/src/shared/ipc-channels.ts:main-event:inspiration:window-state-changed`, `desktop/src/shared/ipc-channels.ts:main-event:main:uncaught-error`, `desktop/src/shared/ipc-channels.ts:main-event:version-update-available`, `desktop/src/shared/ipc-channels.ts:main-event:version-update-download-progress`, `desktop/src/shared/ipc-channels.ts:main-event:window:maximized-changed`, `desktop/src/shared/ipc-channels.ts:renderer-event:inspiration:sticky-close`
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new IPC channel, preload exposure, trustedHandle registration, main-to-renderer event, or renderer event

### ACE-SURF-GATEWAY-ROUTES

- Locator: `crew/gateway::all literal FastAPI HTTP and WebSocket decorators`
- Trust source: Authenticated local or remote account, Desktop instance proof where required, strict request schema, and explicit owner/admin context
- Fail closed: Unknown or unauthenticated callers, owner or role mismatch, invalid schema, replay, ambiguous Content-Length/Transfer-Encoding framing, quota exhaustion, unavailable subsystem, logout revocation, or stale owner configuration snapshots are rejected before side effects
- Lifecycle/revocation owner: Gateway account, connection, session, task, logout, and subsystem-specific lifecycle owners
- Tests: `GATEWAY-AUTH-CONTRACT` — `tests/gateway/test_auth_contract.py`<br>`GATEWAY-ACCOUNT-ISOLATION` — `tests/gateway/test_account_isolation.py`<br>`GATEWAY-ATTACHMENT-QUOTA` — `tests/security/test_attachment_security.py`<br>`CONFIG-FAIL-CLOSED` — `tests/security/test_security_config_fail_closed.py`<br>`REMOTE-OWNER-MATRIX` — `tests/gateway/test_security_api.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: `crew/gateway/app.py:GET /`, `crew/gateway/app.py:GET /{full_path:path}`, `crew/gateway/interaction_bridge.py:POST /api/internal/interactions/ask`, `crew/gateway/interaction_bridge.py:POST /api/internal/team/mention`, `crew/gateway/interaction_bridge.py:POST /api/internal/team/plan/create`, `crew/gateway/interaction_bridge.py:POST /api/internal/team/plan/read`, `crew/gateway/interaction_bridge.py:POST /api/internal/team/plan/update`, `crew/gateway/routers/auth_session.py:POST /api/auth/logout`, `crew/gateway/routers/browser.py:DELETE /api/browser/data`, `crew/gateway/routers/browser.py:GET /api/browser/doctor`, `crew/gateway/routers/browser.py:GET /api/browser/{session_id}/state`, `crew/gateway/routers/browser.py:POST /api/browser/{session_id}/artifact`, `crew/gateway/routers/browser.py:POST /api/browser/{session_id}/control`, `crew/gateway/routers/browser.py:WEBSOCKET /ws/browser-host`, `crew/gateway/routers/browser.py:WEBSOCKET /ws/browser/{session_id}`, `crew/gateway/routers/channels.py:DELETE /api/platforms/{name}/account`, `crew/gateway/routers/channels.py:GET /api/platforms`, `crew/gateway/routers/channels.py:GET /api/platforms/{name}/config`, `crew/gateway/routers/channels.py:POST /api/feishu/events`, `crew/gateway/routers/channels.py:POST /api/platforms/{name}/connect`, `crew/gateway/routers/channels.py:POST /api/platforms/{name}/disconnect`, `crew/gateway/routers/channels.py:POST /api/platforms/{name}/qr-login/start`, `crew/gateway/routers/channels.py:POST /api/platforms/{name}/qr-login/status`, `crew/gateway/routers/channels.py:POST /api/platforms/{name}/reconnect`, `crew/gateway/routers/channels.py:PUT /api/platforms/{name}/config`, `crew/gateway/routers/config.py:DELETE /api/config/models/{model_id}`, `crew/gateway/routers/config.py:GET /api/config`, `crew/gateway/routers/config.py:POST /api/config/model`, `crew/gateway/routers/config.py:POST /api/config/models`, `crew/gateway/routers/config.py:PUT /api/config/models/{model_id}`, `crew/gateway/routers/cron.py:DELETE /api/cron/jobs/{job_id}`, `crew/gateway/routers/cron.py:GET /api/cron/delivery-targets`, `crew/gateway/routers/cron.py:GET /api/cron/jobs`, `crew/gateway/routers/cron.py:GET /api/cron/jobs/{job_id}`, `crew/gateway/routers/cron.py:GET /api/cron/stats`, `crew/gateway/routers/cron.py:POST /api/cron/fires/{fire_id}/retry`, `crew/gateway/routers/cron.py:POST /api/cron/jobs`, `crew/gateway/routers/cron.py:POST /api/cron/jobs/{job_id}/pause`, `crew/gateway/routers/cron.py:POST /api/cron/jobs/{job_id}/resume`, `crew/gateway/routers/cron.py:POST /api/cron/jobs/{job_id}/run`, `crew/gateway/routers/dynamic_kanban.py:GET /api/dynamic-kanban/{session_id}/board`, `crew/gateway/routers/dynamic_kanban.py:GET /api/dynamic-kanban/{session_id}/status`, `crew/gateway/routers/dynamic_kanban.py:POST /api/dynamic-kanban/{session_id}/pause`, `crew/gateway/routers/dynamic_kanban.py:POST /api/dynamic-kanban/{session_id}/resume`, `crew/gateway/routers/mcp_servers.py:DELETE /api/mcp/servers/{name}`, `crew/gateway/routers/mcp_servers.py:GET /api/mcp/servers`, `crew/gateway/routers/mcp_servers.py:POST /api/mcp/servers`, `crew/gateway/routers/mcp_servers.py:POST /api/mcp/servers/{name}/reload`, `crew/gateway/routers/mcp_servers.py:PUT /api/mcp/servers/{name}`, `crew/gateway/routers/mcp_setup.py:GET /api/mcp/cua-driver/setup/{task_id}`, `crew/gateway/routers/mcp_setup.py:GET /api/mcp/cua-driver/status`, `crew/gateway/routers/mcp_setup.py:POST /api/mcp/cua-driver/setup`, `crew/gateway/routers/mcp_setup.py:POST /api/mcp/cua-driver/setup/{task_id}/cancel`, `crew/gateway/routers/misc.py:DELETE /api/skills/{slug}`, `crew/gateway/routers/misc.py:GET /api/complete`, `crew/gateway/routers/misc.py:GET /api/health`, `crew/gateway/routers/misc.py:GET /api/plugins`, `crew/gateway/routers/misc.py:GET /api/skills`, `crew/gateway/routers/misc.py:GET /api/skills/store`, `crew/gateway/routers/misc.py:GET /api/tools`, `crew/gateway/routers/misc.py:GET /api/toolsets`, `crew/gateway/routers/misc.py:POST /api/skills/{slug}/install`, `crew/gateway/routers/misc.py:POST /api/upload`, `crew/gateway/routers/misc.py:PUT /api/skills/evolution`, `crew/gateway/routers/plugins.py:DELETE /api/plugins/{plugin_key}`, `crew/gateway/routers/plugins.py:GET /api/plugins/states`, `crew/gateway/routers/plugins.py:POST /api/plugins/install`, `crew/gateway/routers/plugins.py:PUT /api/plugins/{plugin_key}/enabled`, `crew/gateway/routers/plugins.py:PUT /api/plugins/{plugin_key}/system-enabled`, `crew/gateway/routers/remote_auth.py:GET /api/auth/config`, `crew/gateway/routers/remote_auth.py:GET /api/auth/session`, `crew/gateway/routers/remote_auth.py:POST /api/auth/login`, `crew/gateway/routers/remote_auth.py:POST /api/auth/send-code`, `crew/gateway/routers/runtimes.py:DELETE /api/external-agents/{agent_id}`, `crew/gateway/routers/runtimes.py:DELETE /api/external-teams/{team_id}`, `crew/gateway/routers/runtimes.py:GET /api/external-agents`, `crew/gateway/routers/runtimes.py:GET /api/external-teams`, `crew/gateway/routers/runtimes.py:GET /api/external-teams/roles`, `crew/gateway/routers/runtimes.py:GET /api/runtimes`, `crew/gateway/routers/runtimes.py:POST /api/external-agents`, `crew/gateway/routers/runtimes.py:POST /api/external-teams`, `crew/gateway/routers/runtimes.py:POST /api/external-teams/draft/description`, `crew/gateway/routers/runtimes.py:POST /api/external-teams/draft/formation`, `crew/gateway/routers/runtimes.py:POST /api/external-teams/roles/suggest`, `crew/gateway/routers/runtimes.py:POST /api/external-teams/suggest`, `crew/gateway/routers/runtimes.py:POST /api/runtimes/register`, `crew/gateway/routers/runtimes.py:POST /api/runtimes/scan`, `crew/gateway/routers/scenarios.py:GET /api/scenarios`, `crew/gateway/routers/scenarios.py:GET /api/scenarios/all`, `crew/gateway/routers/scenarios.py:GET /api/scenarios/intro-lines`, `crew/gateway/routers/scenarios.py:GET /api/scenarios/loading-status`, `crew/gateway/routers/security.py:DELETE /rules/{rule_id}`, `crew/gateway/routers/security.py:GET /audit`, `crew/gateway/routers/security.py:GET /audit/export`, `crew/gateway/routers/security.py:GET /capabilities`, `crew/gateway/routers/security.py:GET /full-access-challenge`, `crew/gateway/routers/security.py:GET /pending`, `crew/gateway/routers/security.py:GET /rules`, `crew/gateway/routers/security.py:PATCH /rules/{rule_id}`, `crew/gateway/routers/security.py:POST /audit/purge-expired`, `crew/gateway/routers/security.py:GET /alerts`, `crew/gateway/routers/security.py:POST /alerts/report`, `crew/gateway/routers/security.py:POST /alerts/{alert_id}/isolate`, `crew/gateway/routers/security.py:POST /alerts/{alert_id}/revoke`, `crew/gateway/routers/security.py:POST /alerts/{alert_id}/resolve`, `crew/gateway/routers/security.py:POST /fake-executions`, `crew/gateway/routers/security.py:POST /requests/{request_id}/decision`, `crew/gateway/routers/security.py:PUT /mode`, `crew/gateway/routers/sessions.py:DELETE /api/session/{session_id}`, `crew/gateway/routers/sessions.py:DELETE /api/workspace/{workspace_id}`, `crew/gateway/routers/sessions.py:GET /api/channel-sessions`, `crew/gateway/routers/sessions.py:GET /api/runtime/concurrency`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/agent-config`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/context`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/debug-log`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/model`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/plan`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/status`, `crew/gateway/routers/sessions.py:GET /api/session/{session_id}/todos`, `crew/gateway/routers/sessions.py:GET /api/sessions`, `crew/gateway/routers/sessions.py:GET /api/sessions/status`, `crew/gateway/routers/sessions.py:GET /api/tasks`, `crew/gateway/routers/sessions.py:GET /api/tasks/{task_or_session_id}`, `crew/gateway/routers/sessions.py:GET /api/usage`, `crew/gateway/routers/sessions.py:GET /api/workspaces`, `crew/gateway/routers/sessions.py:POST /api/session/{session_id}/ensure`, `crew/gateway/routers/sessions.py:POST /api/tasks/{task_id}/cancel`, `crew/gateway/routers/sessions.py:POST /api/tasks/{task_id}/wait`, `crew/gateway/routers/sessions.py:POST /api/workspaces`, `crew/gateway/routers/sessions.py:PUT /api/session/{session_id}/agent-config`, `crew/gateway/routers/sessions.py:PUT /api/session/{session_id}/archive`, `crew/gateway/routers/sessions.py:PUT /api/session/{session_id}/model`, `crew/gateway/routers/sessions.py:PUT /api/session/{session_id}/pin`, `crew/gateway/routers/sessions.py:PUT /api/session/{session_id}/title`, `crew/gateway/routers/sessions.py:PUT /api/workspace/{workspace_id}`, `crew/gateway/routers/sites.py:DELETE /inspirations/{inspiration_id}`, `crew/gateway/routers/sites.py:DELETE /{site_id}`, `crew/gateway/routers/sites.py:GET `, `crew/gateway/routers/sites.py:GET /canvases`, `crew/gateway/routers/sites.py:GET /canvases/{canvas_id}`, `crew/gateway/routers/sites.py:GET /canvases/{canvas_id}/render`, `crew/gateway/routers/sites.py:GET /inspirations`, `crew/gateway/routers/sites.py:GET /inspirations/{inspiration_id}`, `crew/gateway/routers/sites.py:GET /widgets/{widget_id}`, `crew/gateway/routers/sites.py:GET /widgets/{widget_id}/render`, `crew/gateway/routers/sites.py:GET /widgets/{widget_id}/render/{asset_path:path}`, `crew/gateway/routers/sites.py:GET /{site_id}`, `crew/gateway/routers/sites.py:GET /{site_id}/export`, `crew/gateway/routers/sites.py:GET /{site_id}/preview`, `crew/gateway/routers/sites.py:GET /{site_id}/preview/{asset_path:path}`, `crew/gateway/routers/sites.py:PATCH /canvases/{canvas_id}/placements/{mount_id}`, `crew/gateway/routers/sites.py:PATCH /inspirations/{inspiration_id}/annotations/{annotation_id}`, `crew/gateway/routers/sites.py:PATCH /{site_id}/annotations/{annotation_id}`, `crew/gateway/routers/sites.py:POST /automations/{automation_id}/run`, `crew/gateway/routers/sites.py:POST /inspirations/{inspiration_id}/annotations`, `crew/gateway/routers/sites.py:POST /inspirations/{inspiration_id}/export`, `crew/gateway/routers/sites.py:POST /widgets/{widget_id}/emit`, `crew/gateway/routers/sites.py:POST /{site_id}/annotations`, `crew/gateway/routers/sites.py:POST /{site_id}/export`, `crew/gateway/routers/sites.py:POST /{site_id}/publish`, `crew/gateway/routers/system.py:GET /api/system/logs`, `crew/gateway/routers/system.py:DELETE /api/system/logs`, `crew/gateway/routers/system.py:GET /api/system/metrics`, `crew/gateway/routers/wiki.py:DELETE /kbs/{kb_id}`, `crew/gateway/routers/wiki.py:DELETE /pages`, `crew/gateway/routers/wiki.py:DELETE /pages/{page_id}`, `crew/gateway/routers/wiki.py:DELETE /sources/{source_id}`, `crew/gateway/routers/wiki.py:GET /agent-sessions`, `crew/gateway/routers/wiki.py:GET /graph`, `crew/gateway/routers/wiki.py:GET /kbs`, `crew/gateway/routers/wiki.py:GET /pages`, `crew/gateway/routers/wiki.py:GET /pages/{page_id}`, `crew/gateway/routers/wiki.py:GET /query`, `crew/gateway/routers/wiki.py:GET /search`, `crew/gateway/routers/wiki.py:GET /sources`, `crew/gateway/routers/wiki.py:GET /sources/{source_id}/file`, `crew/gateway/routers/wiki.py:GET /summary`, `crew/gateway/routers/wiki.py:GET /vault-documents/{document_name}`, `crew/gateway/routers/wiki.py:POST /agent-session`, `crew/gateway/routers/wiki.py:POST /compile`, `crew/gateway/routers/wiki.py:POST /confirmations/{confirmation_id}/cancel`, `crew/gateway/routers/wiki.py:POST /ingest`, `crew/gateway/routers/wiki.py:POST /ingest/cancel`, `crew/gateway/routers/wiki.py:POST /init`, `crew/gateway/routers/wiki.py:POST /kbs`, `crew/gateway/routers/wiki.py:POST /lint`, `crew/gateway/routers/wiki.py:POST /pages`, `crew/gateway/routers/wiki.py:POST /upload`, `crew/gateway/routers/wiki.py:PUT /pages/{page_id}`, `crew/gateway/routers/work.py:DELETE /items/{item_id}`, `crew/gateway/routers/work.py:DELETE /preferences/{preference_id}`, `crew/gateway/routers/work.py:DELETE /references/{reference_id}`, `crew/gateway/routers/work.py:DELETE /sources/{connector_key}/data`, `crew/gateway/routers/work.py:DELETE /templates/{template_id}`, `crew/gateway/routers/work.py:DELETE /workspaces/{workspace_id}/index`, `crew/gateway/routers/work.py:GET /dashboard`, `crew/gateway/routers/work.py:GET /history`, `crew/gateway/routers/work.py:GET /items`, `crew/gateway/routers/work.py:GET /items/{item_id}`, `crew/gateway/routers/work.py:GET /items/{item_id}/activity`, `crew/gateway/routers/work.py:GET /knowledge/organization`, `crew/gateway/routers/work.py:GET /knowledge/personal`, `crew/gateway/routers/work.py:GET /knowledge/publish`, `crew/gateway/routers/work.py:GET /mentions`, `crew/gateway/routers/work.py:GET /preferences`, `crew/gateway/routers/work.py:GET /preferences/settings`, `crew/gateway/routers/work.py:GET /references`, `crew/gateway/routers/work.py:GET /reports`, `crew/gateway/routers/work.py:GET /settings`, `crew/gateway/routers/work.py:GET /settings/workspaces/{workspace_id}`, `crew/gateway/routers/work.py:GET /sources`, `crew/gateway/routers/work.py:GET /sources/records`, `crew/gateway/routers/work.py:GET /templates`, `crew/gateway/routers/work.py:GET /templates/{template_id}`, `crew/gateway/routers/work.py:GET /workspaces/{workspace_id}/index`, `crew/gateway/routers/work.py:PATCH /items/{item_id}`, `crew/gateway/routers/work.py:PATCH /preferences/{preference_id}`, `crew/gateway/routers/work.py:PATCH /templates/{template_id}`, `crew/gateway/routers/work.py:POST /dashboard/archive`, `crew/gateway/routers/work.py:POST /dashboard/refresh`, `crew/gateway/routers/work.py:POST /items`, `crew/gateway/routers/work.py:POST /items/{item_id}/actions`, `crew/gateway/routers/work.py:POST /items/{item_id}/knowledge`, `crew/gateway/routers/work.py:POST /items/{item_id}/processing-session`, `crew/gateway/routers/work.py:POST /knowledge/personal`, `crew/gateway/routers/work.py:POST /knowledge/publish`, `crew/gateway/routers/work.py:POST /preferences`, `crew/gateway/routers/work.py:POST /references`, `crew/gateway/routers/work.py:POST /references/agent-session`, `crew/gateway/routers/work.py:POST /references/{reference_id}/refresh`, `crew/gateway/routers/work.py:POST /reports/archive`, `crew/gateway/routers/work.py:POST /sessions`, `crew/gateway/routers/work.py:POST /sources/records/{record_id}/resolve`, `crew/gateway/routers/work.py:POST /sources/{connector_key}/refresh`, `crew/gateway/routers/work.py:POST /templates`, `crew/gateway/routers/work.py:POST /templates/{template_id}/instantiate`, `crew/gateway/routers/work.py:PUT /preferences/settings`, `crew/gateway/routers/work.py:PUT /settings`, `crew/gateway/routers/work.py:PUT /settings/workspaces/{workspace_id}`, `crew/gateway/routers/work.py:PUT /sources/{connector_key}`, `crew/gateway/routers/work.py:PUT /workspaces/{workspace_id}/index`, `crew/gateway/ws.py:WEBSOCKET /ws`
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any added, removed, renamed, or dynamically constructed FastAPI HTTP or WebSocket route, or any upload quota enforcement change

### ACE-SURF-NETWORK-DESKTOP-WRAPPERS

- Locator: `desktop/src/main/index.ts::reviewed fetch, WebSocket, and HTTPS request boundaries`
- Trust source: Loopback Gateway identity proof, pinned product endpoints, signed update metadata, or validated Desktop auth and feedback configuration
- Fail closed: Non-allowlisted schemes or hosts, missing instance proof, protocol mismatch, invalid redirects, and transfer-limit failures reject the connection
- Lifecycle/revocation owner: Desktop app, auth session, update controller, Browser session, and Gateway socket owners
- Tests: `DESKTOP-GATEWAY-IPC` — `tests/security/test_electron_ipc_contract.py`<br>`UPDATE-URL` — `desktop/tests/unit/update-url.test.ts`
- Evidence: `node-test` — `desktop/tests/unit/update-url.test.ts`
- Artifact references: `node-test` — `desktop/tests/unit/update-url.test.ts`
- Reviewed primitive references: `javascript-network:desktop/src/main/auth-session.ts:fetch`, `javascript-network:desktop/src/main/feedback-service.ts:fetch`, `javascript-network:desktop/src/main/index.ts:WebSocket`, `javascript-network:desktop/src/main/index.ts:fetch`, `javascript-network:desktop/src/main/site-preview-protocol.ts:fetch`, `javascript-network:desktop/src/main/update/download-controller.ts:fetch`, `javascript-network:desktop/src/main/update/download-controller.ts:https.request`, `javascript-network:desktop/src/ui/backend-client.ts:WebSocket`, `javascript-network:desktop/src/ui/backend-client.ts:fetch`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Desktop fetch, WebSocket, HTTP request, redirect, endpoint, or renderer-accessible network bridge

### ACE-SURF-NETWORK-PLATFORM-PLUGINS

- Locator: `plugins/platforms::Feishu and Weixin fixed platform HTTP and long-poll clients`
- Trust source: Administrator-configured platform account, fixed vendor API domains, and platform-specific authenticated session state
- Fail closed: Missing dependency or account, invalid configured endpoint, authentication failure, timeout, malformed response, disconnect, or owner revocation stops the platform operation
- Lifecycle/revocation owner: Platform account owner, adapter connection lifecycle, Gateway logout, and plugin enable state
- Tests: `PLATFORM-FEISHU` — `tests/test_platform_feishu.py`<br>`PLATFORM-WEIXIN` — `tests/test_platform_weixin.py`
- Evidence: `pytest` — `tests/test_platform_weixin.py`
- Artifact references: `pytest` — `tests/test_platform_weixin.py`
- Reviewed primitive references: `python-network:plugins/feishu/__init__.py:urllib.request.urlopen`, `python-network:plugins/platforms/feishu/adapter.py:urllib.request.urlopen`, `python-network:plugins/platforms/weixin/adapter.py:aiohttp.ClientSession`, `python-network:plugins/platforms/weixin/ilink.py:aiohttp.ClientSession`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Vendor domain, HTTP client, ambient proxy behavior, TLS connector, polling, upload/download, account storage, or reconnect changes

### ACE-SURF-NETWORK-PYTHON-WRAPPERS

- Locator: `crew/security/outbound.py::OutboundHttpClient, policy adapters, provider clients, and exact reviewed socket users`
- Trust source: Canonical URL authorization, owner/task scope, pinned DNS result, fixed product endpoint, or sandbox-descendant protocol
- Fail closed: Missing authorization, unsafe address classes, DNS or origin drift, redirect overflow, unsupported response Content-Encoding, transfer limits, and connector failures deny the request
- Lifecycle/revocation owner: SecurityService grant lifecycle and the owning provider, MCP, plugin, skill, CUA, or Gateway task
- Tests: `OUTBOUND-POLICY` — `tests/security/test_outbound_policy.py`<br>`MCP-NETWORK` — `tests/test_mcp_client_lifecycle.py`
- Evidence: `pytest` — `tests/security/test_outbound_policy.py`
- Artifact references: `pytest` — `tests/security/test_outbound_policy.py`
- Reviewed primitive references: `python-network:crew/gateway/mcp_server.py:OutboundHttpClient`, `python-network:crew/gateway/routers/remote_auth.py:OutboundHttpClient`, `python-network:crew/plugins/security.py:OutboundHttpClient`, `python-network:crew/providers/anthropic_provider.py:httpx.AsyncClient`, `python-network:crew/providers/openai_provider.py:httpx.AsyncClient`, `python-network:crew/security/outbound.py:socket.getaddrinfo`, `python-network:crew/security/outbound.py:socket.socket`, `python-network:crew/skills/image-understanding/scripts/image_understand.py:OutboundHttpClient`, `python-network:crew/skills/video-understanding/scripts/video_understand.py:OutboundHttpClient`, `python-network:crew/skills/xlsx/scripts/office/soffice.py:socket.socket`, `python-network:crew/tools/cua_setup.py:OutboundHttpClient`, `python-network:crew/tools/managed_tools.py:OutboundHttpClient`, `python-network:crew/tools/mcp_client.py:httpx2.AsyncClient`, `python-network:crew/tools/security_guard.py:OutboundHttpClient`, `python-network:crew/wiki/sources.py:requests.Session`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Python network client, socket constructor, DNS resolver, transport adapter, redirect path, or ambient proxy use

### ACE-SURF-NETWORK-RUNTIME-WRAPPERS

- Locator: `security-runtime/src/network::native pinned connector and managed proxy listeners`
- Trust source: Signed execution authorization and canonical proxy policy from the broker
- Fail closed: Unapproved addresses, route mismatch, unavailable proxy isolation, timeout, or lifecycle cleanup failure aborts managed networking
- Lifecycle/revocation owner: Native runtime session and broker cancellation lifecycle
- Tests: `RUNTIME-NETWORK-POLICY` — `tests/security/test_native_runtime_contract.py`
- Evidence: `cargo-test` — `security-runtime/tests/linux_adversarial.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/linux_adversarial.rs`
- Reviewed primitive references: `rust-network:security-runtime/src/linux/proxy_routing.rs:TcpListener::bind`, `rust-network:security-runtime/src/linux/proxy_routing.rs:TcpStream::connect_timeout`, `rust-network:security-runtime/src/network/connector.rs:TcpStream::connect_timeout`, `rust-network:security-runtime/src/network/proxy.rs:TcpListener::bind`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Rust listener, outbound connector, socket type, proxy bridge, or platform routing mechanism

### ACE-SURF-NETWORK-WEB-CLIENT

- Locator: `web/src::same-origin Gateway REST and WebSocket client`
- Trust source: Browser same-origin policy, authenticated Gateway session, and server-issued owner/session state
- Fail closed: Authentication loss, non-success response, malformed frame, owner or sequence mismatch, disconnect, and Gateway denial stop the client action
- Lifecycle/revocation owner: Gateway account/session/logout lifecycle and Web client connection owner
- Tests: `WEB-API-CLIENT` — `web/src/api.test.ts`<br>`WEB-WS-CLIENT` — `web/src/ws.test.ts`
- Evidence: `node-test` — `web/src/ws.test.ts`
- Artifact references: `node-test` — `web/src/ws.test.ts`
- Reviewed primitive references: `javascript-network:web/src/api.ts:fetch`, `javascript-network:web/src/ws.ts:WebSocket`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new Web fetch, WebSocket, endpoint construction, authentication transport, frame, reconnect, or browser network primitive

### ACE-SURF-PLUGIN-EXECUTION

- Locator: `crew/plugins/manager.py::plugin discovery, install, capability registration, enable, disable, and uninstall`
- Trust source: Bundled provenance or administrator-approved root/signer plus strict manifest and declared capabilities
- Fail closed: Unsigned remote executable code, malformed manifests, undeclared capabilities, identity drift, and untrusted native imports remain unavailable
- Lifecycle/revocation owner: Plugin manager owner preference, system enable state, uninstall transaction, and current task capability
- Tests: `PLUGIN-EXECUTION-BOUNDARY` — `tests/security/test_plugin_execution_boundary.py`<br>`PLUGIN-SECURITY` — `tests/test_plugin_security.py`
- Evidence: `pytest` — `tests/security/test_plugin_execution_boundary.py`
- Artifact references: `pytest` — `tests/security/test_plugin_execution_boundary.py`
- Reviewed primitive references: `python-file:plugins/browser/compile_tool.py:Path.write_text`, `python-file:plugins/browser/compile_tool.py:os.open`, `python-file:plugins/browser/workflow_store.py:os.open`, `python-file:plugins/platforms/feishu/adapter.py:Path.read_text`, `python-file:plugins/platforms/feishu/adapter.py:Path.write_text`, `python-file:plugins/platforms/feishu/files.py:Path.write_bytes`, `python-file:plugins/platforms/feishu/files.py:builtins.open`, `python-file:plugins/platforms/weixin/adapter.py:Path.read_bytes`, `python-file:plugins/platforms/weixin/adapter.py:Path.read_text`, `python-file:plugins/platforms/weixin/adapter.py:Path.write_text`, `python-file:plugins/platforms/weixin/ilink.py:Path.read_text`, `python-file:plugins/platforms/weixin/ilink.py:Path.write_bytes`, `python-file:plugins/platforms/weixin/ilink.py:Path.write_text`
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new plugin source, manifest capability, executable callback, import mechanism, signer, installation, or cleanup path

### ACE-SURF-PROC-DIRECT-DESKTOP-CHECK

- Locator: `desktop/scripts/check-security.mjs::build-time Electron security configuration checks`
- Trust source: Trusted source checkout and fixed build/test toolchain
- Fail closed: Missing files, unsafe Electron configuration, child failure, or unexpected output fails the build gate
- Lifecycle/revocation owner: Release CI job and local build process
- Tests: `PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Build check command, child tool, packaged file set, Electron security setting, or release workflow changes

### ACE-SURF-PROC-DIRECT-DESKTOP-GATEWAY-AUTH

- Locator: `desktop/src/main/gateway-instance-auth.ts::Windows Gateway instance-key ACL creation and verification helper`
- Trust source: Kernel-reported SystemRoot PowerShell identity, owner-derived Crew home, and fixed encoded script
- Fail closed: Missing fixed helper, path escape, owner or DACL mismatch, helper failure, or malformed key blocks Gateway instance authentication
- Lifecycle/revocation owner: Desktop Gateway process and instance-key rotation lifecycle
- Tests: `GATEWAY-INSTANCE-AUTH` — `desktop/tests/unit/gateway-instance-auth.test.ts`
- Evidence: `node-test` — `desktop/tests/unit/gateway-instance-auth.test.ts`
- Artifact references: `node-test` — `desktop/tests/unit/gateway-instance-auth.test.ts`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: PowerShell path, script, key path, ACL rule, owner verification, instance proof, or rotation changes

### ACE-SURF-PROC-DIRECT-DESKTOP-MAIN

- Locator: `desktop/src/main/index.ts::Gateway spawn/restart, Desktop lifecycle, Browser bridge, and update orchestration`
- Trust source: Packaged product roots, fixed Gateway argv, trusted IPC sender, signed update state, and owner-bound sockets
- Fail closed: Invalid sender or schema, unavailable fixed executable, startup proof failure, unsafe endpoint, update verification failure, and cleanup failure reject the action
- Lifecycle/revocation owner: Desktop application, Gateway process, Browser socket, update controller, and window lifecycle
- Tests: `MAIN-PROCESS-HARDENING` — `tests/security/test_main_process_hardening.py`<br>`ELECTRON-IPC-CONTRACT` — `tests/security/test_electron_ipc_contract.py`
- Evidence: `pytest` — `tests/security/test_main_process_hardening.py`
- Artifact references: `pytest` — `tests/security/test_main_process_hardening.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Gateway argv, process environment, restart, IPC, Browser bridge, update orchestration, shutdown, or recovery changes

### ACE-SURF-PROC-DIRECT-DESKTOP-OPEN-WITH

- Locator: `desktop/src/main/open-with-service.ts::user-directed registered-application discovery and open`
- Trust source: Explicit Desktop user action, validated local file path, and current OS registered application list
- Fail closed: Unregistered application, invalid path, probe timeout or output overflow, launch failure, or cleanup failure rejects open-with
- Lifecycle/revocation owner: Desktop user action and bounded probe/open process lifecycle
- Tests: `OPEN-WITH-SECURITY` — `tests/security/test_main_process_hardening.py`
- Evidence: `pytest` — `tests/security/test_main_process_hardening.py`
- Artifact references: `pytest` — `tests/security/test_main_process_hardening.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Application discovery, executable identity, argv parsing, file path, timeout, output, or process cleanup changes

### ACE-SURF-PROC-DIRECT-DESKTOP-PLAYWRIGHT-PROBE

- Locator: `desktop/scripts/resolve-playwright-candidates.mjs::build/test Playwright browser candidate probe`
- Trust source: Trusted build environment and fixed candidate roots
- Fail closed: Missing candidate, invalid identity, timeout, or probe failure marks the browser unavailable for build/test
- Lifecycle/revocation owner: Desktop build/test process
- Tests: `PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Candidate root, browser executable, probe argv, output parsing, or packaging changes

### ACE-SURF-PROC-DIRECT-DESKTOP-PW-CONTRACT

- Locator: `desktop/scripts/pw-contract.ts::deferred Electron contract profile cleanup helper`
- Trust source: Fixed Node runtime and contract-owned temporary root
- Fail closed: Missing fixed runtime or cleanup target failure leaves the contract run failed and never broadens the cleanup root
- Lifecycle/revocation owner: Electron contract process and temporary profile cleanup
- Tests: `PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`<br>`PW-CONTRACT` — `desktop/scripts/pw-contract.ts`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`<br>`source-contract` — `desktop/scripts/pw-contract.ts`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Contract cleanup runtime, fixed argv, temporary root, or Electron harness lifecycle changes

### ACE-SURF-PROC-DIRECT-DESKTOP-SECURITY-SETUP

- Locator: `desktop/src/main/security-setup.ts::Windows one-time elevated native runtime security setup`
- Trust source: Explicit Desktop user action and packaged fixed runtime/setup artifact
- Fail closed: Unsupported platform, unverified packaged runtime, UAC cancellation, helper error, or readiness verification failure leaves setup disabled
- Lifecycle/revocation owner: Desktop installer/setup transaction and native runtime readiness owner
- Tests: `SECURITY-SETUP` — `tests/security/test_main_process_hardening.py`
- Evidence: `pytest` — `tests/security/test_main_process_hardening.py`
- Artifact references: `pytest` — `tests/security/test_main_process_hardening.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Packaged runtime, elevation helper, fixed argv, UAC flow, readiness, rollback, or IPC setup route changes

### ACE-SURF-PROC-DIRECT-DESKTOP-UNINSTALL

- Locator: `desktop/src/main/uninstall.ts::signed product uninstall cleanup`
- Trust source: User-initiated uninstall from the signed installed product and fixed product-owned paths
- Fail closed: Path or installation identity mismatch, script creation failure, unsupported platform, or launcher failure aborts cleanup outside verified roots
- Lifecycle/revocation owner: Desktop uninstall transaction and operating-system installer lifecycle
- Tests: `UNINSTALL-BOUNDARY` — `tests/security/test_main_process_hardening.py`
- Evidence: `pytest` — `tests/security/test_main_process_hardening.py`
- Artifact references: `pytest` — `tests/security/test_main_process_hardening.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Cleanup root, script content, launcher, platform behavior, delayed deletion, or installer integration changes

### ACE-SURF-PROC-DIRECT-DESKTOP-UPDATE-INSTALLER

- Locator: `desktop/src/main/update/update-installer.ts::verified package installer launch`
- Trust source: Downloaded package and detached signature held through verified descriptors, pinned trust root, and matching platform/architecture
- Fail closed: Signature, descriptor identity, platform, package type, root-owned helper, launch confirmation, timeout, or stable result mismatch blocks install
- Lifecycle/revocation owner: Desktop update controller and installer child lifecycle
- Tests: `UPDATE-INSTALLER` — `desktop/tests/unit/update-installer.test.ts`<br>`UPDATE-FILE-SECURITY` — `desktop/tests/unit/update-file-security.test.ts`
- Evidence: `node-test` — `desktop/tests/unit/update-installer.test.ts`
- Artifact references: `node-test` — `desktop/tests/unit/update-installer.test.ts`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Package type, signature, descriptor pin, Windows launch, Linux/macOS helper, argv, timeout, or completion handling changes

### ACE-SURF-PROC-DIRECT-DEV-NPM-AUDIT

- Locator: `scripts/audit_runtime_npm.py::release-time production npm dependency audit`
- Trust source: Trusted clean checkout, repository lockfiles, and fixed npm audit argv
- Fail closed: Missing lockfile or tool, timeout, malformed result, audit failure, or vulnerable production dependency fails the release gate
- Lifecycle/revocation owner: Release CI job
- Tests: `RELEASE-SECURITY-WORKFLOWS` — `tests/security/test_release_security_workflows.py`
- Evidence: `pytest` — `tests/security/test_release_security_workflows.py`
- Artifact references: `pytest` — `tests/security/test_release_security_workflows.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Lockfile discovery, npm command, audit policy, timeout, output parser, or release workflow changes

### ACE-SURF-PROC-DIRECT-DEV-PARSER-FUZZ

- Locator: `scripts/run_parser_fuzz_gate.py::bounded Python, TypeScript, and Rust parser fuzz gate`
- Trust source: Trusted checkout, repository corpus, and fixed Cargo, Pytest, and npm test argv
- Fail closed: Corpus overflow, missing tool, parser crash, timeout, nonzero child, or artifact write failure fails the release gate
- Lifecycle/revocation owner: Release CI job and fuzz reproduction artifact owner
- Tests: `PARSER-FUZZ-GATE` — `tests/security/test_parser_fuzz_gate.py`
- Evidence: `pytest` — `tests/security/test_parser_fuzz_gate.py`
- Artifact references: `pytest` — `tests/security/test_parser_fuzz_gate.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Corpus root, parser language, child command, resource budget, timeout, reproduction artifact, or workflow changes

### ACE-SURF-PROC-DIRECT-DEV-READINESS

- Locator: `scripts/check_release_readiness.py::commit-bound release readiness check`
- Trust source: Trusted CI checkout, fixed git executable/argv, and expected repository identity
- Fail closed: Dirty checkout, commit or origin mismatch, missing or stale evidence, git failure, or timeout blocks release
- Lifecycle/revocation owner: Release CI job and evidence retention owner
- Tests: `RELEASE-READINESS` — `tests/security/test_release_security_workflows.py`
- Evidence: `pytest` — `tests/security/test_release_security_workflows.py`
- Artifact references: `pytest` — `tests/security/test_release_security_workflows.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Git command, repository identity, clean-checkout rule, evidence requirement, timeout, or release workflow changes

### ACE-SURF-PROC-DIRECT-DEV-RUNTIME-EVIDENCE

- Locator: `scripts/write_security_runtime_evidence.py::commit- and runner-bound native runtime evidence writer`
- Trust source: Trusted CI runner, clean checkout, expected origin, current commit, and native test outputs
- Fail closed: Dirty checkout, repository or commit mismatch, untrusted runner, missing native result, git failure, or output error blocks evidence
- Lifecycle/revocation owner: Release CI job and native evidence retention owner
- Tests: `RELEASE-SECURITY-WORKFLOWS` — `tests/security/test_release_security_workflows.py`
- Evidence: `pytest` — `tests/security/test_release_security_workflows.py`
- Artifact references: `pytest` — `tests/security/test_release_security_workflows.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Git identity, runner identity, native result format, evidence schema, output root, or workflow changes

### ACE-SURF-PROC-DIRECT-DEV-SBOM

- Locator: `scripts/generate_security_sbom.py::deterministic tracked-lockfile SBOM generation`
- Trust source: Trusted CI checkout and fixed git ls-files enumeration
- Fail closed: Git failure, timeout, invalid lockfile, untracked input, parser error, or output failure blocks SBOM generation
- Lifecycle/revocation owner: Release CI job and SBOM artifact retention owner
- Tests: `RELEASE-SECURITY-WORKFLOWS` — `tests/security/test_release_security_workflows.py`
- Evidence: `pytest` — `tests/security/test_release_security_workflows.py`
- Artifact references: `pytest` — `tests/security/test_release_security_workflows.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Tracked-file enumeration, lockfile parser, git argv, timeout, SBOM format, or release workflow changes

### ACE-SURF-PROC-DIRECT-EXTERNAL-LIFECYCLE

- Locator: `crew/agent/external/process_lifecycle.py::validated external process launch, probe, wait, cancel, and tree cleanup`
- Trust source: Normalized absolute executable, immutable launch snapshot or bounded fixed-probe authority, canonical cwd, and minimal environment
- Fail closed: Missing or replayed authority, executable identity drift, unsafe environment, timeout, output overflow, and cleanup failure reject or terminate the process
- Lifecycle/revocation owner: External agent request, probe authority, and process tree owner
- Tests: `EXTERNAL-PROCESS-LIFECYCLE` — `tests/test_external_agents.py`
- Evidence: `pytest` — `tests/test_external_agents.py`
- Artifact references: `pytest` — `tests/test_external_agents.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any process primitive, executable resolver, launch snapshot, environment, probe, timeout, cancellation, or cleanup change

### ACE-SURF-PROC-DIRECT-FILE-TOOLS

- Locator: `crew/tools/file_tools.py::file_read, file_write, glob, grep, and fixed disabled-mode ripgrep`
- Trust source: Canonical owner/workspace file authorization and verified managed-tool identity
- Fail closed: Missing security service, denied path, identity drift, link or special file, byte limit, unavailable verified ripgrep, or subprocess failure rejects the operation
- Lifecycle/revocation owner: SecurityService turn/session capability and file tool request owner
- Tests: `FILE-POLICY` — `tests/security/test_file_policy.py`<br>`GREP-SYMLINK` — `tests/security/test_grep_symlink_escape.py`
- Evidence: `pytest` — `tests/security/test_file_policy.py`
- Artifact references: `pytest` — `tests/security/test_file_policy.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: File tool, path resolver, read/write/search primitive, ripgrep identity, environment, result budget, or managed routing changes

### ACE-SURF-PROC-DIRECT-LAUNCH

- Locator: `crew/security/launch.py::execute_captured and launch routing`
- Trust source: SecurityService authorization result and one-time signed execution snapshot
- Fail closed: Missing context, denied authorization, invalid snapshot, unavailable managed helper, unsupported capability, or protocol failure rejects execution without host fallback
- Lifecycle/revocation owner: SecurityService task/turn/session owner and captured process lifecycle
- Tests: `EXECUTION-ROUTING` — `tests/security/test_execution_routing.py`
- Evidence: `pytest` — `tests/security/test_execution_routing.py`
- Artifact references: `pytest` — `tests/security/test_execution_routing.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Mode routing, ProcessLaunch, snapshot, helper argv, disabled compatibility, timeout, output, or cancellation changes

### ACE-SURF-PROC-DIRECT-PROCESS-REGISTRY

- Locator: `crew/tools/process_registry.py::background process spawn, bridge, checkpoint, recovery, and cleanup`
- Trust source: Owner-bound ProcessLaunch snapshot, verified executable identity, private checkpoint, and current process identity
- Fail closed: Missing context, invalid or replayed snapshot, bridge failure, helper unavailability, PID identity mismatch, checkpoint tamper, or cleanup failure rejects or forgets unsafe recovery
- Lifecycle/revocation owner: Process registry owner/session lifecycle and broker shutdown
- Tests: `PROCESS-REGISTRY` — `tests/test_process_registry.py`<br>`BACKGROUND-BRIDGE` — `tests/security/test_background_bridge_isolation.py`
- Evidence: `pytest` — `tests/test_process_registry.py`
- Artifact references: `pytest` — `tests/test_process_registry.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Background spawn, bridge, checkpoint, process identity, recovery, environment, output, timeout, or cleanup changes

### ACE-SURF-PROC-DIRECT-RUNTIME-CLIENT

- Locator: `crew/security/runtime_client.py::NativeRuntimeClient helper spawn and authenticated protocol`
- Trust source: Packaged runtime manifest, expected helper digest, fixed argv, random startup token, and signed authorization snapshot
- Fail closed: Missing helper, digest or source mismatch, token or nonce error, unknown protocol field, timeout, output overflow, and premature exit reject execution
- Lifecycle/revocation owner: NativeRuntimeClient request, parent broker, and helper process lifecycle
- Tests: `RUNTIME-CLIENT` — `tests/security/test_runtime_client.py`
- Evidence: `pytest` — `tests/security/test_runtime_client.py`
- Artifact references: `pytest` — `tests/security/test_runtime_client.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Helper source, manifest, digest, argv, protocol, token, nonce, capability, timeout, output, or cleanup changes

### ACE-SURF-PROC-DIRECT-RUST-BWRAP-SOURCE

- Locator: `security-runtime/src/linux/bwrap_source.rs::bubblewrap source and identity probe`
- Trust source: Fixed candidate roots and verified bubblewrap executable identity
- Fail closed: Missing, non-absolute, unsafe-owner, wrong-mode, changed, or failed bubblewrap candidate leaves managed Linux execution unavailable
- Lifecycle/revocation owner: Linux runtime request and helper process lifecycle
- Tests: `LINUX-BWRAP` — `security-runtime/tests/linux_bwrap.rs`
- Evidence: `cargo-test` — `security-runtime/tests/linux_bwrap.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/linux_bwrap.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Bubblewrap source, candidate root, identity, probe argv, fallback, or packaging changes

### ACE-SURF-PROC-DIRECT-RUST-LINUX

- Locator: `security-runtime/src/linux/mod.rs::Linux sandbox command launch`
- Trust source: Authenticated runtime protocol, signed authorization, verified bubblewrap identity, and constructed sandbox plan
- Fail closed: Missing bubblewrap, invalid plan, namespace or seccomp setup failure, proxy failure, spawn error, or cleanup failure aborts managed execution
- Lifecycle/revocation owner: Linux runtime request and sandbox descendant process tree
- Tests: `LINUX-BWRAP` — `security-runtime/tests/linux_bwrap.rs`<br>`LINUX-FAIL-CLOSED` — `security-runtime/tests/linux_fail_closed.rs`
- Evidence: `cargo-test` — `security-runtime/tests/linux_fail_closed.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/linux_fail_closed.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Linux sandbox executable, namespace, mount, seccomp, network, environment, resource, spawn, or cleanup changes

### ACE-SURF-PROC-DIRECT-RUST-LINUX-PROXY

- Locator: `security-runtime/src/linux/proxy_routing.rs::Linux proxy namespace bridge and helper fork`
- Trust source: Runtime-created private sockets, fixed loopback ports, and authenticated parent sandbox plan
- Fail closed: Bind, fork, namespace, descriptor, route, timeout, or cleanup failure disables managed networking and aborts the request
- Lifecycle/revocation owner: Linux runtime request, proxy bridge, and sandbox descendant lifecycle
- Tests: `LINUX-ADVERSARIAL` — `security-runtime/tests/linux_adversarial.rs`
- Evidence: `cargo-test` — `security-runtime/tests/linux_adversarial.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/linux_adversarial.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Fork, listener, Unix socket, namespace bridge, fixed port, descriptor, tunnel, timeout, or cleanup changes

### ACE-SURF-PROC-DIRECT-RUST-MACOS

- Locator: `security-runtime/src/macos/mod.rs::macOS sandbox-exec launch and readiness probe`
- Trust source: Authenticated runtime protocol, generated deny-first Seatbelt profile, and fixed system sandbox-exec identity
- Fail closed: Profile generation, fixed helper, probe, spawn, network boundary, resource, timeout, or cleanup failure aborts managed execution
- Lifecycle/revocation owner: macOS runtime request and sandbox process group
- Tests: `MACOS-PROFILE` — `security-runtime/tests/macos_profile.rs`<br>`MACOS-ADVERSARIAL` — `security-runtime/tests/macos_adversarial.rs`
- Evidence: `cargo-test` — `security-runtime/tests/macos_adversarial.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/macos_adversarial.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Seatbelt profile, sandbox-exec path, probe, filesystem rule, network rule, resource, spawn, or cleanup changes

### ACE-SURF-PROC-DIRECT-RUST-SECCOMP

- Locator: `security-runtime/src/linux/seccomp.rs::Linux seccomp launcher exec`
- Trust source: Validated fixed seccomp helper command derived from the native sandbox plan
- Fail closed: Missing policy, invalid command, seccomp load error, exec error, or unsupported architecture aborts managed execution
- Lifecycle/revocation owner: Linux runtime request and exec-replaced sandbox child
- Tests: `LINUX-FAIL-CLOSED` — `security-runtime/tests/linux_fail_closed.rs`
- Evidence: `cargo-test` — `security-runtime/tests/linux_fail_closed.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/linux_fail_closed.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Seccomp policy, helper command, architecture support, exec behavior, or failure mapping changes

### ACE-SURF-PROC-DIRECT-RUST-SHELL

- Locator: `security-runtime/src/shell.rs::fixed shell discovery and classification probes`
- Trust source: Platform-specific fixed shell candidates and bounded classification input
- Fail closed: Missing fixed shell, unsafe candidate, unsupported syntax, timeout, output overflow, or probe failure rejects classification or execution
- Lifecycle/revocation owner: Native runtime request and bounded shell probe lifecycle
- Tests: `RUNTIME-PROTOCOL` — `security-runtime/tests/protocol.rs`
- Evidence: `cargo-test` — `security-runtime/tests/protocol.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/protocol.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Shell candidate, parser, fixed argv, environment, classification, timeout, output, or fallback changes

### ACE-SURF-PROC-DIRECT-RUST-WINDOWS

- Locator: `security-runtime/src/windows/process.rs::Windows restricted-token process creation`
- Trust source: Authenticated runtime protocol, restricted token, verified executable descriptor, fixed inherited handles, and protected sandbox state
- Fail closed: Token, executable, handle, ACL, environment, private Desktop/UserList hardening, process creation, Job assignment, readiness, or cleanup failure terminates or rejects the child
- Lifecycle/revocation owner: Windows runtime request and Job object process tree
- Tests: `WINDOWS-SANDBOX` — `security-runtime/tests/windows_sandbox.rs`<br>`WINDOWS-TOKEN` — `security-runtime/tests/windows_token.rs`
- Evidence: `cargo-test` — `security-runtime/tests/windows_sandbox.rs`
- Artifact references: `cargo-test` — `security-runtime/tests/windows_sandbox.rs`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Windows token, logon fallback, executable descriptor, inherited handle, environment, private Desktop, UserList visibility, Job, readiness, spawn, or cleanup changes

### ACE-SURF-PROC-DIRECT-SECURITY-LIFECYCLE

- Locator: `crew/security/process_lifecycle.py::captured execution process-tree cleanup`
- Trust source: Process identity created by the current broker request and kernel-reported fixed system tools
- Fail closed: Unverified PID, executable, creation time, process group, or fixed helper identity is never targeted as a recovered process
- Lifecycle/revocation owner: Captured execution owner and broker shutdown lifecycle
- Tests: `PROCESS-LIFECYCLE` — `tests/security/test_execution_routing.py`
- Evidence: `pytest` — `tests/security/test_execution_routing.py`
- Artifact references: `pytest` — `tests/security/test_execution_routing.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Process identity, PID recovery, taskkill path, process group, timeout, cancellation, or cleanup changes

### ACE-SURF-PROC-DIRECT-SKILL-DOCX-REDLINING

- Locator: `crew/skills/docx/scripts/office/validators/redlining.py::DOCX redlining validator child tools`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, unavailable child tool, invalid document, timeout, or nonzero validator result fails the skill operation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Validator executable, argv, environment, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-EVAL-VIEWER

- Locator: `crew/skills/skill-creator/eval-viewer/generate_review.py::skill evaluation review generator child processes`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, invalid evaluation input, unavailable child, timeout, or nonzero result fails generation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Evaluation child executable, argv, environment, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-HTML-PDF

- Locator: `crew/skills/html-to-pdf/scripts/convert.cjs::sandboxed Chromium HTML-to-PDF converter`
- Trust source: Bundled converter, native-runtime-only ACE_SANDBOX marker, fixed absolute Chromium identity, and bounded private input/output
- Fail closed: Missing native marker, unavailable or unsandboxed Chromium, any subresource or navigation, limit breach, crash, orphan, or cleanup failure rejects output
- Lifecycle/revocation owner: Managed terminal task, converter timeout, and Chromium descendant process tree
- Tests: `HTML-PDF-HARDENING` — `tests/security/test_html_to_pdf_hardening.py`
- Evidence: `pytest` — `tests/security/test_html_to_pdf_hardening.py`
- Artifact references: `pytest` — `tests/security/test_html_to_pdf_hardening.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Chromium candidate, launch flag, sandbox check, resource policy, local/network request, output publication, timeout, or cleanup changes

### ACE-SURF-PROC-DIRECT-SKILL-IMPROVE

- Locator: `crew/skills/skill-creator/scripts/improve_description.py::skill-description improvement evaluator`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, invalid evaluation input, unavailable child, timeout, or nonzero result fails improvement
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Evaluator executable, argv, environment, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-MARKDOWN-PDF

- Locator: `crew/skills/md-to-pdf/scripts/md2pdf.py::Markdown-to-PDF child converter`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, unavailable converter, invalid input, timeout, or nonzero child result fails the skill operation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Converter executable, argv, environment, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-PDF-CONVERTER

- Locator: `crew/skills/pdf/scripts/md2pdf/md2pdf_convert.py::PDF skill Markdown conversion child`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, unavailable converter, invalid input, timeout, or nonzero child result fails the skill operation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Converter executable, argv, environment, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-RUN-EVAL

- Locator: `crew/skills/skill-creator/scripts/run_eval.py::skill evaluation case runner`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, invalid case input, unavailable child, timeout, output limit, or nonzero result fails evaluation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Evaluation child executable, argv, environment, case input, output limit, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-XLSX-RECALC

- Locator: `crew/skills/xlsx/scripts/recalc.py::spreadsheet recalculation child process`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, unavailable calculator, invalid workbook, timeout, or nonzero result fails recalculation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Calculator executable, argv, environment, workbook root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-XLSX-REDLINING

- Locator: `crew/skills/xlsx/scripts/office/validators/redlining.py::spreadsheet redlining validator child tools`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, unavailable child tool, invalid document, timeout, or nonzero validator result fails the skill operation
- Lifecycle/revocation owner: Managed terminal task and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Validator executable, argv, environment, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-DIRECT-SKILL-XLSX-SOFFICE

- Locator: `crew/skills/xlsx/scripts/office/soffice.py::LibreOffice lifecycle and local UNO bridge`
- Trust source: Bundled read-only skill root executed only as a managed terminal/native-runtime descendant
- Fail closed: Absent managed parent, unavailable LibreOffice, local bridge failure, timeout, or nonzero result fails conversion
- Lifecycle/revocation owner: Managed terminal task, LibreOffice profile, and native runtime descendant process tree
- Tests: `SKILL-PROCESS-INVENTORY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: LibreOffice executable, argv, profile, local socket, input/output root, timeout, or host fallback changes

### ACE-SURF-PROC-INDIRECT-ACP

- Locator: `crew/agent/external/acp_adapter.py::ACP bidirectional stdio adapter`
- Trust source: Registered absolute ACP runtime plus consumed immutable ProcessLaunch snapshot
- Fail closed: Managed mode without a native bidirectional transport, missing launch authority, stale identity, timeout, or cleanup failure rejects startup
- Lifecycle/revocation owner: External agent session and process lifecycle owner
- Tests: `ACP-LAUNCH-BOUNDARY` — `tests/security/test_acp_launch_boundary.py`
- Evidence: `pytest` — `tests/security/test_acp_launch_boundary.py`
- Artifact references: `pytest` — `tests/security/test_acp_launch_boundary.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: ACP transport, argv, environment, snapshot, permission callback, cancellation, or restart changes

### ACE-SURF-PROC-INDIRECT-BROWSER

- Locator: `crew/browser/manager.py::model-reachable BrowserManager commands`
- Trust source: Authenticated owner/session/task context, canonical tool arguments, and approved file/network capabilities
- Fail closed: Missing owner/task context, unavailable host, denied network action, stale file identity, invalid replay, and cleanup failure reject the command
- Lifecycle/revocation owner: BrowserManager owner/session/task lifecycle and Desktop BrowserHost
- Tests: `BROWSER-MANAGER-REVIEW` — `tests/test_browser_manager_review.py`<br>`BROWSER-USE` — `tests/test_browser_use.py`
- Evidence: `pytest` — `tests/test_browser_manager_review.py`
- Artifact references: `pytest` — `tests/test_browser_manager_review.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any Browser command, host RPC, network capability, upload source, download sink, replay effect, artifact, or cleanup change

### ACE-SURF-PROC-INDIRECT-CLI

- Locator: `crew/agent/external/cli_adapter.py::external CLI conversation and stdio adapter`
- Trust source: Registered absolute runtime, fixed protocol argv, and consumed ProcessLaunch snapshot
- Fail closed: Untrusted executable, protocol override, missing snapshot, managed unsupported transport, timeout, or output overflow rejects execution
- Lifecycle/revocation owner: External agent session and process lifecycle owner
- Tests: `EXTERNAL-CLI-BOUNDARY` — `tests/test_external_agents.py`
- Evidence: `pytest` — `tests/test_external_agents.py`
- Artifact references: `pytest` — `tests/test_external_agents.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: CLI runtime candidates, protocol argv, environment, probe authority, streaming transport, or cancellation changes

### ACE-SURF-PROC-INDIRECT-CODEX

- Locator: `crew/agent/external/codex_adapter.py::Codex app-server bidirectional stdio adapter`
- Trust source: Registered Codex runtime identity, fixed app-server protocol, and consumed ProcessLaunch snapshot
- Fail closed: Managed mode, absent app-server support, missing context, identity drift, timeout, or cleanup failure rejects startup without host fallback
- Lifecycle/revocation owner: External agent session and process lifecycle owner
- Tests: `CODEX-ADAPTER-BOUNDARY` — `tests/test_external_agents.py`
- Evidence: `pytest` — `tests/test_external_agents.py`
- Artifact references: `pytest` — `tests/test_external_agents.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Codex protocol, MCP injection, runtime detection, launch snapshot, streaming, or lifecycle changes

### ACE-SURF-PROC-INDIRECT-CRON

- Locator: `crew/cron/scheduler.py::scheduled agent and tool trigger`
- Trust source: Persisted owner-bound schedule and current tool security context
- Fail closed: Missing owner, revoked schedule, unavailable security context, denied tool action, or execution failure records a failed fire without host bypass
- Lifecycle/revocation owner: Cron job owner, scheduler shutdown, and invoked task lifecycle
- Tests: `CRON-SECURITY` — `tests/security/test_execution_surface_inventory.py`
- Evidence: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Artifact references: `static-analysis` — `tests/security/test_execution_surface_inventory.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Schedule source, owner binding, tool dispatch, retry, cancellation, or persistence changes

### ACE-SURF-PROC-INDIRECT-DETECTOR

- Locator: `crew/agent/external/detector.py::external runtime candidate discovery and fixed probes`
- Trust source: Operator environment, known Desktop bundle roots, and normalized absolute candidate identities
- Fail closed: Relative or missing executable, managed session, unsafe environment, timeout, output overflow, and identity mismatch mark the candidate unavailable
- Lifecycle/revocation owner: Runtime registry refresh and probe process lifecycle
- Tests: `EXTERNAL-RUNTIME-DETECTION` — `tests/test_external_agents.py`
- Evidence: `pytest` — `tests/test_external_agents.py`
- Artifact references: `pytest` — `tests/test_external_agents.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Runtime candidate roots, PATH use, probe argv, environment, timeout, or capability inference changes

### ACE-SURF-PROC-INDIRECT-MANAGED-TOOLS

- Locator: `crew/tools/managed_tools.py::ripgrep acquisition, verification, extraction, and fixed version probe`
- Trust source: Pinned release URL, platform tuple, expected SHA-256 digest, and private cache root
- Fail closed: Unknown platform, network denial, digest mismatch, unsafe archive entry, install race, or probe failure leaves the tool unavailable
- Lifecycle/revocation owner: Managed tool cache owner and product maintenance lifecycle
- Tests: `MANAGED-TOOL-INTEGRITY` — `tests/security/test_managed_tool_integrity.py`
- Evidence: `pytest` — `tests/security/test_managed_tool_integrity.py`
- Artifact references: `pytest` — `tests/security/test_managed_tool_integrity.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Tool source, version, digest, archive format, cache root, fixed probe, or managed-mode usage changes

### ACE-SURF-PROC-INDIRECT-MCP

- Locator: `crew/tools/mcp_client.py::outbound MCP stdio, HTTP, and SSE connection manager`
- Trust source: Administrator-approved MCP configuration, identifier-bound secret resolution, owner/task authorization, and pinned command or origin identity
- Fail closed: Stdio remains unavailable without managed transport; unsafe origin, private address, command drift, secret scope, timeout, or revocation closes the worker
- Lifecycle/revocation owner: MCPClientManager server worker plus owner/task SecurityService grants
- Tests: `MCP-LIFECYCLE` — `tests/test_mcp_client_lifecycle.py`<br>`MCP-COMMAND-INTEGRITY` — `tests/security/test_mcp_command_integrity.py`
- Evidence: `pytest` — `tests/test_mcp_client_lifecycle.py`
- Artifact references: `pytest` — `tests/test_mcp_client_lifecycle.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: MCP transport, SDK client, command, environment, secret field, origin, redirect, queue, timeout, or reconnect changes

### ACE-SURF-PROC-INDIRECT-SITES

- Locator: `crew/sites/manager.py::site build, preview, publish, export, and automation commands`
- Trust source: Authenticated owner/workspace context and canonical site blueprint
- Fail closed: Missing security context, unavailable native broker, invalid site root, failed build, or stale ownership rejects the operation without host build fallback
- Lifecycle/revocation owner: Site owner, automation task, and managed process lifecycle
- Tests: `SITES-SECURITY` — `tests/test_sites.py`
- Evidence: `pytest` — `tests/test_sites.py`
- Artifact references: `pytest` — `tests/test_sites.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Build command, package manager, site root, automation, preview, publish, export, or ProcessLaunch changes

### ACE-SURF-PROC-INDIRECT-TEAM

- Locator: `crew/team/team_manager.py::teammate task and shared tool dispatch`
- Trust source: Authenticated parent task, role assignment, workspace scope, and server-issued interaction binding
- Fail closed: Role, owner, workspace, interaction binding, or task lifecycle mismatch rejects dispatch and revokes pending authority
- Lifecycle/revocation owner: Parent team task, teammate runtime, and workspace interaction binding
- Tests: `TEAM-TASKS` — `tests/test_team_tasks.py`
- Evidence: `pytest` — `tests/test_team_tasks.py`
- Artifact references: `pytest` — `tests/test_team_tasks.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Team role, workspace binding, shared tool, internal MCP interaction, cancellation, or delegation changes

### ACE-SURF-PROC-INDIRECT-TERMINAL

- Locator: `crew/tools/builtin.py::terminal foreground/background and structured patch entrypoints`
- Trust source: Canonical model tool call, owner/task context, SecurityService decision, and signed authorization snapshot
- Fail closed: Missing context, denied approval, unavailable managed runtime, invalid snapshot, or file identity drift rejects the tool without host fallback
- Lifecycle/revocation owner: SecurityService turn/session lifecycle and process registry owner
- Tests: `EXECUTION-ROUTING` — `tests/security/test_execution_routing.py`<br>`FILE-POLICY` — `tests/security/test_file_policy.py`<br>`APPROVAL-CONTRACT` — `tests/security/test_security_approvals.py`
- Evidence: `pytest` — `tests/security/test_execution_routing.py`
- Artifact references: `pytest` — `tests/security/test_execution_routing.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Terminal, background, patch, file mutation, approval, ProcessLaunch, or model-reachable command entry changes

### ACE-SURF-PROC-INDIRECT-WIKI

- Locator: `crew/wiki/parser.py::Wiki ingest parser and delegated legacy Office conversion`
- Trust source: Authenticated owner/KB upload, private snapshot, bounded archive/parser input, and delegated ProcessLaunch authority
- Fail closed: Unauthorized or stale input, unsupported type, archive, byte, PDF page/object, converter scratch disk/entry, or parser output budget, missing broker authority, timeout, or output identity failure rejects ingest
- Lifecycle/revocation owner: Wiki ingest task, knowledge-base owner, and delegated process lifecycle
- Tests: `WIKI-PARSER` — `tests/wiki/test_parser.py`<br>`ARCHIVE-SECURITY` — `tests/security/test_archive_security.py`
- Evidence: `pytest` — `tests/wiki/test_parser.py`
- Artifact references: `pytest` — `tests/wiki/test_parser.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Parser type, archive handling, Office conversion, upload source, byte budget, output publication, or cancellation changes

### ACE-SURF-SKILL-ACTIVATION

- Locator: `crew/agent/skills.py::skill discovery and activation into managed tool execution`
- Trust source: Bundled read-only skill roots or declarative assets retained from an approved plugin package
- Fail closed: Escaping paths, malformed metadata, executable untrusted plugins, unavailable managed execution, and missing provenance reject activation or execution
- Lifecycle/revocation owner: Agent session skill registry, plugin uninstall lifecycle, and managed process owner
- Tests: `SKILL-BOUNDARY` — `tests/test_skills.py`<br>`LOCAL-SKILLS` — `tests/test_local_skills.py`
- Evidence: `pytest` — `tests/test_skills.py`
- Artifact references: `pytest` — `tests/test_skills.py`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Any new skill root, package source, activation route, executable hook, or host-side skill runner

### ACE-SURF-UPDATE-PIPELINE

- Locator: `desktop/src/main/update::signed update discovery, bounded download, file verification, and installer handoff`
- Trust source: Pinned HTTPS update source, Ed25519 signature trust root, platform/architecture metadata, and user install action
- Fail closed: Insecure URLs, redirect drift, signature or digest mismatch, wrong platform, unsafe package identity, and helper failure block installation
- Lifecycle/revocation owner: Desktop update controller and installer completion or cancellation lifecycle
- Tests: `UPDATE-INTEGRITY` — `desktop/tests/unit/update-integrity.test.ts`<br>`UPDATE-INSTALLER` — `desktop/tests/unit/update-installer.test.ts`<br>`UPDATE-FILE-SECURITY` — `desktop/tests/unit/update-file-security.test.ts`<br>`UPDATE-STATE` — `desktop/tests/unit/update-state.test.ts`<br>`UPDATE-DOWNLOAD-POLICY` — `desktop/tests/unit/update-download-policy.test.ts`
- Evidence: `node-test` — `desktop/tests/unit/update-installer.test.ts`<br>`node-test` — `desktop/tests/unit/update-download-policy.test.ts`
- Artifact references: `node-test` — `desktop/tests/unit/update-installer.test.ts`<br>`node-test` — `desktop/tests/unit/update-download-policy.test.ts`
- Reviewed primitive references: none
- Covered routes/channels: none
- Exception expiry: `none`
- Review deadline: `2027-12-31`
- Review trigger: Update origin, redirect policy, signature format, package type, download sink, installer helper, or IPC route changes
