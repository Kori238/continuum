"""Can a REAL stored audio name be fetched over HTTP?

put_audio() returns "{card_id}/{slug}-{voice}.mp3" - it has a slash in it. The
route declares {name}, and Starlette's default str converter does not match a
slash. If that is true, every real audio file 404s and the _fcapi_test check
that asserts 404 for slash-bearing names is asserting the bug.
"""
import os, sys, tempfile
from pathlib import Path
TMP = Path(tempfile.mkdtemp(prefix="continuum-fcaudio2-"))
os.environ["CONTINUUM_FLASHCARDS_DB"] = str(TMP / "flashcards.db")
os.environ["CONTINUUM_OPENROUTER_KEY"] = ""
os.environ["CONTINUUM_ELEVENLABS_KEY"] = ""
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from continuum.config import Config
from continuum.app.server import create_app

cfg = Config.load()
assert str(Path(cfg.flashcards_db).resolve()).startswith(str(TMP.resolve())), "not sandboxed"

app = create_app()
c = TestClient(app, client=("127.0.0.1", 51000))

card = c.post("/api/flashcards", json={"swedish": "bok", "english": "book",
                                       "speak": ["en bok", "boken"]}).json()
from continuum.flashcards import FlashcardStore
st = FlashcardStore(cfg)
name = st.put_audio(card["id"], "en bok", "andreas", b"ID3fake-mp3-bytes")
print("stored name:", name)

r = c.get(f"/api/flashcards/audio/{name}")
print(f"GET /api/flashcards/audio/{name} -> {r.status_code}")
print("served the bytes:", r.status_code == 200 and r.content == b"ID3fake-mp3-bytes")

# and what the card itself advertises to the browser
print("card.audio:", c.get(f"/api/flashcards?limit=1").json()["cards"][0]["audio"])

# ---- with the fix: a real name must serve, and traversal must still 400 ----
print()
for spelling in ("../../../etc/passwd", "..%2f..%2fsecret", "%2e%2e/%2e%2e/x",
                 f"{card['id']}/../../../etc/passwd"):
    r = c.get(f"/api/flashcards/audio/{spelling}")
    served = r.status_code == 200
    print(f"  {spelling!r:42} -> {r.status_code} {'SERVED BYTES - LEAK' if served else ''}")
