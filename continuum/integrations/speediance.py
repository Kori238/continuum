"""Speediance (Gym Monster) cloud client — read-only, unofficial.

Speediance publishes no developer API. This talks to the same cloud API the
Android app uses, reverse-engineered by the community tools (`speediance-cli` by
stozo04, built on hbui3's manager and gavinmcfall's influx exporter). That means:

  * It is gray-area on their ToS and can break with any app-server update — the
    upstream projects are winding down *because* Speediance is tightening API
    security. Treat a failed pull as "the integration broke", not "no workouts".
  * The server is brittle: it rejects a request whose headers don't match the app
    byte-for-byte, so the frozen header set below is load-bearing, not cosmetic.
  * Auth is email + password + region. Jake's UK account lives on the EU host.
  * This client only ever READS. It never pushes a program to the machine.

Every value the live API's shape governs — the region hosts, the frozen headers,
the endpoint paths, the request/response field names — is a named constant or an
isolated normaliser here, so an app-server change is a one-place fix. Contract
ported from `stozo04/speediance-cli` (internal/api, internal/workout), verified
live against Jake's account. stdlib only; no third-party dependency to install.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger("continuum.speediance")

# Account region → API base (note the trailing /api). Unknown region → Global,
# mirroring the upstream default. Jake is "EU".
REGION_HOSTS = {
    "GLOBAL": "https://api2.speediance.com/api",
    "EU": "https://euapi.speediance.com/api",
}
_DEFAULT_BASE = REGION_HOSTS["GLOBAL"]

# Endpoint paths, relative to the region base. Frozen to match the app.
_EP_VERIFY = "/app/v2/login/verifyIdentity"
_EP_LOGIN = "/app/v2/login/byPass"
_EP_RECORDS = "/mobile/v2/report/userTrainingDataRecord"
# Per-session detail lives in one of two namespaces under /app/trainingInfo/ (NOT
# /mobile/v2/ — that prefix 404s). A trainingId can be valid in BOTH namespaces as
# two unrelated sessions (confirmed live against Jake's account: id 593429 returns
# a Bench Press program session and an unrelated Lat Pulldown free session), so the
# session's authoritative list `type` must drive dispatch: type 5 (program) probes
# program-first, everything else free-first. Each namespace exposes an INFO path
# (session summary) and a DETAIL path (the per-set rows we actually want).
_EP_PROGRAM_INFO = "/app/trainingInfo/cttTrainingInfo/"       # + {id}
_EP_PROGRAM_DETAIL = "/app/trainingInfo/cttTrainingInfoDetail/"  # + {id}
_EP_FREE_INFO = "/app/trainingInfo/freeTraining/"             # + {id}
_EP_FREE_DETAIL = "/app/trainingInfo/freeTrainingDetail/"     # + {id}

# Speediance tags each exercise with a numeric `trainingPartId2` and reports a
# per-session roll-up as `trainingPartSetsInfoList`. It ships no names for those
# ids, so these were DERIVED FROM EVIDENCE rather than guessed: every session's
# detail was fetched and the exercises grouped by part id (2026-09-18, 16 of
# Jake's 19 sessions, 97 exercises). The groupings were unambiguous —
#
#   11  Barbell Bench Press, Standing/Incline Dual-Handle Chest Fly      (19)
#   12  Seated Shoulder Press, Lateral Raise, Rear Delt Fly              (19)
#   13  Seated Alternating Row, Lat Pulldown, Single-Arm High Row        (20)
#   15  Barbell Squat, Calf Raise, Bulgarian Split Squat, Leg Curl        (5)
#   16  Preacher/Hammer/Biceps Curl, Triceps Extension, Rope Pushdown    (31)
#   17  Kneeling Crunch, Woodchops, Cross-Body Crunch                     (3)
#
# — note 17's exercises are named "Tricep Rope …" after the HANDLE, but every
# movement is a crunch or a woodchop, so it is core work and not arms.
#
# 14 has never appeared in Jake's data, so it is deliberately ABSENT rather than
# guessed at: an unmapped id renders as "part 14", which is honest, where a
# wrong muscle name would silently misattribute his training for months. Same
# for any id Speediance adds later.
TRAINING_PARTS = {
    11: "Chest",
    12: "Shoulders",
    13: "Back",
    15: "Legs",
    16: "Arms",
    17: "Core",
}


def part_name(pid: Any) -> str:
    """A muscle name for a trainingPartId2, or 'part <id>' when unknown."""
    try:
        return TRAINING_PARTS.get(int(pid)) or f"part {int(pid)}"
    except (TypeError, ValueError):
        return "part ?"


def _norm_parts(lst: Any) -> dict[str, int]:
    """`trainingPartSetsInfoList` → {muscle name: sets}.

    This arrives on the LIST response, on every sync, already rolled up by
    Speediance — and was thrown away for months by a hardcoded `{}` under a
    comment claiming it was "not present at list level; detail carries
    per-exercise data". It is present at list level. The consequence was not
    a wrong number but a missing feature: `WorkoutStore.stats()` builds its
    muscle distribution by summing this dict, and its one consumer — the
    `workout_summary` tool's "Most-worked:" clause — is written `if muscles`,
    so it had rendered nothing, ever. 39 of 39 stored sessions held the same
    `'{}'`, one distinct value. (The Workouts tab fetches the same stats
    endpoint but has never read `muscles` at all; that is a separate gap, and
    a UI one.)

    Ids with no known name keep their number rather than borrowing a
    neighbour's; see TRAINING_PARTS.

    An empty result is not always a failure. A freestyle `Free Lift` (type 1)
    omits the key entirely — verified against the raw list payload, 2026-09-18,
    where 17 of Jake's 19 sessions carried parts and the two that did not were
    both Free Lifts. That is the same "totals only" shape `session_detail`
    documents, so a blank on those is Speediance's answer, not a dropped field.
    """
    out: dict[str, int] = {}
    if not isinstance(lst, list):
        return out
    for p in lst:
        if not isinstance(p, dict):
            continue
        sets = int(_num(p.get("sets")))
        if sets <= 0:
            continue
        # Two entries can map to one name (an unknown id, or a future alias),
        # so accumulate rather than assign.
        name = part_name(p.get("trainingPartId2"))
        out[name] = out.get(name, 0) + sets
    return out


# The spoofed device fingerprint the app sends, byte-for-byte. Declared once.
_MOBILE_DEVICES = ('{"brand":"google","device":"emulator64","deviceType":'
                   '"sdk_gphone64","os":"","os_version":"31","manufacturer":"Google"}')
_CODE_TOKEN_EXPIRED = 91


class SpeedianceError(RuntimeError):
    """Any failure talking to Speediance — auth, network, or an error payload."""


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _epoch_s(v: Any) -> Optional[float]:
    """Normalise a timestamp to epoch seconds; values > 1e12 are milliseconds."""
    n = _num(v)
    if n <= 0:
        return None
    return n / 1000.0 if n > 1e12 else n


class SpeedianceClient:
    def __init__(self, email: str, password: str, region: str = "EU",
                 *, timeout: float = 30.0) -> None:
        if not email or not password:
            raise SpeedianceError("Speediance credentials are not configured")
        self._email = email
        self._password = password
        self._base = REGION_HOSTS.get((region or "GLOBAL").upper(), _DEFAULT_BASE)
        self._timeout = timeout
        self._token: Optional[str] = None
        self._user_id: Optional[str] = None

    # ---- transport ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {
            "User-Agent": "Dart/3.9 (dart:io)",
            "Content-Type": "application/json",
            "Timestamp": str(int(time.time() * 1000)),
            "Utc_offset": "+0000",
            "Timezone": "GMT",
            "Versioncode": "40304",
            "Accept-Language": "en",
            "App_type": "SOFTWARE",
            "Mobiledevices": _MOBILE_DEVICES,
        }
        if self._token:
            h["Token"] = self._token
        if self._user_id:
            h["App_user_id"] = str(self._user_id)
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 *, _retry: bool = True) -> dict:
        url = self._base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise SpeedianceError(f"{path} → HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            # Never let a network error carry the credential; report the path only.
            reason = getattr(exc, "reason", exc)
            raise SpeedianceError(f"{path} unreachable: {reason}") from exc
        try:
            env = json.loads(raw)
        except ValueError as exc:
            raise SpeedianceError(f"{path} → non-JSON response") from exc
        code = env.get("code", 0)
        # An expired token: re-login once, then replay the request.
        if code == _CODE_TOKEN_EXPIRED and _retry and path not in (_EP_VERIFY, _EP_LOGIN):
            self._token = None
            self.login()
            return self._request(method, path, body, _retry=False)
        if code not in (0, None):
            msg = env.get("message") or "unknown error"
            raise SpeedianceError(f"{path} → {msg} (code {code})")
        return env

    def _get(self, path: str) -> Any:
        return self._request("GET", path).get("data")

    # ---- auth ---------------------------------------------------------------
    def login(self) -> None:
        """Two-step email/password login: verifyIdentity (account exists + has a
        password) → byPass (returns the token + user id)."""
        env = self._request("POST", _EP_VERIFY,
                            {"type": 2, "userIdentity": self._email})
        v = env.get("data") or {}
        if not v.get("isExist", True):
            raise SpeedianceError("account does not exist — register in the Speediance app")
        if not v.get("hasPwd", True):
            raise SpeedianceError("no password set — set one in the Speediance app (SSO account)")
        env = self._request("POST", _EP_LOGIN,
                            {"userIdentity": self._email, "password": self._password, "type": 2})
        d = env.get("data") or {}
        token = d.get("token")
        if not token:
            raise SpeedianceError("login returned no token")
        self._token = str(token)
        uid = d.get("appUserId")
        self._user_id = str(uid) if uid is not None else None

    def _ensure(self) -> None:
        if not self._token:
            self.login()

    # ---- reads --------------------------------------------------------------
    def list_sessions(self, days: int = 365) -> list[dict]:
        """Completed sessions over the last `days`, newest first. List-level only
        (title, timestamps, duration, calories, volume, type) — per-set detail
        needs `session_detail`. The window is [today+1-days, today+1) so today is
        always included, matching upstream."""
        self._ensure()
        today = time.localtime()
        end_s = time.mktime((today.tm_year, today.tm_mon, today.tm_mday + 1,
                             0, 0, 0, 0, 0, -1))
        start_s = end_s - int(days) * 86400
        path = (f"{_EP_RECORDS}?startDate={time.strftime('%Y-%m-%d', time.localtime(start_s))}"
                f"&endDate={time.strftime('%Y-%m-%d', time.localtime(end_s))}")
        records = self._get(path)
        rows = records if isinstance(records, list) else []
        out = [self._norm_session(r) for r in rows if isinstance(r, dict)]
        out = [s for s in out if s.get("training_id")]
        out.sort(key=lambda s: s.get("start_ts") or 0, reverse=True)
        return out

    def session_detail(self, training_id: str, session_type: int = 0) -> dict:
        """One session's per-set detail. Probes the right namespace by type (5 =
        program), falling back to the other so a misclassification still resolves.
        Returns {training_id, kind, sets:[...]}. sets is [] for a freestyle Free
        Lift session (which reports totals only)."""
        self._ensure()
        tid = str(training_id)
        order = ((_EP_PROGRAM_DETAIL, "program"), (_EP_FREE_DETAIL, "free"))
        if session_type and session_type != 5:
            order = order[::-1]
        for path, kind in order:
            # One namespace erroring (a 403/404 for an id that only lives in the
            # other) must not abort the probe — treat it as "no data here" and
            # fall through to the other namespace.
            try:
                detail = self._get(path + tid)
            except SpeedianceError as exc:
                log.debug("%s detail failed for %s: %s", kind, tid, exc)
                continue
            sets = self._norm_sets(detail)
            if sets:
                return {"training_id": tid, "kind": kind, "sets": sets}
        return {"training_id": tid, "kind": "", "sets": []}

    # ---- normalisers (isolate the live response shape) ----------------------
    @staticmethod
    def _norm_session(r: dict) -> dict:
        tid = r.get("trainingId") or r.get("id")
        title = r.get("title") or "Workout"
        wtype = r.get("courseTypeStr") or r.get("courseCategoryName") or ""
        return {
            "training_id": str(tid) if tid else "",
            "title": title,
            "workout_type": wtype,
            "start_ts": _epoch_s(r.get("startTimestamp")),
            "end_ts": _epoch_s(r.get("endTimestamp")),
            "duration_s": int(_num(r.get("trainingTime"))),
            "calories": _num(r.get("calorie")),
            "total_volume": _num(r.get("totalCapacity")),
            "total_energy": _num(r.get("totalEnergy")),
            "device_type": int(_num(r.get("deviceType"))),
            "session_type": int(_num(r.get("type"))),
            "muscles": _norm_parts(r.get("trainingPartSetsInfoList")),
        }

    @staticmethod
    def _norm_sets(detail: Any) -> list[dict]:
        """Flatten a program session's exercises→finishedReps into set rows. Each
        exercise is {actionLibraryName, maxWeight, finishedReps:[{finishedCount,
        capacity, trainingInfoDetail:{weights:[...]}}]}. A set's representative
        weight is the top per-rep weight (handles drop-sets), falling back to the
        exercise's maxWeight."""
        out: list[dict] = []
        if not isinstance(detail, list):
            return out
        for ex in detail:
            if not isinstance(ex, dict):
                continue
            ex_name = ex.get("actionLibraryName") or ex.get("name") or "Exercise"
            ex_max = _num(ex.get("maxWeight"))
            reps_list = ex.get("finishedReps")
            if not isinstance(reps_list, list):
                continue
            for i, st in enumerate(reps_list):
                if not isinstance(st, dict):
                    continue
                reps = int(_num(st.get("finishedCount") or st.get("targetCount")))
                weights = (st.get("trainingInfoDetail") or {}).get("weights") \
                    if isinstance(st.get("trainingInfoDetail"), dict) else None
                if isinstance(weights, list) and weights:
                    weight = max(_num(w) for w in weights)
                else:
                    weight = ex_max
                out.append({
                    "exercise_id": str(ex.get("actionLibraryId") or ""),
                    "exercise_name": str(ex_name),
                    "set_index": i,
                    "reps": reps,
                    "weight_kg": round(weight, 2),
                })
        return out
