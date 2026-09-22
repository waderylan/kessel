"""Provider account discovery shared by CLI commands."""

from __future__ import annotations

from collections.abc import Sequence

from app.models import ProviderAccountInfo
from app.providers.claude import ClaudeProvider
from app.providers.codex import CodexProvider
from app.providers.health import ProviderHealth
from app.providers.registry import ProviderRegistry
from app.runner import ProcessRunner


async def read_provider_accounts(
    checks: Sequence[ProviderHealth],
) -> list[ProviderAccountInfo]:
    """Read every installed provider and preserve missing-provider status."""

    runner = ProcessRunner(timeout_seconds=15, max_output_bytes=262_144)
    providers = {}
    for check in checks:
        if not check.installed:
            continue
        command = check.executable or check.name
        if check.name == "codex":
            providers[check.name] = CodexProvider(command, runner)
        elif check.name == "claude":
            providers[check.name] = ClaudeProvider(command, runner)

    discovered: dict[str, ProviderAccountInfo] = {}
    if providers:
        registry = ProviderRegistry(
            providers,
            max_concurrent_requests=1,
            slot_wait_seconds=5,
            shutdown_grace_seconds=2,
        )
        try:
            discovered = {
                account.provider: account
                for account in await registry.account_infos()
            }
        finally:
            await registry.close()

    accounts = []
    for check in checks:
        if not check.installed:
            accounts.append(
                ProviderAccountInfo(
                    provider=check.name,
                    status="not_installed",
                )
            )
            continue
        accounts.append(
            discovered.get(
                check.name,
                ProviderAccountInfo(
                    provider=check.name,
                    status="unavailable",
                ),
            )
        )
    return accounts
