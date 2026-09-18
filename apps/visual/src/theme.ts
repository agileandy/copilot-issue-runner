export const theme = {
  bg: "#0b0f14",
  panel: "#121820",
  border: "#2a3644",
  borderFocus: "#3f7fbf",
  text: "#c8d3e0",
  dim: "#67788c",
  accent: "#5fb0f0",
  ok: "#4fd07f",
  warn: "#e0b341",
  bad: "#e0625f",
} as const

export const statusColor: Record<string, string> = {
  done: theme.ok,
  in_progress: theme.warn,
  blocked: theme.bad,
  pending: theme.dim,
}

export const statusGlyph: Record<string, string> = {
  done: "✔",
  in_progress: "●",
  blocked: "⚠",
  pending: "○",
}
