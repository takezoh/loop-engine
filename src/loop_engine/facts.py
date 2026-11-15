"""Pure: a unit is complete when every required fact holds for its present inputs.

The contract is data. `facts` say what must hold and how each is judged; `executions`
say which agent turn reads and establishes which facts. No order is written: `decide`
derives the next execution, and a fact stops holding when what it was judged against
changes. Decisions: adr-20261114-unit-completes-when-facts-hold and
adr-20261114-verdict-computed-from-checkable-findings (both accepted).
"""
import hashlib
import json
import re

SEVERITY = {"minor": 1, "major": 2, "blocker": 3}
STATUS_RE = re.compile(
    r"^loop-engine-check (\S+) tree=(candidate|base) digest=([0-9a-f]+)(?: base=([0-9a-f]+))? exit=(-?\d+)$")
MAX_INVALID = 2
NOT_ACCEPTED = "result not accepted: "


ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CHECK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,95}$")
MAX_TEXT_BYTES = 200_000
MAX_ROUNDS = 10
MAX_ITEMS = 32
CONTRACT_KEYS = frozenset({"unit_id", "repository", "unit", "facts", "executions"})
UNIT_KEYS = frozenset({"goal", "criteria", "scope", "limits"})
UNIT_OPTIONAL = frozenset({"allow_same_model", "examine_min", "base"})
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# The structure of a unit: the facts, the executions that can establish them, and how they refer to
# each other. A fact has id, required, by and the keys of its kind.
FACT_KEYS = {"given": set(), "report": set(), "git": {"subject"}, "checks": {"subject"},
             "verdict": {"subject", "block_at"}}
EXECUTION_KEYS = frozenset({"id", "reads", "establishes", "writes", "independent_of"})
# What a contract adds to the structure for a run: the checks a `checks` fact asks for, and who runs
# each execution with which instructions.
FACT_RUN_KEYS = {"checks": {"checks"}}
EXECUTION_RUN_KEYS = frozenset({"agent", "model", "effort", "instructions"})


class InvalidContract(ValueError):
    """The contract is not the exact shape the engine accepts."""


def _text(value, name):
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value.encode("utf-8", "replace")) > MAX_TEXT_BYTES):
        raise InvalidContract(f"{name} must be non-empty text of at most {MAX_TEXT_BYTES} bytes")


def _keys(name, value, wanted):
    unknown, missing = sorted(set(value) - set(wanted)), sorted(set(wanted) - set(value))
    if unknown or missing:
        raise InvalidContract(f"{name}: " + "; ".join(
            [*(["unknown key " + ", ".join(unknown)] if unknown else []),
             *(["missing key " + ", ".join(missing)] if missing else [])]))


def _named(kind, items, index):
    item = items[index]
    if not isinstance(item, dict):
        raise InvalidContract(f"{kind} {index + 1} must be an object")
    if not isinstance(item.get("id"), str) or not ID_RE.match(item["id"]):
        raise InvalidContract(f"{kind} {index + 1}: id {item.get('id')!r} is not an admitted id")
    return f"{kind} {item['id']}"


def check_limits(limits):
    """Pure: the limits of a unit, or raise `InvalidContract`."""
    if (not isinstance(limits, dict) or set(limits) != {"rounds"} or isinstance(limits["rounds"], bool)
            or not isinstance(limits["rounds"], int) or not 1 <= limits["rounds"] <= MAX_ROUNDS):
        raise InvalidContract(f"limits.rounds must be 1..{MAX_ROUNDS}")


