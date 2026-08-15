# Plugin and Hook Execution Boundary

Ace does not provide a native sandbox for third-party Python plugins or hooks.
Python syscall monkeypatching is not treated as isolation: imported code could
otherwise access host files, sockets, processes, threads, CPU, and memory
directly.

The production boundary is therefore:

- Bundled plugins under the packaged plugin root are trusted host code.
- Local developer plugins require both `plugins.developer_mode: true` and an
  exact parent in `plugins.trusted_executable_roots`.
- A remote signature proves artifact integrity, not execution safety. Installed
  Python executes only when its verified signer is also listed in
  `plugins.trusted_executable_signers`.
- A plugin without executable trust is never imported and no `register(ctx)`
  callback runs. Its link-free, snapshot-verified `skills/` directory may remain
  available as a declarative asset. Executable capabilities, including hooks,
  tools, commands, middleware, routers, platforms, and disposers, remain
  disabled.
- Host-assembled `Plugin` objects passed directly to `PluginManager` are part of
  the application trusted computing base; they are not third-party discovery
  results.

Trusted executable plugins run in the Gateway process with host authority.
Manifest capabilities constrain which Ace registration APIs they may use, but
do not constrain direct Python file, network, process, CPU, or memory access.
They must therefore be reviewed as application code. Sandboxed execution of
untrusted Python is unsupported as a product feature; artifacts that require it
fail closed instead of falling back to unrestricted compatibility execution.

Discovery itself is bounded by root, depth, directory, entry, file, bundle,
per-file byte, aggregate-byte, and process-wide concurrency limits. Within a
request, both successful snapshots and failures are memoized. A snapshot binds
root/member identities, manifest bytes, file digests, and the canonical tree
digest, and is revalidated before trusted code is imported.

Slash commands additionally reject built-in and plugin collisions. Their
runtime attribution binds the active plugin key/source, trusted root identity,
tree digest, normalized relative source entrypoint, and entrypoint digest.
If runtime trust becomes stale or revoked, Ace removes the plugin's
registrations and namespace modules immediately. It deliberately does not call
the plugin disposer on this security-failure path, because that would execute
code after its trust had failed; normal administrator disable/unload still runs
trusted disposers.

Relevant configuration:

```yaml
plugins:
  developer_mode: false
  trusted_keys: {}
  allowed_capabilities: []
  trusted_executable_roots: []
  trusted_executable_signers: []
```

`trusted_keys` and `trusted_executable_signers` are intentionally separate:
trusting a signing key for installation does not authorize its code to execute.
