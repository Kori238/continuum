"""Flashcard routes end-to-end through the real create_app().

Sandboxed the same way as _budget_api_test.py: every store redirected into a
temp dir, and the run aborts before create_app() if any Config path still
resolves inside live data.

Both API keys are forced EMPTY here, for two reasons. The obvious one is that a
test must never spend Jake's ElevenLabs quota. The load-bearing one is that
keyless degradation is itself the claim being tested: import and audio must fail
with a readable 502 while the rest of the tab keeps working. Note the env vars
are set rather than unset - config's _load_dotenv() uses setdefault, so an unset
var would be filled in from the real .env and this file would quietly start
making paid calls.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-fc-api-"))
for var, leaf in [
    ("CONTINUUM_MEMORY_DB", "memory.db"), ("CONTINUUM_CONVERSATIONS_DB", "conversations.db"),
    ("CONTINUUM_SCHEDULER_DB", "scheduler.db"), ("CONTINUUM_MESSAGES_DB", "messages.db"),
    ("CONTINUUM_ROUTINES_DB", "routines.db"), ("CONTINUUM_WORKOUTS_DB", "workouts.db"),
    ("CONTINUUM_ACTIVITY_DB", "activity.db"), ("CONTINUUM_NUTRITION_DB", "nutrition.db"),
    ("CONTINUUM_BODY_DB", "body.db"), ("CONTINUUM_HEALTH_DB", "health.db"),
    ("CONTINUUM_LEDGER_DB", "ledger.db"), ("CONTINUUM_FLASHCARDS_DB", "flashcards.db"),
    ("CONTINUUM_BUDGET_DB", "budget.db"), ("CONTINUUM_UPLOADS_DIR", "uploads"),
]:
    os.environ[var] = str(TMP / leaf)
os.environ["CONTINUUM_OLLAMA_AUTOSTART"] = "0"
os.environ["CONTINUUM_PROACTIVE"] = "0"
os.environ["CONTINUUM_OPENROUTER_KEY"] = ""
os.environ["CONTINUUM_ELEVENLABS_KEY"] = ""

sys.path.insert(0, str(Path(__file__).parent))
from continuum.config import Config              # noqa: E402

cfg = Config.load()
LIVE = str((Path(__file__).parent / "data").resolve()).lower()
bad = [f"{n}={getattr(cfg, n)}" for n in vars(cfg)
       if isinstance(getattr(cfg, n), str) and ("_db" in n or "_dir" in n)
       and getattr(cfg, n)
       and str(Path(getattr(cfg, n)).resolve()).lower().startswith(LIVE)]
if bad:
    sys.exit("REFUSING TO RUN - these still point at live data:\n  " + "\n  ".join(bad))
if cfg.openrouter_key or cfg.elevenlabs_key:
    sys.exit("REFUSING TO RUN - a real API key leaked into the sandbox config")
print(f"sandbox: {TMP}")

from fastapi.testclient import TestClient         # noqa: E402
from continuum.app.server import create_app       # noqa: E402
from continuum.flashcards import FlashcardStore   # noqa: E402

# A second handle on the same sandbox db. Writing audio bytes has no keyless
# HTTP route - synthesis needs a key - so the one way to test that the audio
# route serves what put_audio() stored is to call put_audio() directly.
store = FlashcardStore(cfg)

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


app = create_app()
with TestClient(app, client=("127.0.0.1", 51000)) as client:
    # ---- empty install ----
    r = client.get("/api/flashcards")
    check("GET /api/flashcards is 200", r.status_code, 200)
    b = r.json()
    check("fresh install has no cards", b["cards"], [])
    check("fresh install has no buckets", b["buckets"], [])
    check("stats present", b["stats"]["total"], 0)

    r = client.get("/api/flashcards/status")
    st = r.json()
    check("import reported off without a key", st["import"], False)
    check("audio reported off without a key", st["audio"], False)
    truthy("voices still advertised", st["voices"])
    check("status carries no key material",
          [k for k in st if "key" in k.lower()], [])

    # ---- add / read ----
    r = client.post("/api/flashcards", json={
        "swedish": "bok", "english": "book", "pos": "noun", "gender": "en",
        "tags": ["noun", "en"], "set": "Kap 1", "source": "Rivstart",
        "forms": {"indef_sg": "bok", "def_sg": "boken"}, "speak": ["en bok", "boken"],
        # Every soft field gets a non-blank value here, and the merge block below
        # asserts each one survives. A field left empty at this point cannot tell
        # a merge from a wipe - the control found exactly that hole in example_sv.
        "example_sv": "Jag läser en bok.", "example_en": "I am reading a book.",
        "notes": "femtio kronor"})
    check("POST /api/flashcards is 200", r.status_code, 200)
    card = r.json()
    cid = card["id"]
    truthy("card got a string id", isinstance(cid, str) and len(cid) > 5)
    check("new card is due immediately", card["due_now"], True)
    check("new card is new", card["is_new"], True)
    check("forms round-tripped", card["forms"]["def_sg"], "boken")
    check("speak round-tripped", card["speak"], ["en bok", "boken"])

    # Re-posting the same pair with only the faces is a merge, not a wipe. The
    # route used to spell its soft fields `str(body.get(k) or "")`, which turns
    # "the body never mentioned pos" into "write pos blank" - so a form posting
    # two fields deleted the six the importer had filled in.
    merged = client.post("/api/flashcards", json={
        "swedish": "bok", "english": "book", "set": "Kap 1",
        "source": "Rivstart"}).json()
    check("a bare re-post updates the same card", merged["id"], cid)
    check("  ... and keeps forms", merged["forms"]["def_sg"], "boken")
    check("  ... and keeps speak", merged["speak"], ["en bok", "boken"])
    check("  ... and keeps pos", merged["pos"], "noun")
    check("  ... and keeps gender", merged["gender"], "en")
    check("  ... and keeps tags", sorted(merged["tags"]), ["en", "noun"])
    check("  ... and keeps example_sv", merged["example_sv"], "Jag läser en bok.")
    check("  ... and keeps example_en", merged["example_en"], "I am reading a book.")
    check("  ... and keeps notes", merged["notes"], "femtio kronor")
    # The mirror. Without it, "ignore everything falsy" passes the three above
    # while making a wrong gender impossible to delete through the tab.
    cleared = client.post("/api/flashcards", json={
        "swedish": "bok", "english": "book", "set": "Kap 1",
        "source": "Rivstart", "gender": ""}).json()
    check("an explicitly empty field still clears", cleared["gender"], "")
    check("  ... without taking the others with it", cleared["pos"], "noun")

    check("both faces required", client.post(
        "/api/flashcards", json={"swedish": "x"}).status_code, 400)

    r = client.post("/api/flashcards", json={
        "swedish": "hus", "english": "house", "pos": "noun", "gender": "ett",
        "tags": ["noun", "ett"], "set": "Kap 1", "source": "Rivstart"})
    check("second card added", r.status_code, 200)

    b = client.get("/api/flashcards").json()
    check("two cards listed", len(b["cards"]), 2)
    check("one bucket", [(x["source"], x["set"]) for x in b["buckets"]],
          [("Rivstart", "Kap 1")])
    check("tags collected", sorted(b["tags"]), ["en", "ett", "noun"])

    # Tag filtering must not substring-match: 'en' is a tag on bok, and 'ett'
    # contains no 'en', but this is the direction the bug runs in a LIKE query.
    check("tag filter is exact", len(client.get(
        "/api/flashcards?tag=en").json()["cards"]), 1)
    check("bucket filter applies", len(client.get(
        "/api/flashcards?set=Kap%209").json()["cards"]), 0)

    # ---- study / review ----
    r = client.get("/api/flashcards/study?limit=10")
    check("study queue is 200", r.status_code, 200)
    check("both new cards queued", len(r.json()["cards"]), 2)

    r = client.post(f"/api/flashcards/{cid}/review", json={"quality": 5, "mode": "sv-en"})
    check("review is 200", r.status_code, 200)
    after = r.json()
    check("a Good review schedules it forward", after["due_now"], False)
    truthy("interval advanced", after["srs"]["interval"] >= 1)
    check("reps counted", after["srs"]["reps"], 1)

    check("reviewed card leaves the due queue", len(client.get(
        "/api/flashcards/study?limit=10&due_only=true").json()["cards"]), 1)
    check("review logged in stats",
          client.get("/api/flashcards").json()["stats"]["reviewed_24h"], 1)

    # quality is 0-5, not a boolean: a blank body must not read as a pass.
    # SM-2 lapses reset reps and put the card back in ~10 minutes, so due_now is
    # False right afterwards -- the tell is the interval and the lapse count,
    # not the due flag.
    r = client.post(f"/api/flashcards/{cid}/review", json={})
    lapsed = r.json()["srs"]
    check("empty review body is a lapse, not a pass", lapsed["lapses"], 1)
    check("a lapse drops the interval to zero", lapsed["interval"], 0.0)
    check("a lapse restarts the rep count", lapsed["reps"], 0)

    # Hard / Good / Easy are three different answers, not one bit. Grade three
    # identical fresh cards differently and the intervals must come out apart --
    # a suite that only ever sends 0 and 5 cannot tell SM-2 from a checkbox.
    spread = {}
    for q in (3, 4, 5):
        rr = client.post("/api/flashcards", json={
            "swedish": f"prov{q}", "english": f"test{q}", "source": "Q", "set": "grades"})
        client.post(f"/api/flashcards/{rr.json()['id']}/review", json={"quality": q})
        client.post(f"/api/flashcards/{rr.json()['id']}/review", json={"quality": q})
        spread[q] = client.get(f"/api/flashcards?source=Q").json()
        spread[q] = [c for c in spread[q]["cards"] if c["swedish"] == f"prov{q}"][0]["srs"]["interval"]
    truthy("Hard schedules sooner than Good", spread[3] < spread[4])
    truthy("Easy schedules later than Good", spread[5] > spread[4])
    check("three grades, three intervals", len(set(spread.values())), 3)

    # ---- edit / delete ----
    r = client.patch(f"/api/flashcards/{cid}", json={"notes": "från biblioteket"})
    check("PATCH keeps unicode", r.json()["notes"], "från biblioteket")
    check("PATCH leaves the lapse count alone", r.json()["srs"]["lapses"], 1)

    check("DELETE is 200", client.delete(f"/api/flashcards/{cid}").json()["ok"], True)
    check("deleting twice is honest", client.delete(
        f"/api/flashcards/{cid}").json()["ok"], False)
    check("one card left in that bucket", len(client.get(
        "/api/flashcards?source=Rivstart").json()["cards"]), 1)

    # ---- audio route ----
    # The positive case first, and with a name put_audio actually produced:
    # stored names are "<card id>/<form>-<voice>.mp3", so a flat synthetic name
    # exercises a URL shape that never occurs. Asserting only the guards let a
    # plain {name} converter ship, under which EVERY real audio 404s -- the tab
    # would render silent play buttons and the suite would stay green.
    _ac = client.post("/api/flashcards", json={
        "swedish": "ljud", "english": "sound", "speak": ["ett ljud"]}).json()
    _an = store.put_audio(_ac["id"], "ett ljud", "andreas", b"ID3-not-really-an-mp3")
    check("a stored audio name has a slash in it", "/" in _an, True)
    _ar = client.get(f"/api/flashcards/audio/{_an}")
    check("real audio serves", _ar.status_code, 200)
    check("real audio serves the right bytes", _ar.content, b"ID3-not-really-an-mp3")
    check("real audio is typed as mp3", _ar.headers["content-type"], "audio/mpeg")
    check("the card advertises the name the route accepts",
          client.get(f"/api/flashcards?source=&limit=500").json()["cards"][0]["audio"],
          {"ett ljud": {"andreas": _an}})

    # ...and the guards, which the :path converter makes load-bearing rather
    # than decorative: these spellings now reach the handler instead of being
    # rejected by the router for the wrong reason.
    for spelling, why in (("%2e%2e", "bare dot-dot"),
                          ("%2e%2e%2f%2e%2e%2fetc%2fpasswd", "encoded escape"),
                          (f"{_ac['id']}%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                           "escape from inside a real card dir")):
        r = client.get(f"/api/flashcards/audio/{spelling}")
        check(f"traversal refused ({why})", (r.status_code, r.status_code == 200),
              (400, False))
    check("a slash-bearing traversal never serves bytes", client.get(
        "/api/flashcards/audio/../../../etc/passwd").status_code in (400, 404), True)
    check("missing audio is 404", client.get(
        "/api/flashcards/audio/nope-andreas.mp3").status_code, 404)
    store.delete(_ac["id"])

    # ---- keyless degradation ----
    r = client.post("/api/flashcards/import", json={"text": "bok - book"})
    check("import without a key is 502", r.status_code, 502)
    truthy("import error names the missing setting",
           "CONTINUUM_OPENROUTER_KEY" in r.json()["error"])

    r = client.post("/api/flashcards/voice", json={"limit": 5})
    check("voice without a key is 502", r.status_code, 502)
    truthy("voice error names the missing setting",
           "CONTINUUM_ELEVENLABS_KEY" in r.json()["error"])

    # An empty draft list must be rejected before it reaches the store.
    check("saving nothing is 400", client.post(
        "/api/flashcards/import/save", json={"cards": []}).status_code, 400)

    # ---- the import path is the same mapping a restore uses ----
    drafts = [{"swedish": "gå", "english": "to walk", "pos": "verb",
               "tags": ["verb"], "example_sv": "Jag går hem.",
               "example_en": "I walk home.",
               "forms": {"infinitive": "gå", "present": "går", "past": "gick"},
               "speak": ["gå", "går", "gick"]},
              {"swedish": "stor", "english": "big", "pos": "adjective",
               "tags": ["adjective"]},
              # This one names its own bucket. It must keep it: the payload-level
              # source is a default for cards that don't say, not an override.
              {"swedish": "hej", "english": "hello", "pos": "interjection",
               "source": "Rivstart", "set": "Kap 7"}]
    r = client.post("/api/flashcards/import/save",
                    json={"cards": drafts, "source": "Duolingo", "set": "Unit 3"})
    check("import/save is 200", r.status_code, 200)
    check("all three drafts stored", r.json()["added"], 3)

    b = client.get("/api/flashcards?source=Duolingo").json()
    check("imported into the named bucket", len(b["cards"]), 2)
    check("a draft that names its own bucket keeps it", len(client.get(
        "/api/flashcards?source=Rivstart&set=Kap%207").json()["cards"]), 1)
    gick = [c for c in b["cards"] if c["swedish"] == "gå"][0]
    check("irregular past survived the round trip", gick["forms"]["past"], "gick")
    check("imported card is due now, not in 50,000 years", gick["due_now"], True)

    # Re-saving the same drafts must not duplicate them.
    r = client.post("/api/flashcards/import/save",
                    json={"cards": drafts, "source": "Duolingo", "set": "Unit 3"})
    check("re-import does not duplicate", len(client.get(
        "/api/flashcards?source=Duolingo").json()["cards"]), 2)
    # ...and says so. Reporting "added 2" while adding nothing is the failure
    # that looks like success, which is the only kind worth a check here.
    check("re-import reports zero added", r.json()["added"], 0)
    check("re-import reports them updated", r.json()["updated"], 3)

    check("missing_audio counts the unspoken forms",
          client.get("/api/flashcards/status").json()["missing_audio"] > 0, True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
