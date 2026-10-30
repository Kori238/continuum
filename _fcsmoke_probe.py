"""Smoke the rewritten flashcard store against a scratch db: schema, SM-2 parity
with srs.js, audio-to-disk, the ms->s import conversion, and the legacy drop."""
import json, os, shutil, sqlite3, tempfile, time, base64, sys
sys.path.insert(0, ".")
from continuum.config import Config
from continuum import flashcards as F

tmp = tempfile.mkdtemp(prefix="fcsmoke")
cfg = Config.load()
# Config is a frozen dataclass, so point it at scratch via object.__setattr__ --
# a bare assignment raises FrozenInstanceError.
def aim(path): object.__setattr__(cfg, "flashcards_db", path)
aim(os.path.join(tmp, "flashcards.db"))
ok = fail = 0
def chk(label, got, want):
    global ok, fail
    if got == want: ok += 1; print(f"  ok   {label}")
    else: fail += 1; print(f"  FAIL {label}: got {got!r} want {want!r}")

# --- legacy drop: plant the OLD empty Leitner table first
os.makedirs(os.path.dirname(cfg.flashcards_db), exist_ok=True)
db = sqlite3.connect(cfg.flashcards_db)
db.execute("CREATE TABLE cards(id INTEGER PRIMARY KEY, deck TEXT, swedish TEXT, english TEXT, box INTEGER, due TEXT)")
db.commit(); db.close()

s = F.FlashcardStore(cfg)
cols = {r[1] for r in sqlite3.connect(cfg.flashcards_db).execute("PRAGMA table_info(cards)")}
chk("legacy Leitner table replaced", "box" in cols and "gender" not in cols, False)
chk("new schema present", {"gender","forms","speak","audio","ease"} <= cols, True)

# --- add / dedupe
c = s.add("bok", "book", source="Rivstart", set_name="Kap 1", pos="noun", gender="en",
          tags=["noun","en"], forms={"def_sg":"boken","indef_pl":"böcker"},
          speak=["en bok","boken","böcker"])
chk("added", bool(c["id"]), True)
chk("gender stored", c["gender"], "en")
chk("forms round-trip", c["forms"]["indef_pl"], "böcker")
chk("new card is due now", c["due_now"], True)
chk("new card is_new", c["is_new"], True)
again = s.add("bok", "book", source="Rivstart", set_name="Kap 1", notes="updated")
chk("same pair+bucket dedupes", again["id"], c["id"])
other = s.add("bok", "book", source="Duolingo", set_name="")
chk("same pair, other bucket is a new card", other["id"] != c["id"], True)
chk("blank face rejected", s.add("", "book"), {})

# --- SM-2 parity with srs.js
g = F.grade(F.fresh_srs(), 4)
chk("first Good -> interval 1", g["interval"], 1.0)
g = F.grade(g, 4); chk("second Good -> interval 6", g["interval"], 6.0)
g3 = F.grade(g, 4); chk("third Good -> 6*ease", g3["interval"], round(6.0 * g["ease"]))
h = F.grade(g, 3); chk("Hard applies x0.7", h["interval"], max(1.0, round(round(6.0*g["ease"])*0.7)))
lapse = F.grade(g, 0)
chk("lapse zeroes interval", lapse["interval"], 0.0)
chk("lapse counts", lapse["lapses"], 1)
chk("lapse returns in 10min", round(lapse["due"] - time.time()), 600)
floor = {"due":0,"interval":10,"ease":1.3,"reps":5,"lapses":0,"last":0}
chk("ease never below 1.3", F.grade(floor, 3)["ease"], 1.3)

# --- review writes the log
r = s.review(c["id"], 4, mode="flip")
chk("review advanced reps", r["srs"]["reps"], 1)
chk("review scheduled ahead", r["due_now"], False)
n = sqlite3.connect(cfg.flashcards_db).execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
chk("review logged append-only", n, 1)
s.review(c["id"], 0, mode="type")
n = sqlite3.connect(cfg.flashcards_db).execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
chk("second review appended not replaced", n, 2)

