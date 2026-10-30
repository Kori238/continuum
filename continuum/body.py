"""Body — Jake's physical metrics: weight, height, daily steps, and the energy
he burns.

The third leg of the fitness picture. `nutrition.py` records energy *in* (food),
`workouts.py` records the energy *out* of training; this store holds the body
those numbers are about (weight/height → BMI) and the *other* half of energy-out
— everyday movement (steps). Put together with the workout store it answers "how
much did I burn today", and put together with the nutrition store it answers
"what's my energy balance".

One SQLite file (`data/body.db`), two tables, same principles as the rest of
`data/`: it's Jake's own data (browsed and queried, never injected into a turn),
day-keyed on the LOCAL calendar day so it resets at local midnight with no job.

  * **`measurements`** — one row per day he weighed/measured himself
    (`weight_kg`, `height_cm`, and from a smart scale like the Renpho:
    `body_fat_pct`, `muscle_mass_kg`). Height barely changes, so a day with a
    weight but no fresh height forward-fills the most recent height on/before it —
    you only ever have to type your height once. Body composition is a spot
    reading, so unlike height it does NOT forward-fill (each day is that day's
    scale reading). BMI and fat mass are derived, never stored.
  * **`steps`** — one row per day, the day's step count and where it came from
    (`source`: `manual` from the tab, `google`/`phone` once a health feed pushes
    them). A health source reports the running daily total, so a new report for a
    day REPLACES it rather than adding — latest wins.

Analytics (BMI, the calories a step count burns) live here so the `body_*` tools
and the non-AI Body tab agree — the same single-source-of-analytics rule the
workout, activity and nutrition stores follow. Expenditure that needs *another*
store (workout calories) and balance (vs food intake) are composed by the caller
from this store's pieces, because there is no cross-store transaction in this
system (see docs/DATA.md).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.body")

# Energy a single step costs, at a 70 kg reference body, scaled linearly by
# weight. ~0.04 kcal/step at 70 kg → ~400 kcal for 10k steps, the usual
# rule-of-thumb. Deliberately simple: this is an everyday-movement estimate, not
# a lab measurement, and it's labelled as such in the UI.
_KCAL_PER_STEP_AT_70KG = 0.04
_REF_WEIGHT_KG = 70.0


def _local_day(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _fnum(v: Any) -> Optional[float]:
    """A positive float, or None — a blank/0/garbage measurement is 'not set',
    not a real zero (nobody weighs 0 kg)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f > 0 else None


