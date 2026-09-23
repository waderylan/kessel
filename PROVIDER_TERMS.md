# Provider Terms Assessment

Assessment date: September 22, 2026

This document describes how Kessel's current design relates to published OpenAI
and Anthropic terms and product documentation. It is a compatibility
assessment, not legal advice or a claim of provider endorsement.

## Scope of the assessment

Kessel is designed for local, single-user development:

- It binds to `127.0.0.1` and requires a private local Kessel API key.
- It invokes official provider CLIs already installed and authenticated by the
  operating-system user.
- It does not read, copy, return, or store provider access tokens or login files.
- It does not pool accounts, share subscriptions, resell provider access, or
  bypass provider rate limits.
- It does not expose a user's provider subscription to a remote deployment.
- It reads provider account identity on demand and does not retain it.

Changing any of these properties requires a new provider-terms review.

## Assessment

Kessel's current local design uses documented provider integration surfaces
and keeps provider authentication, limits, and account ownership under each
provider's official CLI. Its OpenAI-compatible and Anthropic-compatible routes
adapt those supported local interfaces without transferring provider
credentials or subscription access.

### OpenAI and Codex

OpenAI publishes Codex CLI, `codex exec`, Codex SDK, and Codex app-server as
integration surfaces. Its Codex platform guidance says app-server is intended
for applications that connect to a local Codex process and control task
lifecycle and streaming. Kessel's Codex adapter uses these documented local
interfaces and keeps authentication under the installed Codex CLI.

OpenAI's Terms of Use also restrict credential sharing, distribution or resale
of its services, circumvention of limits or safeguards, and unauthorized
automated extraction of output. Kessel's local, per-user architecture preserves
those boundaries while using OpenAI's documented Codex integration interfaces.

Current conclusion: local use through the documented Codex interfaces appears
reasonably consistent with OpenAI's published product guidance. Remote access,
account pooling, resale, credential forwarding, or quota circumvention is not
within this assessment.

### Anthropic and Claude Code

Anthropic's current Claude plan guidance states that Claude Agent SDK,
`claude -p`, and third-party application usage draw from the user's Claude
subscription limits. Kessel invokes the documented `claude -p` non-interactive
CLI interface using the user's existing Claude Code login.

Anthropic's Consumer Terms prohibit credential sharing, resale, competing
services, protective-measure bypass, and automated or non-human access except
through an Anthropic API key or where Anthropic otherwise explicitly permits
it. Anthropic's published guidance specifically includes `claude -p` and
third-party applications. Kessel uses that documented non-interactive CLI path
while keeping the subscription attached to its individual user.

Current conclusion: Kessel's local use of `claude -p` follows Anthropic's
documented subscription workflow. Shared production automation should use the
Anthropic API or another provider-supported commercial authentication
mechanism.

## Required operating boundaries

Kessel should continue to enforce these boundaries:

- Each user authenticates their own locally installed provider CLI.
- Provider credentials and authentication files are never exposed or copied.
- The server remains localhost-only by default and is not marketed as a hosted
  provider-access service.
- Accounts, subscription capacity, and usage limits are never shared or pooled.
- Provider limits, safety controls, authentication checks, and supported client
  behavior are not bypassed.
- Documentation does not imply endorsement, affiliation, or approval by OpenAI
  or Anthropic.
- Production and multi-user deployments use provider-supported API or workload
  authentication rather than personal subscriptions.

## Ongoing provider review

Provider documentation and terms can change. Kessel's compatibility checks,
security boundaries, and documentation should be reviewed when either provider
changes its CLI integration guidance, authentication model, or applicable
terms.

The license covering Kessel's source code does not grant rights to OpenAI or
Anthropic services, software, models, accounts, names, or trademarks, and it
does not override any user's agreement with those providers.

## Primary sources

- [OpenAI: Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform)
- [OpenAI Terms of Use](https://openai.com/policies/row-terms-of-use/)
- [OpenAI Services Agreement](https://openai.com/policies/services-agreement/)
- [Anthropic: Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [Anthropic Consumer Terms of Service](https://www.anthropic.com/legal/consumer-terms)
- [Anthropic Commercial Terms of Service](https://www.anthropic.com/legal/commercial-terms)
