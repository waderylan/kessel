---
name: kessel-doctor
description: Diagnose and repair an existing Kessel installation or connection, including provider compatibility, service startup, and requested upgrades. Use when Kessel fails or the user asks to check, fix, or update it.
---

# Diagnose and repair Kessel

Find the failing layer, apply a scoped fix, and repeat the original failing
operation. A check-only request produces findings without making changes.

## Make the work visible

Start with targeted read-only checks. Report the observed failure, likely
layer, proposed repair, affected files/processes, and how to verify it before
making changes. Identify any service interruption or provider version change.

Respect the user's explicit scope and existing authorization. A request to fix
Kessel authorizes routine repairs needed for that failure. Ask only before
expanding scope: unrelated upgrades, replacing a working provider version,
new login startup registration, key rotation, or interruption of other active
applications. Do not ask again for an action the user already authorized.
Installing this skill does not start diagnostics or repairs.

## Workflow

1. Capture the failing command/client, error category, selected provider, and
   expected result from existing context. Ask for a missing error only when
   checks cannot establish it. Do not request full config files, credentials,
   account identity, or user prompts as diagnostic material.
2. Run `kessel --version`, `kessel doctor`, and `kessel status` when installed.
   These need no request to the model. Doctor checks both providers; inspect
   individual results instead of treating its exit code as proof neither works.
   Status may report a running but degraded server. Use the installed `--help`
   before assuming flags. If the executable is missing, distinguish a PATH
   problem from an absent package before reinstalling.
3. Read [failure diagnosis](references/failures.md) for the matching symptom.
   Read [services and upgrades](references/maintenance.md) only for service
   lifecycle, environment mismatch, or an upgrade request. Use
   `kessel logs -n 20` when needed; Kessel logs contain method/path/status, not
   prompts or responses. Do not turn on request-body or authorization logging.
4. State the evidence and apply the smallest authorized fix. Preserve config,
   keys, provider authentication, and unrelated application changes. Use
   compatibility guidance from this installed release; never bypass version or
   capability enforcement to get a passing health check.
5. Repeat the original failing operation with harmless input when practical.
   A real provider test consumes quota; announce it. If only diagnosis was
   requested, report the proposed test instead of generating requests without
   that scope. Gateway setup success alone does not verify an editor or SDK.
6. Stop and explain the remaining blocker if the same failure persists after
   a targeted fix and evidence does not support another. Do not cycle through
   reinstallations, key rotations, logins, or quota retries. User login,
   unavailable network, upstream quota, and unsupported client features need
   their specific resolution, not weakened controls.

## Credentials and boundaries

Never read, copy, or print provider authentication files. Never ask users to
paste tokens or keys. Plain `kessel key`, `kessel env`, and `kessel connect`
print the local key: do not capture raw output. Inspect configuration fields
or environment names selectively without displaying secrets. Keep API
authentication and localhost binding enabled. Provider quota and safeguards
remain enforced. Account details are read on demand only if needed and must
not be retained in diagnostic reports or artifacts.

## Completion report

List the cause and evidence, exact repairs, test result, and remaining blocker
if any. State what runs now and give undo steps for each persistent change.
Record versions when changed, never secrets. If a GUI or browser step needs
the user, name that step and leave verification pending. Distinguish
"service reachable," "provider request passed," and "client verified."
