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
   The branch is `feature/<n>-<slug>`, or `bugfix/<n>-<slug>` when the issue is
   labelled `bug`. Runs saved on an older `issue-<n>-<slug>` branch resume and
   commit there. All build work and commits stay in the run workspace; `git push`
   is denied to the agent unconditionally.
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

`--demo` runs a **real GitHub/Copilot demonstration** from this repository:

```bash
gh-runner --demo --visual
```

Each invocation copies the title and body of permanent seed
[#54](https://github.com/agileandy/copilot-issue-runner/issues/54) into a **new
issue**, then plans and executes only that clone. The clone's issue number
provides a unique run ID. The seed stays open and cannot be executed directly.

The seed specifies three steps: parse integers, filter an inclusive range, then
summarize it. An intentional upper-bound defect must prompt the verifier to
request a stronger test before the coder repairs it. This uses real model calls
and credits, not scripted responses.

Run from a clean checkout of `agileandy/copilot-issue-runner` with `gh` and
Copilot authenticated and project test dependencies installed. The normal
isolated worktree, test gates, visual controls and credit limits apply. Demo
mode creates no extra GitHub sub-issues and does not push or open a PR.

Use `--plan-only` to clone and plan without building. `--dry-run` only reads the
seed and previews the operation: it creates no clone and makes no model call.
To resume a stopped demo, use the **clone number** and command printed by the
runner. Repeating `--demo` deliberately starts a fresh clone instead.

The old offline `--demo-dir` and `--demo-reset` options are removed. Offline
fixtures remain available to the automated tests only.

## Usage

```bash
gh-runner 17 --repo owner/name --dir ~/src/target-repo
gh-runner --issue-file ./issue.md --dir . --plan-only   # plan, no build
gh-runner 17 --dry-run                                  # print the planner call, zero credits
gh-runner 17 --retry-blocked                            # retry only what blocked last time
```

Useful flags: `--test-cmd 'pytest {test_path} -q'` · `--max-rounds N` ·
`--model M --effort low` (override every role, taking precedence over runner.toml) ·
`--role-model ROLE=MODEL` (repeatable, overrides one role and takes precedence over `--model`, e.g.
`--role-model reviser=claude-opus-4.5` to escalate a stuck review-fix round; `resolver` is the
merge-conflict and failed-check resolver) · `--max-ai-credits 30` ·
`--max-run-credits 300` · `--issue-file PATH` ·
`--retry-blocked` · `--visual` · `--agent` · `--comment-issue N` · `--demo` ·
`--regression-cmd 'pytest -q'` · `--setup-cmd 'uv sync'` · `--in-place` ·
`--no-github-tickets` · `--no-pr` · `--deploy` · `--plan-only` · `--dry-run` ·
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
| `go.mod` | `go test -v -count=1 ./...` |
| `Cargo.toml` | `cargo test` |

The first marker in that order wins in a polyglot repo, and the choice is logged
at INFO. With no marker recognised it falls back to pytest and warns. `go test`
and `cargo test` deliberately run unfiltered: their selector flags take a test
*name*, not a file path, so a path filter would match nothing and exit 0 — which
the harness would misread as a passing test.

`test_cmd` in `runner.toml` and `--test-cmd` always override detection.

Python detection uses the target project's environment for both focused tests and
the regression suite: `uv run --no-sync python` when `uv.lock` exists, otherwise
the project's `.venv/bin/python` when present, then `python` from the caller's
PATH. The `.venv` path is relative, and a detected command is detected again in
the run worktree, so tests never use the source checkout's environment. It never
selects the globally installed runner's private interpreter.

Test commands run non-interactively with colour disabled. Go reports each case
and does not reuse cached results. Pytest keeps its result summary visible even
when the project already sets quiet options. Custom commands must emit a
supported pytest/Jest/Vitest/Go/Cargo report or TAP with a plan and result points;
skipped, TODO and non-executed cases do not satisfy the execution requirement.

The independent full-suite gate is detected from the same project markers.
Override it with `regression_cmd` or `--regression-cmd`; it must not contain
`{test_path}`. Unknown projects need an explicit regression command. Both commands
must report actual test execution, not just exit successfully.

`ticket_regression` controls when a ticket runs the full suite. The run always
ends with one full regression gate.

| Value | Per-ticket behaviour |
| --- | --- |
| `shared` (default) | Focused tests, plus the full suite when the ticket changes existing behaviour in shared files. Changes to existing non-Python files always trigger the full suite, because related-test discovery covers Python imports only |
| `focused` | Only the ticket's test file and tests that import the changed modules |
| `full` | The full suite after every ticket |

### Worktree toolchain

Before any model call, each run worktree gets its own dependencies, so agents
never reach into the source checkout (which lies outside the directory Copilot
may use). The steps come from the target repo's own manifests:

| Found | Step, run in the worktree |
|---|---|
| `uv.lock` with `pyproject.toml` at the root | `uv sync --frozen` |
| `requirements*.txt` at the root or one directory down | `uv venv .venv`, then one `uv pip install -r <file>` per file, `requirements.txt` first. `pytest` is added when the test command uses it and no file names it |
| `package-lock.json` up to two directories down | `npm ci --no-audit --no-fund --prefer-offline` in that directory |

Each step logs a `toolchain:` line, uses uv's or npm's shared cache, and is
skipped once a previous run finished it. A failing or slow step stops the run
with exit 1 and names the step. `/.venv/` and `node_modules/` are added to the
repository's `info/exclude`, so they never enter a commit. Configure it in
`runner.toml`:

```toml
provision = true          # false: prepare the worktree yourself
provision_timeout = 900   # seconds per step
```

`setup_cmd` in `runner.toml` (a command or a list of commands) or a repeated
`--setup-cmd` replaces the discovered steps. It runs even when `provision =
false`. Use it for steps discovery cannot know, such as copying test helpers
into the worktree. It shares `provision_timeout`, and a failing command stops the
run with its output.

Provisioning may only write ignored paths. If the discovered steps or
`setup_cmd` change any tracked or untracked file in the worktree, the run stops
and names the files.

`--in-place` runs are never provisioned: they use your own prepared checkout.
The runner does not delete worktrees or branches.

### Agent permissions

Every Copilot call gets `-C <worktree> --add-dir <worktree>` and nothing
broader. `--allow-all-tools` already approves the worktree's own test commands,
so no per-command allow rule is added. `git push` is always denied. Read-only
roles (planner, verifier) also have `write` and every repository-changing git
subcommand denied (`commit`, `reset`, `checkout`, `switch`, `branch`, `stash`,
`worktree`, `config` and others), and keep read-only git such as `status`,
`diff`, `log` and `show`. Copilot matches git rules on the first subcommand
only, so `git branch` is denied as a whole.

Every prompt names the workspace and its exact test commands, and tells the
agent to stay in the workspace, never call the source checkout's `.venv` or
`node_modules`, use uv rather than pip, and, when a tool call is denied, try one
different approach and then report the command instead of repeating it.

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

### Agent mode

`--agent` is for runs driven by another agent or a queue. The run summary is
not printed; it is posted as a comment on the issue (GitHub via `gh`, Gitea via
its API). An aborted run posts its error and partial summary the same way. If
the comment cannot be posted, the summary is printed instead and the exit code
is unchanged. `--agent` overrides `visual = true` in `runner.toml`, but an
explicit `--visual` keeps the display: the summary still goes to the issue.
With `--issue-file` there is no issue of its own, so `--comment-issue NUMBER`
names the issue to post to; without it `--agent` refuses `--issue-file`.
`--comment-issue` also redirects a numbered issue's summary.

### Deploy mode

`--deploy` takes an issue all the way to Dev. After the tickets are built the
runner opens a pull request, gets it through code review, merges it, and waits
until the merge is deployed to Dev. The run succeeds only when the issue's
**Definition of Done** is met. Every other outcome is a failure that names the
gate where it stopped.

| Gate | Passes when |
|---|---|
| `tickets` | every ticket is done and the full suite passes |
| `criteria_tests` | every acceptance criterion the planner marked "tests" is judged met by a read-only acceptor, citing tests the runner then runs and sees pass |
| `review` | on the PR's current head every check and status is green, the Copilot review recommends approval, no review thread is open, nobody requests changes, and required approvals exist |
| `merge` | the PR merged at exactly the reviewed commit |
| `deploy` | a GitHub deployment to the Dev environment reports `success` for a commit that contains the merge |
| `criteria_dev` | every criterion the planner marked "dev" passes its check against Dev |

How each gate is reached:

- **Acceptance criteria** come from the issue's `Acceptance criteria` section.
  The planner must map every one to "tests" (named by the tickets that prove
  it) or "dev". An issue without criteria is refused before any credit is spent.
- **Dev checks** are written before the PR opens, one per "dev" criterion, and
  saved outside the worktree. Run with `dev_check_cmd` they must print
  `ACCEPT-FAIL` against Dev first, and `ACCEPT-PASS` after the deployment.
  Only the last line of output counts.
- **Review findings** (open threads, failing checks, a Copilot verdict other
  than "Approval recommended", requested changes) go to a reviser agent. It is
  held to the same guards as the coder: frozen tests, ticket tests and the
  regression suite. The runner commits `fix(review): ...`, pushes, replies on
  and resolves each thread, and requests the review again. Declining a comment
  needs a second agent to agree.
- **Merging** uses the repository's one allowed merge method. When GitHub says
  the PR is conflicted (or behind, if up-to-date branches are required), the
  runner merges the base in. It never rebases or force-pushes. A resolver
  agent fixes conflicts, and the new head is reviewed again before it may merge.
- **The issue** gets a Definition of Done comment for every finished outcome.
  Criteria are ticked once they hold (tested criteria once merged, Dev criteria
  once checked in Dev). The issue closes only when every gate passes. The PR
  says `Refs #n`, so merging alone never closes it.

A read-only preflight runs first, before any model call. It checks write
access, the merge method, the review rules, the deploy workflow's push trigger,
the environment and the criteria. `--deploy --dry-run` prints it. `--deploy`
refuses `--no-pr`, `--plan-only`, `--issue-file`, `--demo`, `--in-place` and
non-GitHub repositories. Configure it under `[deploy]` in `runner.toml` (see
`runner.example.toml`). Every wait is bounded, and a stopped or failed run
resumes at its saved stage when re-run.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | all tickets done (with `--deploy`: the Definition of Done is met) |
| `1` | the run aborted (planner failure, copilot transport failure, git failure) |
| `2` | bad invocation, or a failed `--deploy` preflight |
| `3` | some tickets blocked |
| `4` | stopped on the run credit budget |
| `5` | `--deploy`: acceptance criteria not met (in tests or in Dev) |
| `6` | `--deploy`: code review did not pass |
| `7` | `--deploy`: the merge failed |
| `8` | `--deploy`: the Dev deployment failed |
| `130` | stopped by the user; saved work is resumable |

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
usage — calls: 14, duration: 4m12s, tokens: 51200 in / 8300 out, by-role: builder.coder=5 (claude-sonnet-4.5), builder.tester=6 (claude-sonnet-4.5), planner=1 (claude-opus-4.5), verifier=2 (claude-opus-4.5)
```

Rollups are written to `.issue-runner/usage-issue-<n>.json`, with per-role and
per-ticket breakdowns. Each per-role entry lists the distinct `models` copilot reported for
that role, falling back to the configured model when none was reported. A resumed run **appends** to `runs` and updates the
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

cd apps/visual && bun test   # the display's frame and state tests
```

## Visual mode

`--visual` on a TTY opens the OpenTUI display in `apps/visual`: pipeline banner,
live ticket board, streaming agent output, and run stats (calls, tokens,
elapsed). Non-TTY invocations fall back to plain text automatically.

The display is a Bun process that the runner starts and feeds over a loopback
socket; the pipeline itself never renders. It therefore needs [bun](https://bun.sh)
on your PATH — its dependencies install themselves on first use. Without bun, or
with `apps/visual` missing, `--visual` reports why and runs in plain text.

`q` does one of two things depending on when you press it:

- **during the run** — detaches the display; the run continues headless and
  progress is printed as plain lines. Type `r` and press Enter in the same
  terminal to reattach with the existing ticket board, output and statistics.
- **after the run** — closes the review. When the pipeline finishes the display
  *stays open* on a finished state showing the outcome, tickets done/blocked,
  the branch, the pull request URL and the usage line, so you can read the
  final board and agent output before dismissing it. The same summary is
  printed to the terminal on exit.

A crash also settles into that finished state, showing the error, rather than
leaving a live-looking display.

Visual mode changes rendering, not the transport or accounting.

### Stopping a run

Press **Ctrl+C** in the visual view, detached view or plain CLI. The runner
immediately reports **"Stopping and cleaning up..."**, finishes the current
operation, and stops before starting another model call.

Press Ctrl+C again to interrupt an active Copilot invocation and clean up its
owned subprocesses. On exit, the runner saves its accepted phase and usage,
releases its locks, and returns `130`. It does not delete branches or worktrees.
Re-run the same command to resume; `--retry-blocked` is not required for a user
stop.
