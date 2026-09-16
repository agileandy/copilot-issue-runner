# Reviewer Task

## Variables

### prompt
{{prompt}}

### previous_envelope
{{previous_envelope}}

### context_handoff_dir
{{context_handoff_dir}}

## Task

1. Read the original request above, and the previous envelope describing what was built.
2. Break the request into the requirements it actually states.
3. For each one, read the code and decide whether it is met, citing the file and line as evidence.
4. List anything that must change before this ships in `blocking`.
5. Set `approved` to match your own findings: approved means nothing blocking and nothing unmet.

## Report

Respond with ONLY valid JSON matching `ReviewOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "one line on whether the work matches the request",
  "artifacts": [],
  "notes_for_next_agent": "anything the builder needs in order to revise",
  "approved": false,
  "findings": [
    {"requirement": "logs the user out on token expiry", "met": true, "evidence": "src/auth/session.py:41"}
  ],
  "blocking": ["expiry is hard-coded to 3600 and the request asked for it to be configurable"]
}
```
