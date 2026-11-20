{instructions}

## Goal

{goal}

## Acceptance criteria

{criteria}

## Reports you are given

Each report is what another role found about the requested work, to be weighed as evidence. Rules that role was given for its own turn are not constraints on you or on the work.

{reports}

## Answers from the requester

Each answer is the requester's decision on a question asked during this work. It applies to the work and to how the acceptance criteria are judged.

{answers}

## Feedback from the previous round

{feedback}

## How this turn is judged

Work only in the current directory and change only these paths. Do not commit, and do not change agent configuration ({agent_config}).

{scope}

When you are finished changing files, run each of these commands exactly as written, each as its own command with nothing else on the line, as your final actions. If you change any file afterwards, run all of them again.

{checks}

A command without `--base` must end with `exit=0`. A command with `--base` runs your tests against the original code and must end with a non-zero exit: at least one of your tests has to fail without your change. Saying that checks passed does not count; only the record of these commands does. A check that cannot pass for a reason your change cannot address, such as the environment or a failure the original code already has, is not settled by a report: end with the question below, naming the check and what you observed.

End with a short report of what you changed and why.
