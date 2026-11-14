"""Run contract scenarios through the engine with real agents and judge how each one stopped.

A scenario is a directory:

    repo/           the files of the base commit (with .dev-skills/checks.json when checks are used)
    change/         optional: files committed on top of the base, the existing change a unit judges
    contract.json   the contract; "{repository}", "{base}" and "{unit_id}" are filled in
    expect.json     {"stop_reason": "...", "unmet": ["criterion id", ...], "max_turns": n}

    scenarios.py prepare <scenario> <work-dir>          build the repository and the contract, start nothing
    scenarios.py run <scenario>... [--home DIR] [--work DIR] [--json]
                                                        start the engine and agents: a live run with their cost

`run` prints one line per scenario and exits 1 when any outcome differs from what was expected.
Use it before and after a change to the engine, a Profile, the Knowledge or a prompt.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "src", "loop_engine", "__main__.py")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from loop_engine.store import home_root  # noqa: E402
from unit_report import load_unit  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=scenario",
                           "-c", "user.email=scenario@example.invalid", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def build_repository(scenario, work):
    """A fresh repository from the scenario's files. Returns (repository, base commit)."""
    repository = os.path.join(work, "repo")
    shutil.copytree(os.path.join(scenario, "repo"), repository)
    _git(repository, "init", "-q")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "base")
    base = _git(repository, "rev-parse", "HEAD")
    change = os.path.join(scenario, "change")
    if os.path.isdir(change):
        shutil.copytree(change, repository, dirs_exist_ok=True)
        _git(repository, "add", "-A")
        _git(repository, "commit", "-qm", "the change under review")
    return repository, base


def fill(value, fields):
    """The contract with its placeholders replaced, in every string."""
    if isinstance(value, str):
        for name, text in fields.items():
            value = value.replace("{" + name + "}", text)
        return value
    if isinstance(value, list):
        return [fill(item, fields) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, fields) for key, item in value.items()}
    return value


def prepare(scenario, work, unit_id=None):
    """Build the repository and return (contract, expectation)."""
    scenario = os.path.abspath(scenario)
    name = os.path.basename(scenario.rstrip(os.sep))
    repository, base = build_repository(scenario, work)
    with open(os.path.join(scenario, "contract.json"), encoding="utf-8") as handle:
        contract = json.load(handle)
    with open(os.path.join(scenario, "expect.json"), encoding="utf-8") as handle:
        expect = json.load(handle)
    fields = {"repository": repository, "base": base, "unit_id": unit_id or f"{name}-{uuid.uuid4().hex[:8]}"}
    return fill(contract, fields), expect


def judge(expect, unit):
    """What differs from the expectation; empty when the unit ended as expected."""
    problems = []
    if unit is None:
        return ["the unit was not recorded"]
    if "stop_reason" in expect and unit["stop_reason"] != expect["stop_reason"]:
        problems.append(f"stopped as {unit['stop_reason']}, expected {expect['stop_reason']}")
    named = {finding.get("criterion") for finding in unit["findings"]}
    for criterion in expect.get("unmet", []):
        if criterion not in named:
            problems.append(f"no finding names criterion {criterion}")
    if "max_turns" in expect and len(unit["turns"]) > expect["max_turns"]:
        problems.append(f"{len(unit['turns'])} turns, expected at most {expect['max_turns']}")
    return problems


def run_contract(contract, home, work):
    """Hand one contract to the engine and wait for the unit to stop. Returns the unit's record."""
    path = os.path.join(work, "contract.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(contract, handle, ensure_ascii=False, indent=1)
    environ = dict(os.environ)
    if home:
        environ["DEV_SKILLS_HOME"] = home
    subprocess.run([sys.executable, "-I", ENGINE, "run", path], env=environ, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL)
    return load_unit(home_root(environ), contract["unit_id"])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scenarios")
    commands = parser.add_subparsers(dest="command", required=True)
    only = commands.add_parser("prepare")
    only.add_argument("scenario")
    only.add_argument("work")
    run = commands.add_parser("run")
    run.add_argument("scenarios", nargs="+")
    run.add_argument("--home")
    run.add_argument("--work")
    run.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        os.makedirs(args.work, exist_ok=True)
        contract, _expect = prepare(args.scenario, args.work)
        json.dump(contract, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0
    results = []
    for scenario in args.scenarios:
        work = tempfile.mkdtemp(prefix="scenario-", dir=args.work)
        contract, expect = prepare(scenario, work)
        unit = run_contract(contract, args.home, work)
        problems = judge(expect, unit)
        results.append({"scenario": os.path.basename(os.path.abspath(scenario)), "unit_id": contract["unit_id"],
                        "ok": not problems, "problems": problems, "unit": unit})
        if not args.json:
            turns = len(unit["turns"]) if unit else 0
            print(f"{'ok  ' if not problems else 'FAIL'} {results[-1]['scenario']}: "
                  f"{unit['stop_reason'] if unit else 'no record'}, {turns} turns"
                  + ("" if not problems else " -- " + "; ".join(problems)))
    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
