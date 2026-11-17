"""Provider sessions composed as the engine composes them: one lifecycle over each driver."""
from pathlib import Path

from provider_transport.acp_driver import AcpDriver
from provider_transport.codex_driver import CodexDriver
from provider_transport.provider_sessions import ProviderSessions


def codex_provider(*, cell_id: str = "cell", **driver) -> ProviderSessions:
    return ProviderSessions(CodexDriver(**driver), cell_id=cell_id)


def acp_provider(
    *, provider: str, generation: str, authority_root: Path, cell_id: str = "cell", **driver
) -> ProviderSessions:
    return ProviderSessions(
        AcpDriver(provider=provider, generation=generation, authority_root=authority_root, **driver),
        cell_id=cell_id,
    )
