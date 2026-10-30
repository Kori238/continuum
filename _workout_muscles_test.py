"""The muscle distribution, from the list response through to stats().

`_norm_session` hardcoded `"muscles": {}` under a comment saying the data was
"not present at list level". It is present at list level, as
`trainingPartSetsInfoList`, on every sync. So `WorkoutStore.stats()["muscles"]`
- which exists solely to sum that dict - had returned `[]` forever: all 39 of
Jake's stored sessions held the same `'{}'`, one distinct value, so the agent's
"Most-worked:" clause (written `if muscles`) had never once rendered. It is the
only consumer; the Workouts tab fetches the same stats endpoint and does not
read `muscles` at all.

The ids carry no names, so TRAINING_PARTS was derived from evidence (each id's
exercises, fetched live). The checks that matter here are therefore as much
about what is NOT claimed - an unmapped id must keep its number rather than
borrow a neighbour's, because a wrong muscle name would misattribute Jake's
training silently and indefinitely.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-wk-muscles-"))
os.environ["CONTINUUM_WORKOUTS_DB"] = str(TMP / "workouts.db")
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config                     # noqa: E402
from continuum.workouts import WorkoutStore             # noqa: E402
from continuum.integrations.speediance import (         # noqa: E402
    SpeedianceClient, TRAINING_PARTS, _norm_parts, part_name)

cfg = Config.load()
assert str(Path(cfg.workouts_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - workouts_db is not in the sandbox"

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


# ---- _norm_parts ------------------------------------------------------------
# The exact payload shape observed live on Jake's account, 2026-09-18.
LIVE = [{"trainingPartId2": 11, "sets": 3},
        {"trainingPartId2": 12, "sets": 1},
        {"trainingPartId2": 16, "sets": 1}]
check("THE BUG: the live list payload is no longer discarded",
      _norm_parts(LIVE), {"Chest": 3, "Shoulders": 1, "Arms": 1})
check("a missing key yields an empty dict, not a crash", _norm_parts(None), {})
check("a non-list yields an empty dict", _norm_parts({"trainingPartId2": 11}), {})
check("a non-dict entry is skipped", _norm_parts([11, {"trainingPartId2": 13, "sets": 2}]),
      {"Back": 2})
check("a zero-set entry is dropped rather than stored as 0",
      _norm_parts([{"trainingPartId2": 11, "sets": 0}]), {})
check("string set counts are coerced", _norm_parts([{"trainingPartId2": 13, "sets": "4"}]),
      {"Back": 4})
# Two ids can share a rendered name, so the merge must add rather than assign.
check("entries sharing a name accumulate",
      _norm_parts([{"trainingPartId2": 99, "sets": 2}, {"trainingPartId2": 99, "sets": 3}]),
      {"part 99": 5})

# ---- naming: what is NOT claimed -------------------------------------------
check("a known id gets its evidenced name", part_name(16), "Arms")
check("an UNMAPPED id keeps its number instead of guessing", part_name(14), "part 14")
check("an id Speediance adds later is honest too", part_name(23), "part 23")
check("a junk id degrades without raising", part_name(None), "part ?")
check("a numeric string still maps", part_name("11"), "Chest")
# Spelled out as literals on purpose: deriving the expectation from
# TRAINING_PARTS would assert the table is self-consistent, not that it is
# right. These six came from 97 exercises fetched live.
check("the derived table is exactly what the evidence supported",
      dict(sorted(TRAINING_PARTS.items())),
      {11: "Chest", 12: "Shoulders", 13: "Back", 15: "Legs", 16: "Arms", 17: "Core"})
truthy("14 is deliberately absent - it never appeared in the evidence",
       14 not in TRAINING_PARTS)

# ---- through _norm_session --------------------------------------------------
sess = SpeedianceClient._norm_session({
    "trainingId": 12345, "title": "push", "type": 5,
    "trainingTime": 1800, "calorie": 210, "totalCapacity": 4200, "totalEnergy": 33,
    "startTimestamp": 1758000000000, "endTimestamp": 1758001800000,
    "trainingPartSetsInfoList": LIVE})
check("_norm_session carries the muscles through",
      sess["muscles"], {"Chest": 3, "Shoulders": 1, "Arms": 1})
check("a session with no part list is an empty dict, not None",
      SpeedianceClient._norm_session({"trainingId": 1, "title": "x"})["muscles"], {})

# ---- end to end: store -> stats --------------------------------------------
w = WorkoutStore(cfg)
truthy("the session stores", w.upsert_session(sess))
second = SpeedianceClient._norm_session({
    "trainingId": 12346, "title": "pull", "type": 5,
    "trainingTime": 1800, "calorie": 200, "totalCapacity": 3900, "totalEnergy": 30,
    "startTimestamp": 1758100000000, "endTimestamp": 1758101800000,
    "trainingPartSetsInfoList": [{"trainingPartId2": 13, "sets": 4},
                                 {"trainingPartId2": 16, "sets": 2}]})
truthy("a second session stores", w.upsert_session(second))

st = w.stats(days=3650)
check("THE FIX, END TO END: stats no longer returns an empty distribution",
      {m["name"]: m["sets"] for m in st["muscles"]},
      {"Back": 4, "Chest": 3, "Arms": 3, "Shoulders": 1})
truthy("...and it is sorted by sets, descending",
       [m["sets"] for m in st["muscles"]] == sorted(
           (m["sets"] for m in st["muscles"]), reverse=True))
check("sessions still counted", st["sessions"], 2)

# A Health Connect session has no part data and must not fabricate any, nor
# knock out the Speediance rows' distribution.
truthy("an hc session stores", w.upsert_session({
    "training_id": "hc:abc", "title": "WALKING", "start_ts": 1758200000,
    "end_ts": 1758203600, "duration_s": 3600, "calories": 180}))
st = w.stats(days=3650)
check("an hc session contributes no muscles and breaks nothing",
      {m["name"]: m["sets"] for m in st["muscles"]},
      {"Back": 4, "Chest": 3, "Arms": 3, "Shoulders": 1})
check("...and is still counted as a session", st["sessions"], 3)

# Re-syncing is how the 39 already-stored rows self-heal: the list response is
# authoritative and the upsert overwrites. Assert the heal, since it is the
# whole deployment plan.
stale = dict(sess)
stale["muscles"] = {}
truthy("a re-report of an existing session is not counted as new",
       not w.upsert_session(stale))
check("a blank re-report DOES blank it - the list response is authoritative",
      {m["name"]: m["sets"] for m in w.stats(days=3650)["muscles"]},
      {"Back": 4, "Arms": 2})
w.upsert_session(sess)
check("...and the next real sync heals it straight back",
      {m["name"]: m["sets"] for m in w.stats(days=3650)["muscles"]},
      {"Back": 4, "Chest": 3, "Arms": 3, "Shoulders": 1})

print()
print(f"{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
sys.exit(1 if FAILED else 0)
