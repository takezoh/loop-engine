# Loop Engine verification tools

Tools for comparing before and after a change to the engine, the protocol texts or the judgment rules. The commands below run from the repository root.

## `unit_report.py`: aggregate the records

```bash
python tools/unit_report.py [--home DIR] [--json] [unit_id ...]
```

It reads only the engine's records. For each unit it shows the stop reason, the number of rounds, the turns and time per execution, whether each turn ran in a new or a continued session, the breakdown of each turn's execution record, the number of refused approval requests, the environment entries left out of each turn's judgment, the facts that hold, the findings that remain, and the environment entries that are in the worktree and not in the patch. The totals show retried turns, turns that did not complete, results that were not counted (with reasons), turns that continued a session, sessions replaced because they could not be continued, and the number of refused requests.

How to read it:

- An execution with many "results not counted" has an agent that does not keep the form of reply the protocol text asks for. Read the reasons and fix the protocol text.
- For a unit with many rounds, read the findings. When findings on the same criterion repeat, suspect the writer's instructions or the wording of the acceptance criteria.
- A review whose execution record has neither `read` nor `command` replied without examining anything.

## `scenarios.py`: confirm with real agents

```bash
python tools/scenarios.py prepare <scenario> <work-dir>   # only makes the repository and the contract
python tools/scenarios.py run <scenario>... [--home DIR] [--work DIR] [--json]
```

`run` starts the engine and agents. It uses agent quota and takes several minutes, so show the user what will run and what it costs and get approval before running. It returns exit code 1 when a scenario stopped differently from what was expected.

The engine places the check program and the copy of the base revision in the published root under `--home` (`published/`), and agents run the checks from there. The records are in `records/` under `--home`. A checks fact means something only when the agent's sandbox can read and cannot write the published root. For a run with real agents, use such a place (the default `~/.loop-engine`). Where the sandbox cannot read, no check status line appears; where it can write, the agent can alter a check on the base revision.

A scenario is a directory with:

| Content | Meaning |
|---|---|
| `repo/` | The files of the base revision. Include `.loop-engine/checks.json` when the scenario uses checks |
| `change/` | Optional. An existing change, committed on top of the base revision, that is the subject of the review |
| `contract.json` | The contract. `{repository}`, `{base}` and `{unit_id}` are filled in at run time |
| `expect.json` | The expectation: `stop_reason`, the criteria the findings must name (`unmet`), and the maximum number of turns (`max_turns`) |

Bundled scenarios:

| Scenario | What it confirms |
|---|---|
| `median-fix` | An implementation and two reviews complete. The checks are "passes on the candidate" and "fails on the original code" |
| `median-missing-empty-check` | Three reviewers stop an existing change that misses one criterion, each naming that criterion |

To add a scenario with a planted defect, put the defective change in `change/` and write the criteria that must be found in `unmet` of `expect.json`.
