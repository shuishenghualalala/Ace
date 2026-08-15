# Ace production threat model and security responsibility matrix

This document is the security ownership contract for Ace's production surfaces. It complements the
execution-surface inventory: the inventory answers *where execution can happen*; this document
answers *who can attack it, which assets cross each trust boundary, which layer enforces the
decision, and what must happen when that layer cannot prove safety*.

No production security boundary may rely on a prompt, model behavior, UI convention, or caller
discipline. Those mechanisms may explain risk, but authorization and enforcement are code and
platform responsibilities.

## Scope and assumptions

Ace treats model output and all externally sourced content as hostile data. The supported security
claim covers attacks from remote clients, malicious content, plugins, dependencies, and local
processes that do not already control the user's OS account or kernel. A fully privileged local
administrator, compromised kernel, compromised firmware, or stolen unlocked user session is outside
the isolation guarantee; release evidence must state that boundary instead of implying resistance.
Platform security claims remain blocked until the corresponding real Linux, macOS, or Windows
runner evidence is present.

## Protected assets

- **credentials and authentication state**: provider keys, session tokens, Gateway instance keys,
  approval grants, snapshot signing keys, and browser authentication material.
- **workspace and host data**: authorized workspace files, protected Ace metadata, user files
  outside approved roots, temporary files, uploads, downloads, and archives.
- **execution authority**: command arguments, environment, current directory, helper identity,
  filesystem roots, network rules, process handles, and installer/update authority.
- **owner-isolated task data**: conversations, tool results, MCP data, process output, replay state,
  browser state, and artifacts belonging to one owner, session, task, or turn.
- **security policy and audit evidence**: rules, approvals, revocations, append-only audit state,
  checkpoints, native-test evidence, provenance, attestations, and release decisions.
- **runtime and release artifacts**: source, lockfiles, native runtime helpers, manifests,
  prebuilt binaries, Desktop packages, signatures, SBOMs, and update payloads.

## Threat actors

| ID | Actor or failure source | Capability | Required treatment |
|---|---|---|---|
| TA-01 | Compromised or prompt-injected model | Produces arbitrary commands, paths, URLs, tool arguments, and persuasive text | Treat every value as untrusted input; authorize a canonical action independently of model text |
| TA-02 | Remote unauthenticated or low-privilege client | Sends malformed, replayed, oversized, cross-owner, or state-confusing HTTP, WebSocket, and IPC messages | Authenticate first, validate strict schemas and quotas, bind owner/session/task, and deny replay |
| TA-03 | Malicious web, MCP, file, archive, or browser content provider | Returns active content, redirects, hostile filenames, prompt injection, private-network destinations, and decompression bombs | Keep content as data; apply file and network policy again at the final I/O boundary |
| TA-04 | Hostile process running as the same OS user | Races files, replaces helpers, reads weakly protected state, guesses sockets, reuses PIDs, and tampers with ambient environment | Use identity-checked I/O, protected state, signed snapshots, process identity, native isolation, and unguessable authenticated channels |
| TA-05 | Different local user or untrusted desktop session | Attempts to read or modify Ace state, connect to local services, or inherit handles and ACLs | Use owner-only permissions or protected DACLs, explicit handle inheritance, authenticated loopback protocols, and per-owner isolation |
| TA-06 | Malicious or compromised plugin, skill, hook, or extension | Supplies code, manifests, hook decisions, capabilities, and subprocess behavior | Require provenance and strict manifests; run code only as a sandbox descendant; fail closed on malformed hooks |
| TA-07 | Compromised dependency, build runner, mirror, package, or update source | Replaces source, lockfiles, helper binaries, manifests, signatures, evidence, or delivered packages | Pin dependencies, verify independent trust roots and digests, bind evidence to commit/runner/repository, and block release |
| TA-08 | Resource-exhaustion input or partial platform failure | Exhausts processes, memory, disk, files, sockets, replay tables, output buffers, timeouts, or cleanup paths | Enforce bounded quotas and lifecycle cleanup; capacity and cleanup failures are explicit denials |

## Trust boundaries

Each row names both implementation evidence and the remaining risk. “Residual risk” is never a
silent allowance: if its required evidence is absent, the affected production capability or release
stays blocked.

