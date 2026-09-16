"""File and database persistence — the trace.

§14.3 puts observability **second**, not last: every module built after this one is debugged
through it, so a factory whose tracer arrives late is a factory built blind.

The data path has no push transport (§9.1)::

    coding agent  ->  tracer  ->  { events.jsonl , SQLite (WAL) }  ->  polling UI

That is the central observability trade. A polled mirror is boring, has no delivery semantics to get
wrong, and makes "what happened three days ago" and "what is happening now" the same query with a
different cursor.

**Dual persistence, and files win** (§9.2). ``events.jsonl`` is the raw record; the database is the
queryable mirror. Both are written as it happens. The tie-break is expressed as write *order* rather
than as a comment: :meth:`Tracer._record` appends to the file first and only then touches the
database, so a crash between the two leaves the file ahead of the mirror — never behind it.

There is no ``sqlite3`` CLI on this machine, so everything here (and every convenience recipe built
on it later) reads the trace through Python.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from f_modules.data_types import (
    AgentSessionRecord,
    EnvelopeRecord,
    EventRecord,
    EventType,
    GateResultRecord,
    PhaseRecord,
    PhaseStatus,
    ProcessRecord,
    SessionRecord,
    SessionStatus,
)
from f_modules.utils import new_id, to_iso, utc_now

# ---------------------------------------------------------------------------------------------
# Schema (§9.3)
# ---------------------------------------------------------------------------------------------

# `sessions.archived` is deliberately ABSENT here. §14.5 records it as a column the UI uses but the
# base DDL never had, resolved as an additive migration — so this schema is the "before", and
# `_migrate()` is the "after". Keeping it out makes §9.4's rule a live code path that runs on every
# open, rather than a paragraph nobody exercises until the first real migration goes wrong.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    f_id         TEXT PRIMARY KEY,
    f_name       TEXT NOT NULL DEFAULT '',
    request      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'running',
    engineer     TEXT NOT NULL DEFAULT '',
    started_at   TEXT,
    ended_at     TEXT,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost   REAL    NOT NULL DEFAULT 0.0
);

-- status DEFAULT 'fail': success must be earned even in the database (§9.3).
CREATE TABLE IF NOT EXISTS phases (
    phase_id    TEXT PRIMARY KEY,
    f_id        TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'fail',
    attempt     INTEGER NOT NULL DEFAULT 1,
    retries     INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    started_at  TEXT,
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    f_id         TEXT NOT NULL,
    phase_id     TEXT NOT NULL DEFAULT '',
    parent_id    TEXT NOT NULL DEFAULT '',
    type         TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    tokens       INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS envelopes (
    envelope_id  TEXT PRIMARY KEY,
    f_id         TEXT NOT NULL,
    phase_id     TEXT NOT NULL,
    agent        TEXT NOT NULL,
    output_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    valid        INTEGER NOT NULL DEFAULT 0,
    attempt      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS gate_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    f_id            TEXT NOT NULL,
    phase_id        TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    gate            TEXT NOT NULL,
    passed          INTEGER NOT NULL DEFAULT 0,
    violations_json TEXT NOT NULL DEFAULT '[]',
    checks_json     TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS processes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    f_id       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    pid        INTEGER NOT NULL,
    command    TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    f_id           TEXT NOT NULL,
    agent          TEXT NOT NULL,
    coding_agent   TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    color          TEXT NOT NULL DEFAULT '',
    session_id     TEXT NOT NULL DEFAULT '',
    context_tokens INTEGER NOT NULL DEFAULT 0,
    context_window INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    last_used_at   TEXT,
    PRIMARY KEY (f_id, agent)
);

CREATE INDEX IF NOT EXISTS idx_events_f_id ON events(f_id);
CREATE INDEX IF NOT EXISTS idx_phases_f_id ON phases(f_id, seq);
"""

# Columns added after the base schema shipped. `CREATE TABLE IF NOT EXISTS` never revisits an
# existing table, so each one needs an explicit ALTER applied only after checking the live column
# list (§9.4). Readers must tolerate their absence — see `_select_list`.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "archived", "INTEGER NOT NULL DEFAULT 0"),
)

