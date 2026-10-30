"""The context store — Continuum's persistent, cross-model memory (v2).

Provider-neutral storage (text + metadata in SQLite) with vectors from the fixed
local embedder, plus the ideas worth taking from the top memory systems:

  * Consolidation (Mem0)        — writes are ADD / UPDATE / NOOP, not blind
                                   appends, so memory dedups and resolves change.
  * Supersession (Zep/Graphiti) — an outdated memory is superseded, not deleted:
                                   history is kept, recall only sees what's current.
  * Importance + recency         — recall ranks on relevance + importance + a
    (Generative Agents)            kind-aware recency decay, not cosine alone.
  * Core block (Letta/MemGPT)   — a small pinned profile that's always injected,
                                   separate from the retrieved long tail.

Recall runs over an in-memory normalized matrix of the *active* (non-superseded)
memories — fast at personal scale; swap in sqlite-vec/FAISS later untouched.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np

from ..config import Config
from .embed import Embedder

log = logging.getLogger("continuum.memory.store")

# Kinds that get consolidated (durable knowledge). Episodes are the raw log:
# always appended, never consolidated (cheap, and they're history by nature).
DURABLE = {"fact", "preference"}
CORE_KIND = "core"

# Thresholds / weights (sensible defaults; tune later).
NEIGHBOR_SIM = 0.55     # min cosine to be considered "related" for consolidation
NOOP_SIM = 0.95         # near-identical → skip the model, call it a duplicate
MIN_RELEVANCE = 0.35    # semantic floor to inject a memory on relevance alone
SEMANTIC_FLOOR = 0.15   # but a tag hit can rescue a memory down to here
W_IMPORTANCE = 0.40     # recall weight on normalized importance
W_RECENCY = 0.30        # recall weight on recency
W_TAG = 0.50            # recall weight on tag/keyword overlap with the query
DECAY_TAU: dict[str, float] = {}   # seconds; kinds absent here don't decay.
# The 'episode' kind was retired: past turns are indexed for search in
# conversations.db (at their source, in full), not copied here — so nothing
# currently decays. Kept as a mechanism in case a decaying kind returns.

_TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "the", "and", "for", "are", "was", "you", "your", "his", "her", "our", "their",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "does",
    "did", "have", "has", "had", "with", "that", "this", "these", "those", "there",
    "here", "about", "into", "from", "just", "now", "one", "two", "get", "got",
    "can", "will", "would", "should", "could", "been", "being", "them", "they",
    "she", "him", "its", "not", "but", "any", "some", "all", "out", "off",
    "jake", "continuum", "please", "tell", "know", "want", "need",
}


def _tokens(text: str) -> set[str]:
    return {w for w in _TOKEN_RE.findall(text.lower())
            if len(w) >= 3 and w not in STOPWORDS}


def _tag_overlap(query_tokens: set[str], memory_terms: set[str]) -> int:
    """Count query tokens that match a memory term, allowing shared stems
    (drive/drives) so surface-form differences don't lose a match."""
    hits = 0
    for q in query_tokens:
        for m in memory_terms:
            if q == m or (len(q) >= 4 and (m.startswith(q) or q.startswith(m))):
                hits += 1
                break
    return hits


def _lexical_tags(text: str, limit: int = 6) -> list[str]:
    """Fallback tags when no model is available — salient words from the text."""
    return list(_tokens(text))[:limit]


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(t) for t in v] if isinstance(v, list) else []
    except Exception:
        return []


@dataclass
class Memory:
    id: int
    ts: float
    kind: str
    source: str
    text: str
    importance: float = 5.0
    tags: list[str] = field(default_factory=list)
    conversation_id: Optional[int] = None   # None = global; set = scoped to a thread
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0   # combined recall score, filled on recall

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "ts": self.ts, "kind": self.kind, "source": self.source,
            "text": self.text, "importance": round(self.importance, 1),
            "tags": self.tags, "conversation_id": self.conversation_id,
            "scope": "global" if self.conversation_id is None else "conversation",
            "meta": self.meta, "score": round(self.score, 4),
        }


@dataclass
class RememberResult:
    op: str                       # "ADD" | "UPDATE" | "NOOP"
    id: int                       # active memory id after the write
    importance: float = 5.0
    superseded_id: Optional[int] = None


class Reasoner(Protocol):
    async def available(self) -> bool: ...
    async def consolidate(self, candidate: str,
                          neighbors: list[tuple[int, str]]) -> Any: ...