| ID | Boundary | Hostile input or actor | Assets at risk | Security owner | Production enforcement entry | Primary controls | Negative/security evidence | Failure behavior | Residual risk or required external evidence | Review state |
|---|---|---|---|---|---|---|---|---|---|---|
| TB-01 | Model and retrieved content → tool action | TA-01, TA-03 | execution authority; workspace and host data; credentials | policy decision | `crew/security/service.py` | canonical actions, grants, approval recheck, plugin/hook validation | `tests/security/test_file_policy.py`; `tests/security/test_terminal_approval.py` | fail-closed on unknown action, missing context, malformed scope, unavailable approval UI, timeout, or changed action | End-to-end matrices must continue proving that every newly added tool enters the same decision layer | reviewed-automated |
| TB-02 | Web/Desktop client → Gateway HTTP and WebSocket | TA-02, TA-05 | owner-isolated task data; credentials and authentication state | identity and tenancy | `crew/gateway/app.py`; `crew/gateway/ws.py` | authentication, admin RBAC, protocol identity, replay checks, quotas, owner/session/task binding, denial audit | `tests/gateway/test_ipc_boundary_hardening.py`; `tests/gateway/test_auth_contract.py`; `tests/gateway/test_account_isolation.py` | fail-closed before dispatch on authentication, schema, quota, sequence, nonce, owner, lifecycle mismatch, or audit-write failure | Multi-worker and real disconnect/restart evidence is required for deployment topologies that use more than one Gateway worker | reviewed-automated |
| TB-03 | Renderer or browser content → Electron main/preload IPC | TA-02, TA-03 | execution authority; browser authentication material; runtime and release artifacts | identity and tenancy | `desktop/src/main/index.ts`; `desktop/src/shared/ipc-channels.ts` | sandboxed renderers, context isolation, narrow preload APIs, channel/sender validation, navigation/download/permission policy | `tests/security/test_electron_ipc_contract.py`; `desktop/tests/unit/browser-host.test.ts` | fail-closed on unknown channel, untrusted sender/origin, oversized payload, invalid navigation, permission, download, or bootstrap hardening failure | Packaged-app tests and platform code-signing/notarization evidence are required; renderer sandbox claims cannot rely only on unit tests | reviewed-automated |
| TB-04 | Policy decision → signed snapshot → broker/native runtime | TA-01, TA-04, TA-08 | execution authority; security policy and audit evidence | process enforcement | `crew/security/launch.py`; `security-runtime/src` | immutable snapshots, HMAC and one-time nonce, helper/source digest, strict bridge protocol, native sandbox | `tests/security/test_runtime_client.py`; `tests/security/test_main_process_hardening.py` | fail-closed on missing key/state, replay, unknown field, digest or identity mismatch, unsupported capability, helper failure, or quota exhaustion | Linux, macOS, and Windows native runners must prove platform-specific enforcement and cleanup behavior | reviewed-automated |
| TB-05 | Authorized file operation → host filesystem object | TA-01, TA-03, TA-04 | workspace and host data; security policy and audit evidence | file enforcement | `crew/tools/file_utils.py` | canonical roots, pinned parents, identity-checked I/O, symlink/reparse/special-file rejection, atomic private writes | `tests/security/test_file_races.py`; `tests/security/test_attachment_security.py` | fail-closed on traversal, link/reparse, identity or digest change, unsupported object, size/depth/count limit, or atomic-write failure | Cross-process races and filesystem-specific behavior still require real POSIX and Windows filesystem test evidence | reviewed-automated |
| TB-06 | Web, MCP, Browser, or tool request → outbound connection | TA-01, TA-03, TA-08 | credentials; owner-isolated task data; network identity | network enforcement | `crew/security/outbound.py` | one canonical network decision, deny precedence, destination normalization, DNS/IP pinning, redirect re-authorization, managed proxy, budgets | `tests/security/test_outbound_policy.py`; `tests/security/test_parser_fuzz_gate.py` | fail-closed on missing policy, private/reserved destination, DNS change, Host/SNI/CONNECT mismatch, redirect/retry budget, proxy bypass, or bridge failure | Real network namespace, Seatbelt, WFP, TLS, DNS-rebinding, and proxy lifecycle tests are required on their owning platforms | reviewed-automated |
| TB-07 | Plugin/dependency/update source → loaded or installed artifact | TA-06, TA-07 | runtime and release artifacts; execution authority; credentials | plugin and content trust | `crew/plugins/manager.py`; `desktop/src/main/update/update-integrity.ts` | strict manifests, declared capabilities, digest/signature/provenance verification, developer-mode separation, locked dependencies, release gates | `tests/security/test_plugin_execution_boundary.py`; `tests/security/test_managed_tool_integrity.py` | fail-closed on unsigned remote material, unknown capability, trust-root or digest mismatch, vulnerable/secret-bearing package, or non-atomic update | Production signing keys, trusted distribution roots, notarization, and repository-host attestation must be provisioned outside the source tree | reviewed-automated |
| TB-08 | Running process and build → recovery, audit, and release decision | TA-04, TA-07, TA-08 | security policy and audit evidence; runtime and release artifacts; owner-isolated task data | audit and recovery | `crew/security/audit.py`; `crew/security/process_lifecycle.py` | HMAC checkpoints, PID creation-time/executable/owner identity, tamper-evident bounded audit, commit-bound evidence gates | `tests/security/test_security_audit.py`; `tests/security/test_audit_grant_race.py` | fail-closed on unsigned legacy checkpoint, PID reuse, owner mismatch, audit write/rotation failure, dirty tree, stale/incomplete evidence, or failed security test | Trusted CI runners, protected artifact storage, retention policy, external attestation, and incident-review procedures are deployment responsibilities | reviewed-automated |


