"""The provider catalog: each provider's command, transport, bindings and skill exclusion.

providers.json declares every provider a subsession may launch. The transport
kind selects the protocol driver; the command is fixed here so no caller
supplies an executable or argv. A provider runs in the user's own environment
(settings, sandbox, plugins). A caller may name skills of one plugin that the
provider must not load; they are excluded in each provider's own form.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence


CATALOG_PATH = Path(__file__).resolve().parent / "providers.json"
SKILL_EXCLUSION_FORMS = frozenset({"codex-skills-config", "claude-disallowed-skills", "pi-exclude-skills"})


def skill_exclusion(
    form: str, plugin: str, names: Sequence[str]
) -> tuple[tuple[str, ...], Mapping[str, object] | None]:
    """The argv and session metadata that hide `plugin`'s `names` skills in one provider.

    Codex and Claude name a plugin skill `<plugin>:<name>`; Pi lists skills by
    their bare name and drops them through the managed `--exclude-skills` flag.
    """
    qualified = [f"{plugin}:{name}" for name in names]
    if form == "codex-skills-config":
        entries = ",".join(f"{{name={json.dumps(name)},enabled=false}}" for name in qualified)
        return ("-c", f"skills.config=[{entries}]"), None
    if form == "claude-disallowed-skills":
        # Claude ignores skillOverrides for plugin skills; a Skill(...) deny rule refuses the call.
        denied = [f"Skill({name})" for name in qualified]
        return (), {"claudeCode": {"options": {"disallowedTools": denied}}}
    if form == "pi-exclude-skills":
        return ("--exclude-skills", ",".join(names)), None
    raise ValueError("malformed-provider-skill-exclusion")


def excluded_skills(raw: object) -> tuple[str, tuple[str, ...]]:
    """The plugin and the names of a caller's `{"plugin": ..., "names": [...]}`, or raise."""
    if not isinstance(raw, Mapping) or set(raw) != {"plugin", "names"}:
        raise ValueError("malformed-excluded-skills")
    plugin, names = raw["plugin"], raw["names"]
    if (
        not isinstance(plugin, str) or not plugin
        or not isinstance(names, Sequence) or isinstance(names, (str, bytes)) or not names
        or not all(isinstance(name, str) and name and "," not in name for name in names)
    ):
        raise ValueError("malformed-excluded-skills")
    return plugin, tuple(names)


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    command_prefix: tuple[str, ...]
    command_base: str
    transport: str
    session_bindings: frozenset[str]
    required_bindings: frozenset[str]
    conformance_generation: str
    isolation_argv: tuple[str, ...] = ()
    isolation_session_meta: Mapping[str, object] | None = None
    launch_bindings: frozenset[str] = frozenset()

    @property
    def command(self) -> tuple[str, ...]:
        return (*self.command_prefix, *self.isolation_argv)

    def argv(
        self,
        *,
        subsession_dir: Path,
        model: str | None = None,
        effort: str | None = None,
    ) -> tuple[str, ...]:
        values = {"model": model, "effort": effort}
        missing = sorted(
            key for key in self.required_bindings if not values.get(key)
        )
        if missing:
            raise ValueError("provider-binding-required:" + ",".join(missing))
        unsupported = sorted(
            key for key, value in values.items() if value and key not in self.session_bindings
        )
        if unsupported:
            raise ValueError("provider-binding-unsupported:" + ",".join(unsupported))
        # A launch binding is also given to the provider's command, so the host can offer that exact
        # value before the session binds it.
        launch = tuple(
            atom for key in sorted(self.launch_bindings) if values.get(key)
            for atom in (f"--{key}", str(values[key]))
        )
        command = self.command
        if self.command_base == "path":
            return (*command, *launch)
        if self.command_base == "subsession-python":
            # A Python script beside the catalog, started by this interpreter on every platform.
            return (
                sys.executable,
                str((subsession_dir / command[0]).absolute()),
                *command[1:],
                *launch,
            )
        raise ValueError("provider-command-base-invalid")

    def command_fingerprint(self, *, argv: Sequence[str]) -> str:
        payload = json.dumps(
            {
                "provider": self.key,
                "transport": self.transport,
                "argv": tuple(argv),
                "generation": self.conformance_generation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_provider(
    provider: str, *, catalog_path: Path = CATALOG_PATH, exclude: object = None,
) -> ProviderSpec:
    """One provider of the catalog file. `exclude` is the caller's `{"plugin", "names"}` of skills the
    provider must not load, or None to exclude nothing."""
    return provider_spec(json.loads(catalog_path.read_text(encoding="utf-8")), provider, exclude=exclude)


def provider_spec(document: object, provider: str, *, exclude: object = None) -> ProviderSpec:
    """One provider of a catalog document, or raise `ValueError` naming what is malformed."""
    providers = document.get("providers") if isinstance(document, Mapping) else None
    if not isinstance(providers, Mapping) or provider not in providers:
        raise ValueError("unknown-acp-provider")
    raw = providers[provider]
    if not isinstance(raw, Mapping):
        raise ValueError("malformed-acp-provider")
    command_prefix = raw.get("command_prefix")
    if (
        not isinstance(command_prefix, Sequence)
        or isinstance(command_prefix, (str, bytes))
        or not command_prefix
        or not all(isinstance(atom, str) and atom for atom in command_prefix)
    ):
        raise ValueError("malformed-acp-provider-command")
    bindings = raw.get("bindings", {})
    if not isinstance(bindings, Mapping):
        raise ValueError("malformed-acp-provider-bindings")
    session_bindings = bindings.get("session", ())
    required_bindings = bindings.get("required", ())
    launch_bindings = bindings.get("launch", ())
    if (
        set(bindings) - {"session", "required", "launch"}
        or not isinstance(session_bindings, Sequence)
        or isinstance(session_bindings, (str, bytes))
        or not isinstance(required_bindings, Sequence)
        or isinstance(required_bindings, (str, bytes))
        or not all(key in {"model", "effort"} for key in session_bindings)
        or not all(key in {"model", "effort"} for key in required_bindings)
        or not isinstance(launch_bindings, Sequence)
        or isinstance(launch_bindings, (str, bytes))
        or not all(key in session_bindings for key in launch_bindings)
    ):
        raise ValueError("malformed-acp-provider-bindings")
    conformance = raw.get("conformance")
    if not isinstance(conformance, Mapping):
        raise ValueError("provider-conformance-missing")
    form = raw.get("skill_exclusion")
    if form not in SKILL_EXCLUSION_FORMS:
        raise ValueError("malformed-provider-skill-exclusion")
    isolation_argv, session_meta = ((), None) if exclude is None else skill_exclusion(form, *excluded_skills(exclude))
    return ProviderSpec(
        provider,
        tuple(command_prefix),
        str(raw.get("command_base", "")),
        str(raw.get("transport", "")),
        frozenset(str(key) for key in session_bindings),
        frozenset(str(key) for key in required_bindings),
        str(conformance.get("generation", "")),
        tuple(isolation_argv),
        session_meta,
        frozenset(str(key) for key in launch_bindings),
    )


def provider_names(*, catalog_path: Path = CATALOG_PATH) -> tuple[str, ...]:
    """Every provider the catalog declares."""
    return names_of(json.loads(catalog_path.read_text(encoding="utf-8")))


def names_of(document: object) -> tuple[str, ...]:
    """Every provider a catalog document declares."""
    providers = document.get("providers") if isinstance(document, Mapping) else None
    if not isinstance(providers, Mapping):
        raise ValueError("malformed-provider-catalog")
    return tuple(sorted(str(name) for name in providers))