def check_structure(facts, executions):
    """Pure: the facts of a unit and the executions that can establish them, without what a run adds
    (`FACT_RUN_KEYS`, `EXECUTION_RUN_KEYS`), or raise `InvalidContract` naming the fact or execution.

    Checks the keys and ids, the kinds of fact, the references between facts and executions, which
    executions may establish which kind of fact, and that what an execution reads does not depend on
    what it establishes."""
    if not isinstance(facts, list) or not 1 <= len(facts) <= MAX_ITEMS:
        raise InvalidContract(f"facts must be a list of 1..{MAX_ITEMS} facts")
    for index, fact in enumerate(facts):
        name = _named("fact", facts, index)
        if fact.get("by") not in FACT_KEYS:
            raise InvalidContract(f"{name}: by must be one of " + ", ".join(FACT_KEYS))
        _keys(name, fact, {"id", "required", "by"} | FACT_KEYS[fact["by"]])
        if not isinstance(fact["required"], bool):
            raise InvalidContract(f"{name}: required must be a boolean")
        if "block_at" in fact and fact["block_at"] not in SEVERITY:
            raise InvalidContract(f"{name}: block_at must be one of " + ", ".join(SEVERITY))
    if not isinstance(executions, list) or not 1 <= len(executions) <= MAX_ITEMS:
        raise InvalidContract(f"executions must be a list of 1..{MAX_ITEMS} executions")
    for index, ex in enumerate(executions):
        name = _named("execution", executions, index)
        _keys(name, ex, EXECUTION_KEYS)
        if not isinstance(ex["writes"], bool):
            raise InvalidContract(f"{name}: writes must be a boolean")
        for key in ("reads", "establishes", "independent_of"):
            if not isinstance(ex[key], list) or any(not isinstance(ref, str) for ref in ex[key]):
                raise InvalidContract(f"{name}: {key} must be a list of ids")
        if not ex["establishes"]:
            raise InvalidContract(f"{name}: an execution establishes at least one fact")
    by_fact = {f["id"]: f for f in facts}
    execs = {e["id"]: e for e in executions}
    if len(by_fact) != len(facts) or len(execs) != len(executions):
        raise InvalidContract("duplicate id")
    for eid, ex in execs.items():
        for ref in [*ex["reads"], *ex["establishes"]]:
            if ref not in by_fact:
                raise InvalidContract(f"execution {eid}: unknown fact {ref}")
        if set(ex["reads"]) & set(ex["establishes"]):
            raise InvalidContract(f"execution {eid}: an execution cannot read a fact it establishes")
        for other in ex["independent_of"]:
            if other not in execs:
                raise InvalidContract(f"execution {eid}: unknown execution {other} in independent_of")
    makers = {fid: [e["id"] for e in executions if fid in e["establishes"]] for fid in by_fact}
    for fid, fact in by_fact.items():
        if "subject" in fact and fact["subject"] not in by_fact:
            raise InvalidContract(f"fact {fid}: subject {fact['subject']!r} is not a fact of the unit")
        if fact["by"] in ("given", "git") and makers[fid]:
            raise InvalidContract(f"fact {fid}: the engine establishes this fact")
        if fact["by"] == "report" and len(makers[fid]) != 1:
            raise InvalidContract(f"fact {fid}: exactly one execution establishes a report")
        if fact["by"] == "checks":
            subject_makers = makers[fact["subject"]]
            if makers[fid] != subject_makers or not all(execs[m]["writes"] for m in subject_makers):
                raise InvalidContract(f"fact {fid}: checks are read from the record of the execution that wrote the subject")
        if fact["by"] == "verdict" and not makers[fid]:
            raise InvalidContract(f"fact {fid}: no execution establishes it")
    structure = {"facts": facts, "executions": executions}
    seen = set()

    def visit(fid, path):  # reads must not form a cycle through establishes
        if fid in path:
            raise InvalidContract(f"cycle through {fid}")
        if fid in seen:
            return
        for dep in deps(structure, fid):
            visit(dep, path | {fid})
        seen.add(fid)

    for fid in by_fact:
        visit(fid, frozenset())


def _without(items, keys_of):
    """The items without the keys `keys_of(item)` names; anything that is not a list of objects as it is."""
    if not isinstance(items, list):
        return items
    return [{k: v for k, v in item.items() if k not in keys_of(item)} if isinstance(item, dict) else item
            for item in items]


