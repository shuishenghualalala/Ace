# Ace native security runtime packaging

`ace-security-runtime` is a per-task helper shipped with Desktop. It never downloads a
sandbox dependency at runtime and never opens a TCP listener.

Linux packages should prefer a probed system `bwrap` outside the workspace. A bundled fallback
must set `ACE_BUNDLED_BWRAP` and a release-generated
`ACE_BUNDLED_BWRAP_SHA256`; the helper verifies the opened file and executes the retained
descriptor through `/proc/self/fd`. The package must record bubblewrap's license, target
architecture, source revision, digest, and signature provenance. WSL2 uses this Linux backend;
WSL1 is rejected for managed execution.

Release CI must run Rust fmt, clippy, unit tests, and the Linux adversarial suite on a real Linux
host. A Python protocol test is not evidence that the OS sandbox works.

Release packaging runs `desktop/scripts/prepare-security-runtime.mjs` and then
`verify-security-runtime.mjs`. The generated manifest contains per-file SHA-256 and size; Desktop
refuses to pass a missing or mismatched helper to Gateway. Linux additionally ships a pinned bwrap
and passes its manifest digest through the desktop-managed Gateway environment. No release may use the empty
`deploy/security/runtime-manifest.json` template as evidence.

Windows install/repair/uninstall operations are documented in `windows-operations.md`. Formal
release remains blocked until both real-runner evidence JSON files are supplied to
`scripts/check_release_readiness.py` through the documented environment variables.
The third variable, `ACE_SECURITY_PACKAGE_EVIDENCE`, must point to JSON confirming a
verified package signature (or equivalent administrator-protected distribution), a committed
Cargo lockfile check, and the manual Desktop approval walkthrough. Runtime and manifest hashes
alone do not defend against replacing both files in a user-writable installation directory.
Copy `package-evidence.example.json` for a release review and set fields to true only from recorded
evidence; the checked-in example deliberately remains blocked.
