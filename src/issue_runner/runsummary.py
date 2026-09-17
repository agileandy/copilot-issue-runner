"""Collect what a run actually produced: commits, touched files and the PR.

Only tickets carrying a ``commit_sha`` count. A ticket without one produced no
durable artefact, however much work was attempted against it.
"""

from .tickets import TicketStore


def artefacts(store: TicketStore) -> dict:
    committed = [t for t in store.tickets if t.commit_sha]
    files: set[str] = set()
    commits = []
    for t in committed:
        files.update(t.changed_files)
        commits.append(
            {
                "sha": t.commit_sha,
                "ticket_id": t.id,
                "title": t.title,
                "files": list(t.changed_files),
            }
        )
    return {"files": sorted(files), "commits": commits, "pr_url": store.pr_url}
