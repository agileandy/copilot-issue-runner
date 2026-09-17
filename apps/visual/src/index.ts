/**
 * Entry point for `--visual`. The Python runner starts this process with the
 * port of its event stream in ISSUE_RUNNER_VISUAL_PORT and inherits its own
 * stdio, so the terminal belongs to the renderer for as long as it is attached.
 */
import { createCliRenderer, type KeyEvent } from "@opentui/core"
import { createApp } from "./app"
import { keyAction } from "./keys"
import { saveSummary } from "./save"
import { theme } from "./theme"
import { connect, type Control } from "./transport"

const port = Number(process.env.ISSUE_RUNNER_VISUAL_PORT)
if (!Number.isFinite(port) || port <= 0) {
  console.error("ISSUE_RUNNER_VISUAL_PORT is not set — this display is started by the runner")
  process.exit(2)
}

const renderer = await createCliRenderer({
  exitOnCtrlC: false, // Ctrl+C asks the runner to stop cleanly; it never kills the display
  backgroundColor: theme.bg,
})

const app = createApp(renderer)
renderer.root.add(app.root)

let link: Awaited<ReturnType<typeof connect>> | null = null
let closing = false

function leave(message: Control): void {
  if (closing) return
  closing = true
  link?.send(message)
  // give the write a turn on the event loop before the process goes away
  setTimeout(() => {
    renderer.destroy()
    process.exit(0)
  }, 30)
}

const onKeyPress = (key: KeyEvent) => {
  const action = keyAction(key, {
    finished: app.state.finished,
    helpOpen: app.helpOpen(),
    summaryOpen: app.summaryOpen(),
  })
  switch (action) {
    case "stop":
      link?.send({ type: "stop" })
      return
    case "close":
      leave({ type: "closed" })
      return
    case "detach":
      leave({ type: "detach" })
      return
    case "toggle-help":
      app.toggleHelp()
      return
    case "close-help":
      app.closeHelp()
      return
    case "save-summary":
      void saveSummary(app.state)
      return
    case "dismiss-summary":
      app.dismissSummary()
      return
    default:
      return
  }
}

renderer.keyInput.on("keypress", onKeyPress)
renderer.once("destroy", () => {
  renderer.keyInput.off("keypress", onKeyPress)
  app.dispose()
})

link = await connect(port, {
  onEvent: (event) => {
    app.apply(event)
    // a stopped run returns the user to the shell; only a run that ran its
    // course is held on screen for review
    if (app.state.summary?.stopped) leave({ type: "closed" })
  },
  onClose: () => {
    // the runner went away: never sit on a display that can no longer update
    if (!closing) {
      closing = true
      renderer.destroy()
      process.exit(0)
    }
  },
})
