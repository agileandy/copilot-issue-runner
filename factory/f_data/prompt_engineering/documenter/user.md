# Documenter Task

## Variables

### prompt
{{prompt}}

### previous_envelope
{{previous_envelope}}

### context_handoff_dir
{{context_handoff_dir}}

## Task

1. Read the previous envelope above. It carries the captured change-set: the base it was taken
   against, the files that changed, and the path to the full diff.
2. Read the diff.
3. Write the documentation to a markdown file, choosing a name that does not already exist.
4. Record the file you wrote in `document_path`, and the files you documented in `documented_files`.

## Report

Respond with ONLY valid JSON matching `DocumentOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "one line on what you documented",
  "artifacts": ["docs/0001-thing.md"],
  "notes_for_next_agent": "anything left undocumented and why",
  "document_path": "docs/0001-thing.md",
  "documented_files": ["src/thing.py"],
  "commit_message": "docs(thing): write up the change"
}
```
