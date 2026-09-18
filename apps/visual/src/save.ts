/**
 * Writing the run summary to disk.
 *
 * The report lands next to the run's own state so a finished run keeps its
 * story after the viewer exits; the filename carries the issue and the moment
 * it was saved so repeated saves never collide.
 */
import * as path from "node:path"
import { summaryReport, type ViewState } from "./model"

function sanitise(issueRef: string): string {
  return issueRef.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "run"
}

export async function saveSummary(state: ViewState, now: number = Date.now()): Promise<string> {
  const directory = state.summary!.stateDir || process.cwd()
  const stamp = new Date(now).toISOString().replace(/[:.]/g, "-")
  const file = path.resolve(directory, `run-summary-${sanitise(state.issueRef)}-${stamp}.md`)
  await Bun.write(file, summaryReport(state, now))
  return file
}
