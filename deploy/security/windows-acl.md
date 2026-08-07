# Windows sandbox identity and ACL lifecycle

Ace's strong managed profile uses a non-administrator local technical account created only
by the signed installer/repair flow after an explicit UAC confirmation. It is not a Ace
product user and does not change `owner_id` isolation. Its random password is DPAPI-protected for
the installing OS user; daily runtime code never asks for elevation.

Before a command, the native helper holds a cross-process mutex, removes stale Ace ACEs
listed in `windows-acl-state.json`, then merges minimal read/write/deny ACEs for the sandbox account
and active root capability SIDs into the existing DACL. Existing owner, group and inherited ACEs
remain intact. The manifest is written before ACL mutation, and normal completion revokes only
those technical principals. Empty synthetic `.git/.agents/.crew` mount points are removed; real or
non-empty paths are preserved. Repair/uninstall must perform the same manifest cleanup before
removing only the installation-owned sandbox account.

The current-user-only `WRITE_RESTRICTED` legacy shape is not a strong read sandbox: restricting
SIDs are authoritative only for writes. Ace must report managed filesystem unavailable if
the dedicated identity, DPAPI material, ACL reconciliation, explicit-handle runner or Job probe is
missing; it must never fall back to the signed-in user's ordinary token.
