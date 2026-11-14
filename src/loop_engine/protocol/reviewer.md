{instructions}

## Goal

{goal}

## Acceptance criteria

{criteria}

## Changed files

{changed}

The original versions are under `{base}/`.

## Reports you are given

{reports}

## Your previous reply

{feedback}

## What to do

Do not change any file. Read every changed file in the current directory; you may run commands. Judge each acceptance criterion separately. Do not state an overall approval; the caller computes it from your answer.

End your reply with only this JSON object:

{{"criteria": [{{"id": "<criterion id>", "met": true, "evidence": [{{"path": "<file>", "line": 3, "quote": "<text copied exactly from that line>"}}]}}], "findings": [{{"severity": "minor | major | blocker", "criterion": "<criterion id>", "path": "<file>", "line": 3, "quote": "<text copied exactly from that line>", "description": "<what is wrong>"}}]}}

Rules: every criterion appears exactly once. A criterion with "met": true needs at least one evidence item: either a quote of a line in the present file (path relative to the current directory, 1-based line number, quote copied exactly from that line) or {{"command": "<a command you ran in this turn, as you ran it>"}}. A criterion with "met": false needs a finding that names it in "criterion". Every finding quotes the line it is about. Use an empty findings list when there is nothing wrong.