def _shape(contract):
    """Keys, ids and text. No value names a program or a path the engine executes; the strings
    that reach an agent command line (agent, model, effort) are held to a fixed alphabet."""
    if not isinstance(contract, dict) or set(contract) != CONTRACT_KEYS:
        raise InvalidContract("contract keys must be exactly " + ", ".join(sorted(CONTRACT_KEYS)))
    if not isinstance(contract["unit_id"], str) or not ID_RE.match(contract["unit_id"]):
        raise InvalidContract("unit_id is not an admitted id")
    repository = contract["repository"]
    if (not isinstance(repository, str) or not repository.startswith("/") or "\x00" in repository
            or any(part in ("", ".", "..") for part in repository.split("/")[1:])):
        raise InvalidContract("repository must be a normalized absolute path")
    unit = contract["unit"]
    if not isinstance(unit, dict) or not UNIT_KEYS <= set(unit) or set(unit) - UNIT_KEYS - UNIT_OPTIONAL:
        raise InvalidContract("unit keys must be goal, criteria, scope, limits")
    _text(unit["goal"], "unit.goal")
    criteria = unit["criteria"]
    if (not isinstance(criteria, list) or len(criteria) > MAX_ITEMS
            or any(not isinstance(c, dict) or set(c) != {"id", "text"} or not isinstance(c["id"], str)
                   or not ID_RE.match(c["id"]) for c in criteria)
            or len({c["id"] for c in criteria}) != len(criteria)):
        raise InvalidContract("unit.criteria must be unique {id, text} entries")
    for criterion in criteria:
        _text(criterion["text"], f"criterion {criterion['id']}")
    scope = unit["scope"]
    if (not isinstance(scope, list) or len(scope) > MAX_ITEMS or any(
            not isinstance(p, str) or not p or p.startswith("/") or "\x00" in p
            or any(part in ("", ".", "..") for part in p.rstrip("/").split("/")) for p in scope)):
        raise InvalidContract("unit.scope must be relative paths inside the repository")
    check_limits(unit["limits"])
    if "base" in unit and (not isinstance(unit["base"], str) or not COMMIT_RE.match(unit["base"])):
        raise InvalidContract("unit.base must be a full commit id")  # what an existing change is compared with
    if not isinstance(unit.get("allow_same_model", False), bool):
        raise InvalidContract("unit.allow_same_model must be a boolean")
    share = unit.get("examine_min", 1.0)
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= share <= 1:
        raise InvalidContract("unit.examine_min must be 0..1")
    check_structure(_without(contract["facts"], lambda f: FACT_RUN_KEYS.get(f.get("by"), ())),
                    _without(contract["executions"], lambda e: EXECUTION_RUN_KEYS))
    for fact in contract["facts"]:
        _keys(f"fact {fact['id']}", fact, {"id", "required", "by"} | FACT_KEYS[fact["by"]]
              | FACT_RUN_KEYS.get(fact["by"], set()))
        if fact["by"] == "checks":
            checks = fact["checks"]
            if (not isinstance(checks, list) or not checks or len(checks) > MAX_ITEMS or any(
                    not isinstance(c, dict) or set(c) != {"name", "expect"} or not isinstance(c["name"], str)
                    or not CHECK_NAME_RE.match(c["name"]) or c["expect"] not in ("pass", "fails_on_base")
                    for c in checks)):
                raise InvalidContract(f"fact {fact['id']}: checks must be {{name, expect}} entries")
    for ex in contract["executions"]:
        _keys(f"execution {ex['id']}", ex, EXECUTION_KEYS | EXECUTION_RUN_KEYS)
        if not isinstance(ex["agent"], str) or not ID_RE.match(ex["agent"]):
            raise InvalidContract(f"execution {ex['id']}: agent is not an admitted id")
        for key in ("model", "effort"):
            if ex[key] is not None and (not isinstance(ex[key], str) or not TARGET_RE.match(ex[key])):
                raise InvalidContract(f"execution {ex['id']}: {key} is not an admitted name")
        _text(ex["instructions"], f"execution {ex['id']}: instructions")


def check_names(contract):
    """The check names the contract's facts ask for, and those that must fail on the base revision."""
    wanted = [c for f in contract["facts"] if f["by"] == "checks" for c in f["checks"]]
    return sorted({c["name"] for c in wanted}), sorted({c["name"] for c in wanted if c["expect"] == "fails_on_base"})


