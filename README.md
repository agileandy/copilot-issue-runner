# copilot-issue-runner

A generic, programmatic runner that drives the **GitHub Copilot CLI** through a
strict three-phase TDD pipeline to implement a GitHub issue:

```
plan  ->  branch  ->  per ticket: [ builder.tester -> builder.coder -> verifier ] -> devops commit
```

## The pipeline

1. **Plan** — one read-only Copilot call reads the issue and explores the
   codebase, producing an ordered list of atomic sub-task tickets, each with a
   **single logical test assertion**. Tickets are stored locally
   (`.issue-runner/issue-<n>.json`, the source of truth) and optionally
   mirrored as GitHub sub-issues via `gh`.
2. **Branch** — `issue-<n>-<slug>` is created in the target repo. All work and
   commits happen there; `git push` is denied to the agent unconditionally.
3. **Build/verify loop** per ticket:
   - `builder.tester` writes the test first. The harness — not the model —
     enforces the non-stub rule: the test must contain a real assertion and
     must **fail** before implementation exists, or it is bounced back.
   - `builder.coder` makes the test pass. It may not touch the test file
     (detected by content hash and restored from git if violated).
   - `verifier` (read-only) judges robustness and routes:
     `refine_test` → back to the tester · `rework_code` → back to the coder ·
     `pass` → `devops` commits the ticket.
   - Hand-backs are capped by `max_rounds` (default 3); a non-converging
     ticket is marked **blocked** and the run continues. State is saved after
     every transition, so a crashed or credit-capped run resumes with no
     repeated model calls.

## Usage

```bash
uv run issue-runner 17 --repo owner/name --dir ~/src/target-repo
uv run issue-runner --issue-file ./issue.md --dir . --plan-only   # plan, no build
uv run issue-runner 17 --dry-run                                  # print the planner call, zero credits
```

Useful flags: `--test-cmd 'pytest {test_path} -q'` · `--max-rounds N` ·
`--model M --effort low` (defaults for all roles) · `--max-ai-credits 30` ·
`--no-github-tickets` · `--plan-only` · `--copilot-cmd /path/to/fake`.

Exit codes: `0` all tickets done · `2` bad invocation · `3` some tickets blocked.

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
