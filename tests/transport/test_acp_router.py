import sys
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

from provider_transport.acp_router import CATALOG_PATH, AcpRouterFailure, CLOSE_TIMEOUT_SECONDS, load_provider, run_acp_request, validate_request
from provider_transport.acp_client import AcpBinding, AcpOutcome, ParentAuthority
from provider_transport.turn_result import TurnCollector


class AcpRouterTests(unittest.TestCase):

    def request(self, cwd):
        return {
            "wire_version": 3,
            "provider": "claude",
            "cwd": cwd,
            "prompt": "do the task",
            "generation": "catalog-1",
            "model": "claude-fable-5",
            "effort": "high",
        }

    def test_claude_uses_only_bundled_launcher(self):
        provider = load_provider("claude")
        argv = provider.argv(
            subsession_dir=CATALOG_PATH.parent,
            model="claude-fable-5",
        )
        self.assertEqual(sys.executable, argv[0])
        self.assertTrue(Path(argv[1]).is_absolute())
        self.assertEqual(("launchers", "claude_agent_acp.py"), Path(argv[1]).parts[-2:])
        self.assertTrue(Path(argv[1]).is_file())
        self.assertNotIn("npx", argv)
        self.assertNotIn("npm", argv)

    def test_rejects_caller_supplied_command_fields_before_launch(self):
        with TemporaryDirectory() as cwd:
            for forbidden in ("executable", "argv", "command"):
                with self.subTest(forbidden=forbidden):
                    request = self.request(cwd)
                    request[forbidden] = ["malicious"]
                    with self.assertRaisesRegex(ValueError, "forbidden-fields"):
                        validate_request(request)

    def test_a_turn_needs_a_prompt(self):
        with TemporaryDirectory() as cwd:
            request = self.request(cwd)
            del request["prompt"]
            with self.assertRaisesRegex(ValueError, "required-string-missing"):
                validate_request(request)

    def test_a_turn_runs_the_catalog_command_and_closes_its_session(self):
        events = []

        class Rpc:
            process = SimpleNamespace(pid=4201)

            def close(self):
                events.append("close")

        class Connection:
            rpc = Rpc()
            capabilities = {}

        provider = load_provider("claude")
        generation = provider.conformance_generation

        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs):
                pass

            def new_session(self, **kwargs):
                events.append(("new", kwargs))
                return AcpBinding("probe-1", "claude", generation)

            def prompt(self, *args, **kwargs):
                events.append("prompt")
                return AcpOutcome(args[0], "end_turn")

        def launch(argv, **kwargs):
            events.append(("argv", tuple(argv)))
            return Connection()

        with TemporaryDirectory() as cwd:
            result = run_acp_request(
                {
                    "wire_version": 3,
                    "provider": "claude",
                    "cwd": cwd,
                    "prompt": "work",
                    "generation": generation,
                    "model": "claude-fable-5",
                    "effort": "high",
                    "boundary_kind": "host_approved",
                },
                authority=ParentAuthority((Path(cwd),)),
                connection_factory=launch,
                client_factory=Client,
            )
        self.assertEqual(("launchers", "claude_agent_acp.py"), Path(events[0][1][1]).parts[-2:])
        self.assertEqual(2, len(events[0][1]))
        self.assertIn("prompt", events)
        self.assertEqual("close", events[-1])
        self.assertEqual("probe-1", result.binding.session_id)
        self.assertEqual("end_turn", result.stop_reason)

    def test_provider_cleanup_stderr_is_returned_as_bounded_diagnostic(self):
        provider = load_provider("claude")

        class Rpc:
            def close(self): pass
            def stderr_text(self):
                return "Session x: query stream error: ACP connection closed\n"

        class Connection:
            rpc = Rpc()
            capabilities = {}

        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs): pass
            def new_session(self, **kwargs):
                return AcpBinding("child-1", "claude", provider.conformance_generation)
            def prompt(self, binding, prompt):
                return AcpOutcome(binding, "end_turn")

        with TemporaryDirectory() as cwd:
            result = run_acp_request(
                {
                    "wire_version": 3, "provider": "claude",
                    "cwd": cwd, "prompt": "work",
                    "generation": provider.conformance_generation,
                    "model": "claude-fable-5", "effort": "medium",
                    "boundary_kind": "host_approved",
                },
                authority=ParentAuthority((Path(cwd),)),
                connection_factory=lambda *args, **kwargs: Connection(),
                client_factory=Client,
            )
        self.assertIn("query stream error", result.cleanup_diagnostics)

    def test_fresh_claude_session_config_receipt_is_observed_after_config_before_prompt(self):
        events = []
        provider = load_provider("claude")

        class Rpc:
            def close(self): events.append("close")
        class Connection:
            rpc = Rpc()
            capabilities = {}
        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs): pass
            def new_session(self, **kwargs):
                events.append(("session", kwargs["meta"]))
                return AcpBinding("claude-child-1", "claude", provider.conformance_generation)
            def set_config_option(self, binding, *, config_id, value):
                events.append(("config", config_id, value))
            def observe_runtime_context(self, *args, **kwargs):
                raise AssertionError("fresh Claude must not depend on parent context observation")
            def prompt(self, binding, prompt):
                events.append("prompt")
                return AcpOutcome(binding, "end_turn")

        def observe(started):
            proof = started.binding_proof
            events.append("proof")
            self.assertEqual("session_start", proof.binding_source)
            self.assertEqual("claude-opus-5-5", proof.model)
            self.assertEqual("claude-child-1", proof.session_id)

        with TemporaryDirectory() as cwd:
            result = run_acp_request(
                {"wire_version": 3, "provider": "claude",
                 "cwd": cwd, "prompt": "work", "generation": provider.conformance_generation,
                 "model": "claude-opus-5-5", "effort": "high", "boundary_kind": "host_approved"},
                authority=ParentAuthority((Path(cwd),)),
                connection_factory=lambda *args, **kwargs: Connection(),
                client_factory=Client,
                started_observer=observe,
            )
        self.assertEqual("session_start", result.binding_proof.binding_source)
        # The model reaches Claude Code as its own option when the session starts, unchanged; only the
        # effort goes through the session configuration and is read back.
        self.assertEqual(("session", {"claudeCode": {"options": {"model": "claude-opus-5-5"}}}), events[0])
        self.assertNotIn("model", [event[1] for event in events if event[0] == "config"])
        self.assertLess(events.index(("config", "effort", "high")), events.index("proof"))
        self.assertLess(events.index("proof"), events.index("prompt"))

    def test_fresh_claude_config_failure_produces_no_receipt_or_prompt(self):
        events = []
        provider = load_provider("claude")

        class Rpc:
            def close(self): events.append("close")
        class Connection:
            rpc = Rpc()
            capabilities = {}
        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs): pass
            def new_session(self, **kwargs): return AcpBinding("child", "claude", provider.conformance_generation)
            def set_config_option(self, *args, **kwargs):
                events.append("config")
                raise RuntimeError("configuration rejected")
            def prompt(self, *args, **kwargs): events.append("prompt")
            def close(self, binding, *, timeout=None):
                events.append("session-close")
                raise ValueError("cleanup rejected")

        with TemporaryDirectory() as cwd:
            with self.assertRaises(AcpRouterFailure) as raised:
                run_acp_request(
                    {"wire_version": 3, "provider": "claude",
                     "cwd": cwd, "prompt": "work", "generation": provider.conformance_generation,
                     "model": "claude-opus-5-5", "effort": "high", "boundary_kind": "host_approved"},
                    authority=ParentAuthority((Path(cwd),)),
                    connection_factory=lambda *args, **kwargs: Connection(),
                    client_factory=Client,
                    started_observer=lambda started: events.append("proof"),
                )
        self.assertNotIn("proof", events)
        self.assertNotIn("prompt", events)
        self.assertEqual("binding_blocked", raised.exception.code)
        self.assertEqual("ValueError:cleanup rejected", raised.exception.cleanup_error)
        self.assertIn("session-close", events)
        self.assertIn("close", events)

    def test_resumed_claude_applies_current_config_before_proof_and_prompt(self):
        events = []
        provider = load_provider("claude")

        class Rpc:
            def close(self): events.append("close")

        class Connection:
            rpc = Rpc()
            capabilities = {"loadSession": True}

        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs): pass
            def load_session(self, identity, **kwargs):
                events.append(("load", identity))
                events.append(("load-meta", kwargs["meta"]))
                return AcpBinding(identity, "claude", provider.conformance_generation)
            def set_config_option(self, binding, *, config_id, value):
                events.append(("config", config_id, value))
            def prompt(self, binding, prompt):
                events.append("prompt")
                return AcpOutcome(binding, "end_turn")
            def close(self, binding, *, timeout=None):
                events.append(("session-close", timeout))

        def observe(started):
            proof = started.binding_proof
            events.append("proof")
            self.assertEqual("session_start", proof.binding_source)
            self.assertEqual("current-B", proof.model)
            self.assertEqual("high", proof.effort)
            self.assertEqual("same-session", proof.session_id)

        with TemporaryDirectory() as cwd:
            run_acp_request(
                {
                    "wire_version": 3,
                    "provider": "claude",
                    "cwd": cwd,
                    "prompt": "work",
                    "session_id": "same-session",
                    "generation": provider.conformance_generation,
                    "model": "current-B",
                    "effort": "high",
                    "boundary_kind": "host_approved",
                },
                authority=ParentAuthority((Path(cwd),)),
                connection_factory=lambda *args, **kwargs: Connection(),
                client_factory=Client,
                started_observer=observe,
            )
        self.assertLess(events.index(("load", "same-session")), events.index("proof"))
        self.assertIn(("load-meta", {"claudeCode": {"options": {"model": "current-B"}}}), events)
        self.assertNotIn(("config", "model", "current-B"), events)
        self.assertLess(events.index(("config", "effort", "high")), events.index("proof"))
        self.assertLess(events.index("proof"), events.index("prompt"))
        self.assertIn(("session-close", CLOSE_TIMEOUT_SECONDS), events)

    def test_launch_visibility_failure_depends_on_actual_selected_boundary(self):
        provider = load_provider("claude")
        generation = provider.conformance_generation

        def missing(*args, **kwargs):
            raise FileNotFoundError("claude-agent-acp")

        with TemporaryDirectory() as cwd:
            base = {
                "wire_version": 3,
                "provider": "claude",
                "cwd": cwd,
                "prompt": "work",
                "generation": generation,
                "model": "claude-fable-5",
            }
            for boundary, code in (("managed", "env_restricted"), ("host_approved", "cli_missing")):
                with self.subTest(boundary=boundary):
                    with self.assertRaises(AcpRouterFailure) as raised:
                        run_acp_request(
                            {**base, "boundary_kind": boundary},
                            authority=ParentAuthority((Path(cwd),)),
                            connection_factory=missing,
                        )
                    self.assertEqual(code, raised.exception.code)

    def test_launch_failures_are_typed_without_fallback(self):
        provider = load_provider("claude")
        generation = provider.conformance_generation
        cases = (
            (PermissionError("denied"), "env_restricted"),
            (TimeoutError("late"), "timeout"),
            (RuntimeError("not authenticated"), "auth_failed"),
            (RuntimeError("bad frame"), "protocol_error"),
        )
        with TemporaryDirectory() as cwd:
            request = {
                "wire_version": 3,
                "provider": "claude",
                "cwd": cwd,
                "prompt": "work",
                "generation": generation,
                "model": "claude-fable-5",
                "boundary_kind": "host_approved",
            }
            for failure, code in cases:
                with self.subTest(code=code):
                    def fail(*args, **kwargs):
                        raise failure

                    with self.assertRaises(AcpRouterFailure) as raised:
                        run_acp_request(
                            request,
                            authority=ParentAuthority((Path(cwd),)),
                            connection_factory=fail,
                        )
                    self.assertEqual(code, raised.exception.code)

    def test_admission_failures_precede_launch(self):
        events = []

        def launch(*args, **kwargs):
            events.append("launch")
            raise AssertionError("admission failure must not launch")

        provider = load_provider("claude")
        with TemporaryDirectory() as cwd:
            base = {
                "wire_version": 3,
                "provider": "claude",
                "cwd": cwd,
                "prompt": "work",
                "generation": provider.conformance_generation,
                "boundary_kind": "host_approved",
            }
            unreadable = Path(cwd) / "providers.json"
            unreadable.write_text("{")
            cases = (
                ({**base, "model": "claude-fable-5"}, unreadable, "catalog_mismatch"),
                ({**base, "generation": "stale", "model": "claude-fable-5"}, CATALOG_PATH, "catalog_mismatch"),
                (base, CATALOG_PATH, "binding_blocked"),
            )
            for request, catalog_path, code in cases:
                with self.subTest(code=code, catalog=str(catalog_path)):
                    with self.assertRaises(AcpRouterFailure) as raised:
                        run_acp_request(
                            request,
                            authority=ParentAuthority((Path(cwd),)),
                            connection_factory=launch,
                            catalog_path=catalog_path,
                        )
                    self.assertEqual(code, raised.exception.code)
        self.assertEqual([], events)

    def test_cleanup_failure_is_typed_after_a_turn_without_a_result(self):
        provider = load_provider("claude")

        class Rpc:
            def close(self):
                raise RuntimeError("close frame failed")

        class Connection:
            rpc = Rpc()
            capabilities = {}

        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs):
                pass

            def new_session(self, **kwargs):
                return AcpBinding(
                    "probe-close", "claude", provider.conformance_generation
                )

            def prompt(self, binding, text):
                return AcpOutcome(binding, "end_turn")

        with TemporaryDirectory() as cwd:
            with self.assertRaises(AcpRouterFailure) as raised:
                run_acp_request(
                    {
                        "wire_version": 3,
                        "provider": "claude",
                        "cwd": cwd,
                        "prompt": "work",
                        "generation": provider.conformance_generation,
                        "model": "claude-fable-5",
                        "boundary_kind": "host_approved",
                    },
                    authority=ParentAuthority((Path(cwd),)),
                    connection_factory=lambda *args, **kwargs: Connection(),
                    client_factory=Client,
                )
        self.assertEqual("protocol_error", raised.exception.code)

    def test_close_exception_retains_completed_primary_turn(self):
        provider = load_provider("claude")
        collector = TurnCollector("primary-before-close")
        collector.observe_text("done")
        primary = collector.seal_completed()

        class Rpc:
            def close(self):
                pass

        class Connection:
            rpc = Rpc()
            capabilities = {}

        class Client:
            def set_config_option(self, *args, **kwargs): pass
            def __init__(self, *args, **kwargs):
                pass

            def new_session(self, **kwargs):
                return AcpBinding(
                    "primary-close", "claude", provider.conformance_generation
                )

            def prompt(self, binding, prompt):
                return AcpOutcome(binding, "end_turn", primary)

            def close(self, binding, *, timeout=None):
                raise RuntimeError("close frame failed")

        with TemporaryDirectory() as cwd:
            result = run_acp_request(
                {
                    "wire_version": 3,
                    "provider": "claude",
                    "cwd": cwd, "prompt": "work",
                    "generation": provider.conformance_generation,
                    "model": "claude-fable-5", "boundary_kind": "host_approved",
                },
                authority=ParentAuthority((Path(cwd),)),
                connection_factory=lambda *args, **kwargs: Connection(),
                client_factory=Client,
            )
        self.assertEqual(primary, result.turn_result)
        self.assertEqual(
            "RuntimeError:close frame failed", result.cleanup_diagnostics
        )

    def test_start_bindings_need_their_form_and_are_not_session_bindings(self):
        from provider_transport.provider_catalog import provider_spec

        document = json.loads(CATALOG_PATH.read_text())
        claude = document["providers"]["claude"]
        spec = provider_spec(document, "claude")
        self.assertEqual(frozenset({"model"}), spec.start_bindings)
        self.assertEqual(frozenset({"effort"}), spec.session_bindings)
        for name in ("codex", "pi"):
            self.assertEqual(frozenset(), provider_spec(document, name).start_bindings)
        for bindings, form, error in (
            ({"session": ["model", "effort"], "start": ["model"]}, "claude-code-options", "malformed-acp-provider-bindings"),
            ({"session": ["effort"], "start": ["model"]}, None, "malformed-provider-start-form"),
            ({"session": ["effort"], "start": ["model"]}, "unknown-form", "malformed-provider-start-form"),
            ({"session": ["model", "effort"]}, "claude-code-options", "malformed-provider-start-form"),
        ):
            changed = {**claude, "bindings": bindings}
            changed.pop("start_form", None)
            if form is not None:
                changed["start_form"] = form
            with self.subTest(bindings=bindings, form=form), self.assertRaisesRegex(ValueError, error):
                provider_spec({**document, "providers": {**document["providers"], "claude": changed}}, "claude")

    def test_catalog_declares_the_three_providers(self):
        document = json.loads(CATALOG_PATH.read_text())
        self.assertEqual({"codex", "claude", "pi"}, set(document["providers"]))
        self.assertEqual(3, document["wire_version"])


if __name__ == "__main__":
    unittest.main()
