import json
import unittest

from provider_transport import claude_adapter
from provider_transport.provider_catalog import CATALOG_PATH


PATCH_PATH = claude_adapter.PATCH_PATH


class ClaudeFiniteJoinSpikeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch = json.loads(PATCH_PATH.read_text(encoding="utf-8"))
        self.before = "\n".join(
            item["before"] for item in self.patch["replacements"]
        ).encode()
        self.after = "\n".join(
            item["after"] for item in self.patch["replacements"]
        ).encode()

    def test_manifest_seals_exact_patch_and_generation(self) -> None:
        self.assertEqual(
            claude_adapter.PATCH_SHA256,
            claude_adapter.sha256_bytes(PATCH_PATH.read_bytes()),
        )
        catalog = json.loads(CATALOG_PATH.read_text())
        generation = catalog["providers"]["claude"]["conformance"][
            "generation"
        ]
        self.assertTrue(generation.endswith(claude_adapter.POSTIMAGE_SHA256[:12]))

    def test_patch_closes_lost_abort_wakeup(self) -> None:
        text = self.after.decode()
        self.assertIn("if (session.closing) {", text)
        self.assertIn("void pendingNext?.catch", text)
        self.assertIn(
            "if (session.closing || !this.sessions[params.sessionId])", text
        )

    def test_join_precedes_every_destructive_release(self) -> None:
        teardown = next(
            item["after"]
            for item in self.patch["replacements"]
            if "async teardownSession" in item["after"]
        )
        joined = teardown.index("await consumer")
        for destructive in (
            "this.closeQueryStream(session)",
            "session.abortController.abort()",
            "delete this.sessions[sessionId]",
        ):
            self.assertLess(joined, teardown.index(destructive))

    def test_concurrent_close_awaits_one_inflight_promise(self) -> None:
        teardown = next(
            item["after"]
            for item in self.patch["replacements"]
            if "async teardownSession" in item["after"]
        )
        self.assertEqual(1, teardown.count("session.closePromise ="))
        self.assertIn("if (session.closePromise)", teardown)
        self.assertGreaterEqual(teardown.count("await session.closePromise"), 2)


if __name__ == "__main__":
    unittest.main()
