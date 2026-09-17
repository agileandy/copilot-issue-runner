/**
 * Pure view state for the runner display.
 *
 * The Python side owns the pipeline and streams RunEvents over a loopback
 * socket; everything here is a fold of those events plus the formatting the
 * panes render. No renderer, so it is testable on its own.
 */

export interface Ticket {
  id: number | string
  title: string
  status: string
  rounds?: number | null
  blocked_reason?: string | null
}

export interface RunEvent {
  kind: string
  payload: Record<string, any>
}

export interface Stats {
  calls: number
  inputTokens: number
  outputTokens: number
  nanoAiu: number | null
  unknownCostCalls: number
}

export interface Artefacts {
  files: string[]
  commits: { sha: string; ticketId: number | string; title: string; files: string[] }[]
  prUrl: string
}

export interface WorktreeState {
  path: string
  branch: string
  head: string
  dirty: boolean
  uncommitted: string[]
}

export interface Summary {
  done: number
  blocked: number
  branch: string
  worktree: string
  prUrl: string
  usage: string
  budget: string
  budgetExhausted: boolean
  planOnly: boolean
  stopped: boolean
  error: string
  artefacts: Artefacts
  worktreeState: WorktreeState
  stateDir: string
}

export interface ViewState {
  issue: string
  issueRef: string
  phase: string
  branch: string
  tickets: Ticket[]
  stopMessage: string
  finished: boolean
  summary: Summary | null
  stats: Stats
  currentRole: string | null
  /** epoch ms the current model call started, or null between calls */
  callStartedAt: number | null
  /** epoch ms the run started; shifted by a replayed stats snapshot */
  startedAt: number
}

export const PHASES = ["plan", "branch", "build", "finished"] as const

/** Appended to the output pane by the event fold, not by the renderer. */
export interface Applied {
  output: string
  boardChanged: boolean
}

export function initialState(now: number = Date.now()): ViewState {
  return {
    issue: "",
    issueRef: "",
    phase: "plan",
    branch: "",
    tickets: [],
    stopMessage: "",
    finished: false,
    summary: null,
    stats: { calls: 0, inputTokens: 0, outputTokens: 0, nanoAiu: null, unknownCostCalls: 0 },
    currentRole: null,
    callStartedAt: null,
    startedAt: now,
  }
}

function costIsComplete(usage: Record<string, any> | undefined): boolean | null {
  if (!usage || Object.keys(usage).length === 0) return null
  if ("cost_complete" in usage) return Boolean(usage.cost_complete)
  return usage.nano_aiu === undefined || usage.nano_aiu === null ? null : true
}

/**
 * Fold one event into `state`. Returns any text the output pane should append
 * and whether the board, banner or summary need redrawing.
 */
function foldArtefacts(raw: unknown, prUrl: string): Artefacts {
  const a = raw as Record<string, any> | null | undefined
  if (!a) return { files: [], commits: [], prUrl }
  return {
    files: (a.files ?? []) as string[],
    commits: ((a.commits ?? []) as Record<string, any>[]).map((c) => ({
      sha: String(c.sha ?? ""),
      ticketId: c.ticket_id,
      title: String(c.title ?? ""),
      files: (c.files ?? []) as string[],
    })),
    prUrl: String(a.pr_url ?? prUrl),
  }
}

function foldWorktreeState(raw: unknown, fallback: WorktreeState): WorktreeState {
  const w = raw as Record<string, any> | null | undefined
  if (!w) return fallback
  return {
    path: String(w.path ?? fallback.path),
    branch: String(w.branch ?? fallback.branch),
    head: String(w.head ?? ""),
    dirty: Boolean(w.dirty),
    uncommitted: (w.uncommitted ?? []) as string[],
  }
}

export function applyEvent(
  state: ViewState,
  event: RunEvent,
  now: number = Date.now(),
): Applied {
  const p = event.payload ?? {}
  let output = ""
  let boardChanged = false

  switch (event.kind) {
    case "run_started":
      state.issue = String(p.title ?? "")
      state.issueRef = String(p.issue_ref ?? "")
      boardChanged = true
      break
    case "phase":
      state.phase = String(p.name ?? state.phase)
      boardChanged = true
      break
    case "stop_requested":
      state.stopMessage = String(p.message ?? "")
      boardChanged = true
      break
    case "tickets_updated":
      state.tickets = (p.tickets ?? []) as Ticket[]
      boardChanged = true
      break
    case "agent_call_started":
      state.stats.calls += 1
      state.currentRole = p.role ? String(p.role) : null
      state.callStartedAt = now
      output = `\n━━ ${p.role ?? "agent"} (${p.session ?? "session"}) ━━\n`
      boardChanged = true
      break
    case "agent_output":
      output = String(p.chunk ?? "")
      break
    case "agent_call_finished": {
      state.currentRole = null
      state.callStartedAt = null
      const usage = (p.usage ?? {}) as Record<string, any>
      state.stats.inputTokens += Number(usage.input_tokens ?? 0)
      state.stats.outputTokens += Number(usage.output_tokens ?? 0)
      if (usage.nano_aiu !== undefined && usage.nano_aiu !== null) {
        state.stats.nanoAiu = (state.stats.nanoAiu ?? 0) + Number(usage.nano_aiu)
      }
      if (costIsComplete(usage) !== true) state.stats.unknownCostCalls += 1
      output = `\n── done in ${p.elapsed ?? "?"}s ──\n`
      boardChanged = true
      break
    }
    case "ticket_blocked":
      output = `\n⚠ BLOCKED: ${p.reason ?? ""}\n`
      boardChanged = true
      break
    case "run_finished":
      state.phase = "finished"
      state.branch = String(p.branch ?? state.branch)
      state.finished = true
      state.currentRole = null
      state.callStartedAt = null
      state.summary = {
        done: Number(p.done ?? 0),
        blocked: Number(p.blocked ?? 0),
        branch: state.branch,
        worktree: String(p.worktree ?? ""),
        prUrl: String(p.pr_url ?? ""),
        usage: String(p.usage ?? ""),
        budget: String(p.budget ?? ""),
        budgetExhausted: Boolean(p.budget_exhausted),
        planOnly: Boolean(p.plan_only),
        stopped: Boolean(p.stopped),
        error: String(p.error ?? ""),
        artefacts: foldArtefacts(p.artefacts, String(p.pr_url ?? "")),
        worktreeState: foldWorktreeState(p.worktree_state, {
          path: String(p.worktree ?? ""),
          branch: state.branch,
          head: "",
          dirty: false,
          uncommitted: [],
        }),
        stateDir: String(p.state_dir ?? ""),
      }
      boardChanged = true
      break
    case "stats_snapshot":
      // Replayed on reattach: the totals and the clock survive a closed display.
      state.stats = {
        calls: Number(p.calls ?? 0),
        inputTokens: Number(p.input_tokens ?? 0),
        outputTokens: Number(p.output_tokens ?? 0),
        nanoAiu: p.nano_aiu === undefined || p.nano_aiu === null ? null : Number(p.nano_aiu),
        unknownCostCalls: Number(p.unknown_cost_calls ?? 0),
      }
      state.startedAt = now - Number(p.run_elapsed ?? 0) * 1000
      boardChanged = true
      break
    default:
      break
  }
  return { output, boardChanged }
}

