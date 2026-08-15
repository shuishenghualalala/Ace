# Ace native security runtime packaging

`ace-security-runtime` is a per-task helper shipped with Desktop. It never downloads a
sandbox dependency at runtime and never exposes its control protocol over TCP. Managed online
profiles use a loopback-only HTTP proxy whose destination policy remains inside the helper.

Linux packages should prefer a probed system `bwrap` outside the workspace. A bundled fallback
must be co-located with the runtime and named in its release-generated manifest. Gateway derives
`ACE_BUNDLED_BWRAP` and `ACE_BUNDLED_BWRAP_SHA256` only from that verified manifest; caller
environment values are ignored. The helper verifies the opened file and executes the retained
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
whose path and digest are derived from the verified runtime manifest. No release may use the empty
`deploy/security/runtime-manifest.json` template as evidence.

The manifest and adjacent hashes are identities, not trust roots. A process that can replace both
the runtime and manifest can manufacture matching hashes. Release acceptance therefore requires an
externally verifiable GitHub artifact attestation for each native evidence file and its CycloneDX
SBOM, plus an independently verified package signature. The release gate never accepts a local
signature claim or a binary that merely signs itself with material shipped beside it.

## Native release evidence

Run each `security-{linux,windows,macos}.yml` workflow with `workflow_dispatch`. Pull-request runs
exercise the same tests but intentionally do not receive release attestations. Each dispatch must
finish on its declared GitHub-hosted native runner and produce four files:

- `security-<platform>-evidence.json`
- `security-<platform>-evidence.sigstore.json`
- `security-<platform>-sbom.cdx.json`
- `runtime-manifest.json`

All three workflows pin action commits and Rust, uv, Python, and Node versions; install npm
dependencies with `npm ci --ignore-scripts`; use frozen Python/Rust/npm locks; run Electron
IPC/process hardening tests; and generate the lock-bound SBOM. Before any repository dependency is
installed or executed, the Linux workflow performs the repository-wide Trivy gate for
`HIGH,CRITICAL` vulnerabilities, secret findings, and misconfiguration. The release gate requires
Linux evidence from the same commit as Windows and macOS, so that source scan cannot be omitted.
After packaging, Linux scans the generated runtime SBOM again; the bundled `bubblewrap` component
has a Debian package URL and version so its known vulnerabilities participate in the same threshold.
The evidence writer refuses dirty source checkouts and binds the tested runtime, Desktop-staged
runtime, manifest, Cargo lock, source tree, SBOM, workflow identity, runner OS, and runner
architecture. The CycloneDX document includes each staged manifest file, including Linux `bwrap`
and its license, as well as committed lock dependencies. `native-evidence.schema.json` is the
machine-readable contract.

Download the three platform bundles outside the source checkout. Keep each attestation beside its
evidence file under the names above. The release gate verifies the evidence JSON, runtime manifest,
and SBOM
against the same GitHub attestation, repository, workflow identity, source commit, and hosted-runner
policy. A skipped native test, missing runner, missing SBOM, missing attestation, or unverifiable
bundle blocks release.

## Signed package closure

Windows install/repair/uninstall operations are documented in `windows-operations.md`. Run
`scripts/check_release_readiness.py --security-release` from a fresh, commit-bound checkout, with
generated evidence and packages stored outside that checkout. Set:

- `ACE_SECURITY_LINUX_EVIDENCE`
- `ACE_SECURITY_WINDOWS_EVIDENCE`
- `ACE_SECURITY_MACOS_EVIDENCE`
- `ACE_SECURITY_PACKAGE_EVIDENCE`
- `ACE_SECURITY_PACKAGE_ATTESTATION`

In GitHub, call `security-release-gate.yml` with the full source commit and the Linux, Windows,
macOS, and package-signing workflow run IDs. It downloads each run independently, keeps the
same-named runtime manifests in separate directories, and fails unless the complete closure above
verifies. The package run must expose an artifact named `security-package-evidence` containing
`security-package-evidence.json`, its Sigstore bundle, and the exact package files it binds. The
actual publishing workflow must call this reusable gate as a required job; a manual dispatch is
diagnostic evidence and is not by itself authorization to publish.

The package evidence must satisfy `package-evidence.schema.json`, bind the SHA-256 of all three
native evidence files and every runtime/manifest/SBOM tuple, identify the exact Linux, Windows, and
macOS package bytes, and record successful verification of each platform's required signature
(Linux package trust, Authenticode, and Apple code signing respectively). One missing package or
signature, native Desktop walkthrough, or proof that the same Ed25519 update trust-root digest
(SHA-256 of the canonical DER SPKI bytes) and fixed HTTPS update source are embedded in every
package blocks the whole release. The signing workflow must derive both values from each unpacked
package's bundled main process, not merely echo the build environment. The package evidence itself must
also have a GitHub attestation from the release workflow fixed by the committed
`package-signing-policy.json`. Runtime environment variables cannot select this signer.
Copy `package-signing-policy.example.json` to the non-example filename only after replacing every
placeholder with independently verified Ed25519, Linux, Authenticode, and Apple signing
identities; the named workflow must exist in the same clean checkout and perform the native
signature checks. Until that real policy and workflow are provisioned, release remains blocked.
Copy
`package-evidence.example.json` only as a checklist; it deliberately remains blocked and contains no
fabricated signer identity.

Repository code cannot create the production trust roots. Deployment owners must still provision
and independently verify the platform signing identities (Authenticode for Windows and
Apple code signing/notarization for macOS, plus the selected Linux package trust), protect the
release workflow and environments with branch/reviewer controls, retain Sigstore verification
material, confirm that the repository's GitHub plan supports artifact attestations, and record
actual hosted-runner workflow URLs. GitHub provenance does not replace platform code signing.

The audit HMAC key created beside a local database is owner-protected and detects mutation by
principals that do not possess that key; it is not non-repudiation against compromise of the same OS
account. Deployments that require that stronger property must inject key material from an
administrator-controlled keystore and define external key rotation/retention. No such external
keystore or signing identity is manufactured by this repository.

Desktop production builds additionally require `ACE_UPDATE_PUBLIC_KEY` to contain a real Ed25519
SPKI public key and `ACE_DOWNLOAD_BASE_URL` to contain the fixed HTTPS update directory. The build
validates and embeds both values in the signed application; runtime environment overrides are
ignored. Renderer-provided package URLs must exactly match the embedded source, redirects are
same-origin and bounded, and signature fetches do not follow redirects. Missing keys or sources
block production builds. Each `.sig` is a signed JSON envelope binding the normalized version,
filename, byte length, and SHA-256; raw package-only signatures and version-mismatched envelopes
are rejected. Missing or invalid signatures block both update download completion and installation
in every security mode.
