import { expect, test } from "bun:test"

test("wrapText breaks at spaces and keeps existing newlines and blank lines", async () => {
  const { wrapText } = await import("./wrap")
  expect(wrapText("aaa bbb ccc\n\nddd", 7)).toBe("aaa bbb\nccc\n\nddd")
})

test("wrapText hard-breaks a word longer than the width into width-sized chunks", async () => {
  const { wrapText } = await import("./wrap")
  expect(wrapText("abcdefghij", 4)).toBe("abcd\nefgh\nij")
})

test("wrapText counts a tab as two columns so tab-indented lines wrap before the edge", async () => {
  const { wrapText } = await import("./wrap")
  expect(wrapText("\t\tab cd", 6)).toBe("\t\tab\ncd")
})
