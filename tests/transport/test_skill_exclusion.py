"""Port contract: a provider session hides the skills its caller names, and nothing else.

The provider otherwise runs in the user's environment: its settings, sandbox and
other skills load as they would for the user. A caller that names nothing excludes nothing.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from provider_transport.acp_client import AcpBinding, AcpOutcome, ParentAuthority
from provider_transport.acp_driver import AcpDriver
from provider_transport.acp_router import CATALOG_PATH, load_provider, run_acp_request
from provider_transport.pi_driver import PiDriver
from provider_transport.provider_catalog import excluded_skills
from provider_transport.provider_drivers import driver_for
from .provider_fixtures import codex_provider

NAMES = ["design", "implement", "review"]
EXCLUDE = {"plugin": "tools", "names": NAMES}
CLAUDE_META = {"claudeCode": {"options": {"disallowedTools": [f"Skill(tools:{name})" for name in NAMES]}}}
CODEX_ENTRIES = ",".join(f'{{name="tools:{name}",enabled=false}}' for name in NAMES)


def catalog():
    return json.loads(CATALOG_PATH.read_text())


class CatalogExclusionTests(unittest.TestCase):
    def test_every_provider_declares_an_exclusion(self):
        for key, row in catalog()["providers"].items():
            with self.subTest(provider=key):
                self.assertIn("skill_exclusion", row)

    def test_each_provider_excludes_only_the_named_skills_in_its_own_form(self):
        codex = load_provider("codex", exclude=EXCLUDE)
        self.assertEqual(("codex", "app-server", "-c", f"skills.config=[{CODEX_ENTRIES}]"),
                         codex.argv(subsession_dir=CATALOG_PATH.parent, model="m", effort="low"))
        self.assertIsNone(codex.isolation_session_meta)
        claude = load_provider("claude", exclude=EXCLUDE)
        self.assertEqual(CLAUDE_META, claude.isolation_session_meta)
        self.assertEqual((), claude.isolation_argv)
        pi = load_provider("pi", exclude=EXCLUDE)
        self.assertEqual(("pi", "--mode", "rpc", "--exclude-skills", ",".join(NAMES)), pi.command)

    def test_a_caller_that_names_no_skills_excludes_nothing(self):
        for key, row in catalog()["providers"].items():
            for spec in (load_provider(key), load_provider(key, exclude=None)):
                with self.subTest(provider=key):
                    self.assertEqual(tuple(row["command_prefix"]), spec.command)
                    self.assertEqual((), spec.isolation_argv)
                    self.assertIsNone(spec.isolation_session_meta)

    def test_no_provider_drops_the_users_settings_or_plugins(self):
        for key in catalog()["providers"]:
            for exclude in (EXCLUDE, None):
                with self.subTest(provider=key, exclude=exclude):
                    spec = load_provider(key, exclude=exclude)
                    text = json.dumps([spec.command, spec.isolation_session_meta])
                    for dropped in ("settingSources", "--disable", "--no-extensions", "--no-skills", "--no-mcp"):
                        self.assertNotIn(dropped, text)

    def test_malformed_exclusion_is_rejected(self):
        # The form is the catalog's; the plugin and the names are the caller's value.
        for value in ({"skill_exclusion": "drop-everything"}, {"skill_exclusion": None}):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                document = catalog()
                document["providers"]["claude"].update(value)
                path = Path(directory) / "providers.json"
                path.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, "malformed-provider-skill-exclusion"):
                    load_provider("claude", catalog_path=path, exclude=EXCLUDE)
        for value in (
            {"plugin": "tools"},
            {"plugin": "", "names": ["design"]},
            {"plugin": "tools", "names": []},
            {"plugin": "tools", "names": ["a,b"]},
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "malformed-excluded-skills"):
                    excluded_skills(value)
                for key in catalog()["providers"]:
                    with self.assertRaisesRegex(ValueError, "malformed-excluded-skills"):
                        load_provider(key, exclude=value)

    def test_a_well_formed_exclusion_is_its_plugin_and_names(self):
        self.assertEqual(("tools", tuple(NAMES)), excluded_skills(EXCLUDE))


class CatalogDocumentTests(unittest.TestCase):
    """What a catalog means is decided from its document, with no file read."""

    DOCUMENT = {"providers": {"one": {
        "transport": "pi-rpc", "command_prefix": ["one", "--rpc"], "command_base": "path",
        "bindings": {"session": ["model"], "required": []}, "skill_exclusion": "pi-exclude-skills",
        "conformance": {"generation": "one:v1"}}}}

    def test_a_document_gives_its_providers_and_each_ones_command(self):
        from provider_transport.provider_catalog import names_of, provider_spec
        self.assertEqual(("one",), names_of(self.DOCUMENT))
        self.assertEqual(("one", "--rpc"), provider_spec(self.DOCUMENT, "one").command)
        self.assertEqual(("one", "--rpc", "--exclude-skills", "a,b"),
                         provider_spec(self.DOCUMENT, "one", exclude={"plugin": "p", "names": ["a", "b"]}).command)

    def test_a_document_that_is_not_a_catalog_is_refused(self):
        from provider_transport.provider_catalog import names_of, provider_spec
        for document in (None, [], {}, {"providers": []}):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    names_of(document)
                with self.assertRaises(ValueError):
                    provider_spec(document, "one")
        with self.assertRaisesRegex(ValueError, "unknown-acp-provider"):
            provider_spec(self.DOCUMENT, "two")


class UndeclaredExclusionTests(unittest.TestCase):
    def test_a_provider_without_an_exclusion_cannot_be_loaded(self):
        # Every driver and the engine's composition obtain a provider through load_provider.
        for key in catalog()["providers"]:
            for exclude in (None, EXCLUDE):
                with self.subTest(provider=key, exclude=exclude), TemporaryDirectory() as directory:
                    document = catalog()
                    del document["providers"][key]["skill_exclusion"]
                    path = Path(directory) / "providers.json"
                    path.write_text(json.dumps(document))
                    with self.assertRaisesRegex(ValueError, "malformed-provider-skill-exclusion"):
                        load_provider(key, catalog_path=path, exclude=exclude)


class AdapterContractTests(unittest.TestCase):
    """Each agent's start carries the exclusion its caller named."""

    def test_app_server_host_starts_with_the_callers_exclusion(self):
        for exclude in (EXCLUDE, None):
            with self.subTest(exclude=exclude):
                adapter = codex_provider(cell_id="cell", exclude=exclude)
                admission = object()
                with patch("provider_transport.jsonl_rpc.JsonlRpcClient.launch", return_value="rpc") as launch:
                    self.assertEqual("rpc", adapter.driver.launch_host(admission, "cell"))
                command = launch.call_args.kwargs["command"]
                self.assertEqual(load_provider("codex", exclude=exclude).command, command)
                self.assertEqual(exclude is not None, "-c" in command)
                self.assertEqual(exclude is not None, f"skills.config=[{CODEX_ENTRIES}]" in command)
                self.assertIs(admission, launch.call_args.kwargs["spawn_admission"])

    def open_acp_session(self, **options):
        """The arguments of the session a routed Claude turn opens."""
        events = []
        provider = load_provider("claude")

        class Connection:
            rpc = SimpleNamespace(process=SimpleNamespace(pid=4301), close=lambda: events.append("close"))
            capabilities = {}

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def new_session(self, **kwargs):
                events.append(("new", kwargs))
                return AcpBinding("probe-1", "claude", provider.conformance_generation)

            def set_config_option(self, binding, *, config_id, value):
                events.append("config:" + config_id)

            def prompt(self, binding, text):
                return AcpOutcome(binding, "end_turn")

        with TemporaryDirectory() as cwd:
            run_acp_request(
                {"wire_version": 3, "provider": "claude", "prompt": "work",
                 "cwd": cwd, "generation": provider.conformance_generation, "model": "claude-opus-5-5",
                 "boundary_kind": "host_approved"},
                authority=ParentAuthority((Path(cwd),)),
                connection_factory=lambda argv, **kwargs: Connection(),
                client_factory=Client,
                **options,
            )
        [(_, opened)] = [event for event in events if isinstance(event, tuple)]
        return opened

    def test_acp_session_carries_the_callers_exclusion(self):
        self.assertEqual(CLAUDE_META, self.open_acp_session(exclude=EXCLUDE)["meta"])
        self.assertIsNone(self.open_acp_session(exclude=None)["meta"])
        self.assertIsNone(self.open_acp_session()["meta"])

    def test_every_composed_driver_starts_its_provider_with_the_callers_exclusion(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            for exclude in (EXCLUDE, None):
                with self.subTest(exclude=exclude):
                    drivers = {name: driver_for(load_provider(name), origin=state, state_dir=state, exclude=exclude)
                               for name in ("codex", "claude", "pi")}
                    self.assertEqual(load_provider("codex", exclude=exclude).command,
                                     drivers["codex"].provider_spec.command)
                    self.assertIsInstance(drivers["claude"], AcpDriver)
                    self.assertEqual(exclude, drivers["claude"].exclude)
                    self.assertIsInstance(drivers["pi"], PiDriver)
                    self.assertEqual(load_provider("pi", exclude=exclude).command,
                                     drivers["pi"].provider_spec.command)


if __name__ == "__main__":
    unittest.main()
