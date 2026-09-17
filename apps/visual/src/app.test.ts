import { expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { createApp } from "./app"

/** Columns of one bordered pane, so a wrapped line is read without its neighbour. */
function paneText(frame: string, pane: number): string {
  return frame
    .split("\n")
    .map((line) => line.split("│")[pane * 2 + 1] ?? "")
    .join("")
    .replace(/[\s█▀▄]/g, "")
}

async function withApp(
  size: { width: number; height: number },
  body: (
    app: ReturnType<typeof createApp>,
    setup: Awaited<ReturnType<typeof createTestRenderer>>,
  ) => Promise<void>,
) {
  const setup = await createTestRenderer(size)
  try {
    const app = createApp(setup.renderer)
    setup.renderer.root.add(app.root)
    await body(app, setup)
    app.dispose()
  } finally {
    setup.renderer.destroy()
  }
}

test("the board, banner and chrome render from the event stream", async () => {
  await withApp({ width: 110, height: 24 }, async (app, setup) => {
    app.apply({ kind: "run_started", payload: { issue_ref: "#19", title: "tile the display" } })
    app.apply({ kind: "phase", payload: { name: "build" } })
    app.apply({
      kind: "tickets_updated",
      payload: {
        tickets: [
          { id: 1, title: "one", status: "done", rounds: 0, blocked_reason: null },
          { id: 2, title: "two", status: "in_progress", rounds: 2, blocked_reason: null },
          { id: 3, title: "three", status: "blocked", rounds: 3, blocked_reason: "verifier refused" },
        ],
      },
    })

    const frame = await setup.waitForFrame((value) => value.includes("three"))
    expect(frame).toContain("issue-runner")
    expect(frame).toContain("tile the display")
    expect(frame).toContain("✔ #1 one")
    expect(frame).toContain("● #2 two")
    expect(frame).toContain("×2")
    expect(frame).toContain("⚠ #3 three")
    expect(frame).toContain("verifier refused")
    expect(frame).toContain("▸ build")
    expect(frame).toContain("q detach")
  })
})

test("an empty board says the planner is still thinking", async () => {
  await withApp({ width: 80, height: 16 }, async (_app, setup) => {
    const frame = await setup.waitForFrame((value) => value.includes("planner is"))
    expect(frame).toContain("tickets")
  })
})

test("streamed output lands in the agent pane and wraps", async () => {
  await withApp({ width: 80, height: 20 }, async (app, setup) => {
    app.apply({ kind: "agent_call_started", payload: { role: "builder.tester", session: "t1" } })
    for (const chunk of ["A streamed", " sentence", ".\n"]) {
      app.apply({ kind: "agent_output", payload: { chunk } })
    }
    const long = `${"ABCDEFGHIJ".repeat(12)}END-MARKER`
    app.apply({ kind: "agent_output", payload: { chunk: long } })

    const frame = await setup.waitForFrame((value) => value.includes("MARKER"))
    expect(frame).toContain("A streamed sentence.")
    expect(frame).toContain("builder.tester")
    expect(paneText(frame, 1)).toContain(long)
  })
})

test("the summary appears only once the run has finished", async () => {
  await withApp({ width: 100, height: 24 }, async (app, setup) => {
    let frame = await setup.waitForFrame((value) => value.includes("tickets"))
    expect(frame).not.toContain("run finished")

    app.apply({
      kind: "run_finished",
      payload: { done: 3, blocked: 0, branch: "issue-9-x", pr_url: "https://x/pull/2" },
    })

    frame = await setup.waitForFrame((value) => value.includes("run finished"))
    expect(frame).toContain("all tickets done")
    expect(frame).toContain("https://x/pull/2")
    expect(frame).toContain("q close")
  })
})

test("a stop request replaces the banner while the run winds down", async () => {
  await withApp({ width: 100, height: 16 }, async (app, setup) => {
    app.apply({ kind: "stop_requested", payload: { message: "Stopping and cleaning up..." } })
    const frame = await setup.waitForFrame((value) => value.includes("Stopping and cleaning up"))
    expect(frame).not.toContain("▸ plan")
  })
})

test("help opens over the panes and closes again", async () => {
  await withApp({ width: 100, height: 20 }, async (app, setup) => {
    app.toggleHelp()
    let frame = await setup.waitForFrame((value) => value.includes("builder.coder"))
    expect(app.helpOpen()).toBe(true)
    expect(frame).toContain("Roles and what each prompt is given")

    app.closeHelp()
    frame = await setup.waitForFrame((value) => value.includes("planner is"))
    expect(app.helpOpen()).toBe(false)
  })
})

test("the finished run shows its summary as a modal over the panes", async () => {
  await withApp({ width: 100, height: 28 }, async (app, setup) => {
    app.apply({
      kind: "run_finished",
      payload: {
        done: 3,
        blocked: 0,
        branch: "issue-9-x",
        artefacts: { files: ["src/a.ts"], commits: [], pr_url: "https://x/pull/2" },
        worktree_state: {
          path: "/tmp/wt",
          branch: "issue-9-x",
          head: "abc1234",
          dirty: true,
          uncommitted: ["notes.md"],
        },
      },
    })

    expect(await setup.waitForFrame((value) => value.includes("run summary"))).toContain(
      "uncommitted notes.md",
    )
  })
})

test("the summary scrolls to the bottom of a long artefact list", async () => {
  await withApp({ width: 80, height: 20 }, async (app, setup) => {
    const files = Array.from({ length: 60 }, (_, index) => `file-${index}.ts`)
    app.apply({
      kind: "run_finished",
      payload: {
        done: 1,
        blocked: 0,
        branch: "issue-9-x",
        artefacts: { files, commits: [], pr_url: "" },
        worktree_state: {
          path: "/tmp/wt",
          branch: "issue-9-x",
          head: "abc1234",
          dirty: false,
          uncommitted: [],
        },
      },
    })

    await setup.waitForFrame((value) => value.includes("run summary"))
    app.scrollSummaryToBottom()

    expect(await setup.waitForFrame((value) => value.includes("file-59.ts"))).toContain("file-59.ts")
  })
})

// The overlay stays up until the user dismisses it, so its visibility has to be
// state the app owns (a `summaryDismissed` flag), not whatever the last render
// happened to leave on the renderable.
test("the summary stays open when later events arrive", async () => {
  await withApp({ width: 100, height: 24 }, async (app, setup) => {
    app.apply({
      kind: "run_finished",
      payload: { done: 3, blocked: 0, branch: "issue-9-x", pr_url: "https://x/pull/2" },
    })
    await setup.waitForFrame((value) => value.includes("run summary"))

    app.apply({ kind: "agent_output", payload: { chunk: "late noise\n" } })
    app.apply({ kind: "tickets_updated", payload: { tickets: [] } })
    await setup.waitForFrame((value) => value.includes("run summary"))

    const tracked = app as unknown as { summaryDismissed?: (() => boolean) | boolean }
    if (tracked.summaryDismissed === undefined) {
      throw new Error(
        "createApp must track the overlay in a summaryDismissed flag so refresh() re-shows it",
      )
    }
    const dismissed =
      typeof tracked.summaryDismissed === "function"
        ? tracked.summaryDismissed()
        : tracked.summaryDismissed
    if (dismissed) throw new Error("nothing dismissed the overlay, yet the app reports it dismissed")

    expect(app.summaryOpen()).toBe(true)
  })
})

// A stopped run is no longer bounced straight back to the shell: it gets the
// same overlay a completed run gets, so the footer has to tell the user how to
// save it and how to put it away again.
test("a stopped run is held on the summary overlay with its dismiss keys", async () => {
  await withApp({ width: 100, height: 24 }, async (app, setup) => {
    app.apply({
      kind: "run_finished",
      payload: { done: 1, blocked: 0, branch: "issue-9-x", stopped: true },
    })

    const frame = await setup.waitForFrame((value) => value.includes("run summary"))
    if (!frame.includes("stopped by user; work saved")) {
      throw new Error(`the stopped run's summary overlay never rendered:\n${frame}`)
    }

    expect(frame).toMatch(/s save summary[\s\S]*d dismiss summary/)
  })
})

test("dismissing the summary puts the terminal back", async () => {
  await withApp({ width: 100, height: 24 }, async (app, setup) => {
    app.apply({
      kind: "run_finished",
      payload: { done: 3, blocked: 0, branch: "issue-9-x", pr_url: "https://x/pull/2" },
    })
    await setup.waitForFrame((value) => value.includes("run summary"))

    app.dismissSummary()

    expect(await setup.waitForFrame((value) => !value.includes("run summary"))).not.toContain(
      "run summary",
    )
  })
})
