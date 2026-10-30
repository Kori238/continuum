"""Which traversal spellings actually REACH the audio handler? A 404 from the
router and a 400 from the guard look the same from outside, and only the second
proves the route wired the guard up."""
import os, sys, tempfile
from pathlib import Path
TMP = Path(tempfile.mkdtemp(prefix="fcaudio"))
for v, leaf in [("CONTINUUM_MEMORY_DB","m.db"),("CONTINUUM_CONVERSATIONS_DB","c.db"),
  ("CONTINUUM_SCHEDULER_DB","s.db"),("CONTINUUM_MESSAGES_DB","ms.db"),
  ("CONTINUUM_ROUTINES_DB","r.db"),("CONTINUUM_WORKOUTS_DB","w.db"),
  ("CONTINUUM_ACTIVITY_DB","a.db"),("CONTINUUM_NUTRITION_DB","n.db"),
  ("CONTINUUM_BODY_DB","b.db"),("CONTINUUM_HEALTH_DB","h.db"),
  ("CONTINUUM_LEDGER_DB","l.db"),("CONTINUUM_FLASHCARDS_DB","f.db"),
  ("CONTINUUM_BUDGET_DB","bu.db"),("CONTINUUM_UPLOADS_DIR","up")]:
    os.environ[v] = str(TMP/leaf)
os.environ["CONTINUUM_PROACTIVE"]="0"; os.environ["CONTINUUM_OLLAMA_AUTOSTART"]="0"
os.environ["CONTINUUM_OPENROUTER_KEY"]=""; os.environ["CONTINUUM_ELEVENLABS_KEY"]=""
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from continuum.app.server import create_app
with TestClient(create_app(), client=("127.0.0.1", 51001)) as c:
    for name in ["..%2F..%2Fetc%2Fpasswd", "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                 "..%5C..%5Cwin.ini", "....%2F%2F....%2F%2Fwin.ini",
                 "..", "%2e%2e", "a%2F..%2F..%2Fwin.ini", "ok-andreas.mp3"]:
        r = c.get(f"/api/flashcards/audio/{name}")
        print(f"{r.status_code}  {name}")
