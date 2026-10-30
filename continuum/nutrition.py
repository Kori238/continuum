"""Nutrition — Jake's calorie and macro intake log.

The other side of the fitness picture from `workouts.py`: that store records
energy *out* (training); this one records energy *in* (food). One SQLite file
(`data/nutrition.db`), three tables, and the same principle as the rest of `data/`
— it is pure observation Jake (or Continuum, logging for him) writes, browsed and
queried but never injected into a turn.

  * **`entries`** — one row per logged food. Each carries its own macros
    (calories, protein, carbs, fat) already computed for the amount eaten, plus
    the `qty`/`unit` and optional `fdc_id` it came from, so the log is
    self-contained: it never has to re-query the food database to total a day,
    and an entry stays correct even if the database later changes. `day` is the
    LOCAL calendar day (YYYY-MM-DD) derived from `ts`, so day totals and the
    Nutrition tab reset at local midnight with no job to run — the same
    date-keyed trick the routine store uses.
  * **`goals`** — a single row (id=1) of daily targets. Optional; absent targets
    read as 0, which the UI renders as "no goal set" rather than a 0 kcal cap.
  * **`meals`** — saved, reusable meals (name + PER-PORTION macros, a `servings`
    count and an optional `ingredients` recipe). `add_meal` sums an ingredient
    recipe's macros and divides by `servings`; `log_meal` scales the per-portion
    macros by a (fractional) portion count and writes an ordinary `entries` row.

Totals are computed here (`day`, `summary`) so the `nutrition_*` tools and the
non-AI Nutrition tab always agree — the same single-source-of-analytics rule as
the workout and activity stores.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.nutrition")

# The meals a day groups into, in the order a day runs. Anything else (or blank)
# falls under "other". Kept small on purpose — this is a food log, not a diary.
MEALS = ("breakfast", "lunch", "dinner", "snack")


def _local_day(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _num(v: Any) -> float:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return 0.0


def _num_strict(v: Any, field: str = "value") -> float:
    """Parse a caller-supplied number, refusing garbage instead of coercing it.

    `_num` maps anything unparseable to 0.0, which is fine for summing macros
    that are already stored, but wrong at the edge of the system: a typo in a
    goal or a bad client payload would silently become a zero *target* rather
    than an error. Raises ValueError so the caller can reject the request.
    """
    if isinstance(v, bool):  # float(True) == 1.0 — almost never what was meant
        raise ValueError(f"{field} must be a number, not a boolean")
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {v!r}") from None


class NutritionStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.nutrition_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, day TEXT, name TEXT,
                meal TEXT DEFAULT '',
                qty REAL DEFAULT 0, unit TEXT DEFAULT '',
                calories REAL DEFAULT 0, protein_g REAL DEFAULT 0,
                carbs_g REAL DEFAULT 0, fat_g REAL DEFAULT 0,
                source TEXT DEFAULT 'manual', fdc_id INTEGER,
                created REAL)"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_entries_day ON entries(day)")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS goals(
                id INTEGER PRIMARY KEY CHECK(id=1),
                calories REAL DEFAULT 0, protein_g REAL DEFAULT 0,
                carbs_g REAL DEFAULT 0, fat_g REAL DEFAULT 0,
                updated REAL)"""
        )
        # Migration: an opt-in flag that makes the calorie target flex with the
        # day's active burn (steps + workouts) — the stored `calories` become a
        # sedentary BASE the day's expenditure is added back onto. Older DBs
        # predate the column, so add it in place. The burn itself is composed by
        # the caller (server/tools) via adjust_for_activity(), since the step and
        # workout stores live elsewhere and this store never reaches across.
        goal_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(goals)")}
        if "activity_adjust" not in goal_cols:
            self._db.execute(
                "ALTER TABLE goals ADD COLUMN activity_adjust INTEGER DEFAULT 0")
        # Saved, reusable meals — a name + per-portion macros, optionally built
        # from an `ingredients` recipe (stored as JSON). Logged by the portion.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS meals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                calories REAL DEFAULT 0, protein_g REAL DEFAULT 0,
                carbs_g REAL DEFAULT 0, fat_g REAL DEFAULT 0,
                servings REAL DEFAULT 1,
                ingredients TEXT DEFAULT '',
                meal TEXT DEFAULT '',
                created REAL, updated REAL)"""
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_meals_name ON meals(name COLLATE NOCASE)")
        self._db.commit()

    # ---- write --------------------------------------------------------------
    def log(self, name: str, calories: float = 0, *, protein_g: float = 0,
            carbs_g: float = 0, fat_g: float = 0, qty: float = 0, unit: str = "",
            meal: str = "", ts: Optional[float] = None, source: str = "manual",
            fdc_id: Optional[int] = None) -> dict[str, Any]:
        """Log one food. `calories`/macros are the totals for the amount eaten
        (already scaled — the store doesn't do portion maths, the caller does).
        Returns the stored row."""
        name = (name or "").strip() or "food"
        ts = float(ts) if ts else time.time()
        meal = (meal or "").strip().lower()
        if meal not in MEALS:
            meal = ""
        cur = self._db.execute(
            """INSERT INTO entries(ts, day, name, meal, qty, unit, calories,
                    protein_g, carbs_g, fat_g, source, fdc_id, created)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, _local_day(ts), name, meal, _num(qty), (unit or "").strip(),
             _num(calories), _num(protein_g), _num(carbs_g), _num(fat_g),
             (source or "manual").strip(),
             int(fdc_id) if fdc_id else None, time.time()))
        self._db.commit()
        return self.entry(int(cur.lastrowid))

    def delete(self, entry_id: int) -> bool:
        cur = self._db.execute("DELETE FROM entries WHERE id=?", (int(entry_id),))
        self._db.commit()
        return cur.rowcount > 0

    def set_goals(self, *, calories: Optional[float] = None,
                  protein_g: Optional[float] = None,
                  carbs_g: Optional[float] = None, fat_g: Optional[float] = None,
                  activity_adjust: Optional[bool] = None) -> dict[str, Any]:
        """Update the daily targets. **Every field is a merge field**: None means
        "leave this one alone", so a partial update — tuning one macro, a client
        that only sends what changed, a half-filled form — can't silently zero
        the rest or switch adaptive calories off. Pass an explicit 0 to clear a
        target on purpose. Raises ValueError on a non-numeric value rather than
        coercing it to 0, which would read as "clear this target".
        """
        cur = self._db.execute(
            "SELECT calories, protein_g, carbs_g, fat_g, activity_adjust "
            "FROM goals WHERE id=1").fetchone()

        def merge(new: Any, col: str) -> float:
            if new is None:
                return _num(cur[col]) if cur else 0.0
            return _num_strict(new, col)

        # Parse every macro before writing anything, so a bad third field can't
        # leave the first two applied.
        values = (merge(calories, "calories"), merge(protein_g, "protein_g"),
                  merge(carbs_g, "carbs_g"), merge(fat_g, "fat_g"))
        if activity_adjust is None:
            adj = int(cur["activity_adjust"]) if (cur and cur["activity_adjust"]) else 0
        else:
            adj = 1 if activity_adjust else 0
        self._db.execute(
            """INSERT INTO goals(id, calories, protein_g, carbs_g, fat_g,
                    activity_adjust, updated)
               VALUES(1,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                    calories=excluded.calories, protein_g=excluded.protein_g,
                    carbs_g=excluded.carbs_g, fat_g=excluded.fat_g,
                    activity_adjust=excluded.activity_adjust,
                    updated=excluded.updated""",
            (*values, adj, time.time()))
        self._db.commit()
        return self.goals()

    # ---- meals (saved, reusable) --------------------------------------------
    # A "meal" is a reusable definition Jake (or Continuum) saves once and logs
    # by the portion. Its stored macros are PER PORTION. It can be defined two
    # ways: directly (name + macros), or from `ingredients` — a list of
    # {name, amount, unit, calories, protein_g, carbs_g, fat_g} whose macros are
    # summed to the recipe total, then divided by `servings` to get one portion.
    # The caller (a tool / the tab) supplies each ingredient's macros already
    # scaled to its amount, exactly like log_food — the store only sums them and
    # does the per-portion arithmetic, so it's self-contained like the diary.
    @staticmethod
    def _sum_ingredients(ingredients: list[dict[str, Any]]) -> dict[str, float]:
        tot = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        for ing in ingredients or []:
            if not isinstance(ing, dict):
                continue
            for k in tot:
                try:
                    tot[k] += float(ing.get(k) or 0)
                except (TypeError, ValueError):
                    pass
        return tot

    def add_meal(self, name: str, *, calories: Optional[float] = None,
                 protein_g: Optional[float] = None,
                 carbs_g: Optional[float] = None, fat_g: Optional[float] = None,
                 ingredients: Optional[list[dict[str, Any]]] = None,
                 servings: Optional[float] = None,
                 meal: Optional[str] = None) -> dict[str, Any]:
        """Create or update (by name, case-insensitive) a saved meal. If
        `ingredients` is given, their macros are summed and override the direct
        macros; `servings` (default 1) splits the recipe into portions, so the
        stored macros are always PER PORTION. Returns the stored meal.

        Every field defaults to None meaning "not supplied", and on update an
        unsupplied field is LEFT ALONE. It used to default to 0 / 1 / "", which
        made this an upsert that overwrote with blanks: `add_meal("Chili",
        meal="lunch")` - changing only the meal-time slot, the most ordinary
        edit there is - zeroed the macros, reset servings to 1 and dropped the
        ingredient list. The meal then logged 0 kcal portions into every later
        daily total, which is the failure tools.py already has scar tissue
        about: a wrong stored macro is worse than no stored meal.

        None and 0 are therefore different inputs here, so callers must pass
        `body.get("calories")` and NOT `body.get("calories") or 0` - the `or`
        collapses exactly the distinction this depends on.

        An ingredient list is provenance, not a live formula: it is summed once
        at write time and never re-derived on read. So supplying macros directly
        for a meal that has ingredients updates the macros and leaves the list
        as the record of where the last computed numbers came from."""
        name = (name or "").strip()
        if not name:
            return {}
        now = time.time()
        row = self._db.execute(
            "SELECT * FROM meals WHERE name = ? COLLATE NOCASE", (name,)).fetchone()

        # Unsupplied -> the stored value on update, the documented default on
        # create. Read the old row BEFORE any arithmetic, because `servings`
        # divides the macros and a stale divisor is silently wrong rather than
        # loud.
        def kept(supplied: Any, column: str, default: Any) -> Any:
            if supplied is not None:
                return supplied
            return row[column] if row else default

        servings = kept(servings, "servings", 1)
        try:
            servings = float(servings)
        except (TypeError, ValueError):
            servings = 1.0
        if servings <= 0:
            servings = 1.0

        if ingredients:
            tot = self._sum_ingredients(ingredients)
            calories, protein_g = tot["calories"], tot["protein_g"]
            carbs_g, fat_g = tot["carbs_g"], tot["fat_g"]
            ing_json = json.dumps(ingredients, ensure_ascii=False)
        else:
            ing_json = (row["ingredients"] if row else "") or ""

        # A kept macro is already per-portion; only a freshly supplied one is a
        # recipe total needing the divide.
        def per(v: Any, column: str) -> float:
            if v is None:
                return _num(float((row[column] if row else 0) or 0))
            return _num(float(v or 0) / servings)

        supplied_meal = meal is not None
        meal = (meal or "").strip().lower()
        if meal not in MEALS:
            meal = ""
        if not supplied_meal:
            meal = (row["meal"] if row else "") or ""
        if row:
            self._db.execute(
                """UPDATE meals SET name=?, calories=?, protein_g=?, carbs_g=?,
                        fat_g=?, servings=?, ingredients=?, meal=?, updated=?
                   WHERE id=?""",
                (name, per(calories, "calories"), per(protein_g, "protein_g"),
                 per(carbs_g, "carbs_g"), per(fat_g, "fat_g"),
                 _num(servings), ing_json, meal, now, row["id"]))
            mid = int(row["id"])
        else:
            cur = self._db.execute(
                """INSERT INTO meals(name, calories, protein_g, carbs_g, fat_g,
                        servings, ingredients, meal, created, updated)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (name, per(calories, "calories"), per(protein_g, "protein_g"),
                 per(carbs_g, "carbs_g"), per(fat_g, "fat_g"),
                 _num(servings), ing_json, meal, now, now))
            mid = int(cur.lastrowid)
        self._db.commit()
        return self.meal_by_id(mid)

    @staticmethod
    def _meal_row(r: sqlite3.Row) -> dict[str, Any]:
        ingredients: list[Any] = []
        if r["ingredients"]:
            try:
                ingredients = json.loads(r["ingredients"])
            except (ValueError, TypeError):
                ingredients = []
        return {
            "id": r["id"], "name": r["name"], "calories": r["calories"],
            "protein_g": r["protein_g"], "carbs_g": r["carbs_g"], "fat_g": r["fat_g"],
            "servings": r["servings"], "ingredients": ingredients,
            "meal": r["meal"] or "", "created": r["created"], "updated": r["updated"],
        }

    def meal_by_id(self, meal_id: int) -> dict[str, Any]:
        r = self._db.execute("SELECT * FROM meals WHERE id=?", (int(meal_id),)).fetchone()
        return self._meal_row(r) if r else {}

    def meals(self) -> list[dict[str, Any]]:
        return [self._meal_row(r) for r in self._db.execute(
            "SELECT * FROM meals ORDER BY name COLLATE NOCASE").fetchall()]

    def resolve_meal(self, name: str) -> Optional[dict[str, Any]]:
        """Find a saved meal by name — exact (case-insensitive) first, then a
        unique substring match. Returns None for no match OR an ambiguous one —
        the same rule the routine store's resolve() uses."""
        name = (name or "").strip()
        if not name:
            return None
        r = self._db.execute(
            "SELECT * FROM meals WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if r:
            return self._meal_row(r)
        rows = self._db.execute(
            "SELECT * FROM meals WHERE name LIKE ? COLLATE NOCASE",
            (f"%{name}%",)).fetchall()
        return self._meal_row(rows[0]) if len(rows) == 1 else None

    def delete_meal(self, meal_id: int) -> bool:
        cur = self._db.execute("DELETE FROM meals WHERE id=?", (int(meal_id),))
        self._db.commit()
        return cur.rowcount > 0

    def log_meal(self, name: str, portions: float = 1, *, meal: str = "",
                 ts: Optional[float] = None, source: str = "manual") -> dict[str, Any]:
        """Log a saved meal by a (possibly fractional) number of portions —
        scales the meal's per-portion macros and writes a normal diary entry.
        Returns {meal, portions, entry}; {} if the meal isn't found."""
        m = self.resolve_meal(name)
        if not m:
            return {}
        try:
            portions = float(portions)
        except (TypeError, ValueError):
            portions = 1.0
        if portions <= 0:
            portions = 1.0
        use_meal = (meal or "").strip().lower()
        if use_meal not in MEALS:
            use_meal = m.get("meal") or ""
        label = m["name"] + (f" x{portions:g}" if portions != 1 else "")
        entry = self.log(
            label, m["calories"] * portions, protein_g=m["protein_g"] * portions,
            carbs_g=m["carbs_g"] * portions, fat_g=m["fat_g"] * portions,
            qty=portions, unit="portion" if portions == 1 else "portions",
            meal=use_meal, ts=ts, source=source)
        return {"meal": m, "portions": portions, "entry": entry}

    # ---- read ---------------------------------------------------------------
    def goals(self) -> dict[str, Any]:
        r = self._db.execute("SELECT * FROM goals WHERE id=1").fetchone()
        if not r:
            return {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0,
                    "activity_adjust": False, "base_calories": 0, "activity_kcal": 0,
                    "set": False}
        # base_calories == calories and activity_kcal == 0 until a caller folds in
        # the day's active burn via adjust_for_activity() (that needs the step and
        # workout stores, which live outside this one).
        return {"calories": r["calories"], "protein_g": r["protein_g"],
                "carbs_g": r["carbs_g"], "fat_g": r["fat_g"],
                "activity_adjust": bool(r["activity_adjust"]),
                "base_calories": r["calories"], "activity_kcal": 0,
                "set": bool(r["calories"] or r["protein_g"] or r["carbs_g"] or r["fat_g"])}

    @staticmethod
    def adjust_for_activity(goals: dict[str, Any], expenditure_kcal: float) -> dict[str, Any]:
        """Fold the day's active burn (steps + workouts, supplied by the caller —
        those stores live elsewhere) into the calorie target when the goal has
        `activity_adjust` on. The stored `calories` is a sedentary base; the burn
        is added back so the target rises as Jake moves. Only calories flex —
        macros are untouched. Returns a new goals dict; a near no-op (base echoed,
        activity 0) when the flag is off, so callers can apply it unconditionally."""
        g = dict(goals)
        base = float(g.get("base_calories") or g.get("calories") or 0)
        g["base_calories"] = round(base)
        if g.get("activity_adjust") and base:
            burn = max(0, round(expenditure_kcal or 0))
            g["activity_kcal"] = burn
            g["calories"] = round(base + burn)
        else:
            g["activity_kcal"] = 0
        return g

    def entry(self, entry_id: int) -> dict[str, Any]:
        r = self._db.execute("SELECT * FROM entries WHERE id=?", (int(entry_id),)).fetchone()
        return self._row(r) if r else {}

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"], "ts": r["ts"], "day": r["day"], "name": r["name"],
            "meal": r["meal"] or "", "qty": r["qty"], "unit": r["unit"],
            "calories": r["calories"], "protein_g": r["protein_g"],
            "carbs_g": r["carbs_g"], "fat_g": r["fat_g"],
            "source": r["source"], "fdc_id": r["fdc_id"],
        }

    @staticmethod
    def _totals(rows: list[dict[str, Any]]) -> dict[str, float]:
        return {
            "calories": round(sum(r["calories"] for r in rows)),
            "protein_g": round(sum(r["protein_g"] for r in rows), 1),
            "carbs_g": round(sum(r["carbs_g"] for r in rows), 1),
            "fat_g": round(sum(r["fat_g"] for r in rows), 1),
        }

    def day(self, date: Optional[str] = None) -> dict[str, Any]:
        """One day's entries (chronological), grouped by meal, with totals and
        the current goals. Omit `date` for today (local)."""
        date = (date or "").strip() or _local_day(time.time())
        rows = [self._row(r) for r in self._db.execute(
            "SELECT * FROM entries WHERE day=? ORDER BY ts", (date,)).fetchall()]
        # Group by meal in the canonical order, blank meals last under "other".
        groups: list[dict[str, Any]] = []
        for meal in (*MEALS, ""):
            items = [r for r in rows if (r["meal"] or "") == meal]
            if items:
                groups.append({"meal": meal or "other",
                               "entries": items, "totals": self._totals(items)})
        return {"date": date, "entries": rows, "meals": groups,
                "totals": self._totals(rows), "goals": self.goals(),
                "count": len(rows)}

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM entries ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Per-day totals over the last `days` (oldest→newest) plus averages —
        drives the Nutrition tab's trend strip and `nutrition_summary`."""
        days = max(1, min(int(days or 7), 366))
        cutoff = time.time() - days * 86400
        rows = [self._row(r) for r in self._db.execute(
            "SELECT * FROM entries WHERE ts >= ? ORDER BY ts", (cutoff,)).fetchall()]
        by_day: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_day.setdefault(r["day"], []).append(r)
        # Build a dense series over the window so empty days show as gaps.
        series = []
        today = datetime.fromtimestamp(time.time())
        for i in range(days - 1, -1, -1):
            d = _local_day(time.time() - i * 86400)
            t = self._totals(by_day.get(d, []))
            series.append({"date": d, **t, "logged": d in by_day})
        logged_days = [s for s in series if s["logged"]]
        n = len(logged_days) or 1
        avg = {
            "calories": round(sum(s["calories"] for s in logged_days) / n),
            "protein_g": round(sum(s["protein_g"] for s in logged_days) / n, 1),
            "carbs_g": round(sum(s["carbs_g"] for s in logged_days) / n, 1),
            "fat_g": round(sum(s["fat_g"] for s in logged_days) / n, 1),
        }
        return {"window_days": days, "series": series, "avg": avg,
                "logged_days": len(logged_days), "goals": self.goals()}