def checks_config(raw, names, on_base):
    """What the project's checks file defines for the named checks, or raise `InvalidContract`.

    The file is read from the base commit: {"checks": {name: {"argv": [...], "test_paths": [...]}},
    "ignore": [...]}. `test_paths` is what the candidate's tests replace on the base revision.
    """
    if not isinstance(raw, dict) or not {"checks"} <= set(raw) <= {"checks", "ignore"} or not isinstance(raw["checks"], dict):
        raise InvalidContract('checks file must be {"checks": {name: {"argv": [...]}}, "ignore": [...]}')
    ignore = raw.get("ignore", [])
    if not isinstance(ignore, list) or any(not isinstance(n, str) or not n or "/" in n or n in (".", "..") for n in ignore):
        raise InvalidContract("ignore must be a list of file or directory names")

    def strings(value):
        return isinstance(value, list) and all(isinstance(part, str) and part and "\x00" not in part for part in value)
    checks = {}
    for name in names:
        entry = raw["checks"].get(name)
        if not isinstance(entry, dict) or not {"argv"} <= set(entry) <= {"argv", "test_paths"} or not strings(entry["argv"]) or not entry["argv"]:
            raise InvalidContract(f"check {name!r} is not defined with a non-empty argv list")
        paths = entry.get("test_paths", [])
        if not strings(paths) or any(p.startswith("/") or any(part in ("", ".", "..") for part in p.split("/")) for p in paths):
            raise InvalidContract(f"check {name!r}: test_paths must be relative paths")
        if name in on_base and not paths:
            raise InvalidContract(f"check {name!r} must fail on the base revision but declares no test_paths")
        checks[name] = {"argv": list(entry["argv"]), "test_paths": list(paths)}
    return {"checks": checks, "ignore": list(ignore)}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def validate(contract):
    """The contract, or raise `InvalidContract`: its unit, its structure (`check_structure`), what a run
    adds to it, and that each execution is independent of those it names."""
    _shape(contract)
    execs = {e["id"]: e for e in contract["executions"]}
    for eid, ex in execs.items():
        for other in ex["independent_of"]:
            same = (execs[other]["agent"], execs[other]["model"]) == (ex["agent"], ex["model"])
            if same and not contract["unit"].get("allow_same_model"):
                raise InvalidContract(f"execution {eid}: not independent of {other} (same agent and model)")
    return contract


def _fact(contract, fid):
    return next(f for f in contract["facts"] if f["id"] == fid)


def _makers(contract, fid):
    return [e for e in contract["executions"] if fid in e["establishes"]]


def deps(contract, fid):
    """The facts whose content a fact was judged against."""
    fact = _fact(contract, fid)
    found = [fact["subject"]] if fact.get("subject") else []
    if fact["by"] != "checks":  # checks are judged against the subject alone
        for ex in _makers(contract, fid):
            found += [r for r in ex["reads"] if r != fid]
    return sorted(set(found))


def new_state(tree, changed=(), base_tree=None):
    """`base_tree` is the digest of the copy of the base revision the engine placed for checks on it."""
    return {"tree": tree, "changed": sorted(changed), "base_tree": base_tree, "facts": {}, "votes": {},
            "invalid": {}, "feedback": {}, "last_findings": {}, "reported": {}, "rounds": 0, "stop": None,
            "notes": [], "log": [], "sessions": {}, "refused": {}}


def content(contract, state, fid):
    if _fact(contract, fid)["by"] == "given":
        return state["tree"]
    record = state["facts"].get(fid)
    if not record or record.get("tree") not in (None, state["tree"]):
        return None  # a candidate recorded for another tree is not the present candidate
    return record["content"]


def basis(contract, state, fid):
    return {dep: content(contract, state, dep) for dep in deps(contract, fid)}


def held(contract, state, fid):
    if _fact(contract, fid)["by"] == "given":
        return True
    record = state["facts"].get(fid)
    return bool(record) and record["ok"] and record["basis"] == basis(contract, state, fid) \
        and all(held(contract, state, dep) for dep in deps(contract, fid))


def _current(contract, state, fid):
    record = state["facts"].get(fid)
    return record if record and record["basis"] == basis(contract, state, fid) else None


