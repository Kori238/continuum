"""Does the device registry stay bounded when the phone varies its hello name?

`devices[ev.source]` is keyed by `phone:<name>` and the name comes straight from
the client, so this is the path a buggy or hostile app uses to grow the registry
(and every /api/status payload) without limit.

Every store is redirected into a temp dir FIRST, and the redirect is *asserted*
before `create_app()` is allowed to run — the previous harness in the workspace
set CONTINUUM_SCHEDULE_DB, but the real variable is CONTINUUM_SCHEDULER_DB, so
it had been quietly writing to the live scheduler database.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="continuum_registry_test_")
for var, name in [
    ("CONTINUUM_MEMORY_DB", "memory.db"),
    ("CONTINUUM_CONVERSATIONS_DB", "conversations.db"),
    ("CONTINUUM_SCHEDULER_DB", "scheduler.db"),
    ("CONTINUUM_MESSAGES_DB", "messages.db"),
    ("CONTINUUM_ROUTINES_DB", "routines.db"),
    ("CONTINUUM_WORKOUTS_DB", "workouts.db"),
    ("CONTINUUM_ACTIVITY_DB", "activity.db"),
    ("CONTINUUM_NUTRITION_DB", "nutrition.db"),
    ("CONTINUUM_BODY_DB", "body.db"),
    ("CONTINUUM_HEALTH_DB", "health.db"),
    ("CONTINUUM_LEDGER_DB", "ledger.db"),
    ("CONTINUUM_FLASHCARDS_DB", "flashcards.db"),
    ("CONTINUUM_BUDGET_DB", "budget.db"),
    ("CONTINUUM_UPLOADS_DIR", "uploads"),
]:
    os.environ[var] = os.path.join(TMP, name)
os.environ["CONTINUUM_OLLAMA_AUTOSTART"] = "0"
os.environ["CONTINUUM_PROACTIVE"] = "0"

sys.path.insert(0, r"C:\Users\kori\continuum")

from continuum.config import Config  # noqa: E402

cfg = Config.load()
LIVE = os.path.join(r"C:\Users\kori\continuum", "data")
escaped = [f"{f}={v}" for f, v in vars(cfg).items()
           if isinstance(v, str) and v.lower().startswith(LIVE.lower())]
if escaped:
    sys.exit("REFUSING TO RUN — these still point at live data:\n  " + "\n  ".join(escaped))
print(f"all stores redirected to {TMP}\n")

from fastapi.testclient import TestClient  # noqa: E402
from continuum.app.server import create_app  # noqa: E402

app = create_app()
fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(label)


with TestClient(app, client=("127.0.0.1", 51000)) as client:
    base = client.get("/api/status").json()["devices"]
    print(f"devices at startup: {[d['name'] for d in base]}\n")

    # One connection per fabricated name — the registry keys off the name, so
    # this is exactly what a client with a nondeterministic name would produce.
    for i in range(60):
        with client.websocket_connect("/phone/ws") as ws:
            ws.send_json({"type": "hello", "name": f"ghost-{i}",
                          "model": "Pixel 7 Pro", "os": "Android 15",
                          "capabilities": ["open_app"]})
            ws.receive_json()  # welcome

    devs = client.get("/api/status").json()["devices"]
    names = [d["name"] for d in devs]
    check("registry stayed bounded after 60 distinct names",
          len(devs) <= 10, f"{len(devs)} entries")
    check("the newest registration survived", "phone:ghost-59" in names)
    check("the oldest ghost was evicted", "phone:ghost-0" not in names)

    # A 100kB name must not become a 100kB dict key served on every status poll.
    with client.websocket_connect("/phone/ws") as ws:
        ws.send_json({"type": "hello", "name": "Z" * 100_000, "model": "Pixel",
                      "os": "Android", "capabilities": [f"c{i}" for i in range(5_000)]})
        ws.receive_json()
    devs = client.get("/api/status").json()["devices"]
    longest = max(len(d["name"]) for d in devs)
    widest = max(len(d["capabilities"]) for d in devs)
    check("a 100k name is truncated", longest <= 80, f"longest name {longest} chars")
    check("a 5k capability list is truncated", widest <= 32, f"widest caps {widest}")
    check("status payload stays small",
          len(client.get("/api/status").content) < 8_000,
          f"{len(client.get('/api/status').content)} bytes")

    # The real device must never be the thing that gets evicted.
    check("desktop still registered",
          any(d["name"].startswith("desktop:") for d in devs),
          ", ".join(n for n in (d["name"] for d in devs) if n.startswith("desktop:")) or "GONE")

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL PASS'}")
sys.exit(1 if fails else 0)
