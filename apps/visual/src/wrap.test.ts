import { expect, test } from "bun:test"

test("wrapText breaks at spaces and keeps existing newlines and blank lines", async () => {
  const { wrapText } = await import("./wrap")
  expect(wrapText("aaa bbb ccc\n\nddd", 7)).toBe("aaa bbb\nccc\n\nddd")
})