export function formatAiu(nanoAiu: number): string {
  return `${(nanoAiu / 1_000_000_000).toFixed(2)} AIU`
}

export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  return `${Math.floor(whole / 60)}m${String(whole % 60).padStart(2, "0")}s`
}

export interface Chip {
  label: string
  state: "done" | "current" | "todo"
}

export function pipelineChips(state: ViewState): Chip[] {
  let reached = true
  return PHASES.map((phase) => {
    if (phase === state.phase) {
      reached = false
      return { label: phase, state: "current" as const }
    }
    return { label: phase, state: reached ? ("done" as const) : ("todo" as const) }
  })
}

export function statsLine(state: ViewState, now: number = Date.now()): string {
  const s = state.stats
  const parts = [
    `calls ${s.calls}`,
    `tokens in ${s.inputTokens.toLocaleString("en-US")} / out ${s.outputTokens.toLocaleString("en-US")}`,
  ]
  if (s.nanoAiu !== null) {
    parts.push(`credits ${s.unknownCostCalls ? "at least " : ""}${formatAiu(s.nanoAiu)}`)
  } else if (s.calls) {
    parts.push("credits unknown")
  }
  parts.push(`run ${formatClock((now - state.startedAt) / 1000)}`)
  if (state.currentRole) {
    const elapsed = state.callStartedAt === null ? 0 : Math.floor((now - state.callStartedAt) / 1000)
    parts.push(`${state.currentRole} working ${elapsed}s`)
  }
  return parts.join("  ·  ")
}

export function summaryOutcome(summary: Omit<Summary, "artefacts" | "worktreeState" | "stateDir">): { text: string; tone: "ok" | "bad" | "plain" } {
  if (summary.error) return { text: "run aborted", tone: "bad" }
  if (summary.stopped) return { text: "stopped by user; work saved", tone: "plain" }
  if (summary.budgetExhausted) return { text: "stopped: credit budget exhausted", tone: "bad" }
  if (summary.planOnly) return { text: "plan ready (no tickets executed)", tone: "plain" }
  if (summary.blocked) return { text: `${summary.blocked} blocked`, tone: "bad" }
  return { text: "all tickets done", tone: "ok" }
}

export function summaryLines(summary: Omit<Summary, "artefacts" | "worktreeState" | "stateDir">): string[] {
  const lines = [`tickets done ${summary.done}  ·  blocked ${summary.blocked}`]
  if (summary.error) lines.push(summary.error.slice(0, 300))
  if (summary.branch) lines.push(`branch ${summary.branch}`)
  if (summary.worktree) lines.push(`worktree ${summary.worktree}`)
  if (summary.prUrl) lines.push(`pull request ${summary.prUrl}`)
  if (summary.usage) lines.push(summary.usage)
  if (summary.budget) lines.push(summary.budget)
  return lines
}

/** The run-time totals of a finished run, one label per line. */
export function metricsLines(state: ViewState, now: number = Date.now()): string[] {
  if (!state.summary) return []
  const s = state.stats
  return [
    `outcome ${summaryOutcome(state.summary).text}`,
    `duration ${formatClock((now - state.startedAt) / 1000)}`,
    `phases ${PHASES.join(" → ")}`,
    `tickets done ${state.summary.done} · blocked ${state.summary.blocked}`,
    `model calls ${s.calls}`,
    `tokens in ${s.inputTokens.toLocaleString("en-US")} / out ${s.outputTokens.toLocaleString("en-US")}`,
    s.nanoAiu !== null ? `credits ${formatAiu(s.nanoAiu)}` : "credits unknown",
  ]
}

/** Keep the output pane bounded; the journal holds the full transcript. */
export function trimOutput(text: string, maxLines = 1000): string {
  const lines = text.split("\n")
  return lines.length <= maxLines ? text : lines.slice(lines.length - maxLines).join("\n")
}
