/**
 * Loopback link to the Python runner.
 *
 * The runner listens on 127.0.0.1 and streams newline-delimited RunEvents; this
 * side streams control messages back. The terminal stays free for rendering
 * because nothing travels over stdio.
 */
import type { RunEvent } from "./model"

export type Control = { type: "stop" } | { type: "detach" } | { type: "closed" }

export interface Link {
  send: (message: Control) => void
  close: () => void
}

export interface LinkHandlers {
  onEvent: (event: RunEvent) => void
  onClose: () => void
}

export function splitLines(buffer: string, onLine: (line: string) => void): string {
  let rest = buffer
  let index = rest.indexOf("\n")
  while (index >= 0) {
    const line = rest.slice(0, index)
    rest = rest.slice(index + 1)
    if (line.trim()) onLine(line)
    index = rest.indexOf("\n")
  }
  return rest
}

export async function connect(port: number, handlers: LinkHandlers): Promise<Link> {
  const decoder = new TextDecoder()
  let buffer = ""

  const socket = await Bun.connect({
    hostname: "127.0.0.1",
    port,
    socket: {
      data(_socket, data) {
        buffer = splitLines(buffer + decoder.decode(data), (line) => {
          try {
            handlers.onEvent(JSON.parse(line) as RunEvent)
          } catch {
            // a malformed line is a transport fault, never a reason to tear the display down
          }
        })
      },
      close() {
        handlers.onClose()
      },
      error() {
        handlers.onClose()
      },
    },
  })

  return {
    send: (message: Control) => {
      socket.write(`${JSON.stringify(message)}\n`)
      socket.flush()
    },
    close: () => socket.end(),
  }
}
