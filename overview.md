# Overview

An executive summary of `copilot-issue-runner`.

## What this repository is

Two agent-driven build systems live here, and they are independent of each other.

1. **`issue-runner`** (`src/issue_runner/`) — the product. A CLI that takes a GitHub issue number
   and drives the GitHub Copilot CLI through a test-first pipeline until the issue is implemented,
   committed, and raised as a pull request.
2. **The factory** (`factory/`) — a generic, reusable harness for chaining coding agents into
   auditable workflows. It was stamped into this repository by the factory installer and is used to
   do work *on* this repository. It is not part of the shipped `issue-runner` program.

Both share one conviction: a model's claim is not evidence. Every judgement a model makes is checked
by deterministic code before it is allowed to count.

## Part 1 — `issue-runner`

### The pipeline

```
clean workspace -> plan -> per ticket: [ tester -> coder -> verifier -> regression gate -> commit ]
```

- **Workspace.** Refuses to start on a dirty tree. Creates a dedicated Git worktree under
  `.issue-runner/worktrees/`, so the source checkout and its branch are never touched. A repository
  lock stops competing runs.
- **Plan.** One read-only Copilot call turns the issue into ordered atomic tickets, each carrying a
  single logical test assertion. Tickets are stored at `.issue-runner/issue-<n>.json` (the source of
  truth) and optionally mirrored as GitHub sub-issues.
- **Build loop.** The tester writes a failing test first; the harness — not the model — demands
  evidence that the test actually executed and actually failed. The coder then makes it pass and is
  forbidden from editing the test file (enforced by content hash, restored from snapshot on
  violation). The verifier routes: `refine_test`, `rework_code`, or `pass`. A `pass` still does not
  commit until the full regression suite runs green. A model verdict alone cannot bypass that gate.
- **Convergence is bounded.** `max_rounds` (default 3) caps hand-backs; a ticket that will not
  converge is marked blocked rather than looped on forever.

### What makes it portable

Test and regression commands are detected from the target repo's project markers — `pyproject.toml`,
`package.json`, `go.mod`, `Cargo.toml` — so a non-Python repo works without configuration. Output
must be a recognised report format (pytest, Jest, Vitest, Go, Cargo, or TAP); an exit code of 0
alone does not prove anything ran.

### Operational properties

- **Resumable.** Accepted phase, test snapshot, base commit and approved change-set are written
  atomically. Re-running the same command continues from the last accepted phase.
- **Budgeted.** Per-invocation and per-run soft credit caps, with real nano-AIU spend recorded per
  role and per ticket. Unknown usage stays unknown rather than being reported as zero.
- **Observable.** Every Copilot invocation is logged with role, model, effort, duration, tokens and
  outcome. `--visual` renders the same event stream as a Textual TUI; detaching does not stop the
  run.
- **Explicit exit codes.** `0` done · `1` aborted · `2` bad invocation · `3` blocked · `4` budget
  stop · `130` user stop with resumable state.

Entry points: `gh-runner` and `issue-runner` (same program). `--demo` runs a real end-to-end
demonstration by cloning a permanent seed issue.

## Part 2 — The factory (`factory/`)

A workflow engine for agent chains. The unit of work is a **phase**; a chain of phases is a
**workflow** (`factory/f_*.py`).

### The model

- **Three phase kinds, three swim lanes.** `engineer` (the ask), `agent` (prompt in, typed envelope
  out), `code` (deterministic work: commit, diff, test). A commit or a test run is never hidden
  inside an agent phase, because that would make the trace lie about who did what.
- **Success is earned.** A phase is constructed failed; only a clean exit flips it to success. The
  database column defaults to `fail` for the same reason.
- **Two separate questions.** A phase succeeding and a run being acceptable are different. A test
  phase that ran a red suite did its job; the run must still be rejected. Acceptance is passed
  explicitly to `run.finish()`, which settles the stored status, the banner and the exit code
  together so they cannot disagree.
