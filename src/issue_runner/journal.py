"""The intra-agent conversation, written to the ticket instead of the chat log.

Every hand-back in this pipeline is one agent talking to another: the harness
rejecting a stub test, the verifier sending code back for rework, the tester
being asked to repair an unsatisfiable spec. That conversation used to exist
only as a string passed into the next prompt, so it died with the process --
nobody could see afterwards why a ticket took four rounds, and a resumed run
started with only the single most recent message.

The journal posts each message as a comment on the ticket's own sub-issue, so
the tracker holds the full transcript. The prompt then carries a pointer to
that thread rather than the text, which is what makes the record the shared
memory of the run rather than a copy of it.

Degradation is deliberate and silent in both directions:
- no tracker (``--no-github-tickets``, ``--issue-file``, a ticket that was
  never mirrored) means there is nothing to point at, so the feedback is
  inlined exactly as before;
- a tracker that is reachable for writes but not readable by the agent (Gitea,
  which has no CLI the agent can use) still receives the transcript for the
  human record, but the prompt keeps the inline text.

A journal write must never end a run. The work is real and already on disk; a
GitHub outage is not a reason to lose it, so every post is best-effort and a
failure downgrades that round to inline feedback.
"""

import logging

log = logging.getLogger("issue_runner")

_INLINE_HEADER = "FEEDBACK ON YOUR PREVIOUS ATTEMPT (fix this):"


class Journal:
    """Records the conversation about one run on its tickets.

    `backend` is a tracker backend (see ticket_mirror). Anything without a
    `comment` method, or a None backend, makes the journal inert.
    """

    def __init__(self, backend=None):
        self.backend = backend if hasattr(backend, "comment") else None
        self._last: dict[int, str] = {}

    def thread(self, ticket) -> str | None:
        """The command an agent runs to read this ticket's transcript.

        None when there is no readable thread, which is the signal to inline
        the feedback instead of pointing at it.
        """
        if self.backend is None or not getattr(ticket, "github_issue", None):
            return None
        ref = getattr(self.backend, "thread_ref", None)
        return ref(ticket) if ref else None

    def post(self, ticket, sender: str, recipient: str, body: str, heading: str = "") -> bool:
        """Append one message to the ticket's transcript. Never raises."""
        if self.backend is None or not getattr(ticket, "github_issue", None) or not body.strip():
            return False
        # A retry loop can produce the identical rejection several times over.
        # Posting each one buries the thread the agents are told to read, so an
        # immediate repeat is dropped: the message is already on the record.
        if self._last.get(ticket.id) == body:
            return True
        text = format_message(ticket, sender, recipient, body, heading)
        try:
            self.backend.comment(ticket, text)
        except Exception:  # noqa: BLE001 — a lost comment must not cost the run
            log.warning("could not record feedback on ticket %s", ticket.id, exc_info=True)
            return False
        self._last[ticket.id] = body
        return True

    def render(self, ticket, body: str) -> str:
        """The prompt block for feedback that is already on the record.

        Used when the orchestrator posted the message itself, so the step only
        has to deliver the pointer.
        """
        if not body.strip():
            return ""
        thread = self.thread(ticket)
        return pointer_block(thread) if thread else inline_block(body)

    def hand_back(self, ticket, sender: str, recipient: str, body: str, heading: str = "") -> str:
        """Record one message and return the prompt block that delivers it.

        This is the single seam every prompt uses, so the choice between "read
        the thread" and "here is the text" is made in exactly one place.
        """
        if not body.strip():
            return ""
        recorded = self.post(ticket, sender, recipient, body, heading)
        thread = self.thread(ticket) if recorded else None
        return pointer_block(thread) if thread else inline_block(body)


def format_message(ticket, sender: str, recipient: str, body: str, heading: str = "") -> str:
    """One transcript entry: who spoke, to whom, on which round, and what about."""
    round_no = getattr(ticket, "rounds", 0)
    head = f"**{sender} → {recipient}** · round {round_no}"
    if heading:
        head += f" · {heading}"
    return f"{head}\n\n{body.strip()}"


def pointer_block(thread: str) -> str:
    """Send the agent to the ticket rather than repeating the conversation."""
    return (
        "\n\nFEEDBACK ON YOUR PREVIOUS ATTEMPT\n"
        "Your earlier attempt was reviewed and sent back. The full review "
        "thread — every reason, every previous round — is on this sub-task's "
        "own issue. Read it FIRST, before you change anything:\n"
        f"  {thread}\n"
        "The newest comment is the one you must address; the earlier ones tell "
        "you what has already been tried, so do not repeat it."
    )


def inline_block(body: str) -> str:
    """The pre-journal behaviour, used whenever there is no readable thread."""
    return f"\n{_INLINE_HEADER}\n{body}"
