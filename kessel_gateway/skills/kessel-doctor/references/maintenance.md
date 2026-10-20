# Services and upgrades

## Service lifecycle

`kessel start` installs and starts durable operation for the current user; it
also registers login startup. `kessel stop` stops the current process without
removing registration. `kessel uninstall-service` stops it and removes that
registration while preserving configuration, key, and logs.

Before a stop/restart, establish whether the current process is a foreground
owner, app-managed temporary server, durable service, or unrelated process.
Use current status and CLI messages; do not dump session/config files.
Stopping a foreground owner ends applications attached through `kessel run`.
Do not interrupt other work without authorization. Do not replace an
app-managed session with durable operation merely to clear an error.

Durable service registration uses Windows user startup, a macOS launch agent,
or a Linux systemd user unit. If that mechanism is unavailable, use a supported
app-managed or foreground mode rather than escalating to a system service.

At `kessel start`, the service captures PATH and any configured `HTTP_PROXY`,
`HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, and
`NODE_EXTRA_CA_CERTS`. If the provider/Node installation moved, or proxy/CA
settings changed, stop and start an existing durable service from a shell with
the intended environment. Check paths and presence of variable names without
printing secret-bearing proxy URLs. A port conflict should not trigger process
killing. Reconfigure the port and affected clients if necessary.

## Requested upgrades

1. Record Kessel/provider versions, pipx or other installation method, selected
   provider, and existing startup mode. Do not include account identity or keys.
2. Identify what the user asked to upgrade. An upgrade to Kessel does not imply
   upgrading both provider CLIs. Preserve a working provider unless requested
   otherwise or evidence establishes a necessary, authorized compatibility fix.
3. For pipx-installed Kessel, use `pipx upgrade kessel-gateway`. Follow the actual
   installer for other installations; do not create a second competing install.
   Account for interruption before upgrading a running service.
4. Run `kessel --version` and `kessel doctor`. Provider compatibility depends on
   capability probes as well as minimum version. If a provider update fails,
   use the known-stable recovery command printed by this release's doctor,
   with authorization for the version replacement. Do not guess a version or
   set `KESSEL_ENFORCE_CLI_VERSIONS=false`.
5. For an existing durable service, refresh its registration/environment through
   the normal stop/start lifecycle if needed. Preserve its prior startup mode;
   do not register a service for a previously app-managed application.
6. Repeat a harmless provider request and the original client operation. Report
   a partial outcome if another installed provider fails even when the selected
   one works. Do not repeatedly retry an upstream quota or authentication error.

If the new Kessel release itself breaks the client, report the evidence and
offer restoring the recorded version using the same installer. Do not delete
configuration to downgrade. Keep configuration and secrets private during any
necessary backup.

## Updating installed skills

Skills are copied snapshots of the installed Kessel release. Updating the
package does not overwrite installed agent skills. Run `kessel skills install
--directory <the original skill parent>` after upgrading. Identical copies are
left alone; differing copies cause refusal before any skill is copied.

For a differing copy, review and back up the existing skill folders, move them
aside, and reinstall only when that replacement is authorized. Preserve local
customizations rather than using a forced overwrite. Start a new agent
conversation if its skill list or loaded instructions are stale.
