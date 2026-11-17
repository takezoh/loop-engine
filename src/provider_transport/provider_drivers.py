"""Select the protocol driver of a catalog provider by the transport it names."""
from __future__ import annotations

from pathlib import Path

from .provider_catalog import ProviderSpec
from .provider_port import ProtocolDriver


def driver_for(
    spec: ProviderSpec, *, origin: Path, state_dir: Path, exclude: object = None,
) -> ProtocolDriver:
    """The protocol driver of one provider; `origin` is the directory its sessions work under and
    `exclude` the caller's `{"plugin", "names"}` of skills its providers must not load."""
    if spec.transport == "codex-app-server":
        from .codex_driver import CodexDriver
        return CodexDriver(provider=spec.key, generation=spec.conformance_generation, exclude=exclude)
    if spec.transport == "acp-v1":
        from .acp_driver import AcpDriver
        return AcpDriver(
            provider=spec.key,
            generation=spec.conformance_generation,
            authority_root=origin,
            allow_tmp=True,
            exclude=exclude,
        )
    if spec.transport == "pi-rpc":
        from .pi_driver import PiDriver
        return PiDriver(provider=spec.key, generation=spec.conformance_generation,
                        session_dir=state_dir / "pi-sessions", exclude=exclude)
    raise ValueError(f"unknown-provider-transport:{spec.transport}")

