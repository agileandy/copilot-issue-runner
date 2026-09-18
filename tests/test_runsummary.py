from issue_runner.tickets import Ticket, TicketStore


def make_ticket(n, title, commit_sha, changed_files):
    return Ticket(
        id=n,
        title=title,
        description="do the thing",
        test_assertion="result == 42",
        commit_sha=commit_sha,
        changed_files=list(changed_files),
    )


def test_artefacts_collects_committed_tickets_only(tmp_path):
    from issue_runner.runsummary import artefacts

    store = TicketStore(tmp_path / "state", issue_ref="17")
    store.set_tickets(
        [
            make_ticket(1, "add x", "abc1234", ["src/a.py"]),
            make_ticket(2, "add y", None, ["src/b.py"]),
        ]
    )
    store.pr_url = "https://x/pull/2"

    assert artefacts(store) == {
        "files": ["src/a.py"],
        "commits": [
            {
                "sha": "abc1234",
                "ticket_id": 1,
                "title": "add x",
                "files": ["src/a.py"],
            }
        ],
        "pr_url": "https://x/pull/2",
    }
