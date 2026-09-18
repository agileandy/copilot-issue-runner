import { expect, test } from "bun:test"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { applyEvent, initialState, summaryReport } from "./model"

test("the saved file holds the summary report", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "run-summary-"))
  const state = initialState(0)
  applyEvent(state, {
    kind: "run_started",
    payload: { issue_ref: "agileandy/copilot-issue-runner#110", title: "save the summary" },
  })
  applyEvent(state, {
    kind: "run_finished",
    payload: {
      done: 1,
      blocked: 0,
      branch: "issue-110-x",
      worktree: "/tmp/wt",
      pr_url: "https://x/pull/2",
      state_dir: stateDir,
      artefacts: {
        files: ["src/a.ts"],
        commits: [{ sha: "abc1234", ticket_id: 1, title: "add x", files: ["src/a.ts"] }],
        pr_url: "https://x/pull/2",
      },
      worktree_state: {
        path: "/tmp/wt",
        branch: "issue-110-x",
        head: "abc1234",
        dirty: false,
        uncommitted: [],
      },
    },
  })

  const { saveSummary } = await import("./save")

  expect(await Bun.file(await saveSummary(state, 65_000)).text()).toBe(summaryReport(state, 65_000))
})
