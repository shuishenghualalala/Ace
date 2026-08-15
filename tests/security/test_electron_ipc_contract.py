"""Electron IPC registration must stay allowlisted, authenticated, and bounded."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "desktop" / "src" / "main" / "index.ts"
PRELOAD = ROOT / "desktop" / "src" / "main" / "preload.ts"
CHANNELS = ROOT / "desktop" / "src" / "shared" / "ipc-channels.ts"
MAIN_PROCESS_SOURCE = ROOT / "desktop" / "src" / "main"
UPDATE_CONTROLLER = MAIN_PROCESS_SOURCE / "update" / "download-controller.ts"
UPDATE_FILE_SECURITY = MAIN_PROCESS_SOURCE / "update" / "update-file-security.ts"
UPDATE_INTEGRITY = MAIN_PROCESS_SOURCE / "update" / "update-integrity.ts"
UPDATE_INSTALLER = MAIN_PROCESS_SOURCE / "update" / "update-installer.ts"
UPDATE_STATE = MAIN_PROCESS_SOURCE / "update" / "update-state.ts"
UPDATE_URL = MAIN_PROCESS_SOURCE / "update" / "update-url.ts"
DESKTOP_BUILD = ROOT / "desktop" / "esbuild.config.mjs"
REQUEST_CLIENT = MAIN_PROCESS_SOURCE / "request.ts"
VERSION_UPDATE_UI = ROOT / "desktop" / "src" / "ui" / "features" / "version-update.ts"


def _channels(source: str, call: str) -> set[str]:
    return set(re.findall(rf"\b{re.escape(call)}\(\s*['\"]([^'\"]+)['\"]", source))


def test_invoke_channels_are_closed_over_main_and_preload() -> None:
    contract = CHANNELS.read_text(encoding="utf-8")
    main = MAIN.read_text(encoding="utf-8")
    preload = PRELOAD.read_text(encoding="utf-8")
    declared_block = contract.split(
        "export const IPC_INVOKE_CHANNELS = [", 1
    )[1].split("] as const", 1)[0]
    declared = set(re.findall(r"['\"]([^'\"]+)['\"]", declared_block))

    assert declared
    assert _channels(main, "trustedHandle") == declared
    assert _channels(preload, "ipcRenderer.invoke") == declared
    assert preload.count("rawIpcRenderer.invoke(") == 1
    assert "ipcMain.handle(" not in main


def test_every_renderer_to_main_event_uses_a_trusted_wrapper() -> None:
    main = MAIN.read_text(encoding="utf-8")
    sticky_preload = (
        ROOT / "desktop" / "src" / "main" / "inspiration-sticky-preload.ts"
    ).read_text(encoding="utf-8")

    assert "ipcMain.on(" not in main
    assert _channels(main, "trustedOn") == _channels(sticky_preload, "sendMain")
    assert "ipcRenderer.send(" not in sticky_preload
    assert "assertTrustedInspirationRenderer" in main
    assert "isIpcRendererToMainEventChannel(channel)" in main
    assert "isIpcRendererToMainEventChannel(channel)" in sticky_preload


def test_main_to_renderer_event_channels_are_closed_over_main_and_preload() -> None:
    contract = CHANNELS.read_text(encoding="utf-8")
    main = MAIN.read_text(encoding="utf-8")
    preload = PRELOAD.read_text(encoding="utf-8")
    declared_block = contract.split(
        "export const IPC_MAIN_TO_RENDERER_EVENT_CHANNELS = [", 1
    )[1].split("] as const", 1)[0]
    declared = set(re.findall(r"['\"]([^'\"]+)['\"]", declared_block))
    sent = _channels(main, "webContents.send") | _channels(
        main, "event.sender.send"
    )

    assert declared
    assert _channels(preload, "ipcRenderer.on") == declared
    assert sent == declared
    assert preload.count("rawIpcRenderer.on(") == 1
    assert "isIpcMainToRendererEventChannel(channel)" in preload


def test_trusted_ipc_wrapper_authenticates_and_bounds_before_dispatch() -> None:
    main = MAIN.read_text(encoding="utf-8")
    wrapper = main.split("function trustedHandle(", 1)[1].split("\n}", 1)[0]

    sender_check = wrapper.index("assertTrustedRenderer(event)")
    size_check = wrapper.index("assertIpcPayloadSize(args)")
    dispatch = wrapper.index("listener(event, ...args)")
    assert sender_check < dispatch
    assert size_check < dispatch
    assert "MAX_TRUSTED_IPC_PAYLOAD_BYTES" in main


def test_audit_ipc_forwards_every_validated_scope_filter() -> None:
    main = MAIN.read_text(encoding="utf-8")
    handler = main.split("trustedHandle('security:audit',", 1)[1].split(
        "trustedHandle('security:audit-export',", 1
    )[0]

    for renderer_name, gateway_name in {
        "actionType": "action_type",
        "sessionId": "session_id",
        "workspaceId": "workspace_id",
        "taskId": "task_id",
        "startTime": "start_time",
        "endTime": "end_time",
    }.items():
        assert f"args.{renderer_name}" in handler
        assert f"query.set('{gateway_name}'" in handler


def test_updates_require_an_embedded_trust_root_in_every_security_mode() -> None:
    main = MAIN.read_text(encoding="utf-8")
    controller = UPDATE_CONTROLLER.read_text(encoding="utf-8")
    file_security = UPDATE_FILE_SECURITY.read_text(encoding="utf-8")
    integrity = UPDATE_INTEGRITY.read_text(encoding="utf-8")
    installer = UPDATE_INSTALLER.read_text(encoding="utf-8")
    state = UPDATE_STATE.read_text(encoding="utf-8")
    update_url = UPDATE_URL.read_text(encoding="utf-8")
    build = DESKTOP_BUILD.read_text(encoding="utf-8")

    assert "process.env['ACE_UPDATE_PUBLIC_KEY']" not in integrity
    assert "__ACE_UPDATE_PUBLIC_KEY__" in integrity
    assert "canonicalUpdateSignaturePayload" in integrity
    assert "envelope.version !== normalizedVersion" in integrity
    assert "envelope.package_sha256 !== packageSha256" in integrity
    assert "envelope.package_size !== packageFile.identity.size" in integrity
    assert "openVerifiedUpdateArtifact" in integrity
    assert "lease.revalidate()" in installer
    assert "__ACE_UPDATE_PUBLIC_KEY__" in build
    assert "Production desktop builds require ACE_UPDATE_PUBLIC_KEY" in build
    assert "asymmetricKeyType !== 'ed25519'" in build
    assert "ACE_DOWNLOAD_BASE_URL" in build
    assert "__ACE_DOWNLOAD_BASE_URL__" in build
    assert "process.env['ACE_DOWNLOAD_BASE_URL']" not in update_url
    assert "isExpectedUpdateUrl(args.url" in controller
    assert "MAX_UPDATE_REDIRECTS" in controller
    assert "redirect.origin !== expectedOrigin" in controller
    assert "redirect: 'error'" in controller
    assert "signal: AbortSignal.timeout(NO_PROGRESS_TIMEOUT_MS)" in controller
    assert "MAX_DOWNLOAD_DURATION_MS" in controller
    assert "for await (const value of response)" in controller
    assert "openSecureResumeFile(" in controller
    assert "publishOpenFileExclusive(" in controller
    assert "createWriteStream(" not in controller
    assert "strictSecurityEnabled()" not in controller
    assert "await downloadAndVerifySignature" in controller
    assert "signatureMetadata: artifact.metadata" in controller
    assert "O_NOFOLLOW" in file_security
    assert "O_EXCL" in file_security
    assert "info.nlink !== allowedLinkCount" in file_security
    assert "fs.renameSync(temporaryFile, file)" in state
    assert "createSecureExclusiveFile(temporaryFile)" in state
    assert "launchVerifiedDownloadedUpdate" in main
    assert "TRUSTED_UPDATE_HELPERS" in installer
    assert "shell.openPath" not in installer
    assert "'sh'" not in installer


def test_update_and_request_logs_do_not_emit_remote_payloads_or_query_secrets() -> None:
    request = REQUEST_CLIENT.read_text(encoding="utf-8")
    update_ui = VERSION_UPDATE_UI.read_text(encoding="utf-8")
    controller = UPDATE_CONTROLLER.read_text(encoding="utf-8")

    assert "'Body:', result" not in request
    assert "urlObj?.toString()" not in request
    assert "'[VersionUpdate] triggerDownload:', mode, args" not in update_ui
    assert "'[VersionUpdate] handleVersionUpdate:', payload" not in update_ui
    assert "(err as any)?.cause" not in controller
    assert "(err as Error)?.stack" not in controller


def _balanced_call(source: str, start: int) -> str:
    opening = source.index("(", start)
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("unterminated child process call")


def test_every_electron_child_process_gets_hardened_options() -> None:
    child_files = [
        path
        for path in MAIN_PROCESS_SOURCE.rglob("*.ts")
        if "from 'child_process'" in path.read_text(encoding="utf-8")
    ]
    assert child_files
    for path in child_files:
        source = path.read_text(encoding="utf-8")
        imports = re.findall(
            r"import\s*\{(?P<bindings>[^}]*)\}\s*from 'child_process'",
            source,
        )
        assert len(imports) == 1
        process_functions: list[str] = []
        for binding in imports[0].split(","):
            item = binding.strip()
            if not item or item.startswith("type "):
                continue
            original, _, alias = item.partition(" as ")
            assert original in {"spawn", "spawnSync"}, (
                f"{path.relative_to(ROOT)} imports forbidden child process API {original}"
            )
            process_functions.append(alias or original)
        assert process_functions
        call_pattern = r"\b(?:" + "|".join(map(re.escape, process_functions)) + r")\s*\("
        for match in re.finditer(call_pattern, source):
            call = _balanced_call(source, match.start())
            assert "hardenedChildProcessOptions(" in call, (
                f"{path.relative_to(ROOT)} has an unhardened child process call"
            )
