"""Provider-neutral record of what an agent did during one turn.

Each wire protocol reports tool use in its own shape. A driver feeds those
events to an `ActionLog`, which keeps the latest entries in the order the
agent started them: shell commands with the tail of their output, edits with
their paths, read-only lookups, and everything else by name. A consumer that
judges from this record reads the command text and the output the provider's
own harness reported, never the agent's description of them.

A request the agent made for an approval, a permission or an answer from the
user is refused by every driver; the log keeps each refused request as its kind
and a short description of what was asked.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Final, Iterable


WIRE_VERSION: Final = 1
MAX_ENTRIES: Final = 256
MAX_COMMAND_BYTES: Final = 4_096
MAX_OUTPUT_TAIL_BYTES: Final = 2_048
MAX_PATHS: Final = 64
MAX_NAME_BYTES: Final = 128

KIND_COMMAND: Final = "command"
KIND_EDIT: Final = "edit"
KIND_READ: Final = "read"
KIND_OTHER: Final = "other"
KINDS: Final = frozenset({KIND_COMMAND, KIND_EDIT, KIND_READ, KIND_OTHER})

MAX_REFUSED: Final = 32
MAX_DESCRIPTION_BYTES: Final = 200
# What a refused request asked for: to run a command, to change files, for a
# permission, or for an answer from the user.
REFUSED_COMMAND: Final = "command"
REFUSED_FILE_CHANGE: Final = "file_change"
REFUSED_PERMISSIONS: Final = "permissions"
REFUSED_INPUT: Final = "input"
REFUSED_KINDS: Final = frozenset({REFUSED_COMMAND, REFUSED_FILE_CHANGE, REFUSED_PERMISSIONS, REFUSED_INPUT})


def _head(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore"), True


def _tail(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    return raw[-limit:].decode("utf-8", "ignore"), True


class ActionLog:
    """The bounded, ordered actions of one turn, keyed by the provider's call id."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: "OrderedDict[str, dict[str, object]]" = OrderedDict()
        self._sequence = 0
        self._dropped = 0
        self._refused: list[dict[str, str]] = []
        self._refused_dropped = 0

    def begin(
        self, call_id: str, kind: str | None, *, name: str | None = None,
        command: str | None = None, paths: Iterable[str] = (),
    ) -> None:
        """Record that an action started, or add what a later event told about it.

        `kind` None keeps the kind an earlier event gave; an action first seen
        without a kind is recorded as `other`.
        """
        if kind is not None and kind not in KINDS:
            kind = KIND_OTHER
        with self._lock:
            entry = self._entries.get(call_id)
            if entry is None:
                self._sequence += 1
                entry = {
                    "seq": self._sequence, "kind": kind or KIND_OTHER, "name": None, "command": None,
                    "command_truncated": False, "paths": [], "ended": False,
                    "output_tail": None, "output_truncated": False, "exit_code": None,
                }
                self._entries[call_id] = entry
                while len(self._entries) > MAX_ENTRIES:
                    self._entries.popitem(last=False)
                    self._dropped += 1
            elif kind is not None:
                entry["kind"] = kind
            if name is not None:
                entry["name"] = _head(name, MAX_NAME_BYTES)[0]
            if command is not None:
                entry["command"], entry["command_truncated"] = _head(command, MAX_COMMAND_BYTES)
            known = entry["paths"]
            assert isinstance(known, list)
            for path in paths:
                if isinstance(path, str) and path not in known and len(known) < MAX_PATHS:
                    known.append(path)

    def output(self, call_id: str, output: str) -> None:
        """Record the output of a started action that has not reported its end yet."""
        with self._lock:
            entry = self._entries.get(call_id)
            if entry is not None:
                entry["output_tail"], entry["output_truncated"] = _tail(output, MAX_OUTPUT_TAIL_BYTES)

    def end(
        self, call_id: str, *, output: str | None = None, exit_code: int | None = None,
        keep_recorded_output: bool = False,
    ) -> None:
        """Record that a started action finished, with the output its provider reported.

        `keep_recorded_output` leaves output recorded by `output` in place.
        """
        with self._lock:
            entry = self._entries.get(call_id)
            if entry is None:
                return
            entry["ended"] = True
            if output is not None and not (keep_recorded_output and entry["output_tail"] is not None):
                entry["output_tail"], entry["output_truncated"] = _tail(output, MAX_OUTPUT_TAIL_BYTES)
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                entry["exit_code"] = exit_code

    def refuse(self, kind: str, description: object) -> None:
        """Record one request of the agent that the driver refused.

        `description` is what the provider showed as the request's title or
        reason, collapsed to one line and cut to a bounded length.
        """
        if kind not in REFUSED_KINDS:
            raise ValueError(f"unknown-refused-kind:{kind}")
        text = " ".join(description.split()) if isinstance(description, str) else ""
        with self._lock:
            if len(self._refused) >= MAX_REFUSED:
                self._refused_dropped += 1
                return
            self._refused.append({"kind": kind, "description": _head(text, MAX_DESCRIPTION_BYTES)[0]})

    def to_wire(self) -> dict[str, object]:
        """The entries in start order; `dropped` counts older entries no longer held.

        `refused` lists the refused requests in the order they came;
        `refused_dropped` counts those past the bound.
        """
        with self._lock:
            return {
                "version": WIRE_VERSION,
                "dropped": self._dropped,
                "entries": [
                    {**entry, "paths": list(entry["paths"])}  # type: ignore[call-overload]
                    for entry in self._entries.values()
                ],
                "refused": [dict(item) for item in self._refused],
                "refused_dropped": self._refused_dropped,
            }
