"""FlashcardStore.add as a merge, straight against the store.

The third door. _fcapi_test.py drives the HTTP route and _fctools_test.py the
agent tool; neither reaches add() directly, and this is the layer where the
distinction actually lives.

What it guards: re-adding a pair Jake already has must MERGE. Before this,
add()'s update branch wrote all eight soft columns unconditionally, so every
unsupplied argument became a blank write — a bare re-add of an imported word
silently deleted the inflection table, the spoken forms, both examples and the
note. update(), twenty lines below it in the same file, had always built its
SET list from the supplied keys only; add() just never did.

The mirror check matters as much as the merge one: `""` / `{}` / `[]` passed on
purpose must still clear the field. A fix that made add() ignore every falsy
value would pass "the note survived" and quietly make clearing impossible.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-fc-store-"))
os.environ["CONTINUUM_FLASHCARDS_DB"] = str(TMP / "flashcards.db")
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config              # noqa: E402
from continuum.flashcards import FlashcardStore   # noqa: E402

cfg = Config.load()
assert str(Path(cfg.flashcards_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - flashcards_db is not in the sandbox"

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r} want={want!r}"))
    if not ok:
        FAILED.append(label)


store = FlashcardStore(cfg)

RICH = dict(source="Rivstart", set_name="Kap 7", pos="noun", gender="en",
            tags=["noun", "en"], example_sv="Jag läser en bok.",
            example_en="I am reading a book.",
            forms={"indef_sg": "en bok", "def_sg": "boken", "indef_pl": "böcker"},
            speak=["en bok", "boken", "böcker"], notes="femtio kronor")

full = store.add("bok", "book", **RICH)
check("the enriched card stored its forms", full["forms"]["def_sg"], "boken")
check("  ... and its note", full["notes"], "femtio kronor")

# ---- 1. a bare re-add is a merge, not a wipe -------------------------------
# This is the shape the agent tool produces: it cannot express forms or speak
# at all, so "add bok = book" used to arrive with five fields silently blank.
again = store.add("bok", "book", source="Rivstart", set_name="Kap 7")
check("a bare re-add updates rather than duplicates", again["id"], full["id"])
for field, want in [("forms", RICH["forms"]), ("speak", RICH["speak"]),
                    ("example_sv", RICH["example_sv"]), ("example_en", RICH["example_en"]),
                    ("notes", RICH["notes"]), ("tags", ["en", "noun"]),
                    ("pos", "noun"), ("gender", "en")]:
    check(f"  ... and {field} survived it", again[field], want)

# A merge that changes nothing must not claim the card was touched. Anything
# sorting by `updated` would otherwise show a re-import as fresh activity.
check("a re-add that names no field leaves `updated` alone",
      again["updated"], full["updated"])

# ---- 2. a partial re-add writes only what it names --------------------------
part = store.add("bok", "book", source="Rivstart", set_name="Kap 7",
                 notes="femtiofem kronor")
check("the named field took", part["notes"], "femtiofem kronor")
check("  ... and the unnamed forms are still there", part["forms"], RICH["forms"])
check("  ... and the unnamed examples are still there", part["example_sv"], RICH["example_sv"])
check("a write does move `updated`", part["updated"] > full["updated"], True)

# ---- 3. the mirror: an explicit blank still clears ---------------------------
# Without this, "ignore falsy" passes every check above while making it
# impossible to ever delete a wrong note or a mis-built inflection table.
cleared = store.add("bok", "book", source="Rivstart", set_name="Kap 7",
                    notes="", forms={}, speak=[], example_sv="")
check("an explicit empty note clears it", cleared["notes"], "")
check("an explicit empty forms clears it", cleared["forms"], {})
check("an explicit empty speak clears it", cleared["speak"], [])
check("an explicit empty example clears it", cleared["example_sv"], "")
check("  ... and leaves the one it did not name", cleared["example_en"], RICH["example_en"])

# ---- 4. the insert path still defaults everything ---------------------------
bare = store.add("hund", "dog", source="Rivstart", set_name="Kap 7")
check("a brand new bare card gets blank forms", bare["forms"], {})
check("  ... blank speak", bare["speak"], [])
check("  ... blank tags", bare["tags"], [])
check("  ... blank pos", bare["pos"], "")
check("  ... and blank notes", bare["notes"], "")
check("  ... and is due now", bare["due_now"], True)

# ---- 5. a merge never touches scheduling ------------------------------------
# The reason add() merges instead of refusing: a bulk import that overlaps an
# earlier one must not knock cards Jake is mid-way through back to new.
store.review(full["id"], 5, mode="flip")
studied = store.get(full["id"])
store.add("bok", "book", source="Rivstart", set_name="Kap 7", notes="igen")
after = store.get(full["id"])
check("a merge leaves reps alone", after["srs"]["reps"], studied["srs"]["reps"])
check("  ... and the due date", after["srs"]["due"], studied["srs"]["due"])
check("  ... while still taking the edit", after["notes"], "igen")

# ---- 6. import_backup: restore is exact, re-import preserves -----------------
# Both go through import_backup on purpose, so the two claims have to hold at
# once. A backup names every field, so it writes every field...
store.import_backup({"cards": [
    {"swedish": "katt", "english": "cat", "source": "Rivstart", "set": "Kap 7",
     "pos": "noun", "gender": "en", "tags": ["noun", "en"],
     "example_sv": "Katten sover.", "example_en": "The cat is sleeping.",
     "forms": {"def_sg": "katten"}, "speak": ["en katt"], "notes": "hers"},
]})
katt = [c for c in store.cards(source="Rivstart") if c["swedish"] == "katt"][0]
check("a backup card restored its forms", katt["forms"]["def_sg"], "katten")
check("  ... and its note", katt["notes"], "hers")

# ...while a re-import of the same word from a plain list names none of them,
# and must leave the enrichment where it is.
store.import_backup({"cards": [{"swedish": "katt", "english": "cat"}],
                     "source": "Rivstart", "set": "Kap 7"})
katt2 = store.get(katt["id"])
check("a re-import over an enriched card keeps the forms", katt2["forms"]["def_sg"], "katten")
check("  ... and the note", katt2["notes"], "hers")
check("  ... and the example", katt2["example_sv"], "Katten sover.")

# A restore that genuinely carries a blank still writes the blank.
store.import_backup({"cards": [
    {"swedish": "katt", "english": "cat", "source": "Rivstart", "set": "Kap 7",
     "notes": "", "forms": {}},
]})
katt3 = store.get(katt["id"])
check("a backup carrying an empty note writes it", katt3["notes"], "")
check("  ... and an empty forms", katt3["forms"], {})

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
