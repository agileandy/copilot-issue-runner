/**
 * Turning a keypress into the one thing the display should do about it.
 *
 * The meaning of a key depends on what is on screen — `escape` closes the help
 * overlay when it is open and otherwise dismisses the summary — so the mapping
 * is kept here as a pure function of the key and the current view.
 */
export type Action =
  | "stop"
  | "close"
  | "detach"
  | "toggle-help"
  | "close-help"
  | "save-summary"
  | "dismiss-summary"
  | "none"

export type KeyPress = { name?: string; ctrl?: boolean }

export type ViewFlags = { finished: boolean; helpOpen: boolean; summaryOpen: boolean }

export function keyAction(key: KeyPress, view: ViewFlags): Action {
  if (key.ctrl && key.name === "c") return view.finished ? "close" : "stop"
  if (key.name === "h") return "toggle-help"
  if (key.name === "escape") {
    if (view.helpOpen) return "close-help"
    return view.summaryOpen ? "dismiss-summary" : "close-help"
  }
  if (key.name === "s" && view.summaryOpen) return "save-summary"
  if (key.name === "d" && view.summaryOpen) return "dismiss-summary"
  if (key.name === "q") return view.finished ? "close" : "detach"
  return "none"
}
