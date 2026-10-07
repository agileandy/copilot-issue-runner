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


PYTEST_REASON = """no usable test result from `pytest -q`: pytest reported 7 collection/setup error(s)
(exit 2)
backend/test/fixtures/roster_fixture.py:15: in <module>
    from openpyxl import Workbook
E   ModuleNotFoundError: No module named 'openpyxl'
Hint: make sure your test modules/packages have valid Python names.
E   ModuleNotFoundError: No module named 'openpyxl'
HINT: remove __pycache__ / .pyc files and/or use a unique basename for your test file modules
ERROR backend/test/test_app.py
"""


def test_blocked_reports_headline_and_distinct_causes(tmp_path):
    from issue_runner.runsummary import blocked

    store = TicketStore(tmp_path / "state", issue_ref="17")
    store.set_tickets([make_ticket(1, "add x", None, []), make_ticket(2, "add y", None, [])])
    store.tickets[0].status = "blocked"
    store.tickets[0].blocked_reason = PYTEST_REASON
    store.tickets[0].phase = "regression"

    assert blocked(store) == [
        {
            "id": 1,
            "title": "add x",
            "stage": "full test-suite check, after the ticket's own test passed review",
            "reason": "no usable test result from `pytest -q`: pytest reported 7 collection/setup error(s)",
            "causes": [
                "ModuleNotFoundError: No module named 'openpyxl'",
                "HINT: remove __pycache__ / .pyc files and/or use a unique basename for your test file modules",
            ],
        }
    ]


def test_notes_drop_per_ticket_details():
    from issue_runner.runsummary import notes

    assert notes(
        [
            "ticket 1 BLOCKED: tests failed\nlong output",
            "ticket 2 done @ abc123: add y",
            "no tickets started: re-run with --retry-blocked",
        ]
    ) == ["no tickets started: re-run with --retry-blocked"]


def test_notes_never_report_success_as_a_reason_to_stop():
    from issue_runner.runsummary import notes

    assert notes(
        [
            "pull request opened: https://github.com/o/n/pull/9",
            "plan-only run; no tickets executed",
            "pull request not opened: gh failed",
        ]
    ) == ["pull request not opened: gh failed"]
