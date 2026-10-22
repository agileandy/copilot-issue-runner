import pytest

from issue_runner.tickets import Ticket, TicketStore


def make_ticket(n=1):
    return Ticket(
        id=n,
        title=f"ticket {n}",
        description="do the thing",
        test_assertion="result == 42",
        files_hint=["src/thing.py"],
    )


def test_new_ticket_is_pending():
    t = make_ticket()
    assert t.status == "pending"


def test_store_round_trips_state(tmp_path):
    store = TicketStore(tmp_path / "state", issue_ref="17")
    store.set_tickets([make_ticket(1), make_ticket(2)])
    store.save()

    reloaded = TicketStore(tmp_path / "state", issue_ref="17")
    assert reloaded.load() is True
    assert [t.id for t in reloaded.tickets] == [1, 2]
    assert reloaded.tickets[0].test_assertion == "result == 42"


def test_load_returns_false_when_no_state(tmp_path):
    store = TicketStore(tmp_path / "state", issue_ref="17")
    assert store.load() is False


def test_status_transitions_persist(tmp_path):
    store = TicketStore(tmp_path / "state", issue_ref="17")
    store.set_tickets([make_ticket(1)])
    store.tickets[0].status = "done"
    store.tickets[0].rounds = 2
    store.save()

    reloaded = TicketStore(tmp_path / "state", issue_ref="17")
    reloaded.load()
    assert reloaded.tickets[0].status == "done"
    assert reloaded.tickets[0].rounds == 2


def test_pending_iterates_in_order_and_skips_done(tmp_path):
    store = TicketStore(tmp_path / "state", issue_ref="17")
    a, b, c = make_ticket(1), make_ticket(2), make_ticket(3)
    a.status = "done"
    store.set_tickets([a, b, c])
    assert [t.id for t in store.pending()] == [2, 3]


def test_invalid_status_rejected():
    t = make_ticket()
    with pytest.raises(ValueError):
        t.status = "nonsense"


def test_pre_pr_rounds_persist_across_save_and_load(tmp_path):
    rounds = [{"round": 1, "findings": [{"command": "c", "text": "t"}]}]
    store = TicketStore(tmp_path / "state", issue_ref="17")
    store.set_tickets([make_ticket(1)])
    store.pre_pr_rounds = rounds
    store.save()

    reloaded = TicketStore(tmp_path / "state", issue_ref="17")
    reloaded.load()
    assert reloaded.pre_pr_rounds == rounds


def test_files_section_returns_none_without_files_heading():
    from issue_runner import tickets

    assert tickets.files_section("## Problem\nsomething\n\n## Scope\n- a") is None


def test_files_section_returns_backtick_paths_up_to_next_heading():
    from issue_runner import tickets

    body = "intro\n\n## Files\n\n`src/a/build.py`, `config.py` (or similar), `tests/*`\n\n## Next\n`x.py`"
    assert tickets.files_section(body) == ["src/a/build.py", "config.py", "tests/*"]


def test_files_section_returns_bare_path_tokens_from_plain_text():
    from issue_runner import tickets

    body = "## Files\n\n- packages/guard/src/guard/cli.py\n- config.toml, plugin/skills/factory/\n\n## Next\nx.py"
    assert tickets.files_section(body) == [
        "packages/guard/src/guard/cli.py",
        "config.toml",
        "plugin/skills/factory/",
    ]


def test_files_section_returns_none_when_heading_names_no_path():
    from issue_runner import tickets

    assert tickets.files_section("## Files\n\nWhatever the fix needs to touch.\n\n## Next\nmore") is None


def test_out_of_scope_returns_sorted_paths_matching_no_files_pattern():
    from issue_runner import tickets

    paths = ["src/a/build.py", "pkg/config.py", "tests/test_x.py", "sql/prod.sql"]
    patterns = ["src/a/build.py", "config.py", "tests/*"]
    assert tickets.out_of_scope(paths, patterns) == ["sql/prod.sql"]


def test_out_of_scope_keeps_test_file_beside_listed_file_in_scope():
    from issue_runner import tickets

    paths = ["src/a/test_build.py", "src/b/test_other.py"]
    assert tickets.out_of_scope(paths, ["src/a/build.py"]) == ["src/b/test_other.py"]


def test_out_of_scope_treats_pattern_ending_in_slash_as_directory_prefix():
    from issue_runner import tickets

    paths = ["packages/guard/tests/test_x.py", "packages/guard/src/x.py"]
    assert tickets.out_of_scope(paths, ["packages/guard/tests/"]) == ["packages/guard/src/x.py"]
