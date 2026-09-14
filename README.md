# copilot-issue-runner

A generic, programmatic runner that drives the **GitHub Copilot CLI** through a
strict three-phase TDD pipeline to implement a GitHub issue:

```
clean workspace -> plan -> per ticket: [ tester -> coder -> verifier -> regression gate -> commit ]
```

## The pipeline

1. **Workspace** — before build-mode model calls, the runner refuses staged,
   unstaged or untracked user changes, records the issue branch, and creates a
   dedicated Git worktree under `.issue-runner/worktrees/`. The source checkout
   and its current branch stay untouched. `--in-place` explicitly opts into using
   the supplied clean checkout instead. A repository lock prevents competing
   runs from changing the same branch or state.
2. **Plan** — one read-only Copilot call reads the issue and explores the
   codebase, producing an ordered list of atomic sub-task tickets, each with a
   **single logical test assertion**. Tickets are stored locally
   (`.issue-runner/issue-<n>.json`, the source of truth) and optionally
   mirrored as GitHub sub-issues via `gh`.
   The branch is `issue-<n>-<slug>`. All build work and commits stay in the run
   workspace; `git push` is denied to the agent unconditionally.
3. **Build/verify loop** per ticket:
   - `builder.tester` writes the test first. The harness — not the model —
     requires evidence of actual test execution and a genuine failing result
     before implementation. Empty, skipped-only, collection-error and invalid
     command results are not accepted as red or green.
   - `builder.coder` makes the test pass. It may not touch the test file
     (detected by content hash and restored from the tester's snapshot if violated).
   - `verifier` (read-only) judges robustness and routes:
     `refine_test` → back to the tester · `rework_code` → back to the coder ·
     `pass` → the harness runs the full regression command, then `devops` stages
     only the approved ticket changes and commits. A model verdict alone cannot
     bypass the regression gate.
   - Hand-backs are capped by `max_rounds` (default 3); a non-converging
     ticket is marked **blocked**. If it left edits, the run stops and retains
     them in its worktree instead of mixing them into another ticket.
   - Accepted phase, test snapshot, base commit and approved change set are saved
     atomically. Resume skips accepted phases, and a commit completed just before
     an interruption is recovered by its recorded operation identity. An
     interrupted model call with no accepted checkpoint may need repeating.

## Install

To get `gh-runner` (and `issue-runner`) on your PATH from anywhere:

```bash
uv tool install --editable /path/to/copilot-issue-runner
```

`--editable` keeps the commands pointed at the working copy, so source changes
take effect without reinstalling. Both names are the same program and accept the
same arguments; `--dir` defaults to the current directory, so from inside a
target repo:

```bash
gh-runner 7                 # implement issue 7 of the repo you are standing in
gh-runner 7 --plan-only -v
```

## Demo mode

`--demo` runs the entire pipeline offline, with **no model call, no credits and
no GitHub access** — for showing people what the runner does:

```bash
gh-runner --demo                          # throwaway sandbox in a temp directory
gh-runner --demo --demo-dir ~/tmp/demo    # keep the sandbox somewhere
gh-runner --demo --demo-dir ~/tmp/demo --demo-reset --visual
```

Reset is allowed only for a runner-owned demo with a valid ownership marker.
Unowned directories, dangerous paths and symlink destinations are refused.
Legacy demos without a marker must be left intact or removed manually.

It creates a small git repo (a `demo_pkg.stats` module and an `issue.md` asking
for `mean` and `median`), then drives the real orchestrator against a scripted
stand-in for Copilot. Nothing is faked inside the pipeline: the tests really
run, the harness really enforces red-then-green, and each ticket is really
committed. The script is written so the demo shows the interesting paths —

- ticket 1 goes straight through: red test → implementation → `pass` → commit;
- ticket 2's first implementation is wrong, so the harness bounces it back;
- the verifier then returns `refine_test` (even-length median is untested),
  sending the loop back to the tester and on to the coder before it passes.

The demo uses its disposable sandbox directly rather than creating another
worktree inside it. The sandbox path is printed at the end; inspect the result with
`git -C <sandbox> log --oneline`. Add `--visual` for the TUI, `--plan-only` to
stop after planning. `ISSUE_RUNNER_DEMO_DELAY` (seconds, default `0.15`) paces
the streamed output in visual mode.

The sandbox runs its tests with whichever interpreter the runner is installed
under. That interpreter has pytest in a dev checkout but not in a
`uv tool install` venv, so the demo falls back to a bundled dependency-free test
runner — the banner prints the command actually in force.

## Usage

```bash
gh-runner 17 --repo owner/name --dir ~/src/target-repo
gh-runner --issue-file ./issue.md --dir . --plan-only   # plan, no build
gh-runner 17 --dry-run                                  # print the planner call, zero credits
gh-runner 17 --retry-blocked                            # retry only what blocked last time
```

Useful flags: `--test-cmd 'pytest {test_path} -q'` · `--max-rounds N` ·
`--model M --effort low` (defaults for all roles) · `--max-ai-credits 30` ·
`--max-run-credits 300` · `--issue-file PATH` ·
`--retry-blocked` · `--visual` · `--demo` ·
`--regression-cmd 'pytest -q'` · `--in-place` ·
`--no-github-tickets` · `--no-pr` · `--plan-only` · `--dry-run` ·
`--copilot-cmd /path/to/fake` · `-v`.

Without a global install, prefix any of these with `uv run` from the runner's
own directory (`uv run issue-runner 17 --dir ~/src/target-repo`).

### Test command

The runner detects the target repo's test command from its project markers, so
a non-Python repo works without configuration:

| Marker | Command |
|---|---|
| `pyproject.toml`, `setup.py`, `setup.cfg` | `python -m pytest {test_path} -q` |
| `package.json` with a `test` script | `npm test -- {test_path}` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |

The first marker in that order wins in a polyglot repo, and the choice is logged
at INFO. With no marker recognised it falls back to pytest and warns. `go test`
and `cargo test` deliberately run unfiltered: their selector flags take a test
*name*, not a file path, so a path filter would match nothing and exit 0 — which
the harness would misread as a passing test.

`test_cmd` in `runner.toml` and `--test-cmd` always override detection.

The independent full-suite gate is detected from the same project markers.
Override it with `regression_cmd` or `--regression-cmd`; it must not contain
`{test_path}`. Unknown projects need an explicit regression command. Both commands
must report actual test execution, not just exit successfully.

Dependencies must be usable from the printed run worktree. Point commands at an
existing environment, prepare dependencies in that worktree before resuming, or
use `--in-place` with your own clean, prepared worktree. The runner does not
silently install dependencies or delete worktrees and branches.

For new runs, state records every accepted phase and its artifacts. Older state
without worktree metadata can resume only from its original clean issue branch.
Ambiguous dirty legacy state is refused rather than adopted into a new commit.
`--retry-blocked` resets the retry allowance and continues from the saved phase.

### Pull requests

When a run finishes clean — every ticket done, none blocked — the runner pushes
the issue branch and opens a pull request titled `Fixes #<n> — <issue title>`,
bodied with the plan summary and each ticket's assertion, and prints its URL.
This needs a GitHub repo (`--repo`, or a github.com `origin`); it is skipped for
`--plan-only`, `--dry-run`, a budget stop, and Gitea remotes. Disable it with
`--no-pr` or `open_pr = false` in `runner.toml`. A push or `gh` failure is
reported, not fatal — the commits are already on the branch.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | all tickets done |
| `1` | the run aborted (planner failure, copilot transport failure, git failure) |
| `2` | bad invocation |
| `3` | some tickets blocked |
| `4` | stopped on the run credit budget |

### Ticket dependencies

The planner may give a ticket a `depends_on` list. The build loop drains the
*ready* set — tickets whose every dependency is `done` — in plan order, so a
ticket is never built before its prerequisite. Tickets are still executed one at
a time.

If nothing is ready but tickets remain, they are blocked explicitly rather than
left pending, with the cause named: `depends on ticket N which is blocked`,
`depends on unknown ticket N`, or a dependency cycle. Root causes are attributed
first, so a dependent points at the prerequisite that actually failed.

### Credit budgets
`--max-ai-credits N` requests Copilot's per-invocation **soft** cap.
`--max-run-credits N` (or `max_run_credits` in `runner.toml`) sets a soft budget
across the planner and all tester/coder/verifier invocations. The runner checks
the allowance between invocations and stops with exit `4` when another call
cannot be admitted. A running invocation can exceed a soft limit; neither flag
is a hard billing guarantee.

Reported nano-AIU charges are measured spend. When usage is unavailable, the
runner keeps an explicitly estimated reservation rather than pretending the call
was free. Unknown usage and genuine zero-cost calls are different. The budget is
unset by default.

A budget stop retains the accepted phase and leaves the active and remaining
tickets `pending`. Re-run with an adequate allowance to resume without
`--retry-blocked` or repeating already accepted phases.

### Usage accounting

Every Copilot invocation is recorded with its role, model, effort, duration and
outcome. Token counts and charges are aggregated across every reported model
turn within that invocation. The `calls` total counts CLI invocations, not
internal model turns. The summary prints in both plain and `--visual` modes:

```
usage — calls: 14, duration: 4m12s, tokens: 51200 in / 8300 out, by-role: builder.coder=5, builder.tester=6, planner=1, verifier=2
```

Rollups are written to `.issue-runner/usage-issue-<n>.json`, with per-role and
per-ticket breakdowns. A resumed run **appends** to `runs` and updates the
cumulative `totals`, so the file is the whole history of an issue, not just the
last attempt. Each run also appends one JSON line to `.issue-runner/usage.log`.

Plain and visual modes consume the same JSON event stream and collect the same
usage evidence. Missing usage stays unknown rather than being shown as a
misleading zero.

### Blank replies

The Copilot CLI sometimes exits 0 having written nothing to stdout — usually a
sign it cannot validate its token (`gh auth status`, or `/login` inside
`copilot`). A blank reply is treated as a transport failure, not as a model
answer: it is retried `empty_reply_retries` times (default 2, set in
`runner.toml`) before the run aborts with exit 1. Every attempt is budgeted and
counted as a failed call in the usage report, because a wasted call is real
spend.

## Frugal mode (free Copilot plan)

Model calls per issue ≈ `1 + 3 × tickets` minimum. To spend nothing while
developing or rehearsing, point Copilot CLI at a local OpenAI-compatible model
(BYOK) — the runner passes the environment straight through:

```bash
export COPILOT_PROVIDER_BASE_URL=http://<your-local-llm-host>:<port>/v1
export COPILOT_PROVIDER_TYPE=openai
export COPILOT_PROVIDER_API_KEY=<your-key>
export COPILOT_MODEL=<a-model-served-by-that-endpoint>
uv run issue-runner --issue-file issue.md --dir /path/to/repo
```

Any OpenAI-compatible server works (Ollama, vLLM, LM Studio, oMLX, …). Local
agent calls are slow compared to hosted models — fine for rehearsal runs.

Per-role budgets go in `runner.toml` (see `runner.example.toml`): e.g. a cheap
model for the verifier, the default for the coder.

## Development

```bash
uv sync
uv run pytest        # no model calls: all agents are faked
uv run ruff check src tests
```

## Visual mode

`--visual` on a TTY opens a contained Textual TUI: pipeline banner, live ticket
board, streaming agent output, and run stats (calls, tokens, elapsed). Non-TTY
invocations fall back to plain text automatically.

`q` does one of two things depending on when you press it:

- **during the run** — detaches the display; the run continues headless and
  progress is printed as plain lines.
- **after the run** — closes the review. When the pipeline finishes the TUI
  *stays open* on a finished state showing the outcome, tickets done/blocked,
  the branch, the pull request URL and the usage line, so you can read the
  final board and agent output before dismissing it. The same summary is
  printed to the terminal on exit.

A crash also settles into that finished state, showing the error, rather than
leaving a live-looking display.

Visual mode changes rendering, not the transport or accounting.
