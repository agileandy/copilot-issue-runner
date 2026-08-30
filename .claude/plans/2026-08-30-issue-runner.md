# Plan: generic Copilot issue runner (plan / build / verify)

Date: 2026-08-30
Branch: feature/issue-runner

## Requirement (Andy's spec)

A generic runner that drives GitHub Copilot CLI to implement a GitHub issue:

1. **Plan** — accept an issue id, read the issue + codebase, produce a detailed
   atomic plan; create sub-task tickets, each with a single-assertion test.
2. **Branch** — create a branch for the change.
3. **Build/verify loop** per ticket:
   - 4.1 `builder.tester` writes the test first — MUST NOT be a stub
   - 4.2 `builder.coder` writes code that passes the test
   - 5.0 `verifier` checks test robustness/edge cases and that code passes
   - 5.1 test weak → back to builder.tester
   - 5.2 test robust, code fails → back to builder.coder
   - 5.3 pass → `builder.devops` commits the change

Constraint: free Copilot plan — limited model calls; test e2e sparingly.

## Research findings (copilot CLI 1.0.82, from local `--help` + help topics)

- Non-interactive: `copilot -p "<prompt>" -s --allow-all-tools --no-ask-user`
- `-C <dir>` working dir; `--model`, `--effort`; `--max-ai-credits` (soft cap, min 30)
- Permissions: `--deny-tool 'shell(git push)'`, `--deny-tool write`, deny > allow
- `--output-format json` (JSONL events); `--usage-output-file` writes usage JSON
- **BYOK**: `COPILOT_PROVIDER_BASE_URL/_TYPE/_API_KEY` + `COPILOT_MODEL` → any
  OpenAI-compatible endpoint. Studio oMLX (192.168.1.205:7777, key 7777,
  Qwen3.8-27B-oQ6e) = zero-credit end-to-end testing rail.

## Design decisions (deviations from literal spec — flagged to Andy)

| # | Decision | Why |
|---|----------|-----|
| D1 | Red/green enforced by the harness, not prompts: tester's test must FAIL pre-code and contain `assert`; coder must turn it GREEN. Deterministic, free. | "Not a stub" is unverifiable by prompt alone |
| D2 | `max_rounds` (default 3) per ticket; then ticket → `blocked`, continue | Unbounded 5.1/5.2 loop burns credits |
| D3 | Local `tickets.json` is source of truth; GitHub sub-issues mirrored via `gh` (opt-out `--no-github-tickets`) | Loop needs local state; issue writes need perms |
| D4 | `--issue-file` alternative to a live GitHub issue | e2e without touching GitHub |
| D5 | "Single assertion" = single logical assertion (prompt + verifier), not AST count | Guard asserts are legitimate |
| D6 | devops commit = plain `git` in the *target* repo on the issue branch | Runner is generic; can't assume qw-git |
| D7 | Per-role model/effort config (`runner.toml` / flags); env passthrough enables BYOK | Free-plan budget control |

## Architecture

Python 3.12, uv project, stdlib-only runtime; pytest dev-dep.

```
src/issue_runner/
  cli.py            argparse entry (issue ref, --repo, --dir, --test-cmd, --dry-run…)
  config.py         RunnerConfig (roles→model/effort, test cmd, limits)
  copilot.py        CopilotClient: builds argv, runs subprocess; injectable for tests
  jsonx.py          tolerant JSON extraction from model replies
  github_io.py      gh wrappers: fetch issue, create/close sub-issues
  tickets.py        Ticket dataclass + state store (.issue-runner/<id>/state.json)
  phases/plan.py    plan prompt → validated ticket list
  phases/build.py   tester + coder calls, red/green enforcement
  phases/verify.py  verifier call → verdict routing
  phases/devops.py  branch create, per-ticket commit
  orchestrator.py   phase wiring + per-ticket loop (max_rounds)
tests/              pytest, FakeCopilot injection, no real model calls
```

Call budget per issue ≈ 1 (plan) + 3 per ticket minimum.

## Build order (TDD, atomic commits via qw-git)

1. scaffold: pyproject, package skeleton — commit
2. jsonx + tickets state machine (tests → impl) — commit
3. copilot client argv/subprocess (tests → impl) — commit
4. phases: plan, build red/green, verify routing, devops (tests → impl) — commit
5. orchestrator + cli (tests incl. --dry-run) — commit
6. README + AGENTS notes — commit
7. e2e smoke: temp repo + --issue-file + BYOK local Qwen (zero credits);
   at most ONE real-Copilot run if BYOK proves flaky

## Out of scope

PR creation, multi-issue queueing, Gitea backend (issue-file hook makes it easy later).
