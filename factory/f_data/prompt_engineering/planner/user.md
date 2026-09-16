# Planner Task

## Variables

### prompt
{{prompt}}

### previous_envelope
{{previous_envelope}}

### context_handoff_dir
{{context_handoff_dir}}

## Task

1. Read the request and any previous envelope above.
2. Explore the repository until you can name the exact files the change touches.
3. Write the plan to a file under `specs/`, choosing a name that does not already exist.
4. Also copy it into `context_handoff_dir` so the next agent can read it without guessing paths.
5. List every file you wrote in `artifacts`.
6. Write a `commit_message` describing **the spec document you just wrote** — not the code that will
   later implement it.

## Report

Respond with ONLY valid JSON matching `PlanOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "one line on what the plan covers",
  "artifacts": ["specs/0001-thing.md"],
  "notes_for_next_agent": "anything the builder needs that is not in the plan",
  "commit_message": "docs(specs): plan the thing"
}
```
