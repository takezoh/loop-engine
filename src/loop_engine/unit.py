"""Drive one unit from its recorded state to its next stop.

Every decision comes from `facts`; this module performs what it asks through
the store, git and the agent port, observes the worktree, and records what
happened. Re-running it after any interruption continues from the saved state.
"""
import hashlib
import os
import secrets
import shutil

from . import facts, prompts
from .agents import TurnRequest
from .check_runner import is_link
from .store import verified_check_program, worktree_path
from .worktree import (
    AGENT_CONFIG_DIRS, AGENT_CONFIG_FILES, UNIT_DIRECTORY_RE, Unreadable, agent_config_digest, changed_paths, environment_entries, read_text,
    tree_digest, tree_links, unexpected_entries,
)

# Stops a later request can continue from keep their worktree; every other stop is final.
CONTINUABLE = ("waiting_for_answer", "unknown_result", "invalid_result")
# The record lists at most this many environment entries, each name cut to this many characters.
MAX_LISTED = facts.MAX_ITEMS
MAX_NAME = 1000
# The agent configuration a writer is told not to change: the same names the engine watches.
AGENT_CONFIG = (AGENT_CONFIG_DIRS, AGENT_CONFIG_FILES)


class Unit:
    def __init__(self, store, contract, admission, *, git, agents, templates, runner, worktrees, quote, clock,
                 interrupt, halting):
        """`admission` is the base commit and the checks the engine fixed when it accepted the unit.

        `runner` is (python, check_runner_path); `worktrees` is the directory unit worktrees are
        made in; `quote(argv)` is the command line the agent is told to run (`Platform.quote`);
        `clock()` returns an ISO timestamp.
        `interrupt` is an event that ends a running turn; `halting()` tells that the
        engine itself is stopping, so the unit must keep its place and record nothing.
        """
        self.store, self.contract, self.admission = store, contract, admission
        self.git, self.agents, self.templates = git, agents, templates
        self.runner, self.worktrees, self.quote, self.clock = runner, worktrees, quote, clock
        self.interrupt, self.halting = interrupt, halting
        self.ignore = admission["ignore"]
        self._base_files = None  # git's record of the base commit, read once

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
        The worktree is where agents write. The copy of the base revision, which a check on the base
        revision runs the candidate's tests on and a reviewer reads, is in the unit's published directory,
        which agents read and only the engine writes.
        """
        repository, base = self.contract["repository"], self.admission["base"]
        # The worktree root is writable by every agent, so the directory gets a name nobody can
        # prepare in advance, and it must be exactly where the engine put it: no link on the way.
        token = secrets.token_hex(8)
        name = f"{self.store.unit_id}-{token}"
        path = worktree_path(self.worktrees, self.git.common_dir(repository), name)
        expected = os.path.join(os.path.realpath(self.worktrees), os.path.basename(os.path.dirname(path)), name)
        git_dir = self.git.add_worktree(repository, path, self.admission["head"])
        base_tree = self.store.published(f"base-{token}")
        self.git.add_worktree(repository, base_tree, base)
        if os.path.realpath(path) != expected:
            raise RuntimeError("the unit worktree is not where the engine created it")
        path = expected
        # What the environment may place in the worktree is told apart by names neither commit has.
        committed = sorted({*self.git.top_names(repository, base),
                            *self.git.top_names(repository, self.admission["head"])})
        self.store.publish_json("checks.json", {"cwd": path, "base": base_tree, "ignore": self.ignore,
                                                "committed": committed, "checks": self.admission["checks"]})
        started = {"type": "unit_started", "base": base, "worktree": path, "base_tree": base_tree,
                   "git_dir": git_dir, "committed": committed, "agent_config": agent_config_digest(path, committed)}
        self._save(facts.new_state(tree_digest(path, self.ignore, committed), self._changed(started),
                                   tree_digest(base_tree, self.ignore, committed)))
        self._append(started)

    def _changed(self, started):
        """The paths whose content differs from git's record of the base commit, except the new files
        that git ignores by the base revision's rules and by the worktree's own; raises `Unreadable`.

        The record and the base revision's rules are read from the git directory fixed when the
        worktree was made and from the engine's copy of the base revision, never from a tree an agent
        can write. The worktree's rules can only keep a file counted: a file is left out only when both
        rule sets exclude it, so whatever git would put in the patch is counted.
        """
        if self._base_files is None:
            self._base_files = self.git.tree_files(started["worktree"], started["git_dir"], started["base"])
        changed = changed_paths(self._base_files, started["worktree"], self.ignore, started["committed"])
        new = [path for path in changed if path not in self._base_files[1]]
        ignored = self.git.ignored(started["base_tree"], started["git_dir"], new, index=False)
        ignored &= self.git.ignored(started["worktree"], started["git_dir"], sorted(ignored), index=True)
        return [path for path in changed if path not in ignored]

    def _left_out(self, started):
        """The environment's entries now in the worktree, as the record lists them: names only, capped."""
        names = environment_entries(started["worktree"], started["committed"])
        return {"count": len(names), "names": [name[:MAX_NAME] for name in names[:MAX_LISTED]]}

    def _runner(self):
        """The command prefix of every check: the published check program, verified before it is named."""
        return self.quote([self.runner[0], "-I", verified_check_program(self.runner[1]),
                           self.store.published("checks.json")])

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
        reports = prompts.inputs(self.contract, state, execution)
        checks = self._check_commands(execution)

        def first_message():
            return prompts.compose(self.templates, self.contract, execution, action["feedback"],
                                   check_commands=checks, changed=state["changed"], base=started["base_tree"],
                                   reports=reports, answers=state["answers"], agent_config=AGENT_CONFIG)

        session = facts.session_of(state, execution["id"])
        prompt = first_message() if session is None else prompts.follow_up(
            self.templates, self.contract, execution, action["feedback"], check_commands=checks,
            changed=state["changed"], reports=reports, sent=facts.delivered(state, execution["id"]),
            answers=state["answers"], answers_sent=facts.answers_delivered(state, execution["id"]),
            agent_config=AGENT_CONFIG)
        # What git would record, so caches a reviewer's own test run leaves behind do not count as a change.
        before = None if execution["writes"] else self._candidate(started)
        self._append({"type": "turn_started", "execution": execution["id"], "turn": number,
                      "session": "new" if session is None else "continued"})
        self._status(state="running", execution=execution["id"], turn=number, round=state["rounds"],
                     refused=state["refused"])
        outcome = self.agents.run(self._request(execution, number, worktree, prompt, session), self.interrupt)
        if session is not None and outcome.session_lost and not self.interrupt.is_set():
            # The session cannot be continued: this turn runs once more, in a new session with the full message.
            self._append({"type": "session_not_continuable", "execution": execution["id"], "turn": number,
                          "detail": outcome.detail})
            state = facts.remember_session(state, execution["id"], None, {}, 0)
            session = None
            if not self._safe(started):
                return self._unsafe(state, execution["id"], "before")
            self._append({"type": "turn_started", "execution": execution["id"], "turn": number, "session": "new"})
            outcome = self.agents.run(self._request(execution, number, worktree, first_message(), None),
                                      self.interrupt)
        turn = outcome.to_wire()
        self.store.write_result(os.path.join("turns", f"{number:02d}-{execution['id']}.json"), turn)
        if self.interrupt.is_set():
            # cancelled or halting: the interrupted turn proves nothing; a session this unit does not
            # keep is closed, the kept one stays for the next turn of the execution
            if outcome.session is not None and outcome.session != session:
                self.agents.end_session(execution["agent"], outcome.session)
            return state if self.halting() else facts.count_refused(state, execution["id"], outcome.refused)
        state = facts.count_refused(state, execution["id"], outcome.refused)
        state = facts.remember_session(state, execution["id"], outcome.session, prompts.digests(reports),
                                       len(state["answers"]))
        if not outcome.released:
            # A process of the agent may still be using the worktree: nothing in it is judged for this turn.
            state = facts.apply_turn(self.contract, state, execution["id"],
                                     {"completed": False, "detail": turn["detail"]}, read=None)
            self._append({"type": "turn_finished", "execution": execution["id"], "turn": number,
                          "completed": False, "detail": turn["detail"], "refused_requests": outcome.refused,
                          "not_counted": None, "environment_entries": None})
            return state
        if not self._safe(started):
            return self._unsafe(state, execution["id"], "after")
        try:
            # Every turn's worktree is read in full before anything is judged: git, too, leaves out
            # what it cannot read, so a path left out would shrink the candidate and the patch.
            tree = tree_digest(worktree, self.ignore, started["committed"])
            changed = self._changed(started)
            left_out = self._left_out(started)
        except Unreadable as error:
            where = os.path.relpath(error.filename, worktree) if isinstance(error.filename, str) else "?"
            detail = f"worktree-not-readable: {where}"[:1000]
            state = facts.apply_turn(self.contract, state, execution["id"], {"completed": False, "detail": detail},
                                     read=None)
            self._append({"type": "turn_finished", "execution": execution["id"], "turn": number,
                          "completed": False, "detail": detail, "refused_requests": outcome.refused,
                          "not_counted": None, "environment_entries": None})
            return state
        if not execution["writes"]:
            tree = state["tree"]
        # What git records is compared only for a turn that must not write and says it completed.
        wrote = bool(turn["completed"]) and not execution["writes"] and self._candidate(started) != before
        completed, detail = facts.turn_completion(self.contract, execution["id"], turn, wrote=wrote)
        state = facts.apply_turn(self.contract, state, execution["id"], {
            "completed": completed, "detail": detail, "text": turn["text"],
            "record": turn["actions"] or {"entries": []}, "tree": tree, "changed": changed,
        }, read=lambda relative: read_text(worktree, relative), runner=self._runner())
        refused = [item["description"] for item in state["feedback"].get(execution["id"], [])
                   if str(item.get("description", "")).startswith(facts.NOT_ACCEPTED)]
        self._append({"type": "turn_finished", "execution": execution["id"], "turn": number,
                      "completed": completed, "detail": detail, "refused_requests": outcome.refused,
                      "not_counted": refused[0][len(facts.NOT_ACCEPTED):] if refused else None,
                      "environment_entries": left_out})
        return state

    def _request(self, execution, number, worktree, prompt, session):
        return TurnRequest(
            unit_id=self.store.unit_id, role=execution["id"], round=number, agent=execution["agent"],
            model=execution["model"], effort=execution["effort"], cwd=worktree, prompt=prompt, session=session,
            excluded_skills=self.contract["unit"].get("excluded_skills"),
        )

    def _patch(self, started):
        worktree = started["worktree"]
        return self.git.patch(worktree, started["base"], started["git_dir"],
                              environment_entries(worktree, started["committed"]), tree_links(worktree))

    def _candidate(self, started):
        return hashlib.sha256(self._patch(started)).hexdigest()

    def _safe(self, started):
        """The worktree's agent configuration is what it was, readable in full, and nothing was put above it."""
        return (started["agent_config"] is not None
                and agent_config_digest(started["worktree"], started["committed"]) == started["agent_config"]
                and not unexpected_entries(self.worktrees, started["worktree"]))

    def _unsafe(self, state, execution, when):
        self._append({"type": "agent_config_changed", "execution": execution, "when": when})
        return facts.unsafe_agent_config(state, execution)

    def _stop(self, state, action):
        started = self._started()
        reason = action["reason"]
        patch = self._patch(started) if started else b""
        stop = {"stop_reason": reason, "base": started["base"] if started else None,
                "worktree": started["worktree"] if started else None,
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
                # what the worktree holds now and the patch leaves out, read from the worktree at the stop
                "environment_entries": self._left_out(started) if started else None,
                "stop": {key: value for key, value in action.items() if key != "do"},
                **facts.summary(self.contract, state)}
        self.store.write_stop(patch, stop)
        self._append({"type": "stopped", "stop_reason": reason})
        if reason not in CONTINUABLE:
            self._end_sessions(state)
        removed = reason not in CONTINUABLE and started is not None and self._discard(started)
        asked = action.get("question") if reason == "waiting_for_answer" else None
        self._status(state="stopped", stop_reason=reason, detail=action.get("detail"), round=state["rounds"],
                     worktree=stop["worktree"],
                     worktree_removed=bool(removed), refused=state["refused"],
                     question={"id": asked["id"], "text": asked["question"]} if asked else None)
        return reason

    def _end_sessions(self, state):
        """Close every session the unit kept; the unit has stopped for good and continues none of them."""
        state, ended = facts.forget_sessions(state)
        agents = {ex["id"]: ex["agent"] for ex in self.contract["executions"]}
        for execution, token in ended:
            try:
                self.agents.end_session(agents[execution], token)
            except Exception:
                pass  # the engine's shutdown still ends every process it started
        if ended:
            self._save(state)
            self._append({"type": "sessions_closed", "executions": [execution for execution, _ in ended]})

    def _discard(self, started):
        """Remove the unit's worktree and its copy of the base revision once it ended for good.

        Its changes are kept in patch.diff.

        Only a directory the engine created is removed: it must still be where the engine put it,
        with the name the engine gave it. Links inside it are removed, never followed.
        """
        root, worktree, base_tree = os.path.realpath(self.worktrees), started["worktree"], started["base_tree"]
        removed = True
        for path, owned in (
                (worktree, os.path.dirname(os.path.dirname(worktree)) == root
                 and UNIT_DIRECTORY_RE.fullmatch(os.path.basename(worktree)) and os.path.realpath(worktree) == worktree),
                (base_tree, os.path.dirname(base_tree) == self.store.published_dir)):
            if not owned or is_link(path):
                removed = False
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed = removed and not os.path.exists(path)
        self.git.prune_worktrees(self.contract["repository"])
        return removed

    def run(self):
        """Advance until a stop and return its reason, or None when the engine is halting."""
        while True:
            if self.halting():
                return None
            started = self._started()
            if started is None:
                self._prepare()
                continue
            state = self.store.state()
            if state["stop"] is None and any(event["type"] == "cancel_requested" for event in self.store.events()):
                state = facts.cancel(state)  # a unit that already reached a stop keeps how it ended
            state, action = facts.decide(self.contract, state)
            self._save(state)
            if action["do"] == "stop":
                return self._stop(state, action)
            self._save(self._turn(started, state, action))
