"""Summarize the units recorded in a Loop Engine state home.

    unit_report.py [--home DIR] [--json] [unit_id ...]

Reads only the engine's records (contract, events, state, result). For each unit it
shows how it stopped, how many turns each execution took, how long they ran, which turns
continued the execution's session, what each turn's execution record contains, how many of
the agent's requests for approval were refused, which facts held, and which environment entries
were left out of each turn's judgment and of the patch. Use it to compare runs before
and after a change to the engine, a Profile, the Knowledge or a prompt.
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from loop_engine.store import home_root, list_units, read_json, stop_result, unit_dir, unit_status  # noqa: E402


def _seconds(start, end):
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def _events(path):
    events = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def _names(listed):
    """The environment entries a record lists, with how many more it did not name; None when not judged."""
    if not isinstance(listed, dict):
        return None
    names = [name for name in listed.get("names") or [] if isinstance(name, str)]
    count = listed.get("count") if isinstance(listed.get("count"), int) else len(names)
    return {"names": names, "count": count}


def _listed(entries):
    more = entries["count"] - len(entries["names"])
    return ", ".join(entries["names"]) + (f" and {more} more" if more > 0 else "")


def load_unit(root, unit_id):
    """One unit as plain values, or None when the state home has no such unit."""
    base = unit_dir(root, unit_id)
    contract = read_json(os.path.join(base, "contract.json"))
    if contract is None:
        return None
    events = _events(os.path.join(base, "events.jsonl"))
    stop_text = stop_result(root, unit_id, "stop.json")
    stop = json.loads(stop_text) if stop_text else {}
    status = unit_status(root, unit_id) or {}
    turns, open_turn = [], {}
    for event in events:
        if event.get("type") == "turn_started":
            open_turn = event
        elif event.get("type") == "turn_finished":
            name = f"{event['turn']:02d}-{event['execution']}.json"
            record = read_json(os.path.join(base, "result", "turns", name)) or {}
            entries = (record.get("actions") or {}).get("entries") or []
            kinds = {}
            for entry in entries:
                kinds[entry.get("kind")] = kinds.get(entry.get("kind"), 0) + 1
            turns.append({"turn": event["turn"], "execution": event["execution"], "completed": event.get("completed"),
                          "detail": event.get("detail"), "not_counted": event.get("not_counted"),
                          "seconds": _seconds(open_turn.get("at"), event.get("at")),
                          "session": open_turn.get("session"), "refused": record.get("refused") or 0,
                          "actions": kinds, "text_chars": len(record.get("text") or ""),
                          "environment_entries": _names(event.get("environment_entries"))})
    by_execution = {}
    refused = {}
    for turn in turns:
        by_execution[turn["execution"]] = by_execution.get(turn["execution"], 0) + 1
        if turn["refused"]:
            refused[turn["execution"]] = refused.get(turn["execution"], 0) + turn["refused"]
    agents = {ex["id"]: {"agent": ex["agent"], "model": ex["model"], "effort": ex["effort"]}
              for ex in contract["executions"]}
    findings = (stop.get("stop") or {}).get("findings") or []
    return {
        "unit_id": unit_id, "state": status.get("state"), "stop_reason": status.get("stop_reason"),
        "rounds": stop.get("rounds", status.get("round")), "turns": turns, "turns_by_execution": by_execution,
        "refused_by_execution": refused,
        "sessions_replaced": sum(1 for event in events if event.get("type") == "session_not_continuable"),
        "seconds": _seconds(events[0].get("at"), events[-1].get("at")) if events else None,
        "agents": agents, "facts": {name: fact["holds"] for name, fact in (stop.get("facts") or {}).items()},
        "notes": len(stop.get("notes") or []),
        # left out of the patch although present in the worktree at the stop
        "environment_entries": _names(stop.get("environment_entries")),
        "findings": [{key: finding.get(key) for key in ("severity", "criterion", "path", "check", "description")}
                     for finding in findings],
    }


def summarize(units):
    """Counts over the given units: how they stopped and what they cost."""
    stops = {}
    for unit in units:
        stops[unit["stop_reason"] or unit["state"] or "unknown"] = stops.get(unit["stop_reason"] or unit["state"] or "unknown", 0) + 1
    turn_counts = [len(unit["turns"]) for unit in units]
    repeats = sum(count - 1 for unit in units for count in unit["turns_by_execution"].values())
    return {
        "units": len(units), "stops": stops,
        "turns_total": sum(turn_counts), "turns_median": statistics.median(turn_counts) if turn_counts else 0,
        "turns_repeated": repeats,  # turns beyond the first of each execution: send-backs and results asked for again
        "incomplete_turns": sum(1 for unit in units for turn in unit["turns"] if not turn["completed"]),
        "results_not_counted": sum(1 for unit in units for turn in unit["turns"] if turn["not_counted"]),
        "turns_continued": sum(1 for unit in units for turn in unit["turns"] if turn["session"] == "continued"),
        "sessions_replaced": sum(unit["sessions_replaced"] for unit in units),
        "refused_requests": sum(sum(unit["refused_by_execution"].values()) for unit in units),
        "seconds_total": round(sum(unit["seconds"] or 0 for unit in units), 1),
    }


def render(units, summary):
    lines = []
    for unit in units:
        lines.append(f"{unit['unit_id']}: {unit['stop_reason'] or unit['state']} | rounds {unit['rounds']} | "
                     f"{len(unit['turns'])} turns | {unit['seconds']} s")
        for turn in unit["turns"]:
            who = unit["agents"].get(turn["execution"], {})
            acts = ", ".join(f"{kind} {count}" for kind, count in sorted(turn["actions"].items(), key=lambda kv: str(kv[0])))
            state = "ok" if turn["completed"] else f"incomplete ({turn['detail']})"
            if turn["not_counted"]:
                state = f"not counted ({turn['not_counted']})"
            if turn["refused"]:
                state += f", {turn['refused']} requests refused"
            if turn["environment_entries"] and turn["environment_entries"]["count"]:
                state += f", left out: {_listed(turn['environment_entries'])}"
            lines.append(f"  {turn['turn']:>2} {turn['execution']:<16} {who.get('agent', '?')}/{who.get('model') or 'default'} "
                         f"{turn['session'] or '?'} session {turn['seconds']} s  {state}  [{acts or 'no record'}]")
        for execution, count in sorted(unit["refused_by_execution"].items()):
            lines.append(f"  {execution}: requests refused {count} times")
        held = [name for name, holds in unit["facts"].items() if holds]
        missing = [name for name, holds in unit["facts"].items() if not holds]
        lines.append(f"  facts held: {', '.join(held) or '-'}" + (f" | not held: {', '.join(missing)}" if missing else ""))
        entries = unit["environment_entries"]
        if entries and entries["count"]:
            lines.append(f"  environment entries in the worktree, not in the patch: {_listed(entries)}")
        for finding in unit["findings"]:
            where = finding.get("check") or finding.get("path") or ""
            lines.append(f"  finding [{finding.get('severity')}] {finding.get('criterion') or ''} {where} {finding.get('description') or ''}".rstrip())
    lines.append(f"total: {summary['units']} units, stops {summary['stops']}, {summary['turns_total']} turns "
                 f"({summary['turns_repeated']} repeated, {summary['incomplete_turns']} incomplete, "
                 f"{summary['results_not_counted']} results not counted, {summary['turns_continued']} continued a session, "
                 f"{summary['sessions_replaced']} sessions replaced), {summary['refused_requests']} requests refused, "
                 f"{summary['seconds_total']} s")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="unit_report")
    parser.add_argument("--home", help="state home (default: LOOP_ENGINE_HOME or ~/.loop-engine)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("units", nargs="*")
    args = parser.parse_args(argv)
    root = home_root({"LOOP_ENGINE_HOME": args.home} if args.home else os.environ)
    units = [unit for unit in (load_unit(root, name) for name in (args.units or list_units(root))) if unit]
    summary = summarize(units)
    if args.json:
        json.dump({"units": units, "summary": summary}, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        print(render(units, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
