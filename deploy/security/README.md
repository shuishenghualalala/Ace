# Ace native security runtime packaging

`ace-security-runtime` is a per-task helper shipped with Desktop. It never downloads a
sandbox dependency at runtime and never exposes its control protocol over TCP. Managed online
profiles use a loopback-only HTTP proxy whose destination policy remains inside the helper.

Linux packages should prefer a probed system `bwrap` outside the workspace. A bundled fallback
must set `ACE_BUNDLED_BWRAP` and a release-generated
`ACE_BUNDLED_BWRAP_SHA256`; the helper verifies the opened file and executes the retained
descriptor through `/proc/self/fd`. The package must record bubblewrap's license, target
architecture, source revision, digest, and signature provenance. WSL2 uses this Linux backend;
WSL1 is rejected for managed execution.

macOS packages build the same helper natively and stage it beside Desktop's runtime manifest.
Each managed command is launched through `/usr/bin/sandbox-exec` with a per-request Seatbelt
profile. User-controlled paths are passed as Seatbelt parameters rather than interpolated into the
profile. Files outside the explicit readable/writable roots remain unavailable, denied roots stay
closed beneath broader writable roots, and outbound sockets can reach only the exact loopback port
owned by the helper's managed HTTP proxy. macOS setup needs no privileged installer or persistent
system service.

Release CI must run Rust fmt, clippy, unit tests, and the native adversarial suite on real Linux,
Windows, and macOS hosts. A Python protocol test is not evidence that the OS sandbox works.

Release packaging runs `desktop/scripts/prepare-security-runtime.mjs` and then
`verify-security-runtime.mjs`. The generated manifest contains per-file SHA-256 and size; Desktop
refuses to pass a missing or mismatched helper to Gateway. Linux additionally ships a pinned bwrap
and passes its manifest digest through the desktop-managed Gateway environment. No release may use the empty
`deploy/security/runtime-manifest.json` template as evidence.

Windows install/repair/uninstall operations are documented in `windows-operations.md`. Formal
release remains blocked until all three real-runner evidence JSON files are supplied to
`scripts/check_release_readiness.py` through the documented environment variables.
The third variable, `ACE_SECURITY_PACKAGE_EVIDENCE`, must point to JSON confirming a
verified package signature (or equivalent administrator-protected distribution), a committed
Cargo lockfile check, and the manual Desktop approval walkthrough. Runtime and manifest hashes
alone do not defend against replacing both files in a user-writable installation directory.
Copy `package-evidence.example.json` for a release review and set fields to true only from recorded
evidence; the checked-in example deliberately remains blocked.
