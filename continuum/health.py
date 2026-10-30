"""Health — Jake's recovery and sleep picture: the metrics a wrist wearable
produces that no other store holds.

The fourth leg of the fitness picture. `nutrition.py` is energy *in*,
`workouts.py` + the steps half of `body.py` are energy *out*, and `body.py`
holds the body itself (weight → BMI). This store holds how that body is *doing*
day to day — how it slept and how recovered it is: sleep duration and stages,
resting heart rate, HRV, blood-oxygen saturation and respiratory rate.

It's built for the Fitbit Air (arriving 2026-08-25) but is wearable-agnostic:
the feed is the phone's Health Connect reader (`android/…/HealthConnect.kt`),
which reads whatever wrote to Health Connect — Fitbit, Garmin, Samsung — and
reports one day's aggregate over the phone link, which `_phone_health` in
`app/server.py` upserts here. It also takes manual entries from the tab / tools.

One SQLite file (`data/health.db`), one table `days`, same principles as the
rest of `data/`: it's Jake's own data (browsed and queried, never injected into
a turn), day-keyed on the LOCAL calendar day so it resets at midnight with no
job. A night that spans midnight is attributed to the **wake** day — the day you
get up is the day the sleep counts for, matching how every wearable reports it.

**Readiness is derived here, never stored** — the same rule `body.py` follows
for BMI. Fitbit's own "Daily Readiness" is a Premium, in-app score that is never
written to Health Connect, so it can't be ingested; instead this store computes
its own recovery score from the raw signals it *does* get (sleep vs an 8h
target, HRV and resting-HR each vs their own trailing baseline). It's a
deliberately simple heuristic — labelled as such wherever it surfaces — not a
clinical figure, exactly like the walking-calorie estimate in `body.py`.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.health")

# Trailing window (days) that HRV and resting HR are compared against to judge a
# day as recovered or not. Two weeks smooths day-to-day noise without lagging so
# far that a genuine trend never moves the baseline.
_BASELINE_DAYS = 14
# A full night. Readiness gives sleep full marks at this and scales down below;
# 8h is the usual adult target and keeps the score legible.
_SLEEP_TARGET_MIN = 480
# Weights of the three readiness components, over whichever are present that day.
_W_SLEEP, _W_HRV, _W_RHR = 0.5, 0.3, 0.2

# The columns a day can carry, all optional — a feed or a manual entry sets
# whatever it has and COALESCE preserves the rest. Kept as one list so the
# upsert, the migration and the readers stay in step.
_METRIC_COLS = (
    "asleep_min", "in_bed_min", "deep_min", "rem_min", "light_min", "awake_min",
    "sleep_start_ts", "sleep_end_ts",
    "resting_hr", "hrv_ms", "spo2", "respiratory_rate", "skin_temp_delta",
)
_INT_COLS = {"asleep_min", "in_bed_min", "deep_min", "rem_min", "light_min",
             "awake_min", "resting_hr"}


def _local_day(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _fnum(v: Any) -> Optional[float]:
    """A positive float, or None — a blank/0/garbage reading is 'not set', not a
    real zero (nobody has a resting heart rate of 0)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f > 0 else None


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _component(ratio: float) -> float:
    """Map a value/baseline ratio to a 0–100 component. At baseline (ratio 1.0)
    → 75; every 10% above adds 15, every 10% below subtracts 15, clamped. So a
    day matching your norm reads 'fine', a clearly better one approaches 100 and
    a clearly worse one falls away — a legible curve, not a cliff."""
    return round(_clamp(75 + (ratio - 1.0) * 150))


def _hms(minutes: Optional[int]) -> str:
    """'7h 20m' from a minute count; '' when unknown."""
    if not minutes or minutes <= 0:
        return ""
    h, m = divmod(int(minutes), 60)
    return (f"{h}h {m}m" if h else f"{m}m")


class HealthStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.health_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        cols = ",\n                ".join(
            f"{c} {'INTEGER' if c in _INT_COLS else 'REAL'}" for c in _METRIC_COLS)
        self._db.execute(
            f"""CREATE TABLE IF NOT EXISTS days(
                day TEXT PRIMARY KEY,
                {cols},
                source TEXT DEFAULT 'manual',
                ts REAL)"""
        )
        self._db.commit()

    # ---- write --------------------------------------------------------------
    def upsert_day(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record whatever metrics `payload` carries for a day (today if the
        `day`/`date` field is absent). Only the fields present are written;
        COALESCE preserves the rest, so a resting-HR-only report never wipes the
        night's sleep and vice-versa — the same partial-update rule body.py uses.
        `source` records where it came from (e.g. 'fitbit', 'phone', 'manual')."""
        day = str(payload.get("day") or payload.get("date") or "").strip() \
            or _local_day(time.time())
        vals: dict[str, Any] = {}
        for c in _METRIC_COLS:
            if c not in payload:
                continue
            n = _fnum(payload[c])
            vals[c] = int(n) if (n is not None and c in _INT_COLS) else n
        if not vals:
            return self.day(day)
        source = str(payload.get("source") or "manual").strip() or "manual"
        cols = list(vals.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        updates = ", ".join(f"{c}=COALESCE(excluded.{c}, days.{c})" for c in cols)
        self._db.execute(
            f"""INSERT INTO days(day, {col_list}, source, ts)
                VALUES(?, {placeholders}, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    {updates}, source=excluded.source, ts=excluded.ts""",
            (day, *[vals[c] for c in cols], source, time.time()))
        self._db.commit()
        return self.day(day)

    # ---- read ---------------------------------------------------------------
    def _row(self, day: str) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM days WHERE day=?", (day,)).fetchone()

    def _baseline(self, col: str, before_day: str, days: int = _BASELINE_DAYS) -> Optional[float]:
        """Average of `col` over the `days` days strictly before `before_day`
        (the day itself is excluded so it's judged against its own history, not
        against itself). None when there isn't enough to be meaningful."""
        rows = self._db.execute(
            f"SELECT {col} FROM days WHERE day<? AND {col} IS NOT NULL "
            "ORDER BY day DESC LIMIT ?", (before_day, days)).fetchall()
        vals = [r[col] for r in rows if r[col]]
        if len(vals) < 3:   # too few points to trust a baseline
            return None
        return sum(vals) / len(vals)

    def readiness(self, row: Any, *, hrv_base: Optional[float] = None,
                  rhr_base: Optional[float] = None) -> Optional[dict[str, Any]]:
        """A 0–100 recovery score for a day's metrics, plus a category and the
        components that fed it. A HEURISTIC (see module docstring): sleep vs an
        8h target, HRV and resting HR each vs their trailing baseline, weighted
        over whichever are present. None when the day has no usable signal."""
        if row is None:
            return None
        comps: dict[str, float] = {}
        asleep = row["asleep_min"] if row["asleep_min"] else None
        if asleep:
            comps["sleep"] = round(_clamp(asleep / _SLEEP_TARGET_MIN * 100))
        if row["hrv_ms"] and hrv_base:
            comps["hrv"] = _component(row["hrv_ms"] / hrv_base)
        # Resting HR: lower than baseline is better, so invert the ratio.
        if row["resting_hr"] and rhr_base:
            comps["rhr"] = _component(rhr_base / row["resting_hr"])
        if not comps:
            return None
        weights = {"sleep": _W_SLEEP, "hrv": _W_HRV, "rhr": _W_RHR}
        wsum = sum(weights[k] for k in comps)
        score = round(sum(comps[k] * weights[k] for k in comps) / wsum)
        cat = "low" if score < 40 else ("moderate" if score < 70 else "high")
        return {"score": score, "category": cat, "components": comps,
                "heuristic": True}

    def day(self, date: Optional[str] = None) -> dict[str, Any]:
        """One day's recovery snapshot — sleep (total, stages, efficiency),
        resting HR, HRV, SpO2, respiratory rate, and the derived readiness score
        with its baselines. Store-only: it doesn't reach into other stores."""
        date = (date or "").strip() or _local_day(time.time())
        row = self._row(date)
        hrv_base = self._baseline("hrv_ms", date)
        rhr_base = self._baseline("resting_hr", date)
        out: dict[str, Any] = {"date": date, "has_data": row is not None}
        for c in _METRIC_COLS:
            out[c] = row[c] if row else None
        out["source"] = row["source"] if row else None
        # Sleep efficiency: fraction of time in bed actually asleep.
        eff = None
        if row and row["asleep_min"] and row["in_bed_min"]:
            eff = round(row["asleep_min"] / row["in_bed_min"] * 100)
        out["sleep_efficiency"] = eff
        out["asleep_label"] = _hms(row["asleep_min"]) if row else ""
        out["hrv_baseline"] = round(hrv_base, 1) if hrv_base else None
        out["resting_hr_baseline"] = round(rhr_base) if rhr_base else None
        out["readiness"] = self.readiness(row, hrv_base=hrv_base, rhr_base=rhr_base)
        return out

    def summary(self, days: int = 30) -> dict[str, Any]:
        """Dense per-day series (oldest→newest) of the headline metrics plus
        readiness over the window — drives the Health tab's trends and weekly
        reports. Empty days show as gaps. Averages are over only the logged days."""
        days = max(1, min(int(days or 30), 366))
        series = []
        for i in range(days - 1, -1, -1):
            d = _local_day(time.time() - i * 86400)
            row = self._row(d)
            r = self.readiness(row, hrv_base=self._baseline("hrv_ms", d),
                               rhr_base=self._baseline("resting_hr", d)) if row else None
            series.append({
                "date": d,
                "asleep_min": row["asleep_min"] if row else None,
                "resting_hr": row["resting_hr"] if row else None,
                "hrv_ms": round(row["hrv_ms"], 1) if row and row["hrv_ms"] else None,
                "spo2": round(row["spo2"], 1) if row and row["spo2"] else None,
                "readiness": r["score"] if r else None,
            })

        def _avg(key: str, rnd: int = 0) -> Optional[float]:
            vals = [s[key] for s in series if s[key] is not None]
            if not vals:
                return None
            a = sum(vals) / len(vals)
            return round(a, rnd) if rnd else round(a)

        logged = [s for s in series if s["asleep_min"] or s["resting_hr"] or s["hrv_ms"]]
        return {
            "window_days": days,
            "series": series,
            "logged_days": len(logged),
            "avg_asleep_min": _avg("asleep_min"),
            "avg_resting_hr": _avg("resting_hr"),
            "avg_hrv_ms": _avg("hrv_ms", 1),
            "avg_spo2": _avg("spo2", 1),
            "avg_readiness": _avg("readiness"),
        }