OPTIONAL_COLUMNS: dict[str, tuple[str, ...]] = {"sessions": ("archived",)}
"""Columns a reader must not assume exist, substituting ``NULL AS <col>`` when they do not."""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply_writer_pragmas(conn: sqlite3.Connection) -> None:
    """§9.6, writer side. Writers use one small transaction per event."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")


def _apply_reader_pragmas(conn: sqlite3.Connection) -> None:
    """§9.6, reader side — with one correction the spec's wording invites you to get wrong.

    §9.6 says writers and readers "both set: WAL journal mode, ``synchronous=NORMAL``,
    ``busy_timeout=5000``". Two of those three are per-*connection* settings and are applied here.
    The third is not: ``journal_mode`` is a persistent property of the database **file**, so setting
    it is a write, and a read-only connection raises ``attempt to write a readonly database``.

    A reader has nothing to set, because the writer already did — the file it opens is already in
    WAL. Attempting it anyway makes the reader crash on any database created outside the harness,
    which is precisely the reader's job to survive.
    """
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")


def _select_list(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> str:
    """Build a select list that tolerates optional columns being absent (§9.4).

    A reader written against today's schema must still open a database written before a migration
    landed, so a missing optional column is substituted as ``NULL AS <col>`` rather than raising.
    """
    live = _columns(conn, table)
    optional = set(OPTIONAL_COLUMNS.get(table, ()))
    parts = []
    for col in columns:
        if col in live:
            parts.append(col)
        elif col in optional:
            parts.append(f"NULL AS {col}")
        else:
            raise sqlite3.OperationalError(
                f"{table}.{col} is missing and is not an optional column; "
                "add it to _MIGRATIONS and OPTIONAL_COLUMNS together"
            )
    return ", ".join(parts)


# ---------------------------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------------------------


class Tracer:
    """Writes the raw record and the queryable mirror, as work happens."""

    def __init__(self, db_path: Path, session_dir: Path, f_id: str) -> None:
        self.f_id = f_id
        self.db_path = Path(db_path)
        self.session_dir = Path(session_dir)
        self.events_path = self.session_dir / "events.jsonl"
        self._closed = False

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        _apply_writer_pragmas(self.conn)
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, decl in _MIGRATIONS:
            if column not in _columns(self.conn, table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.conn.close()

    @property
    def closed(self) -> bool:
        """Whether the writer has shut down. A closed tracer records nothing more."""
        return self._closed

    # -- the one write path -----------------------------------------------------------------

    def _record(self, kind: str, payload: dict[str, Any], write_db) -> None:
        """Append to the raw record, then update the mirror. Order is the contract.

        §9.2 says files win in a disagreement. Rather than asserting that in a comment, this writes
        the file first: a crash between the two steps leaves ``events.jsonl`` ahead of the database,
        which is the only direction the stated rule allows. Reversing these two lines would quietly
        invert the invariant, so they are in one place and only one place.
        """
        line = json.dumps({"record": kind, **payload}, default=str, sort_keys=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        with self.conn:
            write_db()

    # -- sessions ---------------------------------------------------------------------------

    def session_start(self, record: SessionRecord) -> SessionRecord:
        record.started_at = record.started_at or utc_now()
        record.status = SessionStatus.RUNNING
        self._record(
            "session_start",
            record.model_dump(mode="json"),
            lambda: self.conn.execute(
                """INSERT INTO sessions (f_id, f_name, request, status, engineer, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(f_id) DO UPDATE SET
                       f_name = excluded.f_name,
                       status = excluded.status""",
                (
                    record.f_id,
                    record.f_name,
                    record.request,
                    record.status.value,
                    record.engineer,
                    to_iso(record.started_at),
                ),
            ),
        )
        return record

    def session_finish(
        self, status: SessionStatus, total_tokens: int = 0, total_cost: float = 0.0
    ) -> None:
        ended = utc_now()
        self._record(
            "session_finish",
            {
                "f_id": self.f_id,
                "status": status.value,
                "ended_at": to_iso(ended),
                "total_tokens": total_tokens,
                "total_cost": total_cost,
            },
            lambda: self.conn.execute(
                """UPDATE sessions
                      SET status = ?, ended_at = ?, total_tokens = ?, total_cost = ?
                    WHERE f_id = ?""",
                (status.value, to_iso(ended), total_tokens, total_cost, self.f_id),
            ),
        )

    # -- phases -----------------------------------------------------------------------------

    def phase_start(self, phase: PhaseRecord) -> PhaseRecord:
        """Insert a phase. It lands ``fail`` — the schema default — and must earn anything else."""
        phase.started_at = phase.started_at or utc_now()
        self._record(
            "phase_start",
            phase.model_dump(mode="json"),
            lambda: self.conn.execute(
                """INSERT INTO phases
                       (phase_id, f_id, seq, name, kind, owner, description,
                        attempt, retries, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    phase.phase_id,
                    phase.f_id,
                    phase.seq,
                    phase.name,
                    phase.kind.value,
                    phase.owner,
                    phase.description,
                    phase.attempt,
                    phase.retries,
                    to_iso(phase.started_at),
                ),
            ),
        )
        return phase

    def phase_end(self, phase: PhaseRecord) -> None:
        phase.ended_at = phase.ended_at or utc_now()
        self._record(
            "phase_end",
            phase.model_dump(mode="json"),
            lambda: self.conn.execute(
                """UPDATE phases
                      SET status = ?, attempt = ?, error = ?, ended_at = ?
                    WHERE phase_id = ?""",
                (
                    phase.status.value,
                    phase.attempt,
                    phase.error,
                    to_iso(phase.ended_at),
                    phase.phase_id,
                ),
            ),
        )

    # -- events -----------------------------------------------------------------------------

    def event(
        self,
        type: EventType,
        phase_id: str = "",
        name: str = "",
        payload: dict[str, Any] | None = None,
        tokens: int = 0,
        started_at=None,
        ended_at=None,
        parent_id: str = "",
    ) -> EventRecord:
        started = started_at or utc_now()
        record = EventRecord(
            event_id=new_id("e"),
            f_id=self.f_id,
            phase_id=phase_id,
            parent_id=parent_id,
            type=type,
            name=name,
            payload=payload or {},
            tokens=tokens,
            started_at=started,
            ended_at=ended_at or started,
        )
        self._record(
            "event",
            record.model_dump(mode="json"),
            lambda: self.conn.execute(
                """INSERT INTO events
                       (event_id, f_id, phase_id, parent_id, type, name,
                        payload_json, tokens, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.event_id,
                    record.f_id,
                    record.phase_id,
                    record.parent_id,
                    record.type.value,
                    record.name,
                    json.dumps(record.payload, default=str),
                    record.tokens,
                    to_iso(record.started_at),
                    to_iso(record.ended_at),
                ),
            ),
        )
        return record

    # -- envelopes --------------------------------------------------------------------------

    def envelope(self, record: EnvelopeRecord) -> EnvelopeRecord:
        """Persist an envelope — including an invalid one.

        §5.7 requires every failed parse attempt to be stored with ``valid=False``, so the trace
        shows what the agent actually said rather than merely that it was wrong.
        """
        record.created_at = record.created_at or utc_now()
        self._record(
            "envelope",
            record.model_dump(mode="json"),
            lambda: self.conn.execute(
                """INSERT INTO envelopes
                       (envelope_id, f_id, phase_id, agent, output_type,
                        payload_json, valid, attempt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.envelope_id,
                    record.f_id,
                    record.phase_id,
                    record.agent,
                    record.output_type,
                    json.dumps(record.payload, default=str),
                    int(record.valid),
                    record.attempt,
                    to_iso(record.created_at),
                ),
            ),
        )
        return record

    # -- gates ------------------------------------------------------------------------------

    def gate_result(self, record: GateResultRecord) -> None:
        record.created_at = record.created_at or utc_now()
        self._record(
            "gate_result",
            record.model_dump(mode="json"),
            lambda: self.conn.execute(
                """INSERT INTO gate_results
                       (f_id, phase_id, attempt, gate, passed,
                        violations_json, checks_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.f_id,
                    record.phase_id,
                    record.attempt,
                    record.gate,
                    int(record.passed),
                    json.dumps(record.violations),
                    json.dumps([c.model_dump(mode="json") for c in record.checks]),
                    to_iso(record.created_at),
                ),
            ),
        )

    # -- processes --------------------------------------------------------------------------

    def process_start(self, record: ProcessRecord) -> int:
        """Record a spawned child. An open row means *believed alive* (§9.3).

        The ``command`` is stored so a recycled pid is never killed by mistake — the caller must
        compare it before signalling.
        """
        record.started_at = record.started_at or utc_now()
        holder: dict[str, int] = {}

        def write() -> None:
            cursor = self.conn.execute(
                """INSERT INTO processes (f_id, kind, name, pid, command, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.f_id,
                    record.kind,
                    record.name,
                    record.pid,
                    record.command,
                    to_iso(record.started_at),
                ),
            )
            holder["id"] = int(cursor.lastrowid or 0)

        self._record("process_start", record.model_dump(mode="json"), write)
        return holder.get("id", 0)

    def process_end(self, row_id: int) -> None:
        ended = utc_now()
        self._record(
            "process_end",
            {"f_id": self.f_id, "id": row_id, "ended_at": to_iso(ended)},
            lambda: self.conn.execute(
                "UPDATE processes SET ended_at = ? WHERE id = ?", (to_iso(ended), row_id)
            ),
        )

    def close_open_processes(self) -> None:
        """Close every believed-alive row for this run.

        §9.9: a killed run must not leave the trace claiming work is in flight that is already dead.
        """
        ended = utc_now()
        self._record(
            "processes_closed",
            {"f_id": self.f_id, "ended_at": to_iso(ended)},
            lambda: self.conn.execute(
                "UPDATE processes SET ended_at = ? WHERE f_id = ? AND ended_at IS NULL",
                (to_iso(ended), self.f_id),
            ),
        )

    # -- agent sessions ---------------------------------------------------------------------

    def agent_session(self, record: AgentSessionRecord) -> None:
        now = utc_now()
        record.created_at = record.created_at or now
        record.last_used_at = now
        self._record(
            "agent_session",
            record.model_dump(mode="json"),
            lambda: self.conn.execute(
                """INSERT INTO agent_sessions
                       (f_id, agent, coding_agent, model, color, session_id,
                        context_tokens, context_window, created_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(f_id, agent) DO UPDATE SET
                       coding_agent   = excluded.coding_agent,
                       model          = excluded.model,
                       color          = excluded.color,
                       session_id     = excluded.session_id,
                       context_tokens = excluded.context_tokens,
                       context_window = excluded.context_window,
                       last_used_at   = excluded.last_used_at""",
                (
                    record.f_id,
                    record.agent,
                    record.coding_agent,
                    record.model,
                    record.color,
                    record.session_id,
                    record.context_tokens,
                    record.context_window,
                    to_iso(record.created_at),
                    to_iso(record.last_used_at),
                ),
            ),
        )


# ---------------------------------------------------------------------------------------------
# Reader (§9.7)
# ---------------------------------------------------------------------------------------------

_EVENT_COLUMNS = (
    "event_id",
    "f_id",
    "phase_id",
    "parent_id",
    "type",
    "name",
    "payload_json",
    "tokens",
    "started_at",
    "ended_at",
)

_SESSION_COLUMNS = (
    "f_id",
    "f_name",
    "request",
    "status",
    "engineer",
    "started_at",
    "ended_at",
    "total_tokens",
    "total_cost",
    "archived",
)


class TraceReader:
    """Read-only view of the trace.

    Live and history are the same query with a different cursor (§9.1), so this is all a UI needs
    and all it gets: the schema is the contract, and a reader has no privileged access.

    Durations, progress and lane layout are **derived** here, never stored (§9.3).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        # Read-only, so a reader can never accidentally write the trace it is observing.
        self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        _apply_reader_pragmas(self.conn)

    def close(self) -> None:
        self.conn.close()

    def poll_events(self, f_id: str, cursor: int = 0, limit: int = 500) -> list[sqlite3.Row]:
        """§9.7's polling contract. Retain the highest returned ``rowid`` as the next cursor."""
        select = _select_list(self.conn, "events", _EVENT_COLUMNS)
        return list(
            self.conn.execute(
                f"SELECT rowid, {select} FROM events "
                "WHERE f_id = ? AND rowid > ? ORDER BY rowid LIMIT ?",
                (f_id, cursor, limit),
            )
        )

    def session(self, f_id: str) -> sqlite3.Row | None:
        select = _select_list(self.conn, "sessions", _SESSION_COLUMNS)
        return self.conn.execute(
            f"SELECT {select} FROM sessions WHERE f_id = ?", (f_id,)
        ).fetchone()

    def sessions(self) -> list[sqlite3.Row]:
        select = _select_list(self.conn, "sessions", _SESSION_COLUMNS)
        return list(self.conn.execute(f"SELECT {select} FROM sessions ORDER BY started_at DESC"))

    def phases(self, f_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM phases WHERE f_id = ? ORDER BY seq", (f_id,))
        )

    def open_processes(self, f_id: str) -> list[sqlite3.Row]:
        """Believed-alive children. Compare ``command`` against the live pid before signalling."""
        return list(
            self.conn.execute(
                "SELECT * FROM processes WHERE f_id = ? AND ended_at IS NULL", (f_id,)
            )
        )

    def is_running(self, f_id: str) -> bool:
        """Polling stops when the session leaves ``running`` — after one final drain (§9.7)."""
        row = self.conn.execute("SELECT status FROM sessions WHERE f_id = ?", (f_id,)).fetchone()
        return bool(row) and row["status"] == SessionStatus.RUNNING.value

    def phase_status_counts(self, f_id: str) -> dict[str, int]:
        """Derived, not stored. ``queued`` is counted separately because it is not a failure."""
        counts = {status.value: 0 for status in PhaseStatus}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM phases WHERE f_id = ? GROUP BY status", (f_id,)
        ):
            counts[row["status"]] = row["n"]
        return counts
