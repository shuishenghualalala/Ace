# Windows security setup operations

The packaged Desktop invokes the fixed `ace-security-runtime.exe` with an encoded,
absolute state directory. Install and repair use `--windows-setup`; uninstall uses
`--windows-uninstall`. PowerShell requests elevation with `RunAs`, so a user may reject UAC and
the Desktop must keep managed execution unavailable rather than falling back to host execution.

Setup is idempotent: it validates both technical accounts and installs one SID-scoped WFP
provider, sublayer and filter set for each account role. Separate state directories therefore
retain independent network policy. Repair migrates fixed-key legacy filters only when their Ace
provider/sublayer and SID ownership are recognized; legacy rules for another profile remain in
place. If a legacy Ace filter's SID cannot be verified, setup fails and requires administrator
cleanup. Uninstall reads the offline and online account names from the protected identity file,
removes only their derived WFP objects and matching legacy filters, then removes the recorded
technical accounts. If identity data is unavailable, WFP cleanup fails closed instead of deleting
machine-wide keys. Project ACL cleanup remains driven by the Ace ACL lease manifest; unrelated
users, ACEs and WFP providers are not deletion targets. Empty legacy provider/sublayer metadata may
remain after migration because those identifiers were shared by older profiles.

Credentials use machine-scoped DPAPI because a standard user may approve UAC with a separate
administrator credential; user-scoped DPAPI would then make the normal Desktop unable to decrypt
the result. Isolation between local OS users is provided by each user's separate absolute state
directory and its inherited filesystem ACL. The identity file must never be moved to a shared ACL.

Run `tests/security/windows_install_matrix.ps1` in a clean elevated Windows VM before release. It
installs and repairs two independent state directories, verifies both before and after each repair,
then exercises both uninstall orders while confirming the remaining profile stays ready.
Daily app use does not require elevation.

The tray uninstall flow stops Gateway and its task tree first, then requests elevation to remove
the security objects. If that cleanup is rejected or fails, application uninstall is aborted so
the helper remains available for repair; it does not silently orphan the accounts and WFP rules.
