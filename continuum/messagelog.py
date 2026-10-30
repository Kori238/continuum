"""A persistent log of every message Jake receives.

Two feeds, one table:
  * notifications — the system-wide toast feed (Slack/Discord/mail/…), captured
    the instant a message arrives via the Windows UserNotificationListener.
  * windows       — messages read off the Discord/Slack windows on screen, for
    things that never fired a toast (an active channel you're already reading).

Both are de-duplicated into `data/messages.db` so a growing history survives
restarts, is queryable by the agent, and can be shown as a live feed. This is
verbatim received-message history — local, on Jake's machine, his to prune.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .state import messages as window_messages
from .state import notifications

log = logging.getLogger("continuum.messagelog")

# "Author: message body" — split the sender out where chat apps use it.
_AUTHOR = re.compile(r"^([^:\n]{1,40}):\s+(.*)$", re.S)
# Apps whose toasts aren't interpersonal messages — skip to keep the log clean.
_SKIP_APPS = {"windows security", "microsoft defender", "adobe express photos",
              "snip & sketch", "screen snip", "onedrive", "windows update",
              "notification suggestions", "google play games"}


def _key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()


def _split_author(body: str) -> tuple[Optional[str], str]:
    b = body.strip()
    m = _AUTHOR.match(b)
    if m and len(m.group(1).strip()) <= 40:
        return m.group(1).strip(), m.group(2).strip()
    return None, b


class MessageLog:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.messages_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, source TEXT, app TEXT, title TEXT, author TEXT,
                body TEXT, key TEXT UNIQUE)"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts)")
        self._db.commit()

    def record(self, source: str, app: str, title: str, body: str,
               key: str, author: Optional[str] = None, ts: Optional[float] = None) -> bool:
        """Insert one message; returns True if it was new (deduped on key)."""
        if author is None:
            author, body = _split_author(body)
        try:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO messages(ts, source, app, title, author, body, key) "
                "VALUES(?,?,?,?,?,?,?)",
                (ts or time.time(), source, app, title, author, body, key))
            self._db.commit()
            return cur.rowcount > 0
        except Exception:
            log.debug("record failed", exc_info=True)
            return False

    # ---- queries ------------------------------------------------------------
    def recent(self, limit: int = 20, app: Optional[str] = None,
               since: Optional[float] = None) -> list[dict[str, Any]]:
        q = "SELECT ts, source, app, title, author, body FROM messages"
        conds, args = [], []
        if app:
            conds.append("LOWER(app) LIKE ?"); args.append(f"%{app.lower()}%")
        if since:
            conds.append("ts >= ?"); args.append(since)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        rows = self._db.execute(q, args).fetchall()
        return [self._row(r) for r in reversed(rows)]  # oldest→newest for reading

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{query}%"
        rows = self._db.execute(
            "SELECT ts, source, app, title, author, body FROM messages "
            "WHERE body LIKE ? OR author LIKE ? OR title LIKE ? "
            "ORDER BY ts DESC LIMIT ?", (like, like, like, limit)).fetchall()
        return [self._row(r) for r in rows]

    def count_since(self, since: float) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE ts >= ?", (since,)).fetchone()[0]

    @staticmethod
    def _row(r) -> dict[str, Any]:
        ts, source, app, title, author, body = r
        return {"ts": ts, "source": source, "app": app, "title": title,
                "author": author, "body": body}


class MessageCollector:
    """Drives both feeds into the log on an interval. Notifications every tick
    (cheap, must catch them before they're cleared); window scraping less often
    (heavier, and only adds what wasn't already toasted)."""

    def __init__(self, log_store: MessageLog, interval: float = 8.0,
                 window_every: int = 3) -> None:
        self._log = log_store
        self._interval = interval
        self._window_every = window_every

    def collect_notifications(self) -> int:
        n = 0
        for note in notifications.poll_notifications():
            app = (note.get("app") or "").strip()
            title, body = note.get("title", ""), note.get("body", "")
            if app.lower() in _SKIP_APPS:
                continue
            # Never log Continuum's own toasts back in as received messages.
            if "continuum" in f"{app} {title}".lower():
                continue
            if not body and not title:
                continue
            key = _key("notif", app, str(note.get("id")), title, body)
            if self._log.record("notification", app, title, body, key):
                n += 1
        return n

    def collect_windows(self) -> int:
        n = 0
        data = window_messages.read_messages(limit=20)
        for app, rows in data.items():
            for line in rows:
                # Window rows look like "author 21:44 · 21:44 · text" — treat the
                # whole cleaned line as the body; author-splitting is unreliable here.
                key = _key("win", app, line)
                # author-splitting is unreliable on scraped rows — store as-is.
                if self._log.record("window", app, "", line, key, author=""):
                    n += 1
        return n

    async def run(self) -> None:
        import asyncio
        i = 0
        while True:
            try:
                await asyncio.to_thread(self.collect_notifications)
                if i % self._window_every == 0:
                    await asyncio.to_thread(self.collect_windows)
            except Exception:
                log.exception("message collection failed")
            i += 1
            await asyncio.sleep(self._interval)
