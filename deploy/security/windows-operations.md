# Windows security setup operations

The packaged Desktop invokes the fixed `ace-security-runtime.exe` with an encoded,
absolute state directory. Install and repair use `--windows-setup`; uninstall uses
`--windows-uninstall`. PowerShell requests elevation with `RunAs`, so a user may reject UAC and
the Desktop must keep managed execution unavailable rather than falling back to host execution.

Setup is idempotent: it validates both technical accounts and reinstalls the stable WFP provider,
sublayer and filters. Uninstall deletes those stable Ace WFP keys and the two recorded
technical accounts only. Project ACL cleanup remains driven by the Ace ACL lease manifest;
unrelated users, ACEs and WFP providers are not deletion targets.

Credentials use machine-scoped DPAPI because a standard user may approve UAC with a separate
administrator credential; user-scoped DPAPI would then make the normal Desktop unable to decrypt
the result. Isolation between local OS users is provided by each user's separate absolute state
directory and its inherited filesystem ACL. The identity file must never be moved to a shared ACL.

Run `tests/security/windows_install_matrix.ps1` in a clean elevated Windows VM before release.
Daily app use does not require elevation.

The tray uninstall flow stops Gateway and its task tree first, then requests elevation to remove
the security objects. If that cleanup is rejected or fails, application uninstall is aborted so
the helper remains available for repair; it does not silently orphan the accounts and WFP rules.
