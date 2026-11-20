"""Pure: compose the text of one turn from the contract, the execution and the protocol templates.

The first turn of a session gets the role's full message. A later turn of the
same session gets the role's follow-up: what is new since the session's
previous turn, and how to answer.
"""
import hashlib


def _bullets(items, empty="(none)"):
    items = list(items)
    return "\n".join(f"- {item}" for item in items) if items else empty


def _agent_config(agent_config):
    """`agent_config` is (directory names, file names) of the agent configuration a writer must not change."""
    directories, files = agent_config
    return ", ".join(f"`{name}`" for name in (*(f"{d}/" for d in directories), *files))


def kind(contract, execution):
    """Which protocol an execution follows: it changes files, it judges, or it only reports."""
    if execution["writes"]:
        return "writer"
    by = {fact["id"]: fact["by"] for fact in contract["facts"]}
    return "reviewer" if any(by[fid] == "verdict" for fid in execution["establishes"]) else "reporter"


def inputs(contract, state, execution):
    """The reports an execution is given: what it reads, except a report about files it must judge itself.

    A reviewer of a change reads the files, never the author's account of them. A reviewer of a
    report is given that report, because it is the thing being judged.
    """
    facts = {fact["id"]: fact for fact in contract["facts"]}
    writers = {fid for ex in contract["executions"] if ex["writes"] for fid in ex["establishes"]}
    wanted = list(execution["reads"])
    for fid in execution["establishes"]:
        subject = facts[fid].get("subject")
        if facts[fid]["by"] == "verdict" and subject not in wanted:
            wanted.append(subject)
    reports = {}
    for fid in wanted:
        text = (state["facts"].get(fid) or {}).get("text")
        if facts[fid]["by"] == "report" and fid not in writers and text:
            reports[fid] = text
    return reports


def _finding(item):
    where = item.get("check") or ":".join(str(item[key]) for key in ("path", "line") if item.get(key) is not None)
    return " ".join(part for part in (f"[{item['fact']}]" if item.get("fact") else "", where,
                                      str(item.get("description", ""))) if part)


def digests(reports):
    """Report id -> digest of its text: what a session was given."""
    return {fid: hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] for fid, text in reports.items()}


def changed_reports(reports, sent):
    """The reports whose text the session has not been given."""
    now = digests(reports)
    return {fid: text for fid, text in reports.items() if sent.get(fid) != now[fid]}


def follow_up(templates, contract, execution, feedback, *, check_commands, changed, reports, sent, answers,
              answers_sent, agent_config):
    """The message that continues an execution's session: feedback, what changed, and how to answer.

    `sent` is what the session was already given (`digests`); only reports it has not seen go in.
    `answers_sent` is how many of the requester's `answers` it was given; only the later ones go in.
    """
    return _ending(templates, templates[kind(contract, execution) + "-follow-up"].format(
        agent_config=_agent_config(agent_config),
        feedback=_bullets(map(_finding, feedback)),
        checks="\n".join(check_commands) if check_commands else "(no checks are configured for this turn)",
        changed=_bullets(changed),
        reports=_reports(changed_reports(reports, sent)),
        answers=_answers(answers[answers_sent:]),
    ))


def _ending(templates, text):
    """Every message of every role ends with how to ask the requester instead of replying."""
    return text.rstrip("\n") + "\n\n" + templates["question"]


def _reports(reports):
    return "\n\n".join(f"### {fid}\n\n{text.strip()}" for fid, text in reports.items()) or "(none)"


def _answers(answers):
    return "\n\n".join(f"### {item['id']}\n\nQuestion: {item['question'].strip()}\n\nAnswer: {item['answer'].strip()}"
                       for item in answers) or "(none)"


def compose(templates, contract, execution, feedback, *, check_commands, changed, base, reports, answers,
            agent_config):
    """`templates` maps writer | reviewer | reporter -> text with named fields, and question -> the
    text every message ends with."""
    unit = contract["unit"]
    return _ending(templates, templates[kind(contract, execution)].format(
        agent_config=_agent_config(agent_config),
        instructions=execution["instructions"].strip(),
        goal=unit["goal"].strip(),
        criteria=_bullets(f"{c['id']}: {c['text']}" for c in unit["criteria"]),
        scope=_bullets(unit["scope"], "(no file may be changed)"),
        feedback=_bullets(map(_finding, feedback)),
        checks="\n".join(check_commands) if check_commands else "(no checks are configured for this turn)",
        changed=_bullets(changed),
        base=base,
        reports=_reports(reports),
        answers=_answers(answers),
    ))
