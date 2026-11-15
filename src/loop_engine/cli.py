"""The command line entry. `run` is what CI uses: it exits with a code per stop reason."""
import argparse
import json
import os
import sys

from . import client, store

EXIT = {"done": 0, "waiting_for_answer": 10, "rounds_exhausted": 11, "stagnation": 12,
        "unknown_result": 13, "cancelled": 14, "unsafe_agent_config": 15, "rejected": 16,
        "invalid_result": 17, "stuck": 18}
EXIT_INVALID = 2
EXIT_UNAVAILABLE = 3


def _print(value):
    json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def _send(root, payload, environ):
    try:
        response = client.request(root, payload, environ=environ)
    except client.EngineUnavailable as error:
        print(str(error), file=sys.stderr)
        return None, EXIT_UNAVAILABLE
    if not response.get("ok"):
        print(f"rejected: {response.get('error')}", file=sys.stderr)
        return None, EXIT_INVALID
    return response["result"], 0


def _until_stopped(root, unit_id, environ):
    while True:
        current = client.wait(root, unit_id, timeout=30.0, environ=environ)
        if current is not None and current.get("state") == "stopped":
            _print(current)
            return EXIT.get(current.get("stop_reason"), EXIT["unknown_result"])


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="loop-engine")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="start a unit from a contract file and wait until it stops")
    run.add_argument("contract")
    run.add_argument("--no-wait", action="store_true")
    for name in ("status", "cancel", "retry"):
        commands.add_parser(name).add_argument("unit_id")
    answer = commands.add_parser("answer")
    answer.add_argument("unit_id")
    answer.add_argument("question_id")
    answer.add_argument("answer_file")
    wait = commands.add_parser("wait")
    wait.add_argument("unit_id")
    args = parser.parse_args(argv)
    root = store.home_root(environ)
    try:
        if args.command == "run":
            with open(args.contract, encoding="utf-8") as handle:
                contract = json.load(handle)
            unit_id = contract.get("unit_id") if isinstance(contract, dict) else None
            _result, code = _send(root, {"request": "start", "contract": contract}, environ)
            if code or args.no_wait:
                return code
            return _until_stopped(root, unit_id, environ)
        if args.command == "status":
            current = client.status(root, args.unit_id)
            _print(current)
            return 0 if current is not None else EXIT_INVALID
        if args.command == "wait":
            return _until_stopped(root, args.unit_id, environ)
        if args.command == "answer":
            with open(args.answer_file, encoding="utf-8") as handle:
                text = handle.read()
            payload = {"request": "answer", "unit_id": args.unit_id, "question_id": args.question_id, "answer": text}
        else:
            payload = {"request": args.command, "unit_id": args.unit_id}
        result, code = _send(root, payload, environ)
        if not code:
            _print(result)
        return code
    except client.EngineUnavailable as error:  # waiting found no engine and none could be started
        print(str(error), file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (OSError, ValueError) as error:
        print(f"invalid: {error}", file=sys.stderr)
        return EXIT_INVALID