class MemoryStore:
    def __init__(self, cfg: Config, embedder: Optional[Embedder] = None,
                 reasoner: Optional[Reasoner] = None) -> None:
        self._path = Path(cfg.memory_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder or Embedder(cfg)
        self._reasoner = reasoner
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._ensure_schema()
        self._drop_episodes()   # one-time: retire the old per-turn 'episode' copies
        # Active-memory recall cache (normalized vectors + light row copies).
        self._ids: list[int] = []
        self._rows: dict[int, dict[str, Any]] = {}
        self._mat: Optional[np.ndarray] = None
        self._dim: Optional[int] = None
        self._reload_cache()

    @property
    def embed_model(self) -> str:
        return self._embedder.model

    # ---- schema / migration ------------------------------------------------
    def _ensure_schema(self) -> None:
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, updated_at REAL, kind TEXT, source TEXT, text TEXT, meta TEXT,
                embedding BLOB, embed_model TEXT, dim INTEGER,
                importance REAL DEFAULT 5.0, tags TEXT, conversation_id INTEGER,
                superseded_by INTEGER, superseded_at REAL)"""
        )
        # A tiny side table recording which *destructive* one-shot migrations have
        # already run on this database, keyed by name. The additive column
        # migrations below don't need it — whether they're still owed is
        # re-derivable at any time from PRAGMA table_info, so they're idempotent by
        # construction. A DELETE can't be re-derived from anything: once the rows
        # are gone the database looks identical whether the migration ran or not,
        # so the only way to make it one-time is to write down that it happened.
        # Keyed by name rather than bumping a single PRAGMA user_version counter so
        # the next one-shot gets its own key instead of inheriting a global
        # ordering whose numbers nobody will remember the meaning of.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS schema_meta(
                key TEXT PRIMARY KEY, value TEXT, ts REAL)"""
        )
        # Migrate older tables (add any missing columns). Every column CREATE TABLE
        # declares has to be listed here too. A database made before a column
        # existed takes the CREATE ... IF NOT EXISTS path as a plain no-op — nothing
        # reconciles it against the current declaration — so this list is the only
        # thing that ever adds a column to it, and anything missing from the list
        # stays missing forever. That isn't cosmetic drift: _insert() names every
        # one of these columns explicitly, so a gap here is a hard write failure on
        # an older database while a freshly created one works fine.
        have = {r[1] for r in self._db.execute("PRAGMA table_info(memories)")}
        for col, ddl in [
            ("updated_at", "ALTER TABLE memories ADD COLUMN updated_at REAL"),
            ("embed_model", "ALTER TABLE memories ADD COLUMN embed_model TEXT"),
            ("dim", "ALTER TABLE memories ADD COLUMN dim INTEGER"),
            ("importance", "ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 5.0"),
            ("tags", "ALTER TABLE memories ADD COLUMN tags TEXT"),
            ("conversation_id", "ALTER TABLE memories ADD COLUMN conversation_id INTEGER"),
            ("superseded_by", "ALTER TABLE memories ADD COLUMN superseded_by INTEGER"),
            ("superseded_at", "ALTER TABLE memories ADD COLUMN superseded_at REAL"),
        ]:
            if col not in have:
                self._db.execute(ddl)
        self._db.commit()

    def _migration_done(self, name: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM schema_meta WHERE key=?", (f"migration:{name}",)
        ).fetchone()
        return row is not None

    def _mark_migration(self, name: str, note: str = "") -> None:
        """Record a one-shot migration as run, and commit — which also lands the
        migration's own writes, because they're still open on the same connection
        and therefore the same transaction. That coupling is deliberate: if the
        process dies mid-migration, neither the change nor the marker survives, so
        the next boot retries it cleanly instead of skipping a half-applied one."""
        self._db.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value,ts) VALUES(?,?,?)",
            (f"migration:{name}", note, time.time()),
        )
        self._db.commit()

    def _drop_episodes(self) -> None:
        """Migration: the 'episode' kind — a truncated copy of each turn — has moved
        out of memory.db. Turns are now indexed for search in conversations.db, where
        they already live in full. Delete any leftover episode rows so memory is
        strictly durable knowledge (fact / preference / core). data/ has no undo, but
        episodes were redundant by construction.

        Genuinely one-time now, gated on schema_meta. This used to fire its DELETE on
        every single MemoryStore construction, which reads as harmless only because
        no episode rows are left to match. What it actually meant is that any memory
        ever written with kind='episode' — by a future feature, a typo in a caller, a
        restored backup — would be silently destroyed at the next restart, with no
        error and nothing in the log to explain the gap. And this app restarts itself
        routinely to apply its own code changes, so "the next restart" is rarely more
        than minutes away. An unguarded destructive statement sitting on the boot
        path is a standing hazard no matter what it happens to match today."""
        if self._migration_done("drop_episodes"):
            return
        cur = self._db.execute("DELETE FROM memories WHERE kind='episode'")
        n = cur.rowcount or 0
        self._mark_migration("drop_episodes", f"removed {n} row(s)")
        if n:
            log.info("memory: retired %d episode row(s) — turns now indexed in "
                     "conversations.db", n)

    # ---- cache -------------------------------------------------------------
    def _reload_cache(self) -> None:
        self._ids, self._rows, vecs = [], {}, []
        self._dim = None
        skipped = 0
        for row in self._db.execute(
            "SELECT id,ts,updated_at,kind,source,text,meta,embedding,dim,importance,tags,conversation_id "
            "FROM memories WHERE superseded_at IS NULL ORDER BY id"
        ):
            (id_, ts, updated, kind, source, text, meta, emb, _dim, imp, tags, conv) = row
            v = np.frombuffer(emb, dtype=np.float32)
            if self._dim is None:
                self._dim = int(v.size)
            if v.size != self._dim:
                skipped += 1
                continue  # a different embedder space — never mix
            parsed = _parse_tags(tags)
            self._ids.append(id_)
            self._rows[id_] = {"id": id_, "ts": ts, "updated_at": updated, "kind": kind,
                               "source": source, "text": text, "meta": meta,
                               "importance": imp if imp is not None else 5.0,
                               "tags": parsed, "conversation_id": conv,
                               "terms": self._terms(text, parsed)}
            vecs.append(v / (np.linalg.norm(v) + 1e-8))
        self._mat = np.vstack(vecs) if vecs else None
        if skipped:
            log.warning("memory: skipped %d rows from a different embedder space", skipped)
        log.info("memory: %d active memories (dim=%s)", len(self._ids), self._dim)

    # ---- writes ------------------------------------------------------------
    @staticmethod
    def _terms(text: str, tags: list[str]) -> set[str]:
        """The searchable term set for a memory: its tags plus its own words."""
        return {t.lower() for t in tags} | _tokens(text)

    def _insert(self, text: str, kind: str, source: str, meta: Optional[dict],
                vec: np.ndarray, importance: float, ts: float,
                tags: Optional[list[str]] = None,
                conversation_id: Optional[int] = None) -> int:
        cur = self._db.execute(
            "INSERT INTO memories(ts,updated_at,kind,source,text,meta,embedding,embed_model,dim,importance,tags,conversation_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, ts, kind, source, text, json.dumps(meta or {}),
             vec.tobytes(), self._embedder.model, int(vec.size), float(importance),
             json.dumps(tags or []), conversation_id),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def _append_cache(self, id_: int, vec: np.ndarray, kind: str, source: str,
                      text: str, meta: Optional[dict], importance: float, ts: float,
                      tags: Optional[list[str]] = None,
                      conversation_id: Optional[int] = None) -> None:
        if self._dim is None:
            self._dim = int(vec.size)
        if vec.size != self._dim:
            return
        n = (vec / (np.linalg.norm(vec) + 1e-8))[None, :]
        self._mat = n if self._mat is None else np.vstack([self._mat, n])
        self._ids.append(id_)
        tags = tags or []
        self._rows[id_] = {"id": id_, "ts": ts, "updated_at": ts, "kind": kind,
                           "source": source, "text": text, "meta": json.dumps(meta or {}),
                           "importance": importance, "tags": tags,
                           "conversation_id": conversation_id,
                           "terms": self._terms(text, tags)}

    def _supersede(self, old_id: int, new_id: Optional[int], ts: float) -> None:
        self._db.execute(
            "UPDATE memories SET superseded_by=?, superseded_at=? WHERE id=?",
            (new_id, ts, old_id),
        )
        self._db.commit()

    def _neighbors(self, vec: np.ndarray, kinds: set[str], k: int = 5,
                   min_sim: float = NEIGHBOR_SIM) -> list[tuple[int, str, float]]:
        if self._mat is None:
            return []
        qn = vec / (np.linalg.norm(vec) + 1e-8)
        sims = self._mat @ qn
        out = []
        for idx in np.argsort(-sims):
            s = float(sims[idx])
            if s < min_sim:
                break
            row = self._rows[self._ids[idx]]
            if row["kind"] in kinds:
                out.append((row["id"], row["text"], s))
            if len(out) >= k:
                break
        return out

    async def remember(self, text: str, kind: str = "fact", source: str = "agent",
                       meta: Optional[dict] = None,
                       conversation_id: Optional[int] = None) -> RememberResult:
        text = (text or "").strip()
        if not text:
            return RememberResult(op="NOOP", id=-1)
        vec = np.asarray(await self._embedder.embed(text), dtype=np.float32)
        ts = time.time()

        # Reasoner-less writes (or any non-durable kind): straight append, no
        # consolidation. Durable memories still get lexical tags so they stay
        # retrievable even when no reasoner is available.
        if kind not in DURABLE or self._reasoner is None:
            imp = 5.0
            tags = _lexical_tags(text) if kind in DURABLE else []
            id_ = self._insert(text, kind, source, meta, vec, imp, ts, tags, conversation_id)
            self._append_cache(id_, vec, kind, source, text, meta, imp, ts, tags, conversation_id)
            log.info("memory +%s [%s] %s", id_, kind, text[:60])
            return RememberResult(op="ADD", id=id_, importance=imp)

        # Durable: one reasoner call yields the op (ADD/UPDATE/NOOP), an importance
        # rating, AND the tags — so every durable memory is tagged for retrieval.
        neighbors = self._neighbors(vec, kinds=DURABLE)
        if neighbors and neighbors[0][2] >= NOOP_SIM:
            match_id = neighbors[0][0]
            self._db.execute("UPDATE memories SET updated_at=? WHERE id=?", (ts, match_id))
            self._db.commit()
            log.info("memory NOOP (dup of %s) %s", match_id, text[:50])
            return RememberResult(op="NOOP", id=match_id)

        op, target, importance, tags = "ADD", None, 5.0, []
        try:
            j = await self._reasoner.consolidate(text, [(i, t) for i, t, _ in neighbors])
            op, target, importance, tags = j.op, j.target, j.importance, j.tags
        except Exception:
            log.exception("consolidation failed; falling back to ADD")
        if not tags:
            tags = _lexical_tags(text)

        if op == "NOOP" and target is not None:
            self._db.execute("UPDATE memories SET updated_at=? WHERE id=?", (ts, target))
            self._db.commit()
            log.info("memory NOOP (covered by %s) %s", target, text[:50])
            return RememberResult(op="NOOP", id=target)

        id_ = self._insert(text, kind, source, meta, vec, importance, ts, tags, conversation_id)
        if op == "UPDATE" and target is not None:
            self._supersede(target, id_, ts)
            self._reload_cache()  # active set changed
            log.info("memory UPDATE %s -> +%s [imp %.0f tags %s] %s",
                     target, id_, importance, tags, text[:40])
            return RememberResult(op="UPDATE", id=id_, importance=importance, superseded_id=target)

        self._append_cache(id_, vec, kind, source, text, meta, importance, ts, tags, conversation_id)
        log.info("memory +%s [%s imp %.0f tags %s] %s", id_, kind, importance, tags, text[:50])
        return RememberResult(op="ADD", id=id_, importance=importance)

    # ---- core profile block (Letta) ---------------------------------------
    def get_core(self) -> str:
        row = self._db.execute(
            "SELECT text FROM memories WHERE kind=? AND superseded_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (CORE_KIND,)
        ).fetchone()
        return row[0] if row else ""

    async def set_core(self, text: str) -> int:
        text = (text or "").strip()
        ts = time.time()
        old = self._db.execute(
            "SELECT id FROM memories WHERE kind=? AND superseded_at IS NULL", (CORE_KIND,)
        ).fetchall()
        vec = np.asarray(await self._embedder.embed(text or " "), dtype=np.float32)
        id_ = self._insert(text, CORE_KIND, "user", None, vec, 10.0, ts)
        for (old_id,) in old:
            self._supersede(old_id, id_, ts)
        self._reload_cache()
        return id_

    # ---- reads -------------------------------------------------------------
    def _recency(self, kind: str, ts: float, now: float) -> float:
        tau = DECAY_TAU.get(kind)
        if not tau:
            return 1.0  # non-decaying kinds (facts/preferences) stay fresh
        return math.exp(-max(0.0, now - ts) / tau)

    async def recall(self, query: str, k: int = 5, kind: Optional[str] = None,
                     kinds: Optional[set[str]] = None,
                     min_score: float = MIN_RELEVANCE,
                     conversation_id: Optional[int] = None) -> list[Memory]:
        if self._mat is None or not query.strip():
            return []
        qv = np.asarray(await self._embedder.embed(query), dtype=np.float32)
        if qv.size != self._dim:
            log.warning("memory: query dim %d != store dim %s; recall disabled", qv.size, self._dim)
            return []
        qn = qv / (np.linalg.norm(qv) + 1e-8)
        rel = self._mat @ qn
        q_tokens = _tokens(query)
        now = time.time()
        scored: list[tuple[float, Memory]] = []
        for idx in range(len(self._ids)):
            relevance = float(rel[idx])
            row = self._rows[self._ids[idx]]
            if row["kind"] == CORE_KIND:
                continue  # core is injected separately, never via recall
            # Scope: global memories (conversation_id NULL) always; a scoped memory
            # only when we're in its conversation.
            scope = row.get("conversation_id")
            if scope is not None and scope != conversation_id:
                continue
            if kind and row["kind"] != kind:
                continue
            if kinds and row["kind"] not in kinds:
                continue
            # Hybrid: semantic relevance + tag/keyword overlap with the query.
            hits = _tag_overlap(q_tokens, row.get("terms") or set())
            # A tag hit can rescue a memory the embedding ranked low (but never
            # near-random matches); otherwise require the normal semantic floor.
            if hits > 0:
                if relevance < SEMANTIC_FLOOR:
                    continue
            elif relevance < min_score:
                continue
            imp = float(row.get("importance") or 5.0)
            recency = self._recency(row["kind"], row["ts"], now)
            combined = (relevance
                        + W_TAG * min(hits, 3) / 3.0
                        + W_IMPORTANCE * (imp / 10.0)
                        + W_RECENCY * recency)
            scored.append((combined, self._to_mem(row, combined)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:k]]

    def delete(self, memory_id: int) -> bool:
        cur = self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self._db.commit()
        if cur.rowcount:
            self._reload_cache()
        return bool(cur.rowcount)

    async def related(self, memory_id: int, k: int = 6) -> list[Memory]:
        """Memories related to this one — by embedding similarity blended with
        shared tags. Derived on the fly; no stored graph to maintain."""
        if memory_id not in self._rows or self._mat is None:
            return []
        try:
            idx = self._ids.index(memory_id)
        except ValueError:
            return []
        base_terms = self._rows[memory_id].get("terms") or set()
        sims = self._mat @ self._mat[idx]
        scored: list[tuple[float, Memory]] = []
        for j, mid in enumerate(self._ids):
            if mid == memory_id:
                continue
            row = self._rows[mid]
            if row["kind"] == CORE_KIND:
                continue
            overlap = len(base_terms & (row.get("terms") or set()))
            combined = float(sims[j]) + 0.15 * min(overlap, 3)
            if combined < 0.35 and overlap == 0:
                continue
            scored.append((combined, self._to_mem(row, combined)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:k]]

    def by_conversation(self, conversation_id: int) -> list[Memory]:
        """Memories that originated in / are scoped to a conversation."""
        cur = self._db.execute(
            "SELECT id,ts,kind,source,text,meta,importance,tags,conversation_id FROM memories "
            "WHERE conversation_id=? AND superseded_at IS NULL ORDER BY id DESC",
            (conversation_id,))
        cols = [c[0] for c in cur.description]
        return [self._to_mem(dict(zip(cols, r))) for r in cur.fetchall()]

    def recent(self, n: int = 20, kind: Optional[str] = None,
               include_core: bool = False) -> list[Memory]:
        q = ("SELECT id,ts,kind,source,text,meta,importance,tags,conversation_id FROM memories "
             "WHERE superseded_at IS NULL")
        params: list[Any] = []
        if kind:
            q += " AND kind=?"
            params.append(kind)
        elif not include_core:
            q += " AND kind != ?"
            params.append(CORE_KIND)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(n)
        cur = self._db.execute(q, params)
        cols = [c[0] for c in cur.description]
        return [self._to_mem(dict(zip(cols, r))) for r in cur.fetchall()]

    def count(self, active_only: bool = True) -> int:
        q = "SELECT COUNT(*) FROM memories WHERE kind != ?"
        params: list[Any] = [CORE_KIND]
        if active_only:
            q += " AND superseded_at IS NULL"
        return int(self._db.execute(q, params).fetchone()[0])

    @staticmethod
    def _to_mem(row: dict[str, Any], score: float = 0.0) -> Memory:
        meta = row.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except Exception:
                meta = {}
        tags = row.get("tags")
        if isinstance(tags, str):
            tags = _parse_tags(tags)
        return Memory(
            id=row["id"], ts=row["ts"], kind=row["kind"], source=row["source"],
            text=row["text"], importance=float(row.get("importance") or 5.0),
            tags=tags or [], conversation_id=row.get("conversation_id"),
            meta=meta or {}, score=score,
        )