- **Typed envelopes.** Every agent returns a Pydantic-validated shape. Malformed JSON is repaired by
  bounded retries; valid JSON making unacceptable claims is corrected by gates.
- **Gates verify claims, not guesses.** A gate checks what the envelope asserted — its declared
  artifacts exist, its claimed file changes match the real diff. Passing checks record their
  evidence (`plan.md: exists, 2.1KB`), because `passed: true` on its own is a rumour. Gates never
  judge taste; that is the reviewer's job.
- **Write boundaries are enforced after the fact.** `tools:` is a capability list; `writes:` is the
  boundary. The harness fingerprints the change-set before the agent's first prompt and again after
  it finishes, attributes every difference, and rolls back anything unauthorised. Comparing
  change-sets rather than intercepting writes is what catches an agent using `bash` to
  `git checkout` away the check about to judge it. A breach aborts the phase — unlike a gate
  failure, it cannot be fixed by re-prompting.
- **Known commands do not get a model.** There is no tester agent. `pytest` is a command, not a
  judgement call. A workflow whose suite is still a placeholder refuses to start rather than
  spending builder turns satisfying a command that does not exist.

### The roster

`factory/f_config/sssf.config.yaml` defines agents by **identity**, never by model, so swapping a
model is a config edit:

| Agent | Boundary | Job |
|---|---|---|
| `planner` | writes `specs/` only | turn a request into an implementable plan |
| `builder` | unrestricted except protected paths | implement the plan |
| `scout` | repository-read-only | find where things live, cheaply |
| `reviewer` | repository-read-only | is this what was asked for? |
| `documenter` | writes markdown only | write up the change from the diff |

### The workflows

Eleven chains, each directly runnable (`uv run --script factory/f_prompt.py "..."`) with a `just`
recipe for convenience: `prompt`, `plan`, `build`, `plan-build`, `build-test`, `plan-build-test`,
`scout`, `quality`, `build-review`, `document`, `sdlc`.

`f_simple_sdlc` is the full chain and shows the design most clearly: three commits for three work
products by three authors; the suite and the reviewer asked in order because neither can answer the
other's question; a revision after a green suite sets a stale flag and forces a retest, so the tree
that is committed is the tree that was both tested and approved; code lands last, so a failed run
leaves the plan committed and the working tree visible; and the documentation baseline is pinned
before the run's first commit, so the diff is not measured against the run's own output.

### Observability

One data path, no push transport: `agent -> tracer -> { events.jsonl, SQLite (WAL) } -> polling UI`.
Files win — the JSONL is appended before the database is touched, so a crash leaves the raw record
ahead of the mirror, never behind it. `just sessions`, `phases`, `tail`, `procs` and `q` read the
trace; `just obs` boots the browser UI, which ships with the factory rather than with each stamped
repository.

### Backends

`agent_copilot.py` (default) and `agent_pi.py` implement a four-function duck-typed adapter
protocol, resolved lazily by name. This is the seam: everything above it is untouched when the
backend changes.

## Layout

```
src/issue_runner/       the shipped CLI: orchestrator, phases, copilot transport, TUI, budgets
tests/                  the suite; all agents are faked, no model calls
factory/f_*.py          workflow chains — one file per chain, deliberately thin
factory/f_modules/      the engine: types, tracer, phases, agents, gates, permissions, git, quality
factory/f_config/       the agent roster
factory/f_data/         prompts, and per-run session artifacts and the trace database (gitignored)
justfile                operator recipes for the factory
```

## Running it

```bash
uv tool install --editable .     # gh-runner / issue-runner on PATH
gh-runner 7                      # implement issue 7 of the repo you are standing in

uv sync && uv run pytest         # the suite, no model calls
uv run ruff check src tests

just scout "where does X live"   # factory, read-only
just sdlc "the ask"              # factory, full chain
```
