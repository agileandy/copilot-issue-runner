import {
  BoxRenderable,
  ScrollBoxRenderable,
  TextRenderable,
  bold,
  fg,
  t,
  type CliRenderer,
} from "@opentui/core"
import { helpText } from "./help"
import {
  applyEvent,
  initialState,
  pipelineChips,
  statsLine,
  summaryLines,
  summaryOutcome,
  trimOutput,
  type RunEvent,
  type Ticket,
  type ViewState,
} from "./model"
import { statusColor, statusGlyph, theme } from "./theme"

export interface App {
  root: BoxRenderable
  state: ViewState
  apply: (event: RunEvent) => void
  refresh: () => void
  toggleHelp: () => void
  closeHelp: () => void
  helpOpen: () => boolean
  scrollOutput: () => void
  dispose: () => void
}

// The gutter marks position; it should not compete with the board it sits beside.
/** A cell-exact clip: the board aligns on the grid, so nothing may overflow it. */
function clip(text: string, width: number): string {
  if (width <= 1) return ""
  return text.length <= width ? text : `${text.slice(0, width - 1)}…`
}

const QUIET_SCROLLBAR = {
  trackOptions: { backgroundColor: theme.bg, foregroundColor: theme.border },
} as const

const BOARD_WIDTH = 40

const KEYS_RUNNING = "h help  ·  q detach  ·  ↑↓ PgUp/PgDn scroll  ·  Ctrl+C stop"
const KEYS_FINISHED = "h help  ·  q close  ·  ↑↓ PgUp/PgDn scroll"

function panel(renderer: CliRenderer, title: string, extra: Record<string, any> = {}) {
  return new BoxRenderable(renderer, {
    border: true,
    borderStyle: "rounded",
    borderColor: theme.border,
    backgroundColor: theme.bg,
    flexDirection: "column",
    title,
    ...extra,
  })
}

