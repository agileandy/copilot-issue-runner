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
