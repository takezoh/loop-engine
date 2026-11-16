This continues the assessment you were asked for earlier in this session. Its instructions, goal and acceptance criteria still apply, and the original versions are where that message said.

## Changed files

{changed}

## Reports that changed since your previous reply

Each report is what another role found about the requested work, to be weighed as evidence, or judged when your verdict is about it. Rules that role was given for its own turn are not constraints on you or on the work.

{reports}

## Answers from the requester since your previous turn

Each answer is the requester's decision on a question asked during this work. It applies to the work and to how the acceptance criteria are judged.

{answers}

## About your previous reply

{feedback}

## What to do

The rules in this section are the caller's rules for your turn only: they do not bind the work you judge or any other role, so do not cite them as constraints or findings.

Do not change any file. Judge the files as they are now. When your previous reply was not accepted, nothing has changed since it: correct what the reasons above name, and the files you read for it still count as examined. Otherwise read every changed file in the current directory again, because they may have changed since your previous reply. You may run commands; only the commands you run in this turn count as evidence. Judge each acceptance criterion separately. Do not state an overall approval; the caller computes it from your answer.

End your reply with only this JSON object:

{{"criteria": [{{"id": "<criterion id>", "met": true, "evidence": [{{"path": "<file>", "line": 3, "quote": "<text copied exactly from that line>"}}]}}], "findings": [{{"severity": "minor | major | blocker", "criterion": "<criterion id>", "path": "<file>", "line": 3, "quote": "<text copied exactly from that line>", "description": "<what is wrong>"}}]}}

Rules: every criterion appears exactly once. A criterion with "met": true needs at least one evidence item: either a quote of a line in the present file (path relative to the current directory, 1-based line number, quote copied exactly from that line) or {{"command": "<a command you ran in this turn, as you ran it>"}}. A criterion with "met": false needs a finding that names it in "criterion". Every finding quotes the line it is about. When what you judge is a report and not a file, quote the report instead and leave out the path and line: {{"quote": "<text copied exactly from the report>"}}. Use an empty findings list when there is nothing wrong.
