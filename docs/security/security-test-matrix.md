# Native security release matrix

Mocked platform tests are development feedback only. A release is blocked until the corresponding
workflow has executed the real packaged helper on the target OS.

| ID | Boundary | Linux evidence | Windows evidence | macOS evidence | Release rule |
|---|---|---|---|---|---|
| SMX-FS-001 | workspace read/write | bwrap process writes only the mounted workspace | technical account + temporary ACL writes workspace | Seatbelt permits only declared writable roots | required |
| SMX-FS-002 | outside/protected read | unmounted/overlaid roots and symlink tests | technical account lacks ambient ACL and secret output is absent | Seatbelt denies outside content and protected roots | required |
| SMX-FS-003 | structured search link race | glob/grep 在静态 symlink 及 prune 后交换下不返回外部内容/metadata | glob/grep 在静态 junction、prune 后 junction 交换及 leaf swap 下不返回外部内容/metadata | vnode-resolved Seatbelt rules and structured-search race suite | required |
| SMX-PROC-001 | process tree/handles | PID namespace + timeout kill | Job kill-on-close + explicit handle list | helper process group is killed on timeout/cancellation | required |
| SMX-ACP-001 | managed external bidirectional stdio (ACP/Codex/Claude) | native runtime interactive session forwards protocol stdin/stdout and closes child tree | same through Windows runner | same through macOS Seatbelt runtime | required |
| SMX-ACP-002 | Crew interaction MCP callback | one-time binding token reaches proxy; only Gateway loopback host/port is allowed | same through Windows managed proxy | same through macOS managed proxy | required |
| SMX-ACP-003 | external security switch | `config/config.yaml: external_agents.security_enabled` defaults to `false`/legacy external runtime; `true` enables the external native managed boundary, while built-in conversation launch remains managed in both cases | same config value propagated on Gateway restart | same config value propagated on Gateway restart | required |
| SMX-NET-001 | offline direct connect | isolated net namespace | offline technical-account WFP block | Seatbelt has no outbound allow rule | required |
| SMX-NET-002 | allowed destination | Unix-socket proxy bridge, DNS pinning | online account may reach fixed loopback proxy only | exact loopback proxy port + DNS pinning | required |
| SMX-STATE-001 | native security state | N/A | state parent、identity/tmp/bak、ACL/capability/recovery 对其他用户及 sandbox 账号不可读写；junction/hardlink/repair 失败关闭 | N/A：无持久特权账号或防火墙 state | required on Windows |
| SMX-ID-001 | owner/grant separation | Python owner/session lifecycle suite | Python owner/session lifecycle suite | Python owner/session lifecycle suite | required |
| SMX-ID-002 | mode/session authority lifecycle | mode change revokes pending and stops old turn but preserves SESSION grant until true session end | same Gateway/Desktop contract; mode UI commits only after authenticated ACK | same Gateway/Desktop contract | required |
| SMX-CRASH-001 | crash cleanup | helper/process tree exits and mounts disappear | Job closes; stale ACL manifest repair removes only Ace ACE | process group exits and private temporary home is removed | required |
| SMX-LOG-001 | secret output/audit | bounded output and redacted audit | bounded output and redacted audit | bounded output and redacted audit | required |

## Release evidence closure

This gate closes REL-001, SUP-012, TEST-007, TEST-013, TEST-014, TEST-015, and
ACE-020. The GitHub `security-linux`, `security-windows`, and `security-macos`
checks are the canonical native evidence producers. Their artifact and log URLs
must be attached to a release decision; this document does not claim that the
current checkout has passed them.

Each native workflow builds and tests with `--release --locked`, then gives the
same runtime to Desktop staging with `--source-root security-runtime`.
`scripts/write_security_runtime_evidence.py` refuses a dirty tracked/untracked
checkout, a commit or repository mismatch, a platform/target mismatch, a
manifest mismatch, or changed staged bytes. A native evidence JSON binds the
full commit, origin repository, Actions run, platform, target triple,
`workflow_ref`, `runner_os`, `runner_arch`, `source_hash`,
`cargo_lock_sha256`, `runtime_manifest_sha256`, `artifact_sha256`, and
`desktop_staged_artifact_sha256`. It also binds the deterministic CycloneDX
SBOM through `sbom_sha256`; that SBOM includes the attested runtime-manifest files
(including Linux `bwrap` and its license) plus every committed lock dependency.
The Linux `bwrap` component carries its distribution package URL/version, and the native workflow
scans the assembled SBOM against the blocking `HIGH,CRITICAL` vulnerability threshold. The evidence
also records the frozen-lock, vulnerability-threshold, and secret-scan policies.
Debug-runtime results and hand-written JSON are not release evidence.

`scripts/check_release_readiness.py --security-release` derives HEAD and the origin repository
from Git, requires Linux, Windows, and macOS evidence for that exact checkout,
and blocks modified files, untracked files, local test directories, and ignored
security runtime artifacts. The tested and Desktop-staged digests must be
equal. All three native evidence files must then be bound by raw
`runtime_evidence_sha256` values and matching target/artifact/staged/manifest
records in one package evidence manifest.

The package evidence manifest must also bind the real Linux, Windows, and macOS
package files through `packages.<platform>.sha256`, this checkout's source and
Cargo lock digests, each platform's traceable signature verification, and
`desktop_walkthroughs` for every platform. `update_trust_root` and `update_source`
must additionally bind one Ed25519 public-key digest and one fixed HTTPS update
directory, and prove both were embedded in all three packages. Set
`ACE_SECURITY_PACKAGE_EVIDENCE` to that manifest and
`ACE_SECURITY_PACKAGE_ATTESTATION` to the GitHub artifact-attestation bundle
for the manifest. The committed `deploy/security/package-signing-policy.json`
must pin that signer workflow, the embedded update-key digest, fixed update source, and exact
per-platform signature identities and issuers. Environment variables cannot
replace this trust policy. Release readiness verifies the bundle with
`gh attestation verify`, pins the signer workflow and source commit, and
refuses self-hosted attestation signers. The attestation and package signature
require an external release CI or signing runner; they must not be manufactured locally.
`.github/workflows/security-release-gate.yml` downloads the three native workflow runs and one
package-signing run by explicit run ID, then executes this closure. A publishing workflow must call
that reusable gate as a required job; a standalone manual dispatch is not release authorization.
On `workflow_dispatch`, each native security workflow attests its evidence JSON,
runtime manifest, and SBOM in a least-privilege follow-up job. Those native attestations do not
replace the independent Linux package trust, Windows Authenticode, Apple code
signing/notarization, or the separately attested package evidence.

Evidence and package inputs should be downloaded outside the source checkout.
Only a clean aggregation checkout can become `ready`; the current dirty
development checkout is expected to remain `blocked`.