# ---- judging -------------------------------------------------------------------------------

def expected_command(runner, check, tree):
    return f"{runner} {check}" + (" --base" if tree == "base" else "")


def judge_checks(fact, record, runner, tree_digest, base_digest=None):
    """Failed checks by name; empty when the record shows each one run on the present tree.

    The fixed program prints the digest of the tree it ran on. An agent can change files through
    the shell without the record marking an edit, so the order of entries is not relied on. A check
    on the base revision also names the copy of the base revision it ran on, which must be the one
    the engine placed (`base_digest`).
    """
    entries = record.get("entries", [])
    failed = {}
    for check in fact["checks"]:
        tree = "base" if check["expect"] == "fails_on_base" else "candidate"
        command = expected_command(runner, check["name"], tree)
        runs = [e for e in entries if e["kind"] == "command" and e["command"] == command and e["ended"]
                ]
        if not runs:
            failed[f"{check['name']}:{tree}"] = "not run"
            continue
        lines = (runs[-1].get("output_tail") or "").strip().splitlines()
        match = STATUS_RE.match(lines[-1]) if lines else None
        name, ran_on, digest, base, code = match.groups() if match else (None,) * 5
        if not match or name != check["name"] or ran_on != tree or (base is None) != (tree == "candidate"):
            failed[f"{check['name']}:{tree}"] = "no status line"
        elif digest != tree_digest:
            failed[f"{check['name']}:{tree}"] = "not run on the present tree"
        elif tree == "base" and base != base_digest:
            failed[f"{check['name']}:{tree}"] = "not run on the base revision the engine placed"
        elif tree == "candidate" and code != "0":
            failed[f"{check['name']}:{tree}"] = f"exit {code}"
        elif tree == "base" and code == "0":
            failed[f"{check['name']}:{tree}"] = "passes on the base revision: no test fails without the change"
    return failed


def tail_json(text):
    text = (text or "").rstrip()
    if text.endswith("```"):  # agents often fence the object
        text = text[:-3].rstrip()
    if not text.endswith("}"):
        return None
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        try:
            value = json.loads(text[start:])
        except ValueError:
            continue
        return value if isinstance(value, dict) else None
    return None


def question(text):
    """The question a reply ends with, or None."""
    value = tail_json(text)
    if (value is None or set(value) != {"question", "id"} or not isinstance(value["question"], str)
            or not isinstance(value["id"], str) or not ID_RE.match(value["id"])):
        return None
    return value


def examined(record, paths):
    """The share of the given paths that appear in what the agent read or ran."""
    if not paths:
        return 1.0
    seen = set()
    for entry in record.get("entries", []):
        haystack = " ".join([*(entry.get("paths") or []), entry.get("command") or ""])
        for path in paths:
            if path in haystack or path.rsplit("/", 1)[-1] in haystack:
                seen.add(path)
    return len(seen) / len(paths)


def _cited(citation, read, report=None):
    """The quoted text is in the present file, or in the report under judgment when no path is named.

    Agents miscount lines, so the line is only a hint.
    """
    text = read(citation["path"]) if isinstance(citation.get("path"), str) and citation["path"] else report
    quote = " ".join(str(citation.get("quote", "")).split())
    if text is None or len(quote) < 8:
        return False
    return quote in " ".join(text.split())


