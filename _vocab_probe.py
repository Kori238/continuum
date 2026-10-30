"""Offline proof for continuum/vocab.py: the response parser, the missing-key
degradation, and above all the COST logic -- dedup by string and the hard cap.
No network: synth() is replaced with a counter, so what is asserted is how many
billable calls WOULD be made."""
import asyncio, os, sys, tempfile
sys.path.insert(0, ".")
from continuum.config import Config
from continuum.flashcards import FlashcardStore
from continuum.vocab import VocabService, VocabError, _parse_cards

ok = fail = 0
def chk(label, got, want):
    global ok, fail
    if got == want: ok += 1; print(f"  ok   {label}")
    else: fail += 1; print(f"  FAIL {label}: got {got!r} want {want!r}")
def raises(fn, exc):
    try: fn(); return False
    except exc: return True

tmp = tempfile.mkdtemp(prefix="vocab")
cfg = Config.load()
def aim(**kw):
    for k, v in kw.items(): object.__setattr__(cfg, k, v)
aim(flashcards_db=os.path.join(tmp, "f.db"),
    tts_voices={"andreas": "vid1", "anders": "vid2"},
    openrouter_key="", elevenlabs_key="")
store = FlashcardStore(cfg)
svc = VocabService(cfg, store)

# ---- status / degradation
st = svc.status()
chk("no keys -> import off", st["import"], False)
chk("no keys -> audio off", st["audio"], False)
chk("status never leaks key material", any("key" in k for k in st), False)
chk("import without a key raises", raises(
    lambda: asyncio.run(svc.build_cards("bok")), VocabError), True)
chk("audio without a key raises", raises(
    lambda: asyncio.run(svc.voice_cards()), VocabError), True)
chk("blank import is a no-op, not an error", asyncio.run(svc.build_cards("   ")), [])

# ---- parser
good = '{"cards":[{"swedish":"bok","english":"book","pos":"noun","gender":"en"}]}'
chk("clean json", _parse_cards(good)[0]["swedish"], "bok")
chk("fenced json", _parse_cards("```json\n" + good + "\n```")[0]["english"], "book")
chk("chatty prefix", _parse_cards("Sure! Here you go:\n" + good)[0]["gender"], "en")
chk("drops entries missing a face", len(_parse_cards(
    '{"cards":[{"swedish":"bok","english":""},{"swedish":"hus","english":"house"}]}')), 1)
chk("garbage raises", raises(lambda: _parse_cards("not json at all"), VocabError), True)
chk("no cards key raises", raises(lambda: _parse_cards('{"x":1}'), VocabError), True)

# ---- COST: dedup by string, and the cap
aim(elevenlabs_key="present")
calls = []
async def fake_synth(text, voice):
    calls.append((text, voice))
    return b"\xff\xfbmp3"
svc.synth = fake_synth

# Three cards that SHARE the surface form "en bok" -- one synthesis, three writes.
for i in range(3):
    store.add(f"bok{i}", f"book{i}", source="S", set_name="K", speak=["en bok"])
res = asyncio.run(svc.voice_cards(limit=100))
chk("shared form synthesised once per voice", len(calls), 2)          # 2 voices
chk("...but attached to every card", res["attached"], 6)              # 3 cards x 2
chk("synthesised count matches billed calls", res["synthesised"], 2)
chk("no errors", res["errors"], [])
chk("nothing deferred", res["remaining_calls"], 0)
again = asyncio.run(svc.voice_cards(limit=100))
chk("already-voiced cards cost nothing", again["synthesised"], 0)

# ---- the cap is real and is REPORTED
calls.clear()
for i in range(10):
    store.add(f"ord{i}", f"word{i}", source="S", set_name="K2", speak=[f"form{i}"])
res = asyncio.run(svc.voice_cards(limit=5))
chk("cap limits billable calls", len(calls), 5)
chk("cap reported, not silent", res["remaining_calls"], 15)           # 10 forms x2 -5
chk("chars counted for the batch", res["chars"], sum(len(t) for t, _ in calls))
rest = asyncio.run(svc.voice_cards(limit=100))
chk("next batch finishes the rest", rest["remaining_calls"], 0)
chk("zero limit does nothing but still reports", 
    asyncio.run(svc.voice_cards(limit=0))["remaining_calls"], 0)

# ---- one failure must not abort the batch
calls.clear()
for i in range(4):
    store.add(f"fel{i}", f"bad{i}", source="S", set_name="K3", speak=[f"fel{i}"])
async def flaky(text, voice):
    calls.append((text, voice))
    if text == "fel2": raise VocabError("provider said no")
    return b"\xff\xfbmp3"
svc.synth = flaky
res = asyncio.run(svc.voice_cards(limit=100))
# "fel2" fails in BOTH voices -- two jobs, not one. 8 jobs, 2 failures, 6 made.
chk("batch continued past the failure", res["synthesised"], 6)
chk("both failing jobs surfaced", len(res["errors"]), 2)
chk("error text carries the form", "fel2" in res["errors"][0], True)

import shutil; shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
