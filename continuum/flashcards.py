"""Flashcards — Jake's Swedish vocab, the SRS, and the audio that goes with it.

This replaces the original Leitner store (one `cards` table, two faces, a box
number). That store never held a card: both `data/flashcards.db` files — local
and hub — were empty, so there is no migration to perform and nothing to lose by
changing the shape. What lands here instead is the data model Glosor grew on the
phone, which is the one that was actually used.

Why the model is richer than "front/back":

  * A Swedish noun is not one string. `bok` has `boken`/`böcker`/`böckerna` and
    two genitives, a verb has five forms, an adjective five. Glosor's import
    already produces the whole table, and the inflection drill is only possible
    because each form is stored separately rather than mashed into a `notes`
    field.
  * `gender` (en/ett) is its own column, not a tag, because it is the single
    thing an English speaker gets wrong most and it drives its own drill.
  * `speak` records which surface forms are worth hearing — "en bok" rather than
    the bare lemma — so audio matches how the word is actually said.

Three deliberate departures from Glosor:

  * **Scheduling is SM-2, not Leitner.** Ported from `glosor/public/srs.js` so a
    phone export keeps its exact place in the ladder. Leitner's fixed rungs
    cannot represent an ease factor, so importing SM-2 state into the old store
    would have silently rounded every card's history away.
  * **Audio is files on disk, never base64 in a row.** On the phone an mp3 pair
    lived inside the card as a data URL: ~120KB per card, 99% of the payload, and
    the reason an 18-card batch was a 2.1MB JSON. Here the bytes go under
    `data/flashcard_audio/` and the row keeps a filename, so a card is ~1KB, the
    db stays small enough to back up casually, and the browser can cache mp3s by
    URL instead of re-parsing a megabyte of base64 on every render.
  * **Reviews are an append-only log**, not just running tallies. The old store
    kept `reviews`/`correct`/`lapses` counters and threw the history away, which
    makes "am I actually improving" unanswerable. The counters are still derived
    for the header; the log is what any real stat needs.

Time: `due` and `last_reviewed` are **float epoch seconds**, matching every other
store in `data/`. Glosor used epoch *milliseconds* (`Date.now()`); the conversion
happens once, at the import boundary in `import_backup`, and nowhere else. Day
boundaries are never computed here — "due" is an instant comparison, not a date
comparison, which is what lets a lapsed card come back in ten minutes rather than
tomorrow.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import Config

log = logging.getLogger("continuum.flashcards")

# SM-2, ported verbatim from glosor/public/srs.js so exported phone state keeps
# its place. quality: 0=Again, 3=Hard, 4=Good, 5=Easy.
_EASE_FLOOR = 1.3
_EASE_START = 2.5
_RELEARN_S = 10 * 60      # a lapse comes back inside the session, not tomorrow
_DAY_S = 86400.0

# Fallback only -- the live list comes from cfg.tts_voices (see __init__).
_VOICES = ("andreas", "anders")   # Gothenburg-natural, and standard/clear

# A card counts as "learned" once SM-2 has pushed it past three weeks. Chosen to
# match the interval the ladder actually reaches (1 -> 6 -> ~15 -> ~37), not a
# round number: box-style thresholds are what the Leitner store got wrong.
_LEARNED_INTERVAL_D = 21.0


def _now() -> float:
    return time.time()


def new_id() -> str:
    """Glosor's id shape (`c` + base36 time + 6 random chars), kept so ids minted
    here and ids arriving from a phone export can never collide or be told apart."""
    stamp = _base36(int(_now() * 1000))
    tail = "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(6))
    return f"c{stamp}{tail}"


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n <= 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def fresh_srs() -> dict[str, Any]:
    """A never-reviewed card: due immediately, so new cards surface at once."""
    return {"due": _now(), "interval": 0.0, "ease": _EASE_START,
            "reps": 0, "lapses": 0, "last": 0.0}


def grade(srs: Optional[dict[str, Any]], quality: int) -> dict[str, Any]:
    """Advance SM-2 by one answer. A direct port of `srs.js:grade` — including
    the Hard (x0.7) and Easy (x1.3) multipliers, which are Glosor's own tuning on
    top of textbook SM-2 and must not be "corrected" away."""
    s = dict(srs or fresh_srs())
    now = _now()
    q = int(quality)
    if q < 3:
        s["reps"] = 0
        s["lapses"] = int(s.get("lapses", 0)) + 1
        s["interval"] = 0.0
        s["due"] = now + _RELEARN_S
    else:
        s["reps"] = int(s.get("reps", 0)) + 1
        if s["reps"] == 1:
            s["interval"] = 1.0
        elif s["reps"] == 2:
            s["interval"] = 6.0
        else:
            s["interval"] = round(float(s.get("interval", 1.0)) * float(s.get("ease", _EASE_START)))
        if q == 3:
            s["interval"] = max(1.0, round(s["interval"] * 0.7))
        if q == 5:
            s["interval"] = round(s["interval"] * 1.3)
        s["ease"] = max(_EASE_FLOOR,
                        float(s.get("ease", _EASE_START)) + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
        s["due"] = now + s["interval"] * _DAY_S
    s["last"] = now
    return s


def _jloads(text: Any, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


class FlashcardStore:
    """One sqlite file plus a directory of mp3s. Every read returns plain dicts
    so the routes can hand them straight to the browser."""

    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.flashcards_db)
        # Which voices are legal comes from config, never from a constant here:
        # a module tuple would silently reject any voice Jake configured, and the
        # rejection would look like a TTS failure rather than a config mismatch.
        self._voices = tuple(cfg.tts_voices or {}) or _VOICES
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Audio sits beside the db rather than under a configured path of its
        # own: the two are one dataset, and a backup that copies the db without
        # the mp3s produces cards that render but cannot be heard.
        self._audio_dir = self._path.parent / "flashcard_audio"
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL so a long study session's writes never block the dashboard's reads.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    # ---- schema -------------------------------------------------------------
    def _migrate(self) -> None:
        """Create the current schema, and drop the old Leitner `cards` table if a
        legacy empty one is present.

        Dropping is safe *only* because it is verified empty first. Both known
        databases held zero rows; if some third copy ever holds data, this leaves
        it alone and raises rather than silently destroying it."""
        legacy = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cards'").fetchone()
        if legacy:
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(cards)")}
            if "swedish" in cols and "box" in cols:      # the old Leitner shape
                n = self._db.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
                if n:
                    raise RuntimeError(
                        f"refusing to replace the legacy flashcard table: it holds {n} rows. "
                        "Export them first — see docs/DATA.md.")
                self._db.execute("DROP TABLE cards")
                log.info("dropped the empty legacy Leitner cards table")

        self._db.execute(
            """CREATE TABLE IF NOT EXISTS cards(
                id TEXT PRIMARY KEY,
                source TEXT DEFAULT '',        -- e.g. 'Rivstart A1'
                set_name TEXT DEFAULT '',      -- e.g. 'Kapitel 1'
                swedish TEXT NOT NULL,
                english TEXT NOT NULL,
                pos TEXT DEFAULT '',           -- noun | verb | adjective | ...
                gender TEXT DEFAULT '',        -- en | ett | '' (nouns only)
                tags TEXT DEFAULT '[]',        -- json array
                example_sv TEXT DEFAULT '',
                example_en TEXT DEFAULT '',
                forms TEXT DEFAULT '{}',       -- json object, shape depends on pos
                speak TEXT DEFAULT '[]',       -- json array of surface strings
                audio TEXT DEFAULT '{}',       -- json {form: {voice: filename}}
                notes TEXT DEFAULT '',
                due REAL,                      -- epoch SECONDS
                interval REAL DEFAULT 0,       -- days
                ease REAL DEFAULT 2.5,
                reps INTEGER DEFAULT 0,
                lapses INTEGER DEFAULT 0,
                last_reviewed REAL DEFAULT 0,
                created REAL,
                updated REAL)""")
        # due drives the only hot query (what do I study now); the bucket index
        # drives the set picker. Both are read on every render of the tab.
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_fc_due ON cards(due)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_fc_bucket ON cards(source, set_name)")
        # Same pair twice in one set is a duplicate; the same word in two sets is
        # not (Rivstart and Duolingo legitimately overlap, and they schedule
        # independently). Hence the index spans the bucket, unlike the old store.
        self._db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS ix_fc_pair ON cards(
                   source COLLATE NOCASE, set_name COLLATE NOCASE,
                   swedish COLLATE NOCASE, english COLLATE NOCASE)""")
        # Append-only: one row per answer, never updated. This is the history the
        # old store discarded.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS reviews(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                ts REAL NOT NULL,
                quality INTEGER NOT NULL,      -- 0 | 3 | 4 | 5
                mode TEXT DEFAULT '',          -- flip | type | choice | listen | ...
                interval_after REAL,
                ease_after REAL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_fc_rev_ts ON reviews(ts)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_fc_rev_card ON reviews(card_id)")
        self._db.commit()

    # ---- row mapping --------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row, *, now: Optional[float] = None) -> dict[str, Any]:
        now = _now() if now is None else now
        due = r["due"] or 0.0
        return {
            "id": r["id"],
            "source": r["source"] or "", "set": r["set_name"] or "",
            "bucket": f'{r["source"] or ""} {r["set_name"] or ""}'.strip(),
            "swedish": r["swedish"], "english": r["english"],
            "pos": r["pos"] or "", "gender": r["gender"] or "",
            "tags": _jloads(r["tags"], []),
            "example_sv": r["example_sv"] or "", "example_en": r["example_en"] or "",
            "forms": _jloads(r["forms"], {}) or {},
            "speak": _jloads(r["speak"], []),
            "audio": _jloads(r["audio"], {}),
            "notes": r["notes"] or "",
            "srs": {"due": due, "interval": r["interval"] or 0.0,
                    "ease": r["ease"] or _EASE_START, "reps": r["reps"] or 0,
                    "lapses": r["lapses"] or 0, "last": r["last_reviewed"] or 0.0},
            "due_now": due <= now,
            "is_new": not (r["reps"] or 0) and not (r["last_reviewed"] or 0),
            "created": r["created"], "updated": r["updated"],
        }

    # ---- write --------------------------------------------------------------
    def add(self, swedish: str, english: str, *, source: str = "", set_name: str = "",
            pos: Optional[str] = None, gender: Optional[str] = None,
            tags: Optional[Iterable[str]] = None,
            example_sv: Optional[str] = None, example_en: Optional[str] = None,
            forms: Optional[dict] = None, speak: Optional[Iterable[str]] = None,
            notes: Optional[str] = None, card_id: str = "") -> dict[str, Any]:
        """Add one card, or update the soft fields of an identical existing pair.

        **On the update path every soft field is a merge field**: None means
        "leave the stored value alone". Pass an explicit `""` / `{}` / `[]` to
        clear one on purpose.

        That distinction is the entire point of this signature. The agent's
        `add_flashcard` tool cannot express `forms` or `speak` at all, and its
        own description invites the model to re-add an identical pair — so
        treating "unsupplied" as "write blank" made every re-add a silent wipe
        of the inflection table, the spoken forms, the examples and the note,
        i.e. everything the importer had paid a model call to build. `update()`
        below has always done this correctly; `add` did not.

        Returns the stored card; `{}` if either face is blank. Re-adding never
        resets scheduling — a bulk import that overlaps an earlier one must not
        knock cards Jake has been learning back to new."""
        swedish = (swedish or "").strip()
        english = (english or "").strip()
        if not swedish or not english:
            return {}
        source = (source or "").strip()
        set_name = (set_name or "").strip()
        now = _now()
        existing = self._db.execute(
            """SELECT id FROM cards WHERE source=? COLLATE NOCASE AND set_name=? COLLATE NOCASE
                 AND swedish=? COLLATE NOCASE AND english=? COLLATE NOCASE""",
            (source, set_name, swedish, english)).fetchone()
        # column -> stored value, for the soft fields the caller actually named.
        # Membership is the whole signal: absent means "no opinion", present
        # means "write this", including when the value is empty on purpose.
        supplied: dict[str, Any] = {}
        if pos is not None:
            supplied["pos"] = str(pos).strip().lower()
        if gender is not None:
            supplied["gender"] = str(gender).strip().lower()
        if tags is not None:
            supplied["tags"] = json.dumps(
                sorted({str(t).strip().lower() for t in tags if str(t).strip()}))
        if example_sv is not None:
            supplied["example_sv"] = str(example_sv).strip()
        if example_en is not None:
            supplied["example_en"] = str(example_en).strip()
        if forms is not None:
            supplied["forms"] = json.dumps(forms, ensure_ascii=False)
        if speak is not None:
            supplied["speak"] = json.dumps(list(speak), ensure_ascii=False)
        if notes is not None:
            supplied["notes"] = str(notes).strip()
        if existing:
            if not supplied:
                # Nothing named, nothing to say. Not even `updated` moves — a
                # re-add that changes no field did not modify the card, and
                # bumping the timestamp would lie to anything sorting by it.
                return self.get(existing["id"])
            self._db.execute(
                f"UPDATE cards SET {', '.join(f'{c}=?' for c in supplied)}, updated=? WHERE id=?",
                (*supplied.values(), now, existing["id"]))
            self._db.commit()
            return self.get(existing["id"])
        cid = (card_id or "").strip() or new_id()
        # Scheduling comes from fresh_srs(), never from literals inlined here:
        # two definitions of "what a new card looks like" drift the moment one of
        # them is tuned, and the SQL copy is the one no test would be reading.
        s0 = fresh_srs()
        # On the insert path there is nothing to merge with, so an unnamed field
        # takes the column's blank. BLANK is spelled out rather than derived from
        # the schema: these are the values `get()` round-trips back to "" / {} / [].
        BLANK = {"pos": "", "gender": "", "tags": "[]", "example_sv": "",
                 "example_en": "", "forms": "{}", "speak": "[]", "notes": ""}
        self._db.execute(
            """INSERT INTO cards(id, source, set_name, swedish, english, pos, gender, tags,
                    example_sv, example_en, forms, speak, audio, notes,
                    due, interval, ease, reps, lapses, last_reviewed, created, updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'{}',?,?,?,?,?,?,?,?,?)""",
            (cid, source, set_name, swedish, english)
            + tuple(supplied.get(c, BLANK[c]) for c in
                    ("pos", "gender", "tags", "example_sv", "example_en",
                     "forms", "speak", "notes"))
            + (s0["due"], s0["interval"], s0["ease"], s0["reps"], s0["lapses"],
               s0["last"], now, now))
        self._db.commit()
        return self.get(cid)

    def update(self, card_id: str, **fields: Any) -> dict[str, Any]:
        """Partial edit of a card's content. Never touches scheduling."""
        cur = self.get(card_id)
        if not cur:
            return {}
        cols = {"swedish": "swedish", "english": "english", "source": "source",
                "set": "set_name", "pos": "pos", "gender": "gender",
                "example_sv": "example_sv", "example_en": "example_en", "notes": "notes"}
        sets, vals = [], []
        for key, col in cols.items():
            if key in fields and fields[key] is not None:
                sets.append(f"{col}=?")
                vals.append(str(fields[key]).strip())
        for key, col in (("tags", "tags"), ("forms", "forms"), ("speak", "speak")):
            if key in fields and fields[key] is not None:
                sets.append(f"{col}=?")
                vals.append(json.dumps(fields[key], ensure_ascii=False))
        if not sets:
            return cur
        sets.append("updated=?")
        vals.append(_now())
        self._db.execute(f"UPDATE cards SET {', '.join(sets)} WHERE id=?", (*vals, card_id))
        self._db.commit()
        return self.get(card_id)

    def delete(self, card_id: str) -> bool:
        """Remove a card, its review history, and its audio files together.

        Deleting the row but leaving the mp3s would orphan the bytes with no
        index pointing at them — unreachable, unlistable, and still on disk."""
        card = self.get(card_id)
        if not card:
            return False
        for per_form in (card.get("audio") or {}).values():
            for name in (per_form or {}).values():
                self._audio_path(name).unlink(missing_ok=True)
        folder = self._audio_dir / str(card_id)
        if folder.is_dir():
            try:
                folder.rmdir()
            except OSError:
                log.debug("audio folder for %s not empty, left in place", card_id)
        self._db.execute("DELETE FROM reviews WHERE card_id=?", (card_id,))
        cur = self._db.execute("DELETE FROM cards WHERE id=?", (card_id,))
        self._db.commit()
        return cur.rowcount > 0

    def review(self, card_id: str, quality: int, *, mode: str = "") -> dict[str, Any]:
        """Record one answer: append to the log, advance SM-2, store the result."""
        card = self.get(card_id)
        if not card:
            return {}
        q = max(0, min(int(quality), 5))
        s = grade(card["srs"], q)
        now = _now()
        self._db.execute(
            """UPDATE cards SET due=?, interval=?, ease=?, reps=?, lapses=?,
                    last_reviewed=?, updated=? WHERE id=?""",
            (s["due"], s["interval"], s["ease"], s["reps"], s["lapses"],
             s["last"], now, card_id))
        self._db.execute(
            """INSERT INTO reviews(card_id, ts, quality, mode, interval_after, ease_after)
               VALUES(?,?,?,?,?,?)""",
            (card_id, now, q, (mode or "").strip(), s["interval"], s["ease"]))
        self._db.commit()
        return self.get(card_id)

    # ---- audio --------------------------------------------------------------
    def _audio_path(self, name: str) -> Path:
        """Resolve a stored filename under the audio dir, refusing anything that
        escapes it. The names are ours, but they round-trip through JSON in a db
        row, so this is validated rather than trusted."""
        p = (self._audio_dir / name).resolve()
        if not str(p).startswith(str(self._audio_dir.resolve())):
            raise ValueError(f"audio path escapes the store: {name!r}")
        return p

    def put_audio(self, card_id: str, form: str, voice: str, data: bytes) -> str:
        """Write one mp3 and point the card at it. Returns the stored filename."""
        if voice not in self._voices:
            raise ValueError(f"unknown voice {voice!r}; expected one of {self._voices}")
        card = self.get(card_id)
        if not card:
            raise KeyError(card_id)
        slug = re.sub(r"[^a-z0-9]+", "-", (form or "").lower()).strip("-") or "form"
        name = f"{card_id}/{slug}-{voice}.mp3"
        path = self._audio_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        audio = card["audio"]
        audio.setdefault(form, {})[voice] = name
        self._db.execute("UPDATE cards SET audio=?, updated=? WHERE id=?",
                         (json.dumps(audio, ensure_ascii=False), _now(), card_id))
        self._db.commit()
        return name

    def audio_bytes(self, name: str) -> Optional[bytes]:
        p = self._audio_path(name)
        return p.read_bytes() if p.is_file() else None

    def missing_audio(self) -> list[dict[str, Any]]:
        """Every (card, form) that should have audio and doesn't — what a
        generate-the-missing-audio pass works from. Reads `speak`, because that
        is the list of forms worth hearing, not every form that exists."""
        out = []
        for c in self.cards(limit=100000):
            have = c["audio"] or {}
            for form in (c["speak"] or [c["swedish"]]):
                missing = [v for v in self._voices if v not in (have.get(form) or {})]
                if missing:
                    out.append({"id": c["id"], "form": form, "voices": missing})
        return out

    # ---- read ---------------------------------------------------------------
    def get(self, card_id: str) -> dict[str, Any]:
        r = self._db.execute("SELECT * FROM cards WHERE id=?", (str(card_id),)).fetchone()
        return self._row(r) if r else {}

    def cards(self, *, source: str = "", set_name: str = "", tag: str = "",
              limit: int = 500) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if source:
            where.append("source=? COLLATE NOCASE")
            params.append(source)
        if set_name:
            where.append("set_name=? COLLATE NOCASE")
            params.append(set_name)
        rows = self._db.execute(
            f"SELECT * FROM cards WHERE {' AND '.join(where)} ORDER BY created DESC LIMIT ?",
            (*params, int(limit))).fetchall()
        now = _now()
        out = [self._row(r, now=now) for r in rows]
        # Tag filtering is done here, not in SQL: tags are a JSON array, and a
        # LIKE '%verb%' would also match 'adverb'. Substring matching on a packed
        # list is exactly the bug class this avoids.
        if tag:
            t = tag.strip().lower()
            out = [c for c in out if t in c["tags"]]
        return out

    def buckets(self) -> list[dict[str, Any]]:
        """Every source/set with totals — the set picker. Due is counted against
        one `now` so a slow query can't report totals from two different instants."""
        now = _now()
        rows = self._db.execute(
            """SELECT source, set_name, COUNT(*) AS total,
                      SUM(CASE WHEN due <= ? THEN 1 ELSE 0 END) AS due,
                      SUM(CASE WHEN reps = 0 AND last_reviewed = 0 THEN 1 ELSE 0 END) AS new
               FROM cards GROUP BY source COLLATE NOCASE, set_name COLLATE NOCASE
               ORDER BY source COLLATE NOCASE, set_name COLLATE NOCASE""", (now,)).fetchall()
        return [{"source": r["source"] or "", "set": r["set_name"] or "",
                 "total": r["total"], "due": r["due"] or 0, "new": r["new"] or 0}
                for r in rows]

    def tags(self) -> list[str]:
        seen: set[str] = set()
        for r in self._db.execute("SELECT tags FROM cards"):
            seen.update(_jloads(r["tags"], []))
        return sorted(seen)

    def study(self, *, source: str = "", set_name: str = "", tag: str = "",
              limit: int = 20, due_only: bool = False) -> list[dict[str, Any]]:
        """The study queue: everything due (soonest-due first), topped up with
        new cards, then with not-yet-due ones unless `due_only`.

        New cards come before not-yet-due ones deliberately — a card never seen
        is worth more than an early re-run of one already scheduled."""
        limit = max(1, min(int(limit or 20), 200))
        pool = self.cards(source=source, set_name=set_name, tag=tag, limit=100000)
        due = sorted([c for c in pool if c["due_now"]], key=lambda c: c["srs"]["due"])
        if due_only or len(due) >= limit:
            return due[:limit]
        picked = {c["id"] for c in due}
        fresh = [c for c in pool if c["is_new"] and c["id"] not in picked]
        out = due + fresh[: limit - len(due)]
        if len(out) < limit:
            picked |= {c["id"] for c in out}
            ahead = sorted([c for c in pool if c["id"] not in picked],
                           key=lambda c: c["srs"]["due"])
            out += ahead[: limit - len(out)]
        return out[:limit]

    def stats(self, *, source: str = "", set_name: str = "") -> dict[str, Any]:
        pool = self.cards(source=source, set_name=set_name, limit=100000)
        now = _now()
        day_ago = now - _DAY_S
        reviewed = self._db.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE ts >= ?", (day_ago,)).fetchone()["n"]
        return {
            "total": len(pool),
            "due": sum(1 for c in pool if c["due_now"]),
            "new": sum(1 for c in pool if c["is_new"]),
            "learned": sum(1 for c in pool if c["srs"]["interval"] >= _LEARNED_INTERVAL_D),
            "reviewed_24h": reviewed,
            "with_audio": sum(1 for c in pool if c["audio"]),
        }

    def resolve(self, text: str) -> Optional[dict[str, Any]]:
        """Find one card by either face — exact first, then a *unique* substring
        match. Ambiguous returns None rather than guessing, the same rule the
        routine and meal stores use."""
        text = (text or "").strip()
        if not text:
            return None
        r = self._db.execute(
            "SELECT * FROM cards WHERE swedish=? COLLATE NOCASE OR english=? COLLATE NOCASE",
            (text, text)).fetchone()
        if r:
            return self._row(r)
        like = f"%{text}%"
        rows = self._db.execute(
            "SELECT * FROM cards WHERE swedish LIKE ? COLLATE NOCASE OR english LIKE ? COLLATE NOCASE",
            (like, like)).fetchall()
        return self._row(rows[0]) if len(rows) == 1 else None

    def _pair_exists(self, source: str, set_name: str, swedish: str, english: str) -> bool:
        """Does the dedupe key already have a row? Mirrors ix_fc_pair exactly."""
        return self._db.execute(
            """SELECT 1 FROM cards WHERE source=? COLLATE NOCASE AND set_name=? COLLATE NOCASE
                 AND swedish=? COLLATE NOCASE AND english=? COLLATE NOCASE LIMIT 1""",
            (source or "", set_name or "", swedish, english)).fetchone() is not None

    # ---- import -------------------------------------------------------------
    def import_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Load a batch of cards: a `glosor/public/export.html` phone backup, or
        a freshly built set from the import screen. One mapping for both, so a
        restore and an import cannot drift into disagreeing about a field.

        This is the one place milliseconds become seconds. Glosor's `srs.due`
        and `srs.last` are `Date.now()` values; storing them unconverted would
        put every card's due date roughly 50,000 years in the future, and the
        queue would simply render empty forever rather than error.

        Audio arrives as `data:audio/mpeg;base64,...` strings and is written out
        to files here — the single largest reason a backup is megabytes and the
        resulting db is not.
        """
        import base64

        # A backup names source/set on every card; a fresh import names them once,
        # at the top. A per-card value always wins — the payload-level one is a
        # default for cards that don't name a bucket, never an override of one
        # that does.
        dflt_source = str(payload.get("source") or "")
        dflt_set = str(payload.get("set") or "")
        cards = payload.get("cards") or []
        added = updated = skipped = audio_files = 0
        for entry in cards:
            c = entry.get("value") if isinstance(entry, dict) and "value" in entry else entry
            if not isinstance(c, dict):
                skipped += 1
                continue
            sv, en = (c.get("swedish") or "").strip(), (c.get("english") or "").strip()
            if not sv or not en:
                skipped += 1
                continue
            src = c.get("source") or dflt_source
            st = c.get("set") or dflt_set
            # "Existed" has to ask the same question the unique index asks. An id
            # lookup alone is right for a backup, where every card has one, and
            # wrong for an import, where none do — it would report every re-import
            # as entirely new while add() quietly deduplicated it.
            existed = bool(self.get(str(c.get("id") or ""))) or self._pair_exists(src, st, sv, en)
            card = self.add(
                sv, en,
                source=src, set_name=st,
                # Straight `.get`, no `or ""` / `or {}`: a key this card doesn't
                # carry has to reach add() as None. A backup names every field, so
                # a restore still writes all of them exactly — while a re-import of
                # a plain word list over an enriched card leaves the enrichment.
                pos=c.get("pos"), gender=c.get("gender"),
                tags=c.get("tags"), example_sv=c.get("example_sv"),
                example_en=c.get("example_en"), forms=c.get("forms"),
                speak=c.get("speak"), notes=c.get("notes"),
                card_id=str(c.get("id") or ""),
            )
            if not card:
                skipped += 1
                continue
            added, updated = (added, updated + 1) if existed else (added + 1, updated)

            srs = c.get("srs") or {}
            if srs.get("last") or srs.get("reps"):
                self._db.execute(
                    """UPDATE cards SET due=?, interval=?, ease=?, reps=?, lapses=?,
                            last_reviewed=? WHERE id=?""",
                    (float(srs.get("due", 0)) / 1000.0,      # ms -> s, the one conversion
                     float(srs.get("interval", 0) or 0),
                     float(srs.get("ease", _EASE_START) or _EASE_START),
                     int(srs.get("reps", 0) or 0), int(srs.get("lapses", 0) or 0),
                     float(srs.get("last", 0) or 0) / 1000.0,
                     card["id"]))

            for form, per_voice in (c.get("audio") or {}).items():
                for voice, data_url in (per_voice or {}).items():
                    if voice not in self._voices or not isinstance(data_url, str):
                        continue
                    _, _, b64 = data_url.partition("base64,")
                    if not b64:
                        continue
                    try:
                        self.put_audio(card["id"], form, voice, base64.b64decode(b64))
                        audio_files += 1
                    except Exception:
                        log.warning("could not store audio for %s/%s", card["id"], form,
                                    exc_info=True)
        self._db.commit()
        return {"added": added, "updated": updated, "skipped": skipped,
                "audio_files": audio_files, "total": self.stats()["total"]}
