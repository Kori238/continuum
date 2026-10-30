"""Negative control for _fcapi_test.py.

Plants a defect the flashcard routes are supposed to prevent, confirms the API
suite goes RED, restores. A MISSED means the suite has a blind spot exactly
there. Mutations span two files, because the routes and the vocab service are
one feature from the browser's point of view.

PYTHONDONTWRITEBYTECODE is forced for the child: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

SERVER = "continuum/app/server.py"
STORE = "continuum/flashcards.py"
VOCAB = "continuum/vocab.py"

MUTATIONS = [
    # --- the routes -------------------------------------------------------
    ("blank review body counts as a pass", SERVER,
     'flashcards.review, card_id, int(body.get("quality") or 0),',
     'flashcards.review, card_id, int(body.get("quality") or 5),'),
    ("review collapses quality to a boolean", SERVER,
     'int(body.get("quality") or 0),',
     'int(bool(body.get("quality"))) * 5,'),
    # The converter is the whole bug: stored names are "<id>/<form>.mp3", so a
    # plain {name} 404s every real one while a flat synthetic name still passes.
    ("audio route stops matching a real (slashed) name", SERVER,
     '@app.get("/api/flashcards/audio/{name:path}")',
     '@app.get("/api/flashcards/audio/{name}")'),
    ("audio files stop being filed per card, orphaning every stored name", STORE,
     'name = f"{card_id}/{slug}-{voice}.mp3"',
     'name = f"{slug}-{voice}.mp3"'),
    ("audio guard no longer caught, 500s instead of 400", SERVER,
     "        except ValueError:\n            return JSONResponse({\"error\": \"bad audio name\"}, status_code=400)",
     "        except ZeroDivisionError:\n            return JSONResponse({\"error\": \"bad audio name\"}, status_code=400)"),
    ("a card with one face is accepted", SERVER,
     "        if not sv or not en:\n            return JSONResponse({\"error\": \"both faces required\"}, status_code=400)",
     "        if False:\n            return JSONResponse({\"error\": \"both faces required\"}, status_code=400)"),
    ("an empty draft list reaches the store", SERVER,
     "if not isinstance(drafts, list) or not drafts:",
     "if not isinstance(drafts, list):"),
    ("import/save drops the bucket the user named", SERVER,
     '"source": str(body.get("source") or ""),',
     '"source": "",'),
    ("the ?set= alias is dropped, so filtering silently does nothing", SERVER,
     'async def get_flashcards(source: str = "", set_name: str = Query("", alias="set"),',
     'async def get_flashcards(source: str = "", set_name: str = Query("", alias="unused"),'),
    ("status stops reporting outstanding audio", SERVER,
     '"missing_audio": len(await asyncio.to_thread(flashcards.missing_audio))',
     '"missing_audio": 0'),
    ("study ignores due_only", SERVER,
     "flashcards.study, limit=limit, due_only=due_only, **f)",
     "flashcards.study, limit=limit, due_only=False, **f)"),

    # --- the store, as the routes depend on it ----------------------------
    ("buckets go back to a second name for the same field", STORE,
     'return [{"source": r["source"] or "", "set": r["set_name"] or "",',
     'return [{"source": r["source"] or "", "set_name": r["set_name"] or "",'),
    ("payload-level bucket reaches nothing (the original bug)", STORE,
     'dflt_source = str(payload.get("source") or "")',
     'dflt_source = ""'),
    ("a per-card bucket is overridden by the default", STORE,
     'src = c.get("source") or dflt_source',
     'src = dflt_source or c.get("source") or ""'),
    ("re-import reports everything as brand new", STORE,
     'existed = bool(self.get(str(c.get("id") or ""))) or self._pair_exists(src, st, sv, en)',
     'existed = bool(self.get(str(c.get("id") or "")))'),
    ("tag filtering stops filtering", STORE,
     'out = [c for c in out if t in c["tags"]]',
     'out = [c for c in out if t or c["tags"] or True]'),

    # --- the vocab service ------------------------------------------------
    ("status leaks key material into the browser", VOCAB,
     '"import": bool(self._cfg.openrouter_key),',
     '"import": bool(self._cfg.openrouter_key), "openrouter_key": self._cfg.openrouter_key,'),
    ("a missing key stops being an error", VOCAB,
     '        if not self._cfg.openrouter_key:\n'
     '            raise VocabError("No OpenRouter key configured — set CONTINUUM_OPENROUTER_KEY in .env.")',
     '        if False:\n'
     '            raise VocabError("No OpenRouter key configured — set CONTINUUM_OPENROUTER_KEY in .env.")'),
    ("the audio error stops naming the setting to fix", VOCAB,
     'raise VocabError("No ElevenLabs key configured — set CONTINUUM_ELEVENLABS_KEY in .env.")\n'
     '        wanted = await asyncio.to_thread(self._store.missing_audio)',
     'raise VocabError("No text-to-speech key configured.")\n'
     '        wanted = await asyncio.to_thread(self._store.missing_audio)'),
]


def _exact(path):
    """Read with newline="" so CRLF survives the round trip.

    Text mode translates newlines BOTH ways. A control that reads in text mode
    and restores with newline="" silently rewrites a CRLF checkout to LF, and
    its own "restored byte-identical" check re-reads through the same
    translation and cannot see it. That happened on the hub, to
    continuum/workouts.py, while this printed True.
    """
    return io.open(path, encoding="utf-8", newline="").read()


def _anchor(src, text):
    """`text` as it appears in THIS file.

    Anchors are written with \n. On a CRLF checkout a multi-line anchor then
    matches 0x, which is reported UNREADABLE and counted as a miss - loud, but
    it means the control cannot run anywhere it was deployed.
    """
    if text in src or "\r\n" not in src:
        return text
    return text.replace("\n", "\r\n")


orig = {f: _exact(f) for f in (SERVER, STORE, VOCAB)}
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def suite_passes():
    r = subprocess.run([sys.executable, "_fcapi_test.py"], capture_output=True, env=env)
    return r.returncode == 0, r.stdout + r.stderr


# Ground truth first: an UNMUTATED tree must pass, or every CAUGHT below is noise.
base, out = suite_passes()
print(f"baseline (no mutation): {'PASS' if base else 'FAIL'}")
if not base:
    print(out.decode("utf-8", "replace")[-2000:])
    sys.exit("baseline is red - controls would be meaningless")

caught = missed = 0
for label, target, find, repl in MUTATIONS:
    src = orig[target]
    find, repl = _anchor(src, find), _anchor(src, repl)
    if src.count(find) != 1:
        print(f"  UNREADABLE {label}: anchor matches {src.count(find)}x in {target}")
        missed += 1
        continue
    io.open(target, "w", encoding="utf-8", newline="").write(src.replace(find, repl, 1))
    try:
        passed, _ = suite_passes()
    finally:
        io.open(target, "w", encoding="utf-8", newline="").write(src)
    if passed:
        print(f"  MISSED  {label}")
        missed += 1
    else:
        print(f"  CAUGHT  {label}")
        caught += 1

print(f"\n{caught} caught, {missed} missed, of {len(MUTATIONS)}")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if missed or not clean else 0)
