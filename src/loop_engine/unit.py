"""Drive one unit from its recorded state to its next stop.

Every decision comes from `facts`; this module performs what it asks through
the store, git and the agent port, observes the worktree, and records what
happened. Re-running it after any interruption continues from the saved state.
"""
import hashlib
import os
import secrets
import shlex

from . import facts, prompts
from .agents import TurnRequest
from .store import read_json, worktree_path
from .worktree import agent_config_digest, changed_paths, read_text, tree_digest, unexpected_entries


class Unit:
    def __init__(self, store, contract, admission, *, git, agents, templates, runner, worktrees, clock,
                 interrupt, halting):
        """`admission` is the base commit and the checks the engine fixed when it accepted the unit.

        `runner` is (python, check_runner_path); `worktrees` is the directory unit worktrees are
        made in; `clock()` returns an ISO timestamp.
        `interrupt` is an event that ends a running turn; `halting()` tells that the
        engine itself is stopping, so the unit must keep its place and record nothing.
        """
        self.store, self.contract, self.admission = store, contract, admission
        self.git, self.agents, self.templates = git, agents, templates
        self.runner, self.worktrees, self.clock = runner, worktrees, clock
        self.interrupt, self.halting = interrupt, halting
        self.ignore = admission["ignore"]

    # -- recording --

    def _append(self, event):
        self.store.append(dict(event, at=self.clock()))

    def _save(self, state):
        self.store.write_json("state.json", state)

    def _status(self, **fields):
        self.store.write_status(dict(fields, revision=len(self.store.events()), updated_at=self.clock()))

    def _started(self):
        return next((event for event in self.store.events() if event["type"] == "unit_started"), None)

    # -- steps --

    def _prepare(self):
        """Create the worktree at the repository's commit and a copy of the base revision, and fix the checks.

        The two differ only when the contract names an earlier base: the unit then judges an existing change.
        """
        repository, base = self.contract["repository"], self.admission["base"]
        # The worktree root is writable by every agent, so the directory gets a name nobody can
        # prepare in advance, and it must be exactly where the engine put it: no link on the way.
        name = f"{self.store.unit_id}-{secrets.token_hex(8)}"
        path = worktree_path(self.worktrees, self.git.common_dir(repository), name)
        expected = os.path.join(os.path.realpath(self.worktrees), os.path.basename(os.path.dirname(path)), name)
        git_dir = self.git.add_worktree(repository, path, self.admission["head"])
        self.git.add_worktree(repository, path + ".base", base)
        if os.path.realpath(path) != expected or os.path.realpath(path + ".base") != expected + ".base":
            raise RuntimeError("the unit worktree is not where the engine created it")
        path = expected
        self.store.write_json("checks.json", {"cwd": path, "base": path + ".base", "ignore": self.ignore,
                                              "checks": self.admission["checks"]})
        self._save(facts.new_state(tree_digest(path, self.ignore), changed_paths(path + ".base", path, self.ignore)))
        self._append({"type": "unit_started", "base": base, "worktree": path, "base_tree": path + ".base",
                      "git_dir": git_dir, "agent_config": agent_config_digest(path)})

    def _runner(self):
        return shlex.join([self.runner[0], "-I", self.runner[1], self.store.path("checks.json")])

    def _check_commands(self, execution):
        by_id = {fact["id"]: fact for fact in self.contract["facts"]}
        return [facts.expected_command(self._runner(), check["name"],
                                       "base" if check["expect"] == "fails_on_base" else "candidate")
                for fid in execution["establishes"] if by_id[fid]["by"] == "checks" for check in by_id[fid]["checks"]]

    def _turn(self, started, state, action):
        worktree = started["worktree"]
        execution = next(ex for ex in self.contract["executions"] if ex["id"] == action["execution"])
        number = len(state["log"]) + 1
        if not self._safe(started):
            return self._unsafe(state, execution["id"], "before")
        prompt = prompts.compose(
            self.templates, self.contract, execution, action["feedback"],
            check_commands=self._check_commands(execution), changed=state["changed"],
            base=started["base_tree"], reports=prompts.inputs(self.contract, state, execution),
        )
        # What git would record, so caches a reviewer's own test run leaves behind do not count as a change.
        before = None if execution["writes"] else self._candidate(started)
        self._append({"type": "turn_started", "execution": execution["id"], "turn": number})
        self._status(state="running", execution=execution["id"], turn=number, round=state["rounds"])
        outcome = self.agents.run(TurnRequest(
            unit_id=self.store.unit_id, role=execution["id"], round=number, agent=execution["agent"],
            model=execution["model"], effort=execution["effort"], cwd=worktree, prompt=prompt,
        ), self.interrupt)
        turn = outcome.to_wire()
        self.store.write_result(os.path.join("turns", f"{number:02d}-{execution['id']}.json"), turn)
        if self.interrupt.is_set():
            return state  # cancelled or halting: the interrupted turn proves nothing
        if not self._safe(started):
            return self._unsafe(state, execution["id"], "after")
        tree = tree_digest(worktree, self.ignore) if execution["writes"] else state["tree"]
        completed, detail = bool(turn["completed"]), turn["detail"]
        by = {fact["id"]: fact["by"] for fact in self.contract["facts"]}
        judged = any(by[fid] in ("checks", "verdict") for fid in execution["establishes"])
        if completed and judged and not isinstance(turn["actions"], dict) and facts.question(turn["text"]) is None:
            completed, detail = False, "no-execution-record"  # nothing the turn did can be checked
        if completed and not execution["writes"] and self._candidate(started) != before:
            completed, detail = False, "a-turn-that-must-not-write-changed-the-worktree"
        state = facts.apply_turn(self.contract, state, execution["id"], {
            "completed": completed, "text": turn["text"], "record": turn["actions"] or {"entries": []},
            "tree": tree, "changed": changed_paths(started["base_tree"], worktree, self.ignore),
        }, read=lambda relative: read_text(worktree, relative), runner=self._runner())
        refused = [item["description"] for item in state["feedback"].get(execution["id"], [])
                   if str(item.get("description", "")).startswith(facts.NOT_ACCEPTED)]
        self._append({"type": "turn_finished", "execution": execution["id"], "turn": number,
                      "completed": completed, "detail": detail,
                      "not_counted": refused[0][len(facts.NOT_ACCEPTED):] if refused else None})
        return state

    def _candidate(self, started):
        return hashlib.sha256(self.git.patch(started["worktree"], started["base"], started["git_dir"])).hexdigest()

    def _safe(self, started):
        """The worktree's agent configuration is what it was, readable in full, and nothing was put above it."""
        return (started["agent_config"] is not None
                and agent_config_digest(started["worktree"]) == started["agent_config"]
                and not unexpected_entries(self.worktrees, started["worktree"]))

    def _unsafe(self, state, execution, when):
        self._append({"type": "agent_config_changed", "execution": execution, "when": when})
        state["stop"] = {"reason": "unsafe_agent_config", "execution": execution}
        return state

    def _stop(self, state, action):
        started = self._started()
        reason = action["reason"]
        patch = self.git.patch(started["worktree"], started["base"], started["git_dir"]) if started else b""
        self.store.write_result("patch.diff", patch)
        stop = {"stop_reason": reason, "base": started["base"] if started else None,
                "worktree": started["worktree"] if started else None,
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
                "stop": {key: value for key, value in action.items() if key != "do"},
                **facts.summary(self.contract, state)}
        self.store.write_result("stop.json", stop)
        self._append({"type": "stopped", "stop_reason": reason})
        asked = action.get("question") if reason == "waiting_for_answer" else None
        self._status(state="stopped", stop_reason=reason, round=state["rounds"], worktree=stop["worktree"],
                     question={"id": asked["id"], "text": asked["question"]} if asked else None)
        return reason

    def run(self):
        """Advance until a stop and return its reason, or None when the engine is halting."""
        while True:
            if self.halting():
                return None
            started = self._started()
            if started is None:
                self._prepare()
                continue
            state = read_json(self.store.path("state.json"))
            if state["stop"] is None and any(event["type"] == "cancel_requested" for event in self.store.events()):
                state = facts.cancel(state)  # a unit that already reached a stop keeps how it ended
            state, action = facts.decide(self.contract, state)
            self._save(state)
            if action["do"] == "stop":
                return self._stop(state, action)
            self._save(self._turn(started, state, action))
