"""Run one ACP turn: bind a new or loaded session, apply its model and effort, and prompt it."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import subprocess
import sys
from typing import Mapping

from .acp_client import (
    AcpMessageCapture,
    AcpPromptError,
    AcpSessionClient,
    AcpStdioConnection,
    ParentAuthority,
)
from .provider_catalog import CATALOG_PATH, load_provider


ALLOWED_REQUEST_KEYS = frozenset(
    {
        "provider", "cwd", "prompt", "session_id", "generation",
        "model", "effort", "boundary_kind", "wire_version",
    }
)
ALLOWED_BOUNDARIES = frozenset({"managed", "host_approved"})
WIRE_VERSION = 3
CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class AcpRouterResult:
    provider: str
    binding: object
    generation: str
    command_fingerprint: str
    binding_proof: object | None = None
    stop_reason: str | None = None
    turn_result: object | None = None
    tool_failures: tuple[Mapping[str, object], ...] = ()
    cleanup_diagnostics: str | None = None


@dataclass(frozen=True)
class AcpStartedTurn:
    """Identity handed to a caller once a provider turn has started running.

    It exposes the exact launched connection/client and bound session so the
    caller can observe bounded progress, cancel, or terminate the live process
    without waiting for the whole model turn to finish.
    """

    provider: str
    binding: object
    connection: object
    client: object
    message_capture: object | None
    binding_proof: object | None


@dataclass(frozen=True)
class AcpChildBindingProof:
    """Evidence derived from the exact argv that the router actually launched."""

    wire_version: int
    provider: str
    generation: str
    model: str | None
    effort: str | None
    boundary_kind: str
    effective_boundary: str
    cwd: str
    session_id: str
    session_mode: str
    command_fingerprint: str
    binding_source: str


class AcpRouterFailure(RuntimeError):
    """Typed pre-start/transport failure from the single ACP launch owner."""

    def __init__(self, code: str, cause: BaseException | str) -> None:
        self.code = code
        self.cause = cause
        self.cleanup_error: str | None = None
        super().__init__(f"{code}:{cause}")


def validate_request(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("acp-request-must-be-object")
    unknown = set(value) - ALLOWED_REQUEST_KEYS
    if unknown:
        raise ValueError("acp-request-forbidden-fields:" + ",".join(sorted(unknown)))
    required = ("provider", "cwd", "generation", "prompt")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError("acp-request-required-string-missing")
    cwd = Path(str(value["cwd"])).resolve()
    if not cwd.is_dir():
        raise ValueError("acp-request-cwd-invalid")
    session_id = value.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("acp-request-session-id-invalid")
    boundary = value.get("boundary_kind", "managed")
    if boundary not in ALLOWED_BOUNDARIES:
        raise ValueError("acp-request-boundary-invalid")
    wire_version = value.get("wire_version")
    if type(wire_version) is not int or wire_version != WIRE_VERSION:
        raise ValueError("acp-request-wire-version-unsupported")
    for key in ("model", "effort"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item):
            raise ValueError(f"acp-request-{key}-invalid")
    return value


def _classify_launch_failure(exc: BaseException, boundary_kind: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return "cli_missing" if boundary_kind == "host_approved" else "env_restricted"
    if isinstance(exc, PermissionError):
        return "env_restricted"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    lowered = str(exc).lower()
    if "auth" in lowered or "not authenticated" in lowered or "login" in lowered:
        return "auth_failed"
    return "protocol_error"


def admit_provider_route(
    provider_key: str,
    *,
    generation: str,
    model: str | None,
    effort: str | None,
    catalog_path: Path = CATALOG_PATH,
    exclude: object = None,
) -> Mapping[str, object]:
    """Resolve every static router policy before callers allocate work state."""
    try:
        provider = load_provider(provider_key, catalog_path=catalog_path, exclude=exclude)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AcpRouterFailure("catalog_mismatch", exc) from exc
    if provider.conformance_generation != generation:
        raise AcpRouterFailure("catalog_mismatch", provider_key)
    try:
        argv = provider.argv(
            subsession_dir=catalog_path.parent,
            model=model,
            effort=effort,
        )
    except ValueError as exc:
        raise AcpRouterFailure("binding_blocked", exc) from exc
    binding_source = (
        "router_session_config"
        if "model" in provider.session_bindings
        else "session_observation"
    )
    return {
        "outcome": "determinate",
        "provider": provider_key,
        "generation": provider.conformance_generation,
        "binding_source": binding_source,
        "command_fingerprint": provider.command_fingerprint(argv=argv),
    }


def run_acp_request(
    value: object,
    *,
    authority: ParentAuthority,
    catalog_path: Path = CATALOG_PATH,
    connection_factory=None,
    client_factory=None,
    started_observer=None,
    spawn_observer=None,
    message_capture: object | None = None,
    existing_connection: object | None = None,
    retain_connection: bool = False,
    request_timeout: float = 300,
    spawn_admission=None,
    host_cell_id: str | None = None,
    exclude: object = None,
) -> AcpRouterResult:
    """Run one ACP turn on ``existing_connection``, or on a host launched through ``spawn_admission``.

    When ``started_observer`` is supplied it is invoked with an
    :class:`AcpStartedTurn` after the provider process and session are live but
    before the prompt blocks on the model turn. That lets a caller return a
    bounded startup receipt while the turn keeps running in the background.

    ``spawn_observer`` is invoked with the initialized connection as soon as the
    provider process exists, before session binding, so a caller that times out
    during binding can still terminate the exact spawned process.
    """
    connection_factory = connection_factory or AcpStdioConnection.launch
    client_factory = client_factory or AcpSessionClient
    try:
        request = validate_request(value)
    except ValueError as exc:
        raise AcpRouterFailure("malformed_request", exc) from exc
    provider_key = str(request["provider"])
    model = request.get("model") if isinstance(request.get("model"), str) else None
    effort = request.get("effort") if isinstance(request.get("effort"), str) else None
    boundary_kind = str(request.get("boundary_kind", "managed"))
    admission = admit_provider_route(
        provider_key,
        generation=str(request["generation"]),
        model=model,
        effort=effort,
        catalog_path=catalog_path,
        exclude=exclude,
    )
    provider = load_provider(provider_key, catalog_path=catalog_path, exclude=exclude)
    argv = provider.argv(
        subsession_dir=catalog_path.parent,
        model=model,
        effort=effort,
    )
    executable = Path(argv[0])
    if executable.is_absolute() and not executable.is_file():
        code = "cli_missing" if boundary_kind == "host_approved" else "env_restricted"
        raise AcpRouterFailure(code, "acp-provider-not-preinstalled")
    connection = None
    client = None
    binding = None
    result = None
    try:
        if message_capture is None:
            message_capture = AcpMessageCapture()
        if existing_connection is not None:
            connection = existing_connection
        else:
            connection = connection_factory(
                argv,
                authority=authority,
                request_timeout=request_timeout,
                message_capture=message_capture,
                spawn_admission=spawn_admission,
                host_cell_id=host_cell_id,
            )
        # Hand the launched process/rpc to the caller immediately, before the
        # session binding can block. A startup timeout that fires while the
        # session is still binding can then terminate the exact spawned process
        # instead of losing ownership of a live worker.
        if spawn_observer is not None:
            spawn_observer(connection)
        client = client_factory(
            connection.rpc,
            provider=provider_key,
            generation=provider.conformance_generation,
            capabilities=connection.capabilities,
            message_capture=message_capture,
        )
        session_id = request.get("session_id")
        binding = (
            client.load_session(str(session_id), cwd=str(request["cwd"]),
                                meta=provider.isolation_session_meta)
            if isinstance(session_id, str)
            else client.new_session(cwd=str(request["cwd"]), meta=provider.isolation_session_meta)
        )
        if isinstance(session_id, str) and binding.session_id != session_id:
            raise AcpRouterFailure(
                "binding_blocked",
                "loaded-session-identity-mismatch",
            )
        for config_id in sorted(provider.session_bindings, key=lambda key: key != "model"):
            config_value = request.get(config_id)
            if isinstance(config_value, str) and config_value:
                try:
                    client.set_config_option(
                        binding, config_id=config_id, value=config_value
                    )
                except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    raise AcpRouterFailure("binding_blocked", exc) from exc
        fingerprint = str(admission["command_fingerprint"])
        proof = AcpChildBindingProof(
            wire_version=WIRE_VERSION,
            provider=provider_key,
            generation=provider.conformance_generation,
            model=model,
            effort=effort,
            boundary_kind=boundary_kind,
            effective_boundary=(
                "resume-pre-prompt-1" if isinstance(session_id, str) else "pre-prompt-1"
            ),
            cwd=str(request["cwd"]),
            session_id=binding.session_id,
            session_mode="load" if isinstance(session_id, str) else "new",
            command_fingerprint=fingerprint,
            binding_source=str(admission["binding_source"]),
        )
        if started_observer is not None:
            started_observer(
                AcpStartedTurn(
                    provider=provider_key,
                    binding=binding,
                    connection=connection,
                    client=client,
                    message_capture=message_capture,
                    binding_proof=proof,
                )
            )

        def observed_tool_failures() -> tuple[Mapping[str, object], ...]:
            return tuple(
                dict(item)
                for item in getattr(
                    getattr(connection, "request_policy", None), "failures", ()
                )
            )

        try:
            outcome = client.prompt(binding, str(request["prompt"]))
        except AcpPromptError as exc:
            result = AcpRouterResult(
                provider_key,
                binding,
                provider.conformance_generation,
                fingerprint,
                binding_proof=proof,
                turn_result=exc.turn_result,
                tool_failures=observed_tool_failures(),
            )
        else:
            if not outcome.stop_reason:
                raise RuntimeError("acp-prompt-stop-reason-missing")
            result = AcpRouterResult(
                provider_key,
                binding,
                provider.conformance_generation,
                fingerprint,
                binding_proof=proof,
                stop_reason=outcome.stop_reason,
                turn_result=getattr(outcome, "turn_result", None),
                tool_failures=observed_tool_failures(),
            )
    except AcpRouterFailure:
        raise
    except Exception as exc:
        raise AcpRouterFailure(_classify_launch_failure(exc, boundary_kind), exc) from exc
    finally:
        if connection is not None and not retain_connection:
            active_exc = sys.exc_info()[1]
            cleanup_exc = None
            try:
                graceful_close = getattr(client, "close", None)
                if binding is not None and callable(graceful_close):
                    graceful_close(binding, timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception as close_exc:
                cleanup_exc = close_exc
            try:
                connection.rpc.close()
            except Exception as rpc_close_exc:
                if cleanup_exc is None:
                    cleanup_exc = rpc_close_exc
                else:
                    cleanup_exc.add_note(
                        f"rpc-close-failed:{type(rpc_close_exc).__name__}:{rpc_close_exc}"
                    )
            if cleanup_exc is not None:
                if active_exc is not None:
                    if isinstance(active_exc, AcpRouterFailure):
                        active_exc.cleanup_error = (
                            f"{type(cleanup_exc).__name__}:{cleanup_exc}"
                        )
                    active_exc.add_note(
                        f"acp-cleanup-failed:{type(cleanup_exc).__name__}:{cleanup_exc}"
                    )
                elif result is not None and result.turn_result is not None:
                    result = replace(
                        result,
                        cleanup_diagnostics=(
                            f"{type(cleanup_exc).__name__}:{cleanup_exc}"
                        )[:16_384],
                    )
                else:
                    raise AcpRouterFailure(
                        _classify_launch_failure(cleanup_exc, boundary_kind),
                        cleanup_exc,
                    ) from cleanup_exc
    assert result is not None and connection is not None
    stderr_reader = getattr(connection.rpc, "stderr_text", None)
    stderr_text = stderr_reader() if callable(stderr_reader) else ""
    cleanup_markers = ("query stream error", "Error during cleanup")
    if any(marker in stderr_text for marker in cleanup_markers):
        result = replace(result, cleanup_diagnostics=stderr_text[-16_384:])
    return result