`reviewed-automated` means this row is machine-checked for owner, production source, and executable negative/security evidence in the current checkout. It is not human signoff, clean-checkout release approval, or real-platform runner evidence.

## Security responsibility matrix

“Security owner” is an accountable code layer, not the caller that happens to use it. A tool cannot
override that layer. Any new production surface must be added here and to
`execution-surface-inventory.md` before release.

| Layer | Security owner | Canonical implementation | Required evidence | Escalation and deny rule |
|---|---|---|---|---|
| policy decision | Security service and policy maintainers | `crew/security/service.py`, `policy.py`, `grants.py`, `approvals.py`, `rules.py` | policy, approval, grant race, headless, and audit tests | Unknown actions or unavailable decision dependencies are denied; callers cannot synthesize allow |
| process enforcement | Execution broker and native runtime maintainers | `crew/security/snapshot.py`, `crew/security/launch.py`, `crew/security/broker.py`, `crew/security/runtime_client.py`, `crew/tools/process_registry.py`, `security-runtime/src` | snapshot/bridge/runtime/process tests plus three native runners | No helper, capability, identity, quota, or cleanup proof means no managed spawn |
| file enforcement | File capability maintainers | `crew/tools/security_guard.py`, `crew/tools/file_utils.py`, `crew/tools/file_tools.py`, archive and attachment boundaries | policy, race, symlink/reparse, archive, resource-budget, and platform filesystem tests | No stable object identity or bounded operation means no read, write, search, upload, or extraction |
| network enforcement | Network policy and managed proxy maintainers | canonical network policy, client adapters, Browser boundary, native proxy bridge | normalization, SSRF, DNS rebinding, redirect, TLS/CONNECT, bypass, quota, and platform proxy tests | Clients never fall back to ambient proxies or direct sockets when authorization or enforcement is unavailable |
| identity and tenancy | Gateway authentication and protocol maintainers | Gateway auth, instance proof, RBAC, connection/session/task registries | cross-owner, replay, revocation, disconnect, restart, DACL, and protocol tests | Authentication is not authorization; owner/admin/session/task mismatch is denied before data access or mutation |
| plugin and content trust | Plugin, skill, hook, and Browser maintainers | plugin manager, skill discovery, hook validation, browser security and host IPC | signature/provenance, strict manifest, capability, hook, navigation, upload, and sandbox-descendant tests | Remote unsigned code, model-directed installation, malformed hook authority, and host subprocess escape are denied |
| audit and recovery | Security audit and lifecycle maintainers | audit chain/rotation/redaction, signed checkpoints, process identity and cleanup | corruption, truncation, concurrent write, disk failure, redaction, PID reuse, and crash-recovery tests | Missing durable audit for a sensitive transition or unverifiable recovery state blocks the transition and never kills an unverified PID |
| build and release | Release security maintainers and protected CI | lockfiles, runtime manifests, evidence writers, workflow gates, package verification | clean checkout, SBOM, vulnerability/secret scan, source/runtime/package digest, native evidence, signature and attestation | Dirty, stale, unsigned, unattested, vulnerable, secret-bearing, or platform-incomplete artifacts cannot be released |

## Review and change triggers

The threat model and both inventories must be reviewed whenever a change adds a process creation
site, file ingestion/extraction path, outbound client, IPC or Gateway route, plugin/update source,
credential store, recovery state, native capability, build runner, or release artifact. Review must:

1. assign the surface to one trust boundary and one security owner;
2. add hostile-input, fail-closed, resource-limit, and cleanup tests;
3. identify whether a real-platform or external trust-root artifact is required;
4. update the capability baseline only after implementation and evidence agree; and
5. preserve unresolved deployment evidence as a release blocker rather than marking it conforming.
