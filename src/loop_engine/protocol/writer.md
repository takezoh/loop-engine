{instructions}

## Goal

{goal}

## Acceptance criteria

{criteria}

## Reports you are given

{reports}

## Feedback from the previous round

{feedback}

## How this turn is judged

Work only in the current directory and change only these paths. Do not commit, and do not change agent configuration (`.claude/`, `.codex/`, `.pi/`, `.agents/`, `.mcp.json`).

{scope}

When you are finished changing files, run each of these commands exactly as written, each as its own command with nothing else on the line, as your final actions. If you change any file afterwards, run all of them again.

{checks}

A command without `--base` must end with `exit=0`. A command with `--base` runs your tests against the original code and must end with a non-zero exit: at least one of your tests has to fail without your change. Saying that checks passed does not count; only the record of these commands does.

End with a short report of what you changed and why. If you cannot proceed without a decision from the requester, end your reply with only this JSON object instead of a report, using a short lowercase id:

{{"question": "<what you need decided>", "id": "<q-short-id>"}}
