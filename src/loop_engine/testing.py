"""Doubles of the engine's ports for a caller's tests.

Each one runs the same contract tests as the adapter it stands in for (`tests/test_port_contracts.py`
lists them), so a caller that tests against a double tests against what the engine does.
"""
RESULT_NAMES = ("patch.diff", "stop.json")


class FakeEngineClient:
    """An `EngineClient` without an engine. It records every request it is sent and answers from the
    units started through it; `stop` ends a unit the way the engine would have recorded it."""

    def __init__(self):
        self.sent, self.waits, self.units = [], [], {}

    def stop(self, unit_id, reason, *, patch=b"", **stop):
        """End a started unit with `reason`, leaving the results an engine leaves."""
        import json
        unit = self.units[unit_id]
        unit["status"] = {"state": "stopped", "stop_reason": reason, "revision": unit["status"]["revision"] + 1}
        unit["results"] = {"patch.diff": patch,
                           "stop.json": json.dumps({"stop_reason": reason, **stop}).encode("utf-8")}

    def request(self, root, payload, *, environ, timeout=30.0):
        self.sent.append((root, payload))
        kind = payload.get("request") if isinstance(payload, dict) else None
        if kind == "start" and isinstance(payload.get("contract"), dict):
            unit = self.units.setdefault(payload["contract"].get("unit_id"),
                                         {"status": {"state": "running", "revision": 0}, "results": {}})
            return {"ok": True, "result": dict(unit["status"])}
        if kind in ("answer", "retry", "cancel"):
            unit = self.units.get(payload.get("unit_id"))
            if unit is None:
                return {"ok": False, "error": "no such unit"}
            return {"ok": True, "result": dict(unit["status"])}
        return {"ok": False, "error": "unknown request"}

    def status(self, root, unit_id):
        unit = self.units.get(unit_id)
        return None if unit is None else dict(unit["status"])

    def wait(self, root, unit_id, *, after_revision=None, timeout=30.0, environ=None):
        self.waits.append((unit_id, after_revision, timeout))
        return self.status(root, unit_id)

    def result(self, root, unit_id, name):
        if name not in RESULT_NAMES:
            raise ValueError("unknown result")
        unit = self.units.get(unit_id)
        return None if unit is None else unit["results"].get(name)