def judge_verdict(unit, fact, text, record, read, subject_paths, report=None):
    """('invalid', reason) or ('ok'|'refuted', findings, notes). The reviewer never states approval.

    `report` is the text under judgment when the subject is a report and not files.
    """
    value = tail_json(text)
    if value is None or set(value) != {"criteria", "findings"}:
        return ("invalid", "reply must end with the criteria/findings object")
    criteria, findings = value["criteria"], value["findings"]
    wanted = [c["id"] for c in unit["criteria"]]
    if not isinstance(criteria, list) or sorted(str(c.get("id")) for c in criteria) != sorted(wanted):
        return ("invalid", "every criterion needs exactly one judgment")
    if not isinstance(findings, list) or any(f.get("severity") not in SEVERITY for f in findings):
        return ("invalid", "findings need a severity")
    commands = {e["command"] for e in record.get("entries", []) if e["kind"] == "command" and e["command"]}
    for finding in findings:
        if not _cited(finding, read, report):
            return ("invalid", f"finding does not quote the present file or report: {finding.get('path')}:{finding.get('line')}")
    for judgment in criteria:
        evidence = judgment.get("evidence") or []
        if judgment.get("met") is True:
            if not evidence:
                return ("invalid", f"criterion {judgment['id']} is called met without evidence")
            for item in evidence:
                if "command" in item:  # agents report a command without its redirections; containment is enough
                    told = " ".join(str(item["command"]).split())
                    if len(told) < 3 or not any(told in " ".join(c.split()) for c in commands):
                        return ("invalid", f"evidence for {judgment['id']}: the command {told!r} is not among the "
                                           "commands you ran in this turn")
                elif not _cited(item, read, report):
                    return ("invalid", f"evidence for {judgment['id']}: the quote is not at "
                                       f"{item.get('path')}:{item.get('line')} of the present file")
        elif judgment.get("met") is False:
            if not any(f.get("criterion") == judgment["id"] for f in findings):
                return ("invalid", f"criterion {judgment['id']} is called unmet without a finding")
        else:
            return ("invalid", "met must be true or false")
    if examined(record, subject_paths) < unit.get("examine_min", 1.0):
        return ("invalid", "the record does not show the changed files being examined")
    level = SEVERITY[fact["block_at"]]
    blocking = [f for f in findings if SEVERITY[f["severity"]] >= level]
    unmet = [j["id"] for j in criteria if j["met"] is False]
    for criterion in unmet:  # an unmet criterion blocks whatever weight its finding carries
        blocking += [f for f in findings if f.get("criterion") == criterion and f not in blocking]
    notes = [f for f in findings if f not in blocking]
    return ("refuted" if blocking else "ok", blocking, notes)


def judge_git(unit, state):
    outside = [p for p in state["changed"] if not any(p == s or p.startswith(s.rstrip("/") + "/") for s in unit["scope"])]
    return [{"severity": "major", "path": p, "description": "changed outside the declared scope"} for p in outside]


# ---- transitions ---------------------------------------------------------------------------

def _record(contract, state, fid, ok, body, findings=()):
    state["facts"][fid] = {"ok": ok, "basis": basis(contract, state, fid), "content": sha(body),
                           "findings": list(findings)}


def _key(finding):
    return (finding.get("path"), finding.get("criterion"), " ".join(str(finding.get("description", "")).split()))


def settle(contract, state):
    """Judge what the engine itself can judge, and send refuted facts back to their subject."""
    unit = contract["unit"]
    for fact in contract["facts"]:
        fid = fact["id"]
        if state["stop"]:
            return
        if fact["by"] == "git" and held(contract, state, fact["subject"]) and not _current(contract, state, fid):
            findings = judge_git(unit, state)
            _record(contract, state, fid, not findings, [state["tree"], findings], findings)
        if fact["by"] == "verdict" and not _current(contract, state, fid):
            now = sha(basis(contract, state, fid))
            votes = [state["votes"].get(f"{fid}/{ex['id']}") for ex in _makers(contract, fid)]
            if all(v and v["basis"] == now for v in votes) and all(
                    held(contract, state, dep) for dep in deps(contract, fid)):
                findings = [f for v in votes for f in v["findings"]]
                state["notes"] += [n for v in votes for n in v["notes"]]
                _record(contract, state, fid, not findings, [now, findings], findings)
        record = _current(contract, state, fid)
        if not record or record["ok"] or record.get("returned"):
            continue
        record["returned"] = True
        subject = fact["subject"]
        makers = _makers(contract, subject)
        if not makers:
            state["stop"] = {"reason": "rejected", "fact": fid, "findings": record["findings"]}
            return
        keys = sorted(map(str, map(_key, record["findings"])))
        if state["last_findings"].get(fid) == keys:
            state["stop"] = {"reason": "stagnation", "fact": fid, "findings": record["findings"]}
            return
        state["last_findings"][fid] = keys
        state["rounds"] += 1
        if state["rounds"] >= unit["limits"]["rounds"]:
            state["stop"] = {"reason": "rounds_exhausted", "fact": fid, "findings": record["findings"]}
            return
        for maker in makers:
            state["feedback"].setdefault(maker["id"], []).extend(
                {"fact": fid, **finding} for finding in record["findings"])
            for produced in maker["establishes"]:  # the subject and what was read from the same turn
                state["facts"].pop(produced, None)


