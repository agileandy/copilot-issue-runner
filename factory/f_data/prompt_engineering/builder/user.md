# Builder Task

## Variables

### prompt
{{prompt}}

### previous_envelope
{{previous_envelope}}

### context_handoff_dir
{{context_handoff_dir}}

## Task

1. Read the request and any previous envelope above. If a previous envelope carries test failures,
   they are your task: fix what it reports, verbatim, and nothing else.
2. Implement the change.
3. Collect the path of every file you created or modified.
4. Write a `commit_message` describing **the code you just wrote**.

## Report

Respond with ONLY valid JSON matching `BuildOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "one line on what you built",
  "artifacts": [],
  "notes_for_next_agent": "anything the reviewer or the next agent needs to know",
  "changed_files": ["src/thing.py", "tests/test_thing.py"],
  "commit_message": "feat(thing): do the thing"
}
```
