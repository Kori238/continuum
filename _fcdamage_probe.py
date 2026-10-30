"""Did the add()-wipe actually fire on the live flashcards db?

Run on the hub, read-only. The bug blanked forms/speak/examples/notes on any
re-add of an existing pair, so the question is not "are there blank cards" -
a plain word-list import legitimately produces those - but "are there cards
that were enriched and then stripped".

Two signatures, because the strong one is narrow:

  1. audio recorded but speak empty. Audio filenames are generated FROM speak,
     so this combination cannot be produced by any import; it can only be a
     card that had spoken forms and lost them. Zero false positives.
  2. a card with no forms sitting in a bucket where others have them. Weak on
     its own - an adjective legitimately has fewer forms than a noun - so it
     is reported as context, not as damage.

What this CANNOT see: a card enriched and stripped before it was ever spoken,
in a bucket that has no enriched siblings. If both counts are zero the honest
claim is "no evidence the wipe fired", not "it definitely did not".
"""
import json
import os
import sqlite3
import sys

db = os.getenv("CONTINUUM_FLASHCARDS_DB") or "data/flashcards.db"
if not os.path.exists(db):
    sys.exit(f"no flashcards db at {db}")
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute("SELECT * FROM cards")]
print(f"db={db}  cards={len(rows)}")
if not rows:
    sys.exit(0)


def j(s, d):
    try:
        return json.loads(s) if s else d
    except Exception:
        return d


# ---- signature 1: audio without speak --------------------------------------
orphan = [r for r in rows if j(r["audio"], {}) and not j(r["speak"], [])]
print(f"\n[1] audio recorded but speak empty (cannot happen legitimately): {len(orphan)}")
for r in orphan[:20]:
    print(f"      {r['swedish']!r} = {r['english']!r}  audio_forms={list(j(r['audio'], {}))}")

# ---- signature 2: bare card in an enriched bucket ---------------------------
buckets: dict[tuple, list] = {}
for r in rows:
    buckets.setdefault((r["source"] or "", r["set_name"] or ""), []).append(r)
weak = []
for key, cards in buckets.items():
    rich = [c for c in cards if j(c["forms"], {})]
    if not rich or len(rich) == len(cards):
        continue
    weak += [(key, c) for c in cards if not j(c["forms"], {})]
print(f"\n[2] no forms, in a bucket where {len(buckets)} bucket(s) have enriched"
      f" siblings: {len(weak)}   (context only - not proof)")
for key, c in weak[:20]:
    print(f"      {key[0]}/{key[1]}  {c['swedish']!r}  pos={c['pos']!r}"
          f" notes={bool(c['notes'])} ex={bool(c['example_sv'])}")

# ---- plain census -----------------------------------------------------------
print("\ncensus:")
for col, empty in (("forms", "{}"), ("speak", "[]"), ("example_sv", ""),
                   ("notes", ""), ("pos", "")):
    n = sum(1 for r in rows if (r[col] or "") in ("", empty))
    print(f"      {col:<11} blank on {n}/{len(rows)}")

# `updated` far from `created` on a card never reviewed is what a silent
# rewrite looks like; a card Jake edited moves too, so this is a pointer.
touched = [r for r in rows if (r["updated"] or 0) - (r["created"] or 0) > 1
           and not (r["reps"] or 0)]
print(f"\nrewritten after creation, never reviewed: {len(touched)}/{len(rows)}")
for r in touched[:20]:
    print(f"      {r['swedish']!r}  +{(r['updated'] - r['created']):.0f}s"
          f"  forms={bool(j(r['forms'], {}))} speak={bool(j(r['speak'], []))}")

print("\nVERDICT:", "DAMAGE FOUND" if orphan else
      "no evidence the wipe fired (see the docstring for what this cannot see)")