# --- audio to disk, not into the row
MP3 = bytes([255, 251]) + b"x" * 4000   # an mp3 frame header, no source escapes
name = s.put_audio(c["id"], "en bok", "andreas", MP3)
disk = os.path.join(os.path.dirname(cfg.flashcards_db), "flashcard_audio", name)
chk("mp3 written to disk", os.path.isfile(disk), True)
chk("row stores a filename not bytes", s.get(c["id"])["audio"]["en bok"]["andreas"], name)
rowlen = len(str(sqlite3.connect(cfg.flashcards_db).execute(
    "SELECT audio FROM cards WHERE id=?", (c["id"],)).fetchone()[0]))
chk("audio column stays small", rowlen < 200, True)
def raises(fn, exc):
    try: fn(); return False
    except exc: return True
chk("unknown voice refused", raises(lambda: s.put_audio(c["id"], "x", "siri", b"1"), ValueError), True)
chk("audio for a missing card refused", raises(lambda: s.put_audio("nope", "x", "anders", b"1"), KeyError), True)
chk("path escape refused", raises(lambda: s.audio_bytes("../../../etc/passwd"), ValueError), True)
chk("audio_bytes reads back", s.audio_bytes(name), MP3)

# --- the ms -> s import conversion
payload = {"cards": [{"value": {
    "id": "cimported01", "swedish": "hus", "english": "house", "source": "Rivstart",
    "set": "Kap 2", "pos": "noun", "gender": "ett", "speak": ["ett hus"],
    "srs": {"due": 1789000000000, "interval": 6, "ease": 2.36, "reps": 3, "lapses": 1,
            "last": 1788000000000},
    "audio": {"ett hus": {"anders": "data:audio/mpeg;base64," + base64.b64encode(b"\xff\xfbmp3").decode()}},
}}]}
res = s.import_backup(payload)
chk("import added one", res["added"], 1)
chk("import wrote audio file", res["audio_files"], 1)
imp = s.get("cimported01")
chk("id preserved from phone", imp["id"], "cimported01")
chk("due converted ms->s", round(imp["srs"]["due"]), 1789000000)
chk("last converted ms->s", round(imp["srs"]["last"]), 1788000000)
chk("ease preserved", imp["srs"]["ease"], 2.36)
chk("reps preserved", imp["srs"]["reps"], 3)
chk("imported card is NOT new", imp["is_new"], False)
res2 = s.import_backup(payload)
chk("re-import updates, never duplicates", (res2["added"], res2["updated"]), (0, 1))

# --- queue + stats
for i in range(5):
    s.add(f"ord{i}", f"word{i}", source="Rivstart", set_name="Kap 1")
q = s.study(source="Rivstart", set_name="Kap 1", limit=3)
chk("study respects limit", len(q), 3)
chk("study scopes to bucket", all(x["source"] == "Rivstart" for x in q), True)
due_only = s.study(source="Nope", due_only=True)
chk("due_only on empty bucket", due_only, [])
st = s.stats()
chk("stats counts every card", st["total"], len(s.cards(limit=10000)))
b = [x for x in s.buckets() if x["set"] == "Kap 1"][0]
chk("bucket totals", b["total"], 6)   # bok + ord0..ord4

# --- resolve
chk("resolve exact", s.resolve("hus")["id"], "cimported01")
chk("resolve ambiguous -> None", s.resolve("bo"), None)

# --- delete takes the audio with it
s.delete(c["id"])
chk("card gone", s.get(c["id"]), {})
chk("audio file gone too", os.path.isfile(disk), False)
chk("review rows gone", sqlite3.connect(cfg.flashcards_db).execute(
    "SELECT COUNT(*) FROM reviews WHERE card_id=?", (c["id"],)).fetchone()[0], 0)

# --- refuses to destroy a NON-empty legacy table
tmp2 = tempfile.mkdtemp(prefix="fclegacy")
p2 = os.path.join(tmp2, "flashcards.db")
db = sqlite3.connect(p2)
db.execute("CREATE TABLE cards(id INTEGER PRIMARY KEY, deck TEXT, swedish TEXT, english TEXT, box INTEGER, due TEXT)")
db.execute("INSERT INTO cards(deck,swedish,english,box,due) VALUES('Swedish','a','b',1,'2026-01-01')")
db.commit(); db.close()
aim(p2)
try:
    F.FlashcardStore(cfg); chk("refuses to drop a populated legacy table", "no raise", "raise")
except RuntimeError as e:
    chk("refuses to drop a populated legacy table", "holds 1 rows" in str(e), True)

shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(tmp2, ignore_errors=True)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
