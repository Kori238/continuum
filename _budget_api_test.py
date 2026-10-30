"""Budget routes end-to-end through the real create_app().

Sandboxed exactly like _registry_test.py — all fourteen stores redirected, and the
run aborts before create_app() if any Config field still resolves inside live data.

One deliberate restraint: every allowance set here is generous and the sandbox
activity.db is empty, so nothing ever depletes. The state loop is genuinely
running during this test (TestClient's context manager fires startup), which means
a depleted category would call enforce.apply() for real — a toast on Jake's actual
desktop, and if a budgeted app happened to be in the foreground, a real
terminate(). A test must not be able to close his game.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-budget-api-"))
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
print(f"sandbox: {TMP}")

from fastapi.testclient import TestClient         # noqa: E402
from continuum.app.server import create_app       # noqa: E402

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
    r = client.get("/api/budget")
    check("GET /api/budget is 200", r.status_code, 200)
    check("no allowances configured on a fresh install", r.json()["categories"], [])

    r = client.get("/api/budget/rules")
    body = r.json()
    truthy("seed rules are present", len(body["rules"]) >= 20)
    check("categories exposed", body["categories"], ["game", "social", "video"])
    truthy("Elderfield is seeded",
           any(x["match"] == "game.exe" and x["category"] == "game" for x in body["rules"]))

    # Generous on purpose — see the module docstring.
    r = client.post("/api/budget/allowance", json={"category": "game", "minutes": 120})
    check("first allowance applies now", r.json()["applies"], "now")
    r = client.post("/api/budget/allowance", json={"category": "game", "minutes": 240})
    check("a raise is deferred to tomorrow", r.json()["applies"], "tomorrow")
    r = client.post("/api/budget/allowance", json={"category": "game", "minutes": 90})
    check("a cut lands immediately", r.json()["applies"], "now")

    cats = client.get("/api/budget").json()["categories"]
    game = next(c for c in cats if c["category"] == "game")
    check("effective allowance is the cut one", game["minutes"], 90)
    check("the parked raise was cleared by the cut", game["pending_minutes"], None)

    r = client.post("/api/budget/allowance", json={"category": "chess", "minutes": 10})
    check("unknown category is a 400, not a 500", r.status_code, 400)
    r = client.post("/api/budget/allowance", json={"category": "game", "minutes": -1})
    check("negative minutes is a 400", r.status_code, 400)

    r = client.post("/api/budget/rules",
                    json={"field": "proc", "match": "Factorio.exe", "category": "game"})
    check("a new rule is accepted", r.json()["match"], "factorio.exe")
    rules = client.get("/api/budget/rules").json()["rules"]
    rid = next(x["id"] for x in rules if x["match"] == "factorio.exe")
    check("rule deletes", client.delete(f"/api/budget/rules/{rid}").json()["ok"], True)
    r = client.post("/api/budget/rules",
                    json={"field": "nonsense", "match": "x", "category": "game"})
    check("bad rule field is a 400", r.status_code, 400)

    r = client.post("/api/budget/override",
                    json={"category": "game", "minutes": 30, "reason": "test"})
    check("override accepted", r.json()["minutes"], 30)
    st = client.get("/api/budget").json()
    game = next(c for c in st["categories"] if c["category"] == "game")
    check("override shows in today's allowance", game["allowance_seconds"], (90 + 30) * 60)
    check("override is counted", st["overrides_today"], 1)
    check("the chosen number is untouched", game["minutes"], 90)

    ev = client.get("/api/budget/events").json()["events"]
    truthy("the override is in the audit trail",
           any(e["kind"] == "override" for e in ev))

    client.post("/api/budget/enabled", json={"category": "game", "enabled": False})
    game = next(c for c in client.get("/api/budget").json()["categories"]
                if c["category"] == "game")
    check("enforcement can be switched off", game["enabled"], False)

    # GET must be inert: opening the tab cannot spend grace or fire a warning.
    before = len(client.get("/api/budget/events").json()["events"])
    for _ in range(4):
        client.get("/api/budget")
    check("polling /api/budget writes no events",
          len(client.get("/api/budget/events").json()["events"]), before)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all budget API checks passed")
