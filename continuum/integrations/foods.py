"""Food database clients — the nutrition lookup behind calorie/macro logging.

Two providers, one facade. `FoodClient` is a dispatcher the rest of the app
talks to; behind it sit:

  * **FatSecret** (`FatSecretClient`) — the primary when credentials are present.
    A huge branded + restaurant catalogue with human-sized servings, which is
    exactly where daily logging lives. OAuth 2.0 (client-credentials): we fetch a
    bearer token from their token endpoint, cache it until it expires, and call
    `foods.search`. The credentials (`CONTINUUM_FATSECRET_ID` / `_SECRET`) live in
    `.env`, never in the store; the free tier also requires the calling IP to be
    whitelisted in the FatSecret console, so calls must originate from this
    machine (a cloud-egress IP would be rejected).
  * **USDA FoodData Central** (`UsdaClient`) — the fallback, and the default when
    no FatSecret credentials are set. Public, free, works with the built-in
    `DEMO_KEY`; authoritative per-100 g macros for whole foods.
  * **Open Food Facts** (`OpenFoodFactsClient`) — barcode lookup only. Free and
    keyless, with a huge branded/packaged catalogue (the products that carry a
    barcode). `FoodClient.by_barcode` resolves a UPC/EAN to the same normalised
    shape; it's independent of the two text providers above, since neither's free
    tier does barcode.

Every provider normalises to the SAME shape so the picker, the `log_food` tool
and the diary don't care which answered:

  {fdc_id, name, brand, data_type, calories, protein_g, carbs_g, fat_g,
   serving_g, per_100g, basis, ref_qty, ref_unit}

`ref_qty`/`ref_unit` say what the macros are *per* — (100, "g") for USDA and for
FatSecret foods described per a gram amount (normalised to per-100 g), or
(1, "cup"/"serving"/…) for the rare FatSecret food described per a non-gram
serving. `per_100g` is the convenience flag; `basis` is the human label
("100g", "1 cup"). The picker scales macros by `enteredQty / ref_qty`.

`FoodClient.search_result()` returns which provider actually answered (and
whether it was a fallback), so the Nutrition tab can show it. `search()` keeps
the old list-only contract for callers that don't care. stdlib only.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("continuum.foods")


# ===========================================================================
# USDA FoodData Central — the fallback provider.
# ===========================================================================

_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

# Nutrient numbers are stable identifiers; names and units are not. Per 100 g.
_N_PROTEIN = "203"
_N_FAT = "204"
_N_CARBS = "205"
_N_ENERGY_KCAL = "208"

# Whole-food data types are cleaner than crowd-entered branded rows for a query
# like "chicken breast", so we ask for them and rank them ahead of Branded (which
# we still include, so a branded/barcode search resolves). Lower = better.
_TYPE_RANK = {"Foundation": 0, "SR Legacy": 1, "Survey (FNDDS)": 2, "Branded": 3}


class UsdaClient:
    """Thin, stateless wrapper over the FDC search endpoint. Construct once and
    reuse; the only state is the API key."""

    def __init__(self, cfg: Any) -> None:
        self._key = getattr(cfg, "fdc_key", "DEMO_KEY") or "DEMO_KEY"

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Look a food up. Returns candidates, whole foods first, each with
        per-100 g macros in the shared normalised shape. Raises on a
        network/HTTP failure so the caller can report it — an empty list means
        "found nothing", not "broke"."""
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit or 8), 25))
        params = urllib.parse.urlencode({
            "api_key": self._key,
            "query": query,
            # Ask for the whole-food types and branded; the API still ranks by
            # relevance, so we re-rank locally. Over-fetch a little, then trim.
            "dataType": "Foundation,SR Legacy,Survey (FNDDS),Branded",
            "pageSize": min(limit * 3, 50),
        })
        req = urllib.request.Request(
            f"{_SEARCH_URL}?{params}",
            headers={"User-Agent": "Continuum/1.0 (personal nutrition tracker)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            # 403 → bad/rate-limited key; 429 → demo-key daily cap. Surface it.
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            raise RuntimeError(
                f"food database returned HTTP {exc.code}"
                # DEMO_KEY throttling surfaces as 400/403/429 from the gateway;
                # with our fixed request shape those almost always mean rate limit.
                + (" (likely rate limit — set CONTINUUM_FDC_KEY to a free key from "
                   "fdc.nal.usda.gov/api-key-signup.html)"
                   if exc.code in (400, 403, 429) else "")) from exc
        foods = [self._normalise(f) for f in (data.get("foods") or [])]
        # Drop unparseable rows, and rows with no usable numbers at all — some
        # Foundation search results carry an empty `foodNutrients` (their data
        # lives on a linked entry), which would otherwise surface a 0-kcal,
        # 0-macro food at the top and be useless to log against.
        foods = [f for f in foods if f and (
            f["calories"] or f["protein_g"] or f["carbs_g"] or f["fat_g"])]
        # Whole foods first, then FDC's own relevance order within a type.
        foods.sort(key=lambda f: _TYPE_RANK.get(f["data_type"], 9))
        return foods[:limit]

    @staticmethod
    def _normalise(f: dict[str, Any]) -> dict[str, Any] | None:
        by_num: dict[str, dict[str, Any]] = {}
        energy_kcal: float | None = None
        for n in f.get("foodNutrients") or []:
            num = str(n.get("nutrientNumber") or "")
            val = n.get("value")
            if val is None:
                continue
            by_num[num] = n
            # Energy: prefer the 208 (kcal) row; otherwise accept any Energy row
            # explicitly in KCAL (covers the Atwater-factor variants 957/958).
            if num == _N_ENERGY_KCAL:
                energy_kcal = _num(val)
            elif (energy_kcal is None
                  and "energy" in str(n.get("nutrientName") or "").lower()
                  and str(n.get("unitName") or "").upper() == "KCAL"):
                energy_kcal = _num(val)

        def macro(num: str) -> float:
            row = by_num.get(num)
            return _num(row.get("value")) if row else 0.0

        fdc_id = f.get("fdcId")
        name = (f.get("description") or "").strip()
        if not fdc_id or not name:
            return None
        name = name.capitalize() if name.isupper() else name
        serving = f.get("servingSize")
        unit = str(f.get("servingSizeUnit") or "").lower()
        return {
            "fdc_id": int(fdc_id),
            "name": name,
            "brand": (f.get("brandOwner") or f.get("brandName") or "").strip(),
            "data_type": f.get("dataType") or "",
            "calories": round(energy_kcal or 0.0, 1),
            "protein_g": round(macro(_N_PROTEIN), 1),
            "carbs_g": round(macro(_N_CARBS), 1),
            "fat_g": round(macro(_N_FAT), 1),
            # Only grams are useful for scaling; ignore ml/oz label servings.
            "serving_g": round(_num(serving), 1) if serving and unit in ("g", "gram", "grams") else None,
            # USDA is always per-100 g by construction.
            "per_100g": True,
            "basis": "100g",
            "ref_qty": 100,
            "ref_unit": "g",
        }


# ===========================================================================
# FatSecret — the primary provider (when credentials are present).
# ===========================================================================

_FS_TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
_FS_API_URL = "https://platform.fatsecret.com/rest/server.api"

# `foods.search` returns each food's macros as a `food_description` string, e.g.
#   "Per 100g - Calories: 165kcal | Fat: 3.57g | Carbs: 0.00g | Protein: 31.02g"
#   "Per 1 cup  - Calories: 233kcal | Fat: 12.32g | Carbs: 8.06g | Protein: 22.30g"
# We parse the basis ("Per <X> -") and the four macros out of it.
_FS_DESC_RE = re.compile(r"Per\s+(.+?)\s*-\s*(.*)", re.IGNORECASE)
_FS_GRAM_RE = re.compile(r"^([\d.]+)\s*g$", re.IGNORECASE)
_FS_NUM = lambda field, s: (  # noqa: E731 — small local helper
    float(m.group(1)) if (m := re.search(field + r":\s*([\d.]+)", s, re.IGNORECASE)) else 0.0)


class FatSecretClient:
    """OAuth 2.0 (client-credentials) client over FatSecret's `foods.search`.

    Holds a cached bearer token (theirs last ~24 h; we refresh a minute early).
    Stateless otherwise. Raises RuntimeError with a readable message on auth,
    IP-whitelist or transport failures so `FoodClient` can fall back to USDA and
    the tab can say why.
    """

    def __init__(self, cfg: Any) -> None:
        self._id = getattr(cfg, "fatsecret_id", "") or ""
        self._secret = getattr(cfg, "fatsecret_secret", "") or ""
        self._token: str | None = None
        self._token_exp: float = 0.0

    @staticmethod
    def configured(cfg: Any) -> bool:
        return bool(getattr(cfg, "fatsecret_id", "") and getattr(cfg, "fatsecret_secret", ""))

    def _bearer(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "basic"}).encode()
        auth = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        req = urllib.request.Request(
            _FS_TOKEN_URL, data=body, method="POST",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "Continuum/1.0 (personal nutrition tracker)"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                tok = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"FatSecret auth failed (HTTP {exc.code}"
                + ("; check CONTINUUM_FATSECRET_ID / _SECRET"
                   if exc.code in (400, 401) else "") + ")") from exc
        token = tok.get("access_token")
        if not token:
            raise RuntimeError("FatSecret auth returned no access_token")
        self._token = token
        self._token_exp = now + float(tok.get("expires_in", 86400) or 86400)
        return token

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit or 8), 25))
        params = urllib.parse.urlencode({
            "method": "foods.search", "search_expression": query,
            "format": "json", "max_results": limit})
        req = urllib.request.Request(
            f"{_FS_API_URL}?{params}",
            headers={"Authorization": f"Bearer {self._bearer()}",
                     "User-Agent": "Continuum/1.0 (personal nutrition tracker)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"FatSecret search returned HTTP {exc.code}") from exc
        # FatSecret reports application-level failures as a 200 with an `error`
        # object — most importantly code 21, "Invalid IP address detected" (the
        # free-tier whitelist), which is exactly the state we must fall back on.
        err = data.get("error")
        if err:
            code = err.get("code")
            msg = err.get("message") or "unknown error"
            if code == 21 or "ip address" in str(msg).lower():
                msg += " (whitelist this machine's IP in the FatSecret console)"
            raise RuntimeError(f"FatSecret error {code}: {msg}")
        block = data.get("foods") or {}
        items = block.get("food") or []
        if isinstance(items, dict):  # a single result comes back unwrapped
            items = [items]
        out = [self._normalise(f) for f in items]
        return [f for f in out if f][:limit]

    @staticmethod
    def _normalise(f: dict[str, Any]) -> dict[str, Any] | None:
        food_id = f.get("food_id")
        name = (f.get("food_name") or "").strip()
        if not food_id or not name:
            return None
        desc = str(f.get("food_description") or "")
        m = _FS_DESC_RE.match(desc.strip())
        if not m:
            return None
        basis_text = m.group(1).strip()
        rest = m.group(2)
        cal = _FS_NUM("Calories", rest)
        fat = _FS_NUM("Fat", rest)
        carbs = _FS_NUM("Carbs", rest)
        protein = _FS_NUM("Protein", rest)
        if not (cal or fat or carbs or protein):
            return None

        gram = _FS_GRAM_RE.match(basis_text)
        if gram:
            basis_g = float(gram.group(1)) or 100.0
            factor = 100.0 / basis_g
            calories, protein_g, carbs_g, fat_g = (
                cal * factor, protein * factor, carbs * factor, fat * factor)
            per_100g = True
            basis = "100g"
            ref_qty, ref_unit = 100, "g"
            # A non-100 gram basis (e.g. "Per 30g") is a real serving worth
            # prefilling; a plain "Per 100g" has no natural serving.
            serving_g = round(basis_g, 1) if abs(basis_g - 100.0) > 0.01 else None
        else:
            # Non-gram serving ("1 cup", "1 serving"): keep the macros AS-IS for
            # that serving; the picker scales by count, not grams.
            calories, protein_g, carbs_g, fat_g = cal, protein, carbs, fat
            per_100g = False
            basis = basis_text
            qm = re.match(r"([\d.]+)\s*(.*)", basis_text)
            ref_qty = float(qm.group(1)) if qm and qm.group(1) else 1
            ref_unit = (qm.group(2).strip() if qm and qm.group(2) else basis_text) or "serving"
            serving_g = None

        food_type = str(f.get("food_type") or "")
        return {
            "fdc_id": int(food_id) if str(food_id).isdigit() else None,
            "name": name,
            "brand": (f.get("brand_name") or "").strip(),
            "data_type": food_type or ("Branded" if f.get("brand_name") else "Generic"),
            "calories": round(calories, 1),
            "protein_g": round(protein_g, 1),
            "carbs_g": round(carbs_g, 1),
            "fat_g": round(fat_g, 1),
            "serving_g": serving_g,
            "per_100g": per_100g,
            "basis": basis,
            "ref_qty": ref_qty,
            "ref_unit": ref_unit,
        }


# ===========================================================================
# Open Food Facts — barcode lookup. Free, keyless, and heavy on branded/packaged
# products — exactly the stuff that carries a barcode. Neither USDA's nor
# FatSecret's free tier resolves a UPC, so a scan/manual-barcode always routes
# here, independent of which text-search provider is primary.
# ===========================================================================

_OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
# Ask for only the fields we normalise, to keep the payload small.
_OFF_FIELDS = ("product_name,brands,nutriments,serving_size,serving_quantity,"
               "code,quantity")


class OpenFoodFactsClient:
    """Resolve a barcode (UPC/EAN) to a product + per-100 g macros in the SAME
    normalised shape every other provider yields, so the picker/tools don't care
    it came from a scan. Stateless and keyless — OFF asks only for a descriptive
    User-Agent. `by_barcode` returns the food dict, or None when the code isn't
    in the catalogue; it raises RuntimeError on a transport/HTTP failure so the
    caller can tell "not found" from "couldn't reach it"."""

    def __init__(self, cfg: Any) -> None:  # cfg unused today; kept for symmetry
        self._cfg = cfg

    def by_barcode(self, code: str) -> dict[str, Any] | None:
        code = re.sub(r"\D", "", str(code or ""))  # a scanner may add spaces/dashes
        if not code:
            return None
        url = (_OFF_URL.format(code=urllib.parse.quote(code))
               + "?fields=" + urllib.parse.quote(_OFF_FIELDS, safe=","))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Continuum/1.0 (personal nutrition tracker)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            # A missing product is a 404 — that's "not found", not a failure.
            if exc.code == 404:
                return None
            raise RuntimeError(f"Open Food Facts returned HTTP {exc.code}") from exc
        if int(data.get("status") or 0) != 1:
            return None
        return self._normalise(data.get("product") or {}, code)

    @staticmethod
    def _normalise(p: dict[str, Any], code: str) -> dict[str, Any] | None:
        name = (p.get("product_name") or "").strip()
        if not name:
            return None
        nut = p.get("nutriments") or {}
        # Energy: prefer the kcal figure; else convert the kJ one (1 kcal = 4.184 kJ).
        cal = _num(nut.get("energy-kcal_100g"))
        if not cal:
            kj = _num(nut.get("energy-kj_100g")) or (
                _num(nut.get("energy_100g"))
                if str(nut.get("energy_unit") or "").lower() == "kj" else 0.0)
            if kj:
                cal = kj / 4.184
        protein = _num(nut.get("proteins_100g"))
        carbs = _num(nut.get("carbohydrates_100g"))
        fat = _num(nut.get("fat_100g"))
        if not (cal or protein or carbs or fat):
            return None  # no usable numbers — useless to log against
        brand = (p.get("brands") or "").split(",")[0].strip()
        serving_g = _num(p.get("serving_quantity"))
        return {
            # OFF ids ARE barcodes, not FDC ids; keep the code in `barcode` as
            # provenance and leave fdc_id None so nothing tries to re-query FDC.
            "fdc_id": None,
            "barcode": code,
            "name": name,
            "brand": brand,
            "data_type": "Branded",
            "calories": round(cal, 1),
            "protein_g": round(protein, 1),
            "carbs_g": round(carbs, 1),
            "fat_g": round(fat, 1),
            "serving_g": round(serving_g, 1) if serving_g else None,
            "per_100g": True,  # OFF nutriments are per 100 g by construction
            "basis": "100g",
            "ref_qty": 100,
            "ref_unit": "g",
        }


# ===========================================================================
# Dispatcher — FatSecret primary, USDA fallback (text); Open Food Facts for
# barcode. The public facade.
# ===========================================================================

class FoodClient:
    """What the tools and the server talk to. Prefers FatSecret when it's
    configured, and transparently falls back to USDA when FatSecret is absent,
    errors, or (its most likely failure) hasn't got this machine's IP whitelisted
    yet — so food search keeps working the whole time."""

    def __init__(self, cfg: Any) -> None:
        self._usda = UsdaClient(cfg)
        self._fatsecret = FatSecretClient(cfg) if FatSecretClient.configured(cfg) else None
        self._off = OpenFoodFactsClient(cfg)  # barcode lookup — always available

    @property
    def primary(self) -> str:
        return "fatsecret" if self._fatsecret else "usda"

    def status(self) -> dict[str, Any]:
        """What providers exist and which is primary — drives the tab's widget."""
        fs = self._fatsecret is not None
        return {
            "primary": self.primary,
            "fatsecret_configured": fs,
            "fallback": "usda",
            "providers": (["fatsecret", "usda"] if fs else ["usda"]),
            "barcode": "openfoodfacts",  # who resolves a scanned/typed UPC
        }

    def by_barcode(self, code: str) -> dict[str, Any]:
        """Resolve a barcode (UPC/EAN) to a food via Open Food Facts. Returns
        `{barcode, food, provider}` — `food` is None when the code isn't in the
        catalogue. Raises only if OFF itself is unreachable, so the caller can
        distinguish "no such product" from "lookup broke"."""
        food = self._off.by_barcode(code)
        return {"barcode": re.sub(r"\D", "", str(code or "")),
                "food": food, "provider": "openfoodfacts"}

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """List-only contract for callers that don't care who answered."""
        return self.search_result(query, limit)["foods"]

    def search_result(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Search, returning `{foods, provider, fallback, note}` so the caller
        can tell the user which database actually answered. Raises only if the
        provider that ends up answering raises (e.g. USDA is also down)."""
        if not (query or "").strip():
            return {"foods": [], "provider": self.primary, "fallback": False}
        if self._fatsecret is None:
            return {"foods": self._usda.search(query, limit),
                    "provider": "usda", "fallback": False}
        try:
            foods = self._fatsecret.search(query, limit)
        except Exception as exc:
            log.warning("FatSecret search failed (%s); falling back to USDA", exc)
            return {"foods": self._usda.search(query, limit), "provider": "usda",
                    "fallback": True, "note": f"FatSecret unavailable — {exc}"}
        if foods:
            return {"foods": foods, "provider": "fatsecret", "fallback": False}
        # FatSecret reachable but no match — try USDA before giving up.
        usda = self._usda.search(query, limit)
        if usda:
            return {"foods": usda, "provider": "usda", "fallback": True,
                    "note": "no FatSecret match — showing USDA results"}
        return {"foods": [], "provider": "fatsecret", "fallback": False}


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
