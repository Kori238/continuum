"""The flashcard tools against the real store, through the real executor.

Worth its own file rather than folding into _fcapi_test.py: the tab and the
tools reach the same store by different routes, and before this rewrite the
executor called f.cards(deck, limit) positionally against a keyword-only
signature - it would have raised TypeError on Jake's first "add that word",
while every HTTP route stayed green.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-fc-tools-"))
os.environ["CONTINUUM_FLASHCARDS_DB"] = str(TMP / "flashcards.db")
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config              # noqa: E402
from continuum.flashcards import FlashcardStore   # noqa: E402
from continuum.tools import ToolLoop, FLASHCARD_NAMES, FLASHCARD_WRITE  # noqa: E402

cfg = Config.load()
assert str(Path(cfg.flashcards_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - flashcards_db is not in the sandbox"

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r} want={want!r}"))
    if not ok:
        FAILED.append(label)


def truthy(label, got):
    ok = bool(got)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r}"))
    if not ok:
        FAILED.append(label)


class FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


store = FlashcardStore(cfg)
bus = FakeBus()
loop = ToolLoop(cfg, flashcards=store, bus=bus)


def run(name, **args):
    return asyncio.run(loop._flashcards_action(name, args))


# ---- the write set must actually drive the action flag -------------------
# This is the whole reason FLASHCARD_WRITE exists; it sat unread until now, so
# assert the wiring rather than the constant.
check("add is an action", ToolLoop._is_action("add_flashcard"), True)
check("remove is an action", ToolLoop._is_action("remove_flashcard"), True)
check("list is not an action", ToolLoop._is_action("list_flashcards"), False)
check("every write name is a real tool", FLASHCARD_WRITE <= FLASHCARD_NAMES, True)

# ---- empty ---------------------------------------------------------------
truthy("empty deck says so", "no flashcards yet" in run("list_flashcards"))

# ---- add -----------------------------------------------------------------
out = run("add_flashcard", swedish="bok", english="book", pos="noun", gender="en",
          source="Rivstart", set="Kap 1", notes="femtio kronor")
truthy("add confirms the pair", "bok = book" in out)
truthy("add names the bucket", "Rivstart Kap 1" in out)
check("add published an action", len(bus.events), 1)
check("the audited action is named", bus.events[0].payload["action"], "add_flashcard")

card = store.resolve("bok")
check("pos became a tag", "noun" in card["tags"], True)
check("gender became a tag", "en" in card["tags"], True)

# A verb must not pick up a gender tag even if the model supplies one - gender
# is a noun property, and a stray 'en' tag would pollute the study filter.
run("add_flashcard", swedish="prata", english="to talk", pos="verb", gender="en",
    source="Rivstart", set="Kap 1")
check("a verb gets no gender tag", store.resolve("prata")["tags"], ["verb"])

check("a card needs both faces", run("add_flashcard", swedish="x"),
      "need both a Swedish word/phrase and its English meaning")

# ---- re-adding an existing pair merges -------------------------------------
# This door is the one that most needed it. The tool schema has no `forms` or
# `speak` field at all and its description invites a second add of the same
# word, so when the executor spelled its arguments `args.get(k) or ""` a plain
# "add bok = book" deleted the inflection table an import had paid a model call
# to build. Enrich through the store, then re-add through the tool.
store.update(card["id"], forms={"def_sg": "boken"}, example_sv="Jag läser en bok.")
run("add_flashcard", swedish="bok", english="book", source="Rivstart", set="Kap 1")
kept = store.resolve("bok")
check("a bare re-add keeps the forms", kept["forms"], {"def_sg": "boken"})
check("  ... and the example", kept["example_sv"], "Jag läser en bok.")
check("  ... and the note", kept["notes"], "femtio kronor")
# pos and gender need their own checks: the tool derives `tags` from them, so
# blanking pos also empties the derived list, which then reads as "no opinion"
# and leaves the stored tags alone. The tag check below cannot see it.
check("  ... and pos", kept["pos"], "noun")
check("  ... and gender", kept["gender"], "en")
check("  ... and the tags, though the tool recomputes them", sorted(kept["tags"]),
      ["en", "noun"])
check("  ... and did not duplicate the card", kept["id"], card["id"])
# The mirror: a named-but-empty argument is still a deliberate clear.
run("add_flashcard", swedish="bok", english="book", source="Rivstart", set="Kap 1",
    notes="")
check("an explicitly empty note clears", store.resolve("bok")["notes"], "")
# Put it back - the list checks below read it, and a suite that quietly depends
# on a field this block deletes would fail somewhere unrelated to the cause.
run("add_flashcard", swedish="bok", english="book", source="Rivstart", set="Kap 1",
    notes="femtio kronor")

# ---- list ----------------------------------------------------------------
out = run("list_flashcards")
truthy("list counts the cards", "2 card(s)" in out)
truthy("list shows due", "due now" in out)
truthy("list shows the id", card["id"] in out)
truthy("list carries the note", "femtio kronor" in out)

run("add_flashcard", swedish="hund", english="dog", pos="noun", gender="en",
    source="Duolingo", set="Unit 1")
truthy("source filter works", "hund" not in run("list_flashcards", source="Rivstart"))
truthy("set filter works", "bok" not in run("list_flashcards", source="Duolingo"))
truthy("tag filter works", "bok" not in run("list_flashcards", tag="verb"))
truthy("tag filter keeps the match", "prata" in run("list_flashcards", tag="verb"))
truthy("an empty filter result says so",
       "no flashcards yet" in run("list_flashcards", tag="adverb"))

# ---- remove --------------------------------------------------------------
bus.events.clear()
out = run("remove_flashcard", text="hund")
truthy("removed by text", "hund = dog" in out)
check("remove published an action", len(bus.events), 1)
check("card is gone", store.resolve("hund"), None)

# An ambiguous text match must refuse rather than guess. This tool is marked
# SENSITIVE; deleting the first of two matches is the wrong guess to make.
# Note neither card IS "katt" - an exact hit is not ambiguous and should still
# resolve, which is why the first draft of this check tested nothing.
run("add_flashcard", swedish="katten", english="the cat", pos="noun", gender="en")
run("add_flashcard", swedish="kattunge", english="kitten", pos="noun", gender="en")
out = run("remove_flashcard", text="katt")
truthy("ambiguous removal refuses", "which card?" in out)
check("both ambiguous cards survive",
      sorted(c["swedish"] for c in store.cards(tag="noun")),
      ["bok", "katten", "kattunge"])
# ...and an exact hit is still removable, so the refusal is about ambiguity
# rather than the substring path being broken outright.
truthy("an exact hit still resolves", "kattunge = kitten" in run(
    "remove_flashcard", text="kattunge"))

out = run("remove_flashcard", id=card["id"])
truthy("removed by id", "bok = book" in out)
check("removing a missing id refuses", run("remove_flashcard", id="nope123"),
      "which card? give its id (from list_flashcards) or text that matches only one card")
check("removing with no argument refuses", run("remove_flashcard"),
      "which card? give its id (from list_flashcards) or text that matches only one card")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
