import { expect, test } from "bun:test"

test("s saves the summary while the summary overlay is open", async () => {
  const { keyAction } = await import("./keys")
  expect(keyAction({ name: "s" }, { finished: true, helpOpen: false, summaryOpen: true })).toBe(
    "save-summary",
  )
})
