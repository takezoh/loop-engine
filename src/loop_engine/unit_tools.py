"""The unit tools an entry offers a session: start a unit from its contract, follow it, answer it,
retry it, cancel it and read its result.

Each tool has one purpose and a closed input schema. The tools hand requests to the engine as files
and read the engine's records. They hold no progress or judgment logic, start no agent and run no
command a caller names; the engine is a separate process. A caller that builds contracts itself
offers its own start tool and hands the contract to `UnitTools.start`.
"""
import os
import re
from pathlib import Path

from . import store

MAX_RESULT_BYTES = 200_000
TEXT = {"type": "string", "minLength": 1}
UNIT = {"type": "string", "pattern": store.ID_RE.pattern}


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = {
    "unit_start": (
        "Start one unit from its contract: the unit, the facts that must hold and the executions that can "
        "establish them. Returns the unit's status; the unit runs on.",
        schema({"contract": {"type": "object",
                             "description": "The contract. Its repository is the absolute path of a git work tree "
                                            "inside this connection's directory."}}, ["contract"])),
    "unit_status": ("Read a unit's current status.", schema({"unit_id": UNIT}, ["unit_id"])),
    "unit_wait": (
        "Wait at most 30 seconds for a unit to stop or move on. Timing out does not cancel the unit.",
        schema({"unit_id": UNIT, "after_revision": {"type": "integer", "minimum": 0},
                "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 30000}}, ["unit_id", "timeout_ms"])),
    "unit_answer": (
        "Answer the question a unit is waiting on.",
        schema({"unit_id": UNIT, "question_id": TEXT, "answer": TEXT}, ["unit_id", "question_id", "answer"])),
    "unit_retry": ("Run again the turn whose result was unknown or could not be checked.",
                   schema({"unit_id": UNIT}, ["unit_id"])),
    "unit_cancel": ("Cancel a unit. A unit that already ended keeps how it ended.", schema({"unit_id": UNIT}, ["unit_id"])),
    "unit_result": (
        "Read a stopped unit's result: how it stopped and which facts held (stop.json), or its changes (patch.diff).",
        schema({"unit_id": UNIT, "name": {"enum": ["stop.json", "patch.diff"]}}, ["unit_id", "name"])),
}
INSTRUCTIONS = ("Use unit_start to run a unit from its contract, unit_wait or unit_status to follow it, "
                "unit_answer when it asks a question, and unit_result once it stopped. "
                "A wait that times out does not cancel the unit.")


class Refused(ValueError):
    """A request this entry or the engine does not accept."""


def misfit(declared, value, where="arguments"):
    """Why `value` does not fit the schema `declared`, or None. It reads the keywords the tool schemas use:
    enum, the types object, string and integer, properties, required, minLength, pattern, minimum and
    maximum. An object that declares no properties is left to its owner. A pattern applies to the whole
    value."""
    if "enum" in declared:
        return None if value in declared["enum"] else f"{where} is not one of {', '.join(declared['enum'])}"
    kind = declared.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{where} is not an object"
        properties = declared.get("properties")
        if properties is None:
            return None
        for name in declared.get("required", ()):
            if name not in value:
                return f"{where} lacks {name}"
        for name, item in value.items():
            if name not in properties:
                return f"{where} has a key the tool does not take"
            reason = misfit(properties[name], item, name)
            if reason:
                return reason
    elif kind == "string":
        if not isinstance(value, str) or len(value) < declared.get("minLength", 0):
            return f"{where} is not a string of the required length"
        if "pattern" in declared and not re.fullmatch(declared["pattern"], value):
            return f"{where} does not have the required form"
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{where} is not an integer"
        if value < declared.get("minimum", value) or value > declared.get("maximum", value):
            return f"{where} is out of range"
    return None


class UnitTools:
    """The unit tools of one connection. The connection's directory and the engine's records root are
    fixed here; tool arguments cannot change them."""

    tools, instructions = TOOLS, INSTRUCTIONS

    def __init__(self, connection, engine_root, *, client, environ):
        self.connection = Path(connection).resolve(strict=True)
        self.engine_root, self.client, self.environ = engine_root, client, dict(environ)

    def repository_of(self, unit_id):
        """The repository a recorded unit was started for, or None."""
        try:
            contract = store.read_json(os.path.join(store.unit_dir(self.engine_root, unit_id), "contract.json"))
        except ValueError:
            return None
        return contract.get("repository") if isinstance(contract, dict) else None

    def admit(self, repository, unit_id):
        """Refuse a start this connection may not make: a repository outside its directory, or a unit id
        another connection holds. Such an id is refused without describing the unit."""
        path = Path(repository) if isinstance(repository, str) else None
        if (path is None or not path.is_absolute() or not path.is_dir() or path.resolve() != path
                or not path.is_relative_to(self.connection)):
            raise Refused("repository is not a directory inside this connection")
        taken = self.repository_of(unit_id)
        if isinstance(taken, str) and not Path(taken).resolve().is_relative_to(self.connection):
            raise Refused("this unit_id cannot be used")

    def start(self, contract):
        """Hand a contract to the engine. The engine validates it; this entry only fixes whose it is."""
        if not isinstance(contract, dict):
            raise Refused("contract is not an object")
        self.admit(contract.get("repository"), contract.get("unit_id"))
        return self._send({"request": "start", "contract": contract})

    def _own(self, unit_id):
        """A unit of another connection does not exist as far as this connection can tell."""
        repository = self.repository_of(unit_id)
        if not isinstance(repository, str) or not Path(repository).resolve().is_relative_to(self.connection):
            raise Refused("no such unit")

    def _send(self, payload):
        response = self.client.request(self.engine_root, payload, environ=self.environ)
        if not response.get("ok"):
            raise Refused(str(response.get("error")))
        return response["result"]

    def invoke(self, name, arguments):
        if name not in TOOLS:
            raise Refused("unknown tool")
        reason = misfit(TOOLS[name][1], arguments)
        if reason:
            raise Refused(reason)
        if name == "unit_start":
            return self.start(arguments["contract"])
        unit_id = arguments["unit_id"]
        self._own(unit_id)
        if name == "unit_status":
            return self.client.status(self.engine_root, unit_id)
        if name == "unit_wait":
            return self.client.wait(self.engine_root, unit_id, after_revision=arguments.get("after_revision"),
                                    timeout=arguments["timeout_ms"] / 1000, environ=self.environ)
        if name == "unit_answer":
            return self._send({"request": "answer", "unit_id": unit_id, "question_id": arguments["question_id"],
                               "answer": arguments["answer"]})
        if name in ("unit_retry", "unit_cancel"):
            return self._send({"request": name.removeprefix("unit_"), "unit_id": unit_id})
        data = self.client.result(self.engine_root, unit_id, arguments["name"])
        if data is None:
            raise Refused("the unit has no such result yet")
        return {"name": arguments["name"], "truncated": len(data) > MAX_RESULT_BYTES,
                "text": data[:MAX_RESULT_BYTES].decode("utf-8", "replace")}
