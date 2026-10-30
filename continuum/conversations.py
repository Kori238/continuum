"""Conversations — multi-turn threads, persisted and browsable.

Until now the task console was stateless: each prompt was a one-shot. A
conversation is a sequence of turns with a stable id, so follow-ups ("and
tomorrow?") can see prior turns as context, and threads survive restarts and can
be reopened later. This is verbatim short-term working memory of one thread;
`memory/` remains the distilled, cross-thread semantic layer.

Assistant turns store their metadata (tier, tools used, memory/state flags) so a
reopened conversation renders exactly as it happened.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .config import Config
from .memory.embed import Embedder

log = logging.getLogger("continuum.conversations")


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(t) for t in v] if isinstance(v, list) else []
    except Exception:
        return []


@dataclass
class Message:
    id: int
    role: str          # "user" | "assistant"
    content: str
    ts: float
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, "content": self.content,
                "ts": self.ts, "meta": self.meta}


class ConversationStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.conversations_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS conversations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, created REAL, updated REAL, tags TEXT,
                kind TEXT DEFAULT 'task')"""
        )
        have = {r[1] for r in self._db.execute("PRAGMA table_info(conversations)")}
        if "tags" not in have:
            self._db.execute("ALTER TABLE conversations ADD COLUMN tags TEXT")
        if "kind" not in have:
            self._db.execute("ALTER TABLE conversations ADD COLUMN kind TEXT DEFAULT 'task'")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER, role TEXT, content TEXT, meta TEXT, ts REAL)"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id)")
        # Turn-level semantic search lives HERE now (v2), not as truncated 'episode'
        # copies in memory.db: each message carries an embedding from the same fixed
        # local embedder memory uses, so a thread is searchable at its source and
        # linked by conversation_id. Columns are added by migration for older DBs.
        mcols = {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}
        for col, ddl in [
            ("embedding", "ALTER TABLE messages ADD COLUMN embedding BLOB"),
            ("embed_model", "ALTER TABLE messages ADD COLUMN embed_model TEXT"),
            ("dim", "ALTER TABLE messages ADD COLUMN dim INTEGER"),
        ]:
            if col not in mcols:
                self._db.execute(ddl)
        self._db.commit()
        self._embedder = Embedder(cfg)
        # In-memory normalized vectors of the embedded messages — built once, then
        # appended to on each new turn. The same fast-at-personal-scale trick the
        # memory store uses; swap in a vector index later untouched.
        self._idx_ids: list[int] = []
        self._idx_mat: Optional[np.ndarray] = None
        self._idx_meta: dict[int, dict[str, Any]] = {}
        self._idx_dim: Optional[int] = None
        self._load_index()

    # ---- writes ------------------------------------------------------------
    def create(self, title: str = "", kind: str = "task") -> int:
        now = time.time()
        title = (title or "New conversation").strip()[:80]
        cur = self._db.execute(
            "INSERT INTO conversations(title, created, updated, kind) VALUES(?,?,?,?)",
            (title, now, now, kind))
        self._db.commit()
        return int(cur.lastrowid)

    def main(self) -> int:
        """The singleton 'main thread' — the ongoing day-to-day, always present.
        Created once and reused; task conversations break off from it."""
        r = self._db.execute(
            "SELECT id FROM conversations WHERE kind='main' ORDER BY id LIMIT 1").fetchone()
        if r:
            return int(r[0])
        return self.create(title="Main thread", kind="main")

    def dev(self, fresh: bool = False) -> int:
        """The Dev tab's thread. Self-development is a conversation like any other
        now — it lives in the same store, so the agent remembers what it changed
        last message, and you can read the whole trail back. `fresh` starts a new
        one, but reuses an untouched thread rather than littering empties."""
        r = self._db.execute(
            "SELECT c.id, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) "
            "FROM conversations c WHERE c.kind='dev' ORDER BY c.id DESC LIMIT 1").fetchone()
        if r and not (fresh and r[1]):
            return int(r[0])
        return self.create(title="Self-development", kind="dev")

    def is_main(self, conv_id: int) -> bool:
        r = self._db.execute("SELECT kind FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return bool(r and r[0] == "main")

    def append(self, conv_id: int, role: str, content: str,
               meta: Optional[dict] = None) -> int:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO messages(conversation_id, role, content, meta, ts) VALUES(?,?,?,?,?)",
            (conv_id, role, content, json.dumps(meta or {}), now))
        self._db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, conv_id))
        self._db.commit()
        return int(cur.lastrowid)

    def rename(self, conv_id: int, title: str) -> None:
        self._db.execute("UPDATE conversations SET title=? WHERE id=?",
                         (title.strip()[:80], conv_id))
        self._db.commit()

    def set_tags(self, conv_id: int, tags: list[str]) -> None:
        self._db.execute("UPDATE conversations SET tags=? WHERE id=?",
                         (json.dumps(tags or []), conv_id))
        self._db.commit()

    def tags_of(self, conv_id: int) -> list[str]:
        r = self._db.execute("SELECT tags FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return _parse_tags(r[0]) if r else []

    def delete(self, conv_id: int) -> bool:
        if self.is_main(conv_id):
            return False  # the main thread is permanent
        self._db.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        self._db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        self._db.commit()
        return True

    # ---- reads -------------------------------------------------------------
    def exists(self, conv_id: int) -> bool:
        return self._db.execute(
            "SELECT 1 FROM conversations WHERE id=?", (conv_id,)).fetchone() is not None

    def recent_turns(self, conv_id: int, n: int = 8) -> list[Message]:
        """The last n messages, oldest-first — the context window for a reply."""
        rows = self._db.execute(
            "SELECT id, role, content, meta, ts FROM messages WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?", (conv_id, n)).fetchall()
        rows.reverse()
        return [self._to_msg(r) for r in rows]

    def messages(self, conv_id: int) -> list[Message]:
        rows = self._db.execute(
            "SELECT id, role, content, meta, ts FROM messages WHERE conversation_id=? "
            "ORDER BY id", (conv_id,)).fetchall()
        return [self._to_msg(r) for r in rows]

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        # Main thread pinned first, then task conversations by recency.
        rows = self._db.execute(
            "SELECT c.id, c.title, c.created, c.updated, c.tags, c.kind, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) "
            "FROM conversations c ORDER BY (c.kind='main') DESC, c.updated DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"id": r[0], "title": r[1], "created": r[2], "updated": r[3],
                 "tags": _parse_tags(r[4]), "kind": r[5] or "task", "messages": r[6]}
                for r in rows]

    def get(self, conv_id: int) -> Optional[dict[str, Any]]:
        r = self._db.execute(
            "SELECT id, title, created, updated, tags, kind FROM conversations WHERE id=?",
            (conv_id,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "title": r[1], "created": r[2], "updated": r[3],
                "tags": _parse_tags(r[4]), "kind": r[5] or "task",
                "messages": [m.as_dict() for m in self.messages(conv_id)],
                "related": self.related(conv_id)}

    def related(self, conv_id: int, k: int = 5) -> list[dict[str, Any]]:
        """Other conversations that share tags with this one (derived graph)."""
        mine = set(self.tags_of(conv_id))
        if not mine:
            return []
        out = []
        for c in self.list(200):
            if c["id"] == conv_id:
                continue
            overlap = mine & set(c.get("tags", []))
            if overlap:
                out.append({"id": c["id"], "title": c["title"],
                            "shared": sorted(overlap), "n": len(overlap)})
        out.sort(key=lambda x: x["n"], reverse=True)
        return out[:k]

    # ---- semantic index (turn-level search; replaces memory.db 'episode' rows) ----
    def _load_index(self) -> None:
        self._idx_ids, self._idx_meta = [], {}
        self._idx_dim = None
        vecs: list[np.ndarray] = []
        for r in self._db.execute(
            "SELECT id, conversation_id, role, content, ts, embedding "
            "FROM messages WHERE embedding IS NOT NULL ORDER BY id"):
            mid, conv, role, content, ts, emb = r
            v = np.frombuffer(emb, dtype=np.float32)
            if self._idx_dim is None:
                self._idx_dim = int(v.size)
            if v.size != self._idx_dim:
                continue  # a different embedder space — never mix
            self._idx_ids.append(mid)
            self._idx_meta[mid] = {"conversation_id": conv, "role": role,
                                   "content": content, "ts": ts}
            vecs.append(v / (np.linalg.norm(v) + 1e-8))
        self._idx_mat = np.vstack(vecs) if vecs else None

    def _append_index(self, mid: int, conv: int, role: str, content: str,
                      ts: float, vec: np.ndarray) -> None:
        if self._idx_dim is None:
            self._idx_dim = int(vec.size)
        if vec.size != self._idx_dim:
            return
        n = (vec / (np.linalg.norm(vec) + 1e-8))[None, :]
        self._idx_mat = n if self._idx_mat is None else np.vstack([self._idx_mat, n])
        self._idx_ids.append(mid)
        self._idx_meta[mid] = {"conversation_id": conv, "role": role,
                               "content": content, "ts": ts}

    async def index_message(self, msg_id: int) -> bool:
        """Embed one message and add it to the search index. Index each id once
        (the agent indexes a turn's messages exactly once, after answering)."""
        row = self._db.execute(
            "SELECT conversation_id, role, content, ts FROM messages WHERE id=?",
            (msg_id,)).fetchone()
        if not row:
            return False
        conv, role, content, ts = row
        content = (content or "").strip()
        if not content:
            return False
        vec = np.asarray(await self._embedder.embed(content), dtype=np.float32)
        self._db.execute(
            "UPDATE messages SET embedding=?, embed_model=?, dim=? WHERE id=?",
            (vec.tobytes(), self._embedder.model, int(vec.size), msg_id))
        self._db.commit()
        self._append_index(msg_id, conv, role, content, ts, vec)
        return True

    async def index_turn(self, *msg_ids: Optional[int]) -> None:
        """Index the messages of a just-completed turn (user + assistant)."""
        for mid in msg_ids:
            if mid:
                await self.index_message(int(mid))

    async def backfill(self, limit: Optional[int] = None) -> int:
        """Embed any messages that have no vector yet. Guarded on the embedder
        being up; stops on the first embed error so a mid-run failure just leaves
        the rest for the next call (NULL embeddings are retried)."""
        if not await self._embedder.available():
            return 0
        ids = [r[0] for r in self._db.execute(
            "SELECT id FROM messages WHERE embedding IS NULL ORDER BY id").fetchall()]
        if limit:
            ids = ids[:limit]
        done = 0
        for mid in ids:
            try:
                if await self.index_message(mid):
                    done += 1
            except Exception:
                log.exception("conversation backfill stopped at message %s", mid)
                break
        if done:
            log.info("conversations: indexed %d message(s) for search", done)
        return done

    async def search(self, query: str, k: int = 5,
                     conversation_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Semantic search over past turns — the full text of what was actually
        said. Returns matching messages with their conversation title and score."""
        query = (query or "").strip()
        if not query or self._idx_mat is None:
            return []
        qv = np.asarray(await self._embedder.embed(query), dtype=np.float32)
        if self._idx_dim is not None and qv.size != self._idx_dim:
            return []
        qn = qv / (np.linalg.norm(qv) + 1e-8)
        sims = self._idx_mat @ qn
        out: list[tuple[int, float, dict[str, Any]]] = []
        for idx in np.argsort(-sims):
            mid = self._idx_ids[int(idx)]
            meta = self._idx_meta[mid]
            if conversation_id is not None and meta["conversation_id"] != conversation_id:
                continue
            out.append((mid, float(sims[int(idx)]), meta))
            if len(out) >= k:
                break
        titles: dict[int, str] = {}
        for _, _, meta in out:
            cid = meta["conversation_id"]
            if cid not in titles:
                r = self._db.execute(
                    "SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
                titles[cid] = r[0] if r else ""
        return [{"message_id": mid, "conversation_id": meta["conversation_id"],
                 "title": titles.get(meta["conversation_id"], ""), "role": meta["role"],
                 "content": meta["content"], "ts": meta["ts"], "score": round(score, 4)}
                for mid, score, meta in out]

    @staticmethod
    def _to_msg(row: tuple) -> Message:
        meta = {}
        try:
            meta = json.loads(row[3] or "{}")
        except Exception:
            pass
        return Message(id=row[0], role=row[1], content=row[2], ts=row[4], meta=meta)