export function createApp(renderer: CliRenderer): App {
  const state = initialState()
  let output = ""

  const root = new BoxRenderable(renderer, {
    flexGrow: 1,
    backgroundColor: theme.bg,
    flexDirection: "column",
  })

  const header = new BoxRenderable(renderer, {
    height: 1,
    paddingLeft: 1,
    paddingRight: 1,
    flexDirection: "row",
    justifyContent: "space-between",
    backgroundColor: theme.panel,
  })
  const issueLabel = new TextRenderable(renderer, {
    content: t`${bold(fg(theme.accent)("issue-runner"))}`,
    wrapMode: "none",
  })
  const branchLabel = new TextRenderable(renderer, { content: "", wrapMode: "none" })
  header.add(issueLabel)
  header.add(branchLabel)

  const banner = new BoxRenderable(renderer, {
    height: 1,
    paddingLeft: 1,
    paddingRight: 1,
    flexDirection: "row",
    backgroundColor: theme.bg,
  })
  const pipeline = new TextRenderable(renderer, { content: "", wrapMode: "none" })
  banner.add(pipeline)

  const body = new BoxRenderable(renderer, { flexGrow: 1, flexDirection: "row" })

  // A fixed, integer sidebar width: a percentage rounds to a fraction of a cell
  // at some terminal widths and the wrapped text loses a column.
  const boardPane = panel(renderer, " tickets ", { width: BOARD_WIDTH, flexShrink: 0, paddingLeft: 1 })
  const board = new ScrollBoxRenderable(renderer, {
    flexGrow: 1,
    backgroundColor: theme.bg,
    contentOptions: { backgroundColor: theme.bg, flexDirection: "column", paddingRight: 1 },
    scrollbarOptions: QUIET_SCROLLBAR,
  })
  boardPane.add(board)

  const outputPane = panel(renderer, " agent ", { flexGrow: 1, paddingLeft: 1 })
  const outputScroll = new ScrollBoxRenderable(renderer, {
    flexGrow: 1,
    backgroundColor: theme.bg,
    stickyScroll: true,
    stickyStart: "bottom",
    contentOptions: { backgroundColor: theme.bg, flexDirection: "column", paddingRight: 1 },
    scrollbarOptions: QUIET_SCROLLBAR,
  })
  const outputText = new TextRenderable(renderer, { content: "", fg: theme.text, wrapMode: "word" })
  outputScroll.add(outputText)
  outputPane.add(outputScroll)

  body.add(boardPane)
  body.add(outputPane)

  const summaryPane = panel(renderer, " run finished ", {
    borderColor: theme.ok,
    paddingLeft: 1,
    paddingRight: 1,
  })
  summaryPane.visible = false

  const statsRow = new BoxRenderable(renderer, {
    height: 1,
    paddingLeft: 1,
    paddingRight: 1,
    backgroundColor: theme.panel,
  })
  const stats = new TextRenderable(renderer, { content: "", fg: theme.text, wrapMode: "none" })
  statsRow.add(stats)

  const footer = new BoxRenderable(renderer, {
    height: 1,
    paddingLeft: 1,
    paddingRight: 1,
    backgroundColor: theme.bg,
  })
  const keys = new TextRenderable(renderer, {
    content: KEYS_RUNNING,
    fg: theme.dim,
    wrapMode: "none",
  })
  footer.add(keys)

  const help = new BoxRenderable(renderer, {
    position: "absolute",
    left: 0,
    top: 0,
    width: "100%",
    height: "100%",
    border: true,
    borderStyle: "rounded",
    borderColor: theme.warn,
    backgroundColor: theme.panel,
    paddingLeft: 1,
    paddingRight: 1,
    title: " help — h or escape to close ",
  })
  const helpScroll = new ScrollBoxRenderable(renderer, {
    flexGrow: 1,
    backgroundColor: theme.panel,
    contentOptions: { backgroundColor: theme.panel, paddingRight: 1 },
  })
  helpScroll.add(new TextRenderable(renderer, { content: helpText(), fg: theme.text }))
  help.add(helpScroll)
  help.visible = false

  root.add(header)
  root.add(banner)
  root.add(body)
  root.add(summaryPane)
  root.add(statsRow)
  root.add(footer)
  root.add(help)

  outputScroll.focus()

  function renderHeader(): void {
    const ref = state.issueRef ? `${state.issueRef}  ` : ""
    issueLabel.content = t`${bold(fg(theme.accent)("issue-runner"))}  ${fg(theme.dim)(ref)}${fg(theme.text)(state.issue)}`
    branchLabel.content = state.branch
      ? t`${fg(theme.dim)("branch ")}${fg(theme.text)(state.branch)}`
      : ""
  }

  function renderBanner(): void {
    if (state.stopMessage) {
      pipeline.content = t`${fg(theme.warn)(state.stopMessage)}`
      return
    }
    const chunks = pipelineChips(state).map((chip) => {
      if (chip.state === "current") return bold(fg(theme.accent)(`▸ ${chip.label}`))
      if (chip.state === "done") return fg(theme.ok)(`${chip.label} ✔`)
      return fg(theme.dim)(`${chip.label} ○`)
    })
    pipeline.content = t`${chunks[0]!}${fg(theme.dim)("  →  ")}${chunks[1]!}${fg(theme.dim)("  →  ")}${chunks[2]!}${fg(theme.dim)("  →  ")}${chunks[3]!}`
  }

  /** Columns a board row may use; laid out before the first frame it is the fixed width. */
  function boardWidth(): number {
    const measured = board.viewport?.width ?? 0
    return measured > 8 ? measured - 1 : BOARD_WIDTH - 4
  }

  // Rows are reused rather than rebuilt: a board that churns renderables on
  // every event leaves half-drawn rows behind on the next frame.
  const boardRows: TextRenderable[] = []

  function boardRow(index: number): TextRenderable {
    let row = boardRows[index]
    if (!row) {
      // one ticket, one row: a wrapped title breaks the alignment of the board
      row = new TextRenderable(renderer, { content: "", fg: theme.text, wrapMode: "none" })
      boardRows[index] = row
      board.add(row)
    }
    row.visible = true
    return row
  }

  function ticketLines(ticket: Ticket): { content: any; fg?: string }[] {
    const color = statusColor[ticket.status] ?? theme.text
    const glyph = statusGlyph[ticket.status] ?? "?"
    const rounds = ticket.rounds ? ` ×${ticket.rounds}` : ""
    const width = boardWidth()
    const head = `${glyph} #${ticket.id} `
    const title = clip(ticket.title, width - head.length - rounds.length)
    const lines: { content: any; fg?: string }[] = [
      { content: t`${fg(color)(`${head}${title}`)}${fg(theme.dim)(rounds)}` },
    ]
    if (ticket.status === "blocked" && ticket.blocked_reason) {
      lines.push({ content: `  ${clip(ticket.blocked_reason, width - 2)}`, fg: theme.bad })
    }
    return lines
  }

  function renderBoard(): void {
    let used = 0
    if (state.tickets.length === 0) {
      const row = boardRow(used++)
      row.content = "no tickets yet — planner is thinking"
      row.fg = theme.dim
    } else {
      for (const ticket of state.tickets) {
        for (const line of ticketLines(ticket)) {
          const row = boardRow(used++)
          row.content = line.content
          row.fg = line.fg ?? theme.text
        }
      }
    }
    for (let index = used; index < boardRows.length; index += 1) {
      boardRows[index]!.visible = false
    }
  }

  const summaryOutcomeText = new TextRenderable(renderer, { content: "", height: 1 })
  const summaryBody = new TextRenderable(renderer, { content: "", fg: theme.text, height: 1 })
  summaryPane.add(summaryOutcomeText)
  summaryPane.add(summaryBody)

  function renderSummary(): void {
    if (!state.summary) return
    const outcome = summaryOutcome(state.summary)
    const tone = outcome.tone === "ok" ? theme.ok : outcome.tone === "bad" ? theme.bad : theme.text
    const lines = summaryLines(state.summary)
    summaryOutcomeText.content = t`${bold(fg(tone)(outcome.text))}`
    summaryBody.content = lines.join("\n")
    // auto height measures the content before it is set, so the rows are explicit
    summaryBody.height = lines.length
    summaryPane.height = lines.length + 3
    summaryPane.visible = true
    keys.content = KEYS_FINISHED
    outputPane.borderColor = theme.border
  }

  function renderStats(): void {
    stats.content = statsLine(state)
    const active = state.currentRole !== null
    outputPane.borderColor = active ? theme.borderFocus : theme.border
  }

  function refresh(): void {
    renderHeader()
    renderBanner()
    renderBoard()
    renderStats()
    if (state.finished) renderSummary()
  }

  function apply(event: RunEvent): void {
    const result = applyEvent(state, event)
    if (result.output) {
      output = trimOutput(output + result.output)
      outputText.content = output
    }
    if (result.boardChanged) refresh()
    else renderStats()
  }

  const clock = setInterval(renderStats, 1000)

  refresh()

  return {
    root,
    state,
    apply,
    refresh,
    toggleHelp: () => {
      if (help.visible) {
        help.visible = false
        outputScroll.focus()
      } else {
        help.visible = true
        helpScroll.scrollTo(0)
        helpScroll.focus()
      }
    },
    closeHelp: () => {
      help.visible = false
      outputScroll.focus()
    },
    helpOpen: () => help.visible,
    scrollOutput: () => outputScroll.scrollTo(outputScroll.scrollHeight),
    dispose: () => clearInterval(clock),
  }
}