def decide(contract, state):
    """(state, action). action: run an execution, or stop with a reason."""
    state = json.loads(json.dumps(state))  # the state is plain JSON by contract
    if state["stop"]:
        return state, {"do": "stop", **state["stop"]}
    settle(contract, state)
    if state["stop"]:
        return state, {"do": "stop", **state["stop"]}
    required = [f["id"] for f in contract["facts"] if f["required"]]
    if all(held(contract, state, fid) for fid in required):
        state["stop"] = {"reason": "done"}
        return state, {"do": "stop", "reason": "done"}
    for ex in contract["executions"]:
        if not all(held(contract, state, r) for r in ex["reads"]):
            continue
        open_facts = []
        for fid in ex["establishes"]:
            fact = _fact(contract, fid)
            if fact["by"] == "verdict":
                if not held(contract, state, fact["subject"]) or _current(contract, state, fid):
                    continue
                vote = state["votes"].get(f"{fid}/{ex['id']}")
                if vote and vote["basis"] == sha(basis(contract, state, fid)):
                    continue
                open_facts.append(fid)
            elif not held(contract, state, fid):
                open_facts.append(fid)
        if open_facts:
            return state, {"do": "run", "execution": ex["id"], "feedback": state["feedback"].get(ex["id"], [])}
    waiting = [fid for fid in required if not held(contract, state, fid)]
    state["stop"] = {"reason": "stuck", "facts": waiting}
    return state, {"do": "stop", **state["stop"]}


def apply_turn(contract, state, execution_id, outcome, *, read, runner="RUNNER"):
    """Record what one finished turn established. `outcome`: completed, text, record, tree, changed."""
    state = json.loads(json.dumps(state))  # the state is plain JSON by contract
    ex = next(e for e in contract["executions"] if e["id"] == execution_id)
    state["log"].append(execution_id)
    if not outcome.get("completed"):
        state["stop"] = {"reason": "unknown_result", "execution": execution_id}
        if outcome.get("detail"):  # why the turn proves nothing, when the engine could tell
            state["stop"]["detail"] = outcome["detail"]
        return state
    record = outcome.get("record") or {"entries": []}
    asked = question(outcome.get("text"))
    if asked is not None:
        state["stop"] = {"reason": "waiting_for_answer", "execution": execution_id, "question": asked}
        return state
    feedback = state["feedback"].pop(execution_id, None) or []
    had_feedback = bool(feedback)
    if ex["writes"]:
        # failed checks can be answered by running them again; any other finding asks for a change
        needs_change = any(_fact(contract, f["fact"])["by"] != "checks" for f in feedback if "fact" in f)
        if needs_change and outcome["tree"] == state["tree"]:
            state["stop"] = {"reason": "stagnation", "execution": execution_id, "detail": "candidate unchanged"}
            return state
        state["tree"], state["changed"] = outcome["tree"], sorted(outcome.get("changed", ()))
    for fid in sorted(ex["establishes"], key=lambda f: _fact(contract, f)["by"] != "report"):
        fact = _fact(contract, fid)
        if fact["by"] == "report":
            body = state["tree"] if ex["writes"] else outcome.get("text", "")
            if had_feedback and not ex["writes"] and sha(body) == state["reported"].get(fid):
                state["stop"] = {"reason": "stagnation", "execution": execution_id, "detail": "report unchanged"}
                return state
            state["reported"][fid] = sha(body)
            _record(contract, state, fid, True, body)
            state["facts"][fid]["text"] = (outcome.get("text") or "")[:MAX_TEXT_BYTES]
            if ex["writes"]:
                state["facts"][fid]["tree"] = state["tree"]
        elif fact["by"] == "checks":
            failed = judge_checks(fact, record, runner, state["tree"], state["base_tree"])
            findings = [{"severity": "major", "check": name, "description": why} for name, why in sorted(failed.items())]
            _record(contract, state, fid, not failed, [state["tree"], failed], findings)
        elif fact["by"] == "verdict":
            key, now = f"{fid}/{execution_id}", sha(basis(contract, state, fid))
            subject = state["facts"].get(fact["subject"]) or {}
            report = subject.get("text") if "tree" not in subject else None  # files are quoted from the tree
            result = judge_verdict(contract["unit"], fact, outcome.get("text"), record, read, state["changed"], report)
            if result[0] == "invalid":
                count = state["invalid"].get(key, 0) + 1
                state["invalid"][key] = count
                state["feedback"][execution_id] = [{"severity": "major", "description": NOT_ACCEPTED + result[1]}]
                if count >= MAX_INVALID:
                    state["stop"] = {"reason": "invalid_result", "execution": execution_id, "detail": result[1]}
                continue
            state["invalid"].pop(key, None)
            state["votes"][key] = {"basis": now, "findings": result[1], "notes": result[2]}
    return state


