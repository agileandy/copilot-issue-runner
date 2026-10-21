/**
 * Soft-wrapping text to a column width.
 *
 * Each logical line (split on "\n") is wrapped on its own, so existing
 * newlines and blank lines stay where they are. A break replaces exactly one
 * space with "\n"; every other character is kept as is. Columns are measured
 * with `Bun.stringWidth`, so glyphs such as `⚙ ━ ✔ →` count as one column.
 */
export function wrapText(text: string, width: number): string {
  if (width <= 0) return text
  return text
    .split("\n")
    .map((line) => wrapLine(line, width))
    .join("\n")
}

function wrapLine(line: string, width: number): string {
  if (Bun.stringWidth(line) <= width) return line
  const words = line.split(" ")
  const rows: string[] = []
  let row = words[0] ?? ""
  for (const word of words.slice(1)) {
    const candidate = `${row} ${word}`
    if (Bun.stringWidth(candidate) <= width) {
      row = candidate
    } else {
      rows.push(row)
      row = word
    }
  }
  rows.push(row)
  return rows.join("\n")
}
