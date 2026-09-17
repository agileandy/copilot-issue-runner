# issue-runner visual display

The `--visual` display: an [OpenTUI](https://opentui.com) app that renders the
runner's event stream.

It is never started by hand. `issue_runner.visual_display` opens a loopback
socket, starts this app with the port in `ISSUE_RUNNER_VISUAL_PORT`, streams
RunEvents as newline-delimited JSON, and acts on the controls sent back
(`stop`, `detach`, `closed`). The terminal is inherited, so the renderer owns
the screen while it is attached.

```bash
bun test           # state and frame assertions
bun run typecheck  # types
```

- `src/model.ts` — the event fold and every formatter. No renderer, so the
  rules are testable on their own.
- `src/app.ts` — the tree: banner, ticket board, agent output, stats, summary,
  help. Takes a renderer; never creates one.
- `src/transport.ts` — the loopback link and its line framing.
- `src/index.ts` — the real renderer, the key bindings and the shutdown paths.
