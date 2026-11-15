"""The engine process: one per state home, driving every unit on its own thread.

Entries hand it requests as files and read its records; they hold no progress,
judgment or recording logic. When this process stops, units keep their place in
their events, and the next engine continues every unit that was running.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

from . import facts
from .facts import InvalidContract
from .git import GitError
from .store import (
    ProcessLock, UnitStore, drop_old_responses, list_units, publish_check_program, read_json, take_requests,
    write_response,
)
from .unit import Unit

CHECKS_FILE = ".dev-skills/checks.json"
POLL_SECONDS = 0.2
LOCK_ATTEMPTS = 20
LOCK_RETRY_SECONDS = 0.05
RESPONSE_LIFETIME_SECONDS = 3600
MAX_ANSWER_BYTES = 100_000
# Stops a later request can continue from; cancelling one of them ends the unit as cancelled.
RESUMABLE_STOPS = ("waiting_for_answer", "unknown_result", "invalid_result")


class Rejected(ValueError):
    """A request the engine refuses; the reason goes back to the entry."""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Engine:
    def __init__(self, root, *, git, agents, templates, runner, worktrees, platform, own_code=(), idle_seconds=30.0,
                 clock=timestamp):
        """`runner` is (python, the check program's file): the engine publishes the program under its
        state home and names that copy in the commands it gives, so an agent's sandbox can read it.
        `platform` is the `platform_facts.Platform` of the machine the engine runs on.
        `own_code` lists the directories the engine's code runs from; a repository
        that contains one of them is refused, so the engine never drives its own code."""
        self.root, self.git, self.agents = root, git, agents
        python, program = runner
        self.runner = (python, publish_check_program(root, program))
        self.templates, self.own_code = templates, tuple(own_code)
        self.worktrees, self.platform = worktrees, platform
        self.idle_seconds, self.clock = idle_seconds, clock
        self._threads = {}
        self._interrupts = {}
        self._stores = {}
        self._halting = threading.Event()
        self._guard = threading.Lock()

    # -- units --

    def _store(self, unit_id):
        with self._guard:
            if unit_id not in self._stores:
                self._stores[unit_id] = UnitStore(self.root, unit_id)
            return self._stores[unit_id]

    def _existing(self, unit_id):
        try:
            store = self._store(unit_id)
        except ValueError:
            raise Rejected("invalid unit id") from None
        if not store.exists():
            raise Rejected("no such unit")
        return store

    def _running(self, unit_id):
        thread = self._threads.get(unit_id)
        return thread is not None and thread.is_alive()

    def _launch(self, unit_id):
        with self._guard:
            if self._running(unit_id):
                return
            interrupt = threading.Event()
            self._interrupts[unit_id] = interrupt
            thread = threading.Thread(target=self._drive, args=(unit_id, interrupt),
                                      name=f"unit-{unit_id}", daemon=True)
            self._threads[unit_id] = thread
            thread.start()

    def _drive(self, unit_id, interrupt):
        store = self._store(unit_id)
        try:
            Unit(store, store.contract(), store.admission(), git=self.git, agents=self.agents,
                 templates=self.templates, runner=self.runner, worktrees=self.worktrees, quote=self.platform.quote,
                 clock=self.clock, interrupt=interrupt, halting=self._halting.is_set).run()
        except Exception as error:  # the unit stops where it is; a retry continues from its events
            detail = f"{type(error).__name__}: {error}"[:1000]
            store.append({"type": "engine_error", "detail": detail, "at": self.clock()})
            store.write_status({"state": "stopped", "stop_reason": "unknown_result", "detail": detail,
                                "revision": len(store.events()), "updated_at": self.clock()})

    # -- requests --

    def start(self, raw):
        contract = facts.validate(raw, self.platform.path_style)
        for execution in contract["executions"]:
            if not self.agents.known(execution["agent"]):
                raise Rejected(f"{execution['id']}.agent is not in the installed catalog")
        repository = contract["repository"]
        names, on_base = facts.check_names(contract)
        try:
            if self.git.toplevel(repository) != repository:
                raise Rejected("repository must be the top directory of a git work tree")
            head = self.git.head(repository)
            base = contract["unit"].get("base", head)
            if base != head and not self.git.is_ancestor(repository, base, head):
                raise Rejected("unit.base is not a commit the repository's current commit was built on")
            raw_checks = self.git.read_file(repository, base, CHECKS_FILE) if names else None
        except (GitError, OSError) as error:
            raise Rejected(f"repository cannot be read: {error}") from None
        real = os.path.realpath(repository)
        if any(os.path.commonpath([os.path.realpath(code), real]) == real for code in self.own_code):
            raise Rejected("the engine does not drive the repository its own code runs from")
        checks = {"checks": {}, "ignore": []}
        if names:
            if raw_checks is None:
                raise Rejected(f"{CHECKS_FILE} is not in the repository's current commit")
            try:
                checks = facts.checks_config(json.loads(raw_checks), names, on_base, self.platform.path_style)
            except ValueError as error:
                raise Rejected(f"{CHECKS_FILE}: {error}") from None
        store = self._store(contract["unit_id"])
        if store.exists():
            if store.contract() != contract:
                raise Rejected("a different unit already has this unit_id")
        else:
            store.create(contract, {"base": base, "head": head, **checks})
            store.write_status({"state": "running", "revision": 0, "updated_at": self.clock()})
            self._launch(contract["unit_id"])
        return store.status()

    def _stopped_for(self, store, reason):
        status = store.status() or {}
        if self._running(store.unit_id) or status.get("state") != "stopped" or status.get("stop_reason") != reason:
            raise Rejected(f"the unit is not stopped for {reason}")
        return status

    def _continue(self, store, event, state):
        """Record the request, save the state it leads to, and drive the unit again."""
        store.append(dict(event, at=self.clock()))
        if state is not None:
            store.write_json("state.json", state)
        store.write_status({"state": "running", "revision": len(store.events()), "updated_at": self.clock()})
        self._launch(store.unit_id)
        return store.status()

    @staticmethod
    def _state(store):
        return read_json(store.path("state.json"))

    def answer(self, unit_id, question_id, answer):
        store = self._existing(unit_id)
        self._stopped_for(store, "waiting_for_answer")
        if not isinstance(answer, str) or not answer.strip() or len(answer.encode("utf-8", "replace")) > MAX_ANSWER_BYTES:
            raise Rejected("answer must be non-empty text")
        state = facts.answer(self._state(store), question_id, answer)
        if state is None:
            raise Rejected("the unit is not waiting for this question")
        return self._continue(store, {"type": "answered", "question_id": question_id, "answer": answer}, state)

    def retry(self, unit_id):
        store = self._existing(unit_id)
        status = store.status() or {}
        if self._running(unit_id) or status.get("state") != "stopped":
            raise Rejected("the unit is not stopped for a result that can be retried")
        state = self._state(store)
        if isinstance(state, dict) and state.get("stop"):
            state = facts.retry(state)
            if state is None:
                raise Rejected("the unit is not stopped for a result that can be retried")
        elif status.get("stop_reason") != "unknown_result":  # the engine itself failed before deciding
            raise Rejected("the unit is not stopped for a result that can be retried")
        return self._continue(store, {"type": "retry"}, state)

    def cancel(self, unit_id):
        store = self._existing(unit_id)
        status = store.status() or {}
        if (not self._running(unit_id) and status.get("state") == "stopped"
                and status.get("stop_reason") not in RESUMABLE_STOPS):
            return status  # a finished unit keeps how it ended
        if not any(event["type"] == "cancel_requested" for event in store.events()):
            store.append({"type": "cancel_requested", "at": self.clock()})
        interrupt = self._interrupts.get(unit_id)
        if self._running(unit_id) and interrupt is not None:
            interrupt.set()
        else:
            state = self._state(store)
            if isinstance(state, dict):  # a stop that could have continued now ends as cancelled
                store.write_json("state.json", facts.cancel(state))
            self._launch(unit_id)
        return store.status()

    def handle(self, payload):
        """One request -> one response. Unknown shapes are refused, never interpreted."""
        try:
            if not isinstance(payload, dict) or not isinstance(payload.get("request"), str):
                raise Rejected("malformed request")
            kind, keys = payload["request"], set(payload) - {"request"}
            if kind == "start" and keys == {"contract"}:
                result = self.start(payload["contract"])
            elif kind == "answer" and keys == {"unit_id", "question_id", "answer"}:
                result = self.answer(payload["unit_id"], payload["question_id"], payload["answer"])
            elif kind == "retry" and keys == {"unit_id"}:
                result = self.retry(payload["unit_id"])
            elif kind == "cancel" and keys == {"unit_id"}:
                result = self.cancel(payload["unit_id"])
            elif kind == "ping" and not keys:
                result = {"engine": "running"}
            else:
                raise Rejected("unknown request")
        except (Rejected, InvalidContract) as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "result": result}

    # -- process --

    def resume(self):
        """Continue every unit a previous engine left running."""
        for unit_id in list_units(self.root):
            store = self._store(unit_id)
            if store.exists() and (store.status() or {}).get("state") == "running":
                self._launch(unit_id)

    def serve(self, lock_path):
        """Serve requests until idle. Returns False when another engine holds the state home.

        Raises `LockFailed` when the state home cannot be locked at all.
        """
        lock = ProcessLock(lock_path)
        for _attempt in range(LOCK_ATTEMPTS):
            if lock.acquire():
                break
            time.sleep(LOCK_RETRY_SECONDS)  # an entry probing for a live engine holds the lock for an instant
        else:
            return False
        try:
            drop_old_responses(self.root, time.time() - RESPONSE_LIFETIME_SECONDS)
            self.resume()
            idle_since = time.monotonic()
            while not self._halting.is_set():
                requests = take_requests(self.root)
                for request_id, payload in requests:
                    write_response(self.root, request_id, self.handle(payload))
                if requests or any(thread.is_alive() for thread in self._threads.values()):
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= self.idle_seconds:
                    break
                time.sleep(POLL_SECONDS)
        finally:
            self.halt()
            lock.release()
        return True

    def halt(self):
        """Stop driving: running turns end, units keep `running` and continue in the next engine."""
        self._halting.set()
        for interrupt in list(self._interrupts.values()):
            interrupt.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=30)
        self.agents.close()
