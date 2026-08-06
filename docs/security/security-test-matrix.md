# Native security release matrix

Mocked platform tests are development feedback only. A release is blocked until the corresponding
workflow has executed the real packaged helper on the target OS.

| ID | Boundary | Linux evidence | Windows evidence | Release rule |
|---|---|---|---|---|
| SMX-FS-001 | workspace read/write | bwrap process writes only the mounted workspace | technical account + temporary ACL writes workspace | required |
| SMX-FS-002 | outside/protected read | unmounted/overlaid roots and symlink tests | technical account lacks ambient ACL and secret output is absent | required |
| SMX-FS-003 | structured search link race | glob/grep 在静态 symlink 及 prune 后交换下不返回外部内容/metadata | glob/grep 在静态 junction、prune 后 junction 交换及 leaf swap 下不返回外部内容/metadata | required |
| SMX-PROC-001 | process tree/handles | PID namespace + timeout kill | Job kill-on-close + explicit handle list | required |
| SMX-NET-001 | offline direct connect | isolated net namespace | offline technical-account WFP block | required |
| SMX-NET-002 | allowed destination | Unix-socket proxy bridge, DNS pinning | online account may reach fixed loopback proxy only | required |
| SMX-STATE-001 | native security state | N/A：Linux 不持有 Windows 技术账号 state | state parent、identity/tmp/bak、ACL/capability/recovery 对其他用户及 sandbox 账号不可读写；junction/hardlink/repair 失败关闭 | required on Windows |
| SMX-ID-001 | owner/grant separation | Python owner/session lifecycle suite | Python owner/session lifecycle suite | required |
| SMX-ID-002 | mode/session authority lifecycle | mode change revokes pending and stops old turn but preserves SESSION grant until true session end | same Gateway/Desktop contract; mode UI commits only after authenticated ACK | required |
| SMX-CRASH-001 | crash cleanup | helper/process tree exits and mounts disappear | Job closes; stale ACL manifest repair removes only Ace ACE | required |
| SMX-LOG-001 | secret output/audit | bounded output and redacted audit | bounded output and redacted audit | required |

The GitHub `security-linux` and `security-windows` checks are the canonical evidence producers.
Their artifact/log URLs must be attached to a release decision; this document does not claim that
the current checkout has passed them. Both workflows build with `--release --locked`; the native
matrix and Desktop staging consume that same path. `scripts/write_security_runtime_evidence.py`
then rejects a changed staged copy and records repository, commit, workflow run, target triple,
artifact/source/Cargo.lock/manifest hashes. Debug-runtime results are not release evidence.

Release readiness consumes the two workflow evidence files plus a package evidence JSON. The
package evidence must attest `package_signature_verified`, `cargo_lock_verified`, and
`desktop_walkthrough`; all three are mandatory booleans, not free-form reviewer notes.
