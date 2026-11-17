# Loop Engine development guidance

Write all documentation, code comments, protocol texts and schema descriptions in English. Respond to the user in the language they ask for.

## Read first

Read [README.md](README.md), [PRODUCT.md](PRODUCT.md), [ARCHITECTURE.md](ARCHITECTURE.md) and [docs/development.md](docs/development.md). The development guide maps each concern to the document or source that owns it and lists the commands to run. Confirm a claim about behavior against the implementation and the tests before relying on it.

## What the engine is

- The engine receives a contract and nothing else. It knows no caller. Do not put a caller's concepts (its role names, Profiles, skills, judgment knowledge) or a caller's name into code, protocol texts, identifiers, defaults or the current documents.
- The engine starts only git and the agent commands of the installed catalog, never a project's command, and it neither creates nor changes a sandbox. These and the other rules a change must keep are the [design principles](ARCHITECTURE.md#design-principles).
- User pages say what a caller can do and rely on. Implementation belongs in the architecture and the technical reference, the reason for a decision in an ADR, and what was observed at a point in time in a note.

## Authority

- The design principles and the accepted ADRs under [docs/adr/](docs/adr/) are the user's authority. Do not change them for the convenience of an implementation.
- When an implementation conflicts with them, or needs a decision they do not make about a boundary or safety (permissions, isolation, the sandbox, approvals, user settings, how MCP or agents are handled), stop that implementation, state the conflict and the options, and wait for the user's decision. Do not resolve it by deleting or relaxing a rule, by appending "in the current implementation" to an ADR, or by avoiding a check.
- An approval covers what its wording covers. Do not derive from an approved direction another design decision the user has not seen.
- A commit that creates, accepts or changes an accepted ADR carries `Authority-Approval: <quote of, or reference to, the user's approval>` in its message and contains no implementation change. Without a statement to cite, stop and ask.

## Changing the engine

- State the problem and a verifiable completion condition before starting. Before adding structure, ask whether deleting, merging or automating reaches the same outcome.
- Implement the current contract only. Add no reader for an earlier shape, alias, migration or fallback; change producer and consumer together.
- Keep decisions pure and effects behind ports, as the design principles say. Do not add a port for an effect that does not differ by platform or agent.
- The text a role receives lives in `src/loop_engine/protocol/`. Do not embed prompt text in Python. Protocol text states the present instruction directly and does not describe history or precedence.
- Do not delete a test, add a skip or weaken an assertion to make a check pass.
- Make only the change the request needs. Report problems you find outside it instead of fixing them.
- Keep the user's untracked and uncommitted changes, stage nothing unrelated, and commit or push only when asked.

## Documents

- Architecture documents describe the current state only. History, earlier structures and rejected alternatives stay in ADRs and in git history.
- `docs/` is a dev-docs docs root. Change frontmatter, relations and status through the dev-docs CLI where it can, and run its lint after editing.
- When a rule moves or is removed, search the remaining documents for references to it.

## Verification

Choose among three kinds and report them separately.

- **Lint and static checks**: the docs lint after a change under `docs/`.
- **Tests**: `python -m unittest discover -s tests` from the repository root. They use real git repositories and a real engine process with scripted agents.
- **Scenarios with real agents**: `tools/scenarios.py run` starts real agents and uses their quota. Run it only when the user explicitly asks for or approves that run, after showing the command, what it starts and its approximate time and cost. A change to the engine or a wish for more coverage is not approval. Without approval, report `not run (approval not requested)`.

For each kind report the command, its scope, the number that passed, skips and warnings. A suite interrupted after it started is `incomplete`, neither passed nor failed. Judge completion by whether the completion condition holds, not by the steps taken.