def bmi(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[dict[str, Any]]:
    """Body Mass Index + WHO category, or None if either input is missing."""
    if not weight_kg or not height_cm or height_cm <= 0:
        return None
    m = height_cm / 100.0
    value = round(weight_kg / (m * m), 1)
    if value < 18.5:
        cat = "underweight"
    elif value < 25:
        cat = "normal"
    elif value < 30:
        cat = "overweight"
    else:
        cat = "obese"
    return {"value": value, "category": cat}


def steps_kcal(steps: int, weight_kg: Optional[float] = None) -> int:
    """Rough calories burned walking `steps`, scaled by weight (70 kg if unknown)."""
    if not steps or steps <= 0:
        return 0
    w = weight_kg if (weight_kg and weight_kg > 0) else _REF_WEIGHT_KG
    return round(steps * _KCAL_PER_STEP_AT_70KG * (w / _REF_WEIGHT_KG))


class BodyStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.body_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS measurements(
                day TEXT PRIMARY KEY,
                weight_kg REAL, height_cm REAL,
                body_fat_pct REAL, muscle_mass_kg REAL,
                ts REAL)"""
        )
        # Self-migrate older DBs that predate the smart-scale composition columns
        # (same PRAGMA-table_info + ALTER pattern the rest of data/ uses).
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(measurements)")}
        for col in ("body_fat_pct", "muscle_mass_kg"):
            if col not in have:
                self._db.execute(f"ALTER TABLE measurements ADD COLUMN {col} REAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS steps(
                day TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0,
                source TEXT DEFAULT 'manual',
                ts REAL)"""
        )
        self._db.commit()

    # ---- write --------------------------------------------------------------
    def set_measurement(self, *, weight_kg: Any = None, height_cm: Any = None,
                        body_fat_pct: Any = None, muscle_mass_kg: Any = None,
                        day: Optional[str] = None) -> dict[str, Any]:
        """Record a weight and/or height (and, from a smart scale, body-fat % and
        muscle mass) for a day (today if omitted). Only the fields you pass are
        touched — logging just a weight keeps the day's (or the forward-filled)
        height and any composition already recorded. Returns the day's metrics."""
        day = (day or "").strip() or _local_day(time.time())
        w, h = _fnum(weight_kg), _fnum(height_cm)
        bf, mm = _fnum(body_fat_pct), _fnum(muscle_mass_kg)
        if w is None and h is None and bf is None and mm is None:
            return self.day(day)
        # COALESCE so a partial update preserves the other stored fields — a
        # weight-only log never nulls the day's height or composition, and a
        # scale sending composition never nulls a weight typed in earlier.
        self._db.execute(
            """INSERT INTO measurements(day, weight_kg, height_cm,
                                        body_fat_pct, muscle_mass_kg, ts)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                    weight_kg=COALESCE(excluded.weight_kg, measurements.weight_kg),
                    height_cm=COALESCE(excluded.height_cm, measurements.height_cm),
                    body_fat_pct=COALESCE(excluded.body_fat_pct, measurements.body_fat_pct),
                    muscle_mass_kg=COALESCE(excluded.muscle_mass_kg, measurements.muscle_mass_kg),
                    ts=excluded.ts""",
            (day, w, h, bf, mm, time.time()))
        self._db.commit()
        return self.day(day)

    def set_steps(self, count: Any, *, day: Optional[str] = None,
                  source: str = "manual") -> dict[str, Any]:
        """Set a day's step count (today if omitted). A health source reports the
        running daily total, so this REPLACES the day's count — latest wins."""
        day = (day or "").strip() or _local_day(time.time())
        try:
            n = max(0, int(count))
        except (TypeError, ValueError):
            n = 0
        self._db.execute(
            """INSERT INTO steps(day, count, source, ts) VALUES(?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                    count=excluded.count, source=excluded.source, ts=excluded.ts""",
            (day, n, (source or "manual").strip() or "manual", time.time()))
        self._db.commit()
        return self.day(day)

    # ---- read ---------------------------------------------------------------
    def _weight_on(self, day: str) -> Optional[float]:
        r = self._db.execute(
            "SELECT weight_kg FROM measurements WHERE day=?", (day,)).fetchone()
        return r["weight_kg"] if r and r["weight_kg"] else None

    def _height_on(self, day: str) -> Optional[float]:
        """The day's own height, else the most recent height recorded on/before it
        (height forward-fills — you type it once)."""
        r = self._db.execute(
            "SELECT height_cm FROM measurements WHERE day<=? AND height_cm IS NOT NULL "
            "ORDER BY day DESC LIMIT 1", (day,)).fetchone()
        return r["height_cm"] if r and r["height_cm"] else None

    def _composition_on(self, day: str) -> tuple[Optional[float], Optional[float]]:
        """The day's own body-fat % and muscle mass (a smart-scale reading is a
        spot value for that day — unlike height it does NOT forward-fill)."""
        r = self._db.execute(
            "SELECT body_fat_pct, muscle_mass_kg FROM measurements WHERE day=?",
            (day,)).fetchone()
        if not r:
            return (None, None)
        return (r["body_fat_pct"] or None, r["muscle_mass_kg"] or None)

    def latest_weight(self) -> Optional[float]:
        r = self._db.execute(
            "SELECT weight_kg FROM measurements WHERE weight_kg IS NOT NULL "
            "ORDER BY day DESC LIMIT 1").fetchone()
        return r["weight_kg"] if r and r["weight_kg"] else None

    def _latest(self, column: str) -> Optional[float]:
        r = self._db.execute(
            f"SELECT {column} FROM measurements WHERE {column} IS NOT NULL "
            "ORDER BY day DESC LIMIT 1").fetchone()
        return r[column] if r and r[column] else None

    def latest_height(self) -> Optional[float]:
        r = self._db.execute(
            "SELECT height_cm FROM measurements WHERE height_cm IS NOT NULL "
            "ORDER BY day DESC LIMIT 1").fetchone()
        return r["height_cm"] if r and r["height_cm"] else None

    def _steps_on(self, day: str) -> tuple[int, str]:
        r = self._db.execute(
            "SELECT count, source FROM steps WHERE day=?", (day,)).fetchone()
        return (int(r["count"] or 0), r["source"] or "manual") if r else (0, "")

    def day(self, date: Optional[str] = None) -> dict[str, Any]:
        """One day's body metrics — weight, forward-filled height, BMI, steps and
        the calories those steps burned. Expenditure that needs the workout store,
        and balance vs food intake, are composed by the caller."""
        date = (date or "").strip() or _local_day(time.time())
        weight = self._weight_on(date)
        # Fall back to the last known weight so step-calories and BMI still work
        # on a day he didn't re-weigh; flag whether it was measured that day.
        eff_weight = weight if weight else self.latest_weight()
        # Height barely changes, so a day before you first recorded it still gets
        # a BMI from your latest known height rather than a blank.
        height = self._height_on(date) or self.latest_height()
        count, source = self._steps_on(date)
        body_fat, muscle = self._composition_on(date)
        # Fall back to the last known composition (like weight) so a day he didn't
        # step on the scale still shows a figure, flagged whether it was measured.
        eff_fat = body_fat if body_fat is not None else self._latest("body_fat_pct")
        eff_muscle = muscle if muscle is not None else self._latest("muscle_mass_kg")
        # Fat mass is derived from the effective weight and body-fat %, never
        # stored — the same rule BMI follows (a scale gives the %, not the mass).
        fat_mass = round(eff_weight * eff_fat / 100.0, 1) \
            if (eff_weight and eff_fat is not None) else None
        return {
            "date": date,
            "weight_kg": weight,
            "weight_effective_kg": eff_weight,
            "height_cm": height,
            "bmi": bmi(eff_weight, height),
            "body_fat_pct": body_fat,
            "body_fat_effective_pct": eff_fat,
            "muscle_mass_kg": muscle,
            "muscle_mass_effective_kg": eff_muscle,
            "fat_mass_kg": fat_mass,
            "steps": count,
            "steps_source": source,
            "steps_kcal": steps_kcal(count, eff_weight),
        }

    def summary(self, days: int = 30) -> dict[str, Any]:
        """Dense per-day series (oldest→newest) of weight, BMI and steps over the
        window — drives the Body tab's trends. Empty days show as gaps."""
        days = max(1, min(int(days or 30), 366))
        series = []
        latest_h = self.latest_height()
        for i in range(days - 1, -1, -1):
            d = _local_day(time.time() - i * 86400)
            w = self._weight_on(d)
            h = self._height_on(d) or latest_h
            count, _ = self._steps_on(d)
            bf, _mm = self._composition_on(d)
            b = bmi(w, h)
            series.append({"date": d, "weight_kg": w,
                           "bmi": b["value"] if b else None,
                           "body_fat_pct": bf,
                           "steps": count})
        weights = [s["weight_kg"] for s in series if s["weight_kg"]]
        stepped = [s["steps"] for s in series if s["steps"]]
        return {
            "window_days": days,
            "series": series,
            "latest_weight": self.latest_weight(),
            "latest_height": self.latest_height(),
            "latest_body_fat": self._latest("body_fat_pct"),
            "latest_muscle_mass": self._latest("muscle_mass_kg"),
            "avg_steps": round(sum(stepped) / len(stepped)) if stepped else 0,
            "logged_weights": len(weights),
        }
