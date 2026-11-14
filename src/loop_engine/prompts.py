"""Pure: compose the text of one turn from the contract, the execution and the protocol templates."""


def _bullets(items, empty="(none)"):
    items = list(items)
    return "\n".join(f"- {item}" for item in items) if items else empty


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


def compose(templates, contract, execution, feedback, *, check_commands, changed, base, reports):
    """`templates` maps writer | reviewer | reporter -> text with named fields."""
    unit = contract["unit"]
    return templates[kind(contract, execution)].format(
        instructions=execution["instructions"].strip(),
        goal=unit["goal"].strip(),
        criteria=_bullets(f"{c['id']}: {c['text']}" for c in unit["criteria"]),
        scope=_bullets(unit["scope"], "(no file may be changed)"),
        feedback=_bullets(map(_finding, feedback)),
        checks="\n".join(check_commands) if check_commands else "(no checks are configured for this turn)",
        changed=_bullets(changed),
        base=base,
        reports="\n\n".join(f"### {fid}\n\n{text.strip()}" for fid, text in reports.items()) or "(none)",
    )
