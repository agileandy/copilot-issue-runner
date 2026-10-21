/**
 * Soft-wrapping text to a column width.
 *
 * Each logical line (split on "\n") is wrapped on its own, so existing
 * newlines and blank lines stay where they are. A break replaces exactly one
 * space with "\n"; every other character is kept as is. A word wider than the
 * width is hard-broken into chunks of at most `width` columns, cut by code
 * point, and text after its last chunk keeps wrapping normally. Columns are
 * measured with `Bun.stringWidth`, so glyphs such as `⚙ ━ ✔ →` count as one
 * column, and a tab counts as 2 columns because that is how OpenTUI draws it.
 */
export function wrapText(text: string, width: number): string {
  if (width <= 0) return text
  return text
    .split("\n")
    .map((line) => wrapLine(line, width))
    .join("\n")
}

function wrapLine(line: string, width: number): string {
  if (columns(line) <= width) return line
  const words = line.split(" ")
  const rows: string[] = []
  let row: string | undefined
  for (const word of words) {
    if (row !== undefined) {
      const candidate = `${row} ${word}`
      if (columns(candidate) <= width) {
        row = candidate
        continue
      }
      rows.push(row)
    }
    const chunks = splitWord(word, width)
    rows.push(...chunks.slice(0, -1))
    row = chunks[chunks.length - 1]
  }
  rows.push(row ?? "")
  return rows.join("\n")
}

function splitWord(word: string, width: number): string[] {
  if (columns(word) <= width) return [word]
  const chunks: string[] = []
  let chunk = ""
  for (const char of word) {
    if (chunk !== "" && columns(chunk + char) > width) {
      chunks.push(chunk)
      chunk = ""
    }
    chunk += char
  }
  chunks.push(chunk)
  return chunks
}

function columns(text: string): number {
  return Bun.stringWidth(text) + 2 * (text.split("\t").length - 1)
}
