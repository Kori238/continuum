"""Action ledger — the durable, append-only record of everything Continuum DID.

Every state-changing tool already announces itself on the bus as an
`action.performed` event, and — new with the enforcement gate — a *refused*
attempt as `action.refused`. Those events are transient: they fan out to the live
UI and then they're gone. This store is the half that survives. A bus subscriber
persists each one here, so "what did Continuum actually do to my machine today?"
becomes a real, answerable question instead of something you can only try to
reconstruct by grepping `tools_used` back out of the conversation log.

That blind spot is not hypothetical. It is exactly what let a wrongful "I didn't
send that notification" stand for a whole turn — there was no independent record
to check against. The ledger is that record.

One SQLite file (`data/ledger.db`), one table, same principles as the rest of
`data/`: it is **append-only** — there is deliberately no update and no delete,
because an audit trail you can quietly rewrite is not an audit trail — and it is
Continuum's own evidence, never injected into a turn. It's read on request via
the `review_actions` tool, and it's the enforcement point's witness: when the
notification gate refuses a call, the refusal lands here too.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.ledger")


class ActionLedger:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(getattr(cfg, "ledger_db", "data/ledger.db"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS actions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                action TEXT NOT NULL,
                args TEXT,
                result TEXT,
                allowed INTEGER NOT NULL DEFAULT 1,
                reason TEXT,
                request_id TEXT,
                source TEXT)"""
        )
        self._db.commit()

    # ---- write (append-only) ------------------------------------------------
    def record(self, *, action: str, args: Any = None, result: str = "",
               allowed: bool = True, reason: str = "", request_id: str = "",
               source: str = "agent") -> int:
        """Append one row. There is deliberately no update or delete: the whole
        point of the ledger is that it can't be quietly rewritten after the fact.
        Args are stored as JSON (capped) so a huge clipboard write can't bloat the
        trail; a value that won't serialise falls back to its string form."""
        try:
            args_json = json.dumps(args, default=str)[:4000] if args is not None else None
        except (TypeError, ValueError):
            args_json = str(args)[:4000]
        try:
            cur = self._db.execute(
                "INSERT INTO actions(ts, action, args, result, allowed, reason,"
                " request_id, source) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), str(action), args_json, (result or "")[:1000],
                 1 if allowed else 0, reason or "", request_id or "", source or ""))
            self._db.commit()
            return int(cur.lastrowid)
        except Exception:
            # The ledger must never break a turn — a failed audit write is logged
            # and swallowed, exactly like the bus's own transparency emitters.
            log.exception("ledger write failed")
            return 0

    # ---- read ---------------------------------------------------------------
    def recent(self, limit: int = 20, *, action: Optional[str] = None,
               allowed: Optional[bool] = None, since_ts: Optional[float] = None
               ) -> list[dict[str, Any]]:
        """Most-recent-first rows, optionally filtered to one action, to just the
        refusals (`allowed=False`) or just the performed (`allowed=True`), and/or
        to everything since a timestamp."""
        limit = max(1, min(int(limit or 20), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if action:
            clauses.append("action = ?"); params.append(str(action))
        if allowed is not None:
            clauses.append("allowed = ?"); params.append(1 if allowed else 0)
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(float(since_ts))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._db.execute(
            f"SELECT * FROM actions{where} ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    def count(self, *, since_ts: Optional[float] = None,
              allowed: Optional[bool] = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(float(since_ts))
        if allowed is not None:
            clauses.append("allowed = ?"); params.append(1 if allowed else 0)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        r = self._db.execute(f"SELECT COUNT(*) c FROM actions{where}", params).fetchone()
        return int(r["c"]) if r else 0