# ---- agent sessions --------------------------------------------------------------------------
#
# Each execution keeps the session its first turn ran in, and every later turn of the same
# execution continues it. A session belongs to one execution: no other execution is ever
# given its token. Sessions end when the unit stops for good.

def session_of(state, execution_id):
    """The session the execution's next turn continues, or None to start a new one."""
    entry = state["sessions"].get(execution_id)
    return entry["token"] if entry else None


def delivered(state, execution_id):
    """What the execution's session has been sent: report id -> digest of the text it was given."""
    entry = state["sessions"].get(execution_id)
    return dict(entry["reports"]) if entry else {}


def remember_session(state, execution_id, token, reports):
    """Keep the session a turn ran in and what it has now been given; None forgets it."""
    state = json.loads(json.dumps(state))
    if token is None:
        state["sessions"].pop(execution_id, None)
    else:
        state["sessions"][execution_id] = {"token": token, "reports": dict(reports)}
    return state


def forget_sessions(state):
    """(state without sessions, [(execution, token)] to close): the unit stopped for good."""
    state = json.loads(json.dumps(state))
    ended = sorted((eid, entry["token"]) for eid, entry in state["sessions"].items())
    state["sessions"] = {}
    return state, ended


def count_refused(state, execution_id, count):
    """Add the requests of a turn that the agent's transport refused to the execution's count."""
    if not count:
        return state
    state = json.loads(json.dumps(state))
    state["refused"][execution_id] = state["refused"].get(execution_id, 0) + count
    return state


# ---- requests from outside the unit --------------------------------------------------------

def answer(state, question_id, text):
    """Give the answer to the execution that asked; None when the unit is not waiting for this question."""
    stop = state["stop"] or {}
    if stop.get("reason") != "waiting_for_answer" or stop["question"].get("id") != question_id:
        return None
    state = json.loads(json.dumps(state))
    state["feedback"].setdefault(stop["execution"], []).append(
        {"severity": "minor", "answer": question_id, "description": f"Answer to your question {question_id}: {text}"})
    state["stop"] = None
    return state


def retry(state):
    """Run again the turn whose result was unknown or could not be checked; None for any other stop."""
    stop = state["stop"] or {}
    if stop.get("reason") not in ("unknown_result", "invalid_result"):
        return None
    state = json.loads(json.dumps(state))
    state["invalid"], state["stop"] = {}, None
    return state


def cancel(state):
    state = json.loads(json.dumps(state))
    state["stop"] = {"reason": "cancelled"}
    return state


def summary(contract, state):
    """How each fact stands, for the result: what holds, how it was judged, and what was noted."""
    return {"facts": {f["id"]: {"by": f["by"], "required": f["required"], "holds": held(contract, state, f["id"])}
                      for f in contract["facts"]},
            "notes": state["notes"], "turns": state["log"], "rounds": state["rounds"],
            "refused": dict(state["refused"])}
