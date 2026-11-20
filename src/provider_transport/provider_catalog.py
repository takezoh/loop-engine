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
START_FORMS = frozenset({"claude-code-options"})
BINDING_KEYS = frozenset({"model", "effort"})


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


def start_options(form: str, values: Mapping[str, str]) -> Mapping[str, object]:
    """The session metadata that gives a provider the values it binds when a session starts.

    Claude Code takes them as its own options, as its command line takes `--model`; the adapter
    hands `_meta.claudeCode.options` of `session/new` and `session/load` to it unchanged.
    """
    if form == "claude-code-options":
        return {"claudeCode": {"options": dict(values)}}
    raise ValueError("malformed-provider-start-form")


def merged_meta(
    first: Mapping[str, object] | None, second: Mapping[str, object] | None
) -> Mapping[str, object] | None:
    """Two session metadata documents as one; nested mappings are merged key by key."""
    if first is None or second is None:
        return first if second is None else second
    merged = dict(first)
    for key, value in second.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = merged_meta(merged[key], value)
        else:
            merged[key] = value
    return merged


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
    start_bindings: frozenset[str] = frozenset()
    start_form: str | None = None

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
            key for key, value in values.items()
            if value and key not in self.session_bindings | self.start_bindings
        )
        if unsupported:
            raise ValueError("provider-binding-unsupported:" + ",".join(unsupported))
        command = self.command
        if self.command_base == "path":
            return tuple(command)
        if self.command_base == "subsession-python":
            # A Python script beside the catalog, started by this interpreter on every platform.
            return (
                sys.executable,
                str((subsession_dir / command[0]).absolute()),
                *command[1:],
            )
        raise ValueError("provider-command-base-invalid")

    def session_meta(self, *, model: str | None = None, effort: str | None = None) -> Mapping[str, object] | None:
        """The metadata of `session/new` and `session/load`: the skills to exclude and the start bindings."""
        values = {key: value for key, value in (("model", model), ("effort", effort))
                  if value and key in self.start_bindings}
        start = start_options(str(self.start_form), values) if values else None
        return merged_meta(self.isolation_session_meta, start)

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
    start_bindings = bindings.get("start", ())
    if (
        set(bindings) - {"session", "required", "start"}
        or not all(
            isinstance(keys, Sequence) and not isinstance(keys, (str, bytes))
            and all(key in BINDING_KEYS for key in keys)
            for keys in (session_bindings, required_bindings, start_bindings)
        )
        or set(session_bindings) & set(start_bindings)
    ):
        raise ValueError("malformed-acp-provider-bindings")
    # Values bound when a session starts need the provider's form for them, and only they do.
    start_form = raw.get("start_form")
    if (start_form not in START_FORMS) if start_bindings else (start_form is not None):
        raise ValueError("malformed-provider-start-form")
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
        frozenset(str(key) for key in start_bindings),
        start_form,
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
