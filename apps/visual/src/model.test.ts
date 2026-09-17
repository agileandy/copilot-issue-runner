import { expect, test } from "bun:test"
import {
  applyEvent,
  initialState,
  pipelineChips,
  statsLine,
  summaryLines,
  summaryOutcome,
  trimOutput,
} from "./model"

test("the pipeline marks the current phase and the phases already passed", () => {
  const state = initialState()
  applyEvent(state, { kind: "phase", payload: { name: "build" } })
  expect(pipelineChips(state)).toEqual([
    { label: "plan", state: "done" },
    { label: "branch", state: "done" },
    { label: "build", state: "current" },
    { label: "finished", state: "todo" },
  ])
})

test("agent calls accumulate calls, tokens and credits", () => {
  const state = initialState()
  applyEvent(state, { kind: "agent_call_started", payload: { role: "builder.coder" } })
  const finished = applyEvent(state, {
    kind: "agent_call_finished",
    payload: {
      role: "builder.coder",
      elapsed: 12,
      usage: { input_tokens: 41000, output_tokens: 2000, nano_aiu: 7_000_000_000 },
    },
  })
  expect(state.stats).toEqual({
    calls: 1,
    inputTokens: 41000,
    outputTokens: 2000,
    nanoAiu: 7_000_000_000,
    unknownCostCalls: 0,
  })
  expect(finished.output).toContain("done in 12s")
})

test("stats distinguish zero, unknown and partial costs", () => {
  const zero = initialState()
  zero.stats = { calls: 1, inputTokens: 0, outputTokens: 0, nanoAiu: 0, unknownCostCalls: 0 }
  expect(statsLine(zero, zero.startedAt)).toContain("0.00 AIU")

  const unknown = initialState()
  unknown.stats.calls = 1
  expect(statsLine(unknown, unknown.startedAt)).toContain("credits unknown")

  const partial = initialState()
  partial.stats = {
    calls: 1,
    inputTokens: 0,
    outputTokens: 0,
    nanoAiu: 7_000_000_000,
    unknownCostCalls: 1,
  }
  expect(statsLine(partial, partial.startedAt)).toContain("at least 7.00 AIU")
})

test("the stats line reports the run clock and the working role", () => {
  const state = initialState()
  applyEvent(state, { kind: "agent_call_started", payload: { role: "builder.coder" } }, 0)
  state.startedAt = 0
  const line = statsLine(state, 125_000)
  expect(line).toContain("calls 1")
  expect(line).toContain("2m05s")
  expect(line).toContain("builder.coder working 125s")
})

test("a replayed stats snapshot restores the totals and the run clock", () => {
  const state = initialState()
  applyEvent(
    state,
    {
      kind: "stats_snapshot",
      payload: {
        calls: 4,
        input_tokens: 10,
        output_tokens: 20,
        nano_aiu: 1_000_000_000,
        unknown_cost_calls: 0,
        run_elapsed: 90,
      },
    },
    100_000,
  )
  expect(state.stats.calls).toBe(4)
  expect(statsLine(state, 100_000)).toContain("1m30s")
})

test("a finished run keeps its outcome, links and usage", () => {
  const state = initialState()
  applyEvent(state, {
    kind: "run_finished",
    payload: {
      done: 3,
      blocked: 0,
      branch: "issue-9-x",
      pr_url: "https://x/pull/2",
      usage: "usage — calls: 4",
    },
  })
  expect(state.finished).toBe(true)
  expect(summaryOutcome(state.summary!)).toEqual({ text: "all tickets done", tone: "ok" })
  expect(summaryLines(state.summary!)).toContain("pull request https://x/pull/2")
  expect(summaryLines(state.summary!)).toContain("usage — calls: 4")
})

test("summary outcomes cover blocked, budget, plan-only and aborted runs", () => {
  const base = {
    done: 0,
    blocked: 0,
    branch: "",
    worktree: "",
    prUrl: "",
    usage: "",
    budget: "",
    budgetExhausted: false,
    planOnly: false,
    stopped: false,
    error: "",
  }
  expect(summaryOutcome({ ...base, blocked: 2 }).text).toBe("2 blocked")
  expect(summaryOutcome({ ...base, budgetExhausted: true }).text).toContain(
    "credit budget exhausted",
  )
  expect(summaryOutcome({ ...base, planOnly: true }).text).toContain("plan ready")
  expect(summaryOutcome({ ...base, error: "planner failed: no JSON" }).text).toBe("run aborted")
  expect(summaryLines({ ...base, error: "planner failed: no JSON" })).toContain(
    "planner failed: no JSON",
  )
})

test("streamed fragments concatenate and the pane stays bounded", () => {
  const state = initialState()
  const chunks = ["A streamed", " sentence", "."]
  let output = ""
  for (const chunk of chunks) {
    output += applyEvent(state, { kind: "agent_output", payload: { chunk } }).output
  }
  expect(output).toBe("A streamed sentence.")

  const long = Array.from({ length: 2005 }, (_, i) => `line-${i}`).join("\n")
  const trimmed = trimOutput(long, 1000)
  expect(trimmed.split("\n").length).toBe(1000)
  expect(trimmed.endsWith("line-2004")).toBe(true)
})

test("a blocked ticket keeps its full reason in the output", () => {
  const state = initialState()
  const reason = `${"failure details ".repeat(30)}END-OF-FAILURE`
  const applied = applyEvent(state, { kind: "ticket_blocked", payload: { reason } })
  expect(applied.output).toContain(reason)
})

test("a finished run folds its artefacts into the summary", () => {
  const state = initialState()
  applyEvent(state, {
    kind: "run_finished",
    payload: {
      done: 1,
      blocked: 0,
      branch: "issue-9-x",
      artefacts: {
        files: ["src/a.ts"],
        commits: [{ sha: "abc1234", ticket_id: 1, title: "add x", files: ["src/a.ts"] }],
        pr_url: "https://x/pull/2",
      },
    },
  })
  expect(state.summary!.artefacts.commits[0]).toEqual({
    sha: "abc1234",
    ticketId: 1,
    title: "add x",
    files: ["src/a.ts"],
  })
})
