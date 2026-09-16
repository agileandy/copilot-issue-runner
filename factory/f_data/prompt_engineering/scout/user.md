# Scout Task

## Variables

### prompt
{{prompt}}

### previous_envelope
{{previous_envelope}}

### context_handoff_dir
{{context_handoff_dir}}

## Task

1. Read the question above.
2. Search the repository until you can answer it with real paths.
3. Record one finding per location that matters, each with the file and a short note.
4. If there is nothing to find, return no findings and say so in the summary.

## Report

Respond with ONLY valid JSON matching `ScoutOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "one line answering the question",
  "artifacts": [],
  "notes_for_next_agent": "anything worth knowing that is not a location",
  "findings": [
    {"file": "src/auth/session.py", "note": "issues the token; expiry is hard-coded on line 41"}
  ]
}
```
