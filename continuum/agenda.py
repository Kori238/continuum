"""The calendar — what's actually coming up.

Continuum's proactive gate watches apps, files and windows, so it can notice
Jake has been in one editor for three hours but not that he has a call in
twenty minutes. Events are the highest-signal proactive input there is; this
module supplies them.

Read-only by construction. It reads a *private ICS feed URL* (Google Calendar's
"Secret address in iCal format", and the equivalent in Outlook/Apple) rather
than going through OAuth. That choice buys a lot: no client-secrets file, no
consent flow, no token refresh, no write scope to get wrong — and it works with
any provider that can emit an ICS feed.

The URL is itself the credential, so it lives only in .env (gitignored) and is
never logged, never put in an error message, and never returned to the model.
The functions here raise plain messages that name the *problem*, not the URL.

Named agenda.py, not calendar.py, so nothing in the package can shadow the
stdlib `calendar` module that icalendar itself imports.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("continuum.agenda")

_TIMEOUT = 20.0
_CACHE_SECONDS = 300.0     # ICS feeds are static-ish; Google throttles hammering
_MAX_BYTES = 8_000_000

_cache: dict[str, tuple[float, bytes]] = {}


async def _load(url: str) -> bytes:
    """Fetch the ICS feed, cached. Errors never carry the URL."""
    now = asyncio.get_running_loop().time()
    hit = _cache.get(url)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url)
    except Exception as exc:
        # Deliberately only the exception *type* — httpx puts the full URL,
        # which is the secret, into str(exc).
        raise RuntimeError(f"could not reach the calendar feed ({type(exc).__name__})") from None
    if r.status_code != 200:
        raise RuntimeError(f"calendar feed returned HTTP {r.status_code}")
    data = r.content[:_MAX_BYTES]
    if b"BEGIN:VCALENDAR" not in data[:2000]:
        raise RuntimeError("that URL did not return an iCalendar feed — check the "
                           "secret iCal address in your calendar settings")
    _cache[url] = (now, data)
    return data


def _fmt(value: Any, tz: ZoneInfo) -> tuple[str, bool]:
    """(display string, is_all_day). Dates without a time are all-day events."""
    if isinstance(value, dt.datetime):
        local = value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
        return local.strftime("%a %d %b %H:%M"), False
    return value.strftime("%a %d %b"), True     # a bare date


async def upcoming(url: str, hours: float = 48.0, tz_name: str = "Europe/London",
                   max_events: int = 25) -> list[dict]:
    """Events between now and now+hours, recurrences expanded, soonest first."""
    if not url:
        raise RuntimeError("no calendar configured — set CONTINUUM_CALENDAR_ICS in .env "
                           "to your calendar's private iCal address")
    data = await _load(url)
    tz = ZoneInfo(tz_name)
    start = dt.datetime.now(tz)
    end = start + dt.timedelta(hours=hours)

    def _parse() -> list[dict]:
        # icalendar + RRULE expansion are pure CPU on a multi-MB feed; keep the
        # event loop free or the UI stalls while the agenda is read.
        import icalendar
        import recurring_ical_events
        cal = icalendar.Calendar.from_ical(data)
        out = []
        for ev in recurring_ical_events.of(cal).between(start, end):
            begin = ev.decoded("DTSTART")
            when, all_day = _fmt(begin, tz)
            begin_dt = (begin.astimezone(tz) if isinstance(begin, dt.datetime)
                        else dt.datetime.combine(begin, dt.time.min, tzinfo=tz))
            mins = round((begin_dt - start).total_seconds() / 60)
            out.append({
                "summary": str(ev.get("SUMMARY", "(no title)")),
                "when": when,
                "all_day": all_day,
                "minutes_until": mins,
                "location": str(ev.get("LOCATION", "") or ""),
                "_sort": begin_dt,
            })
        out.sort(key=lambda e: e["_sort"])
        for e in out:
            e.pop("_sort")
        return out[:max_events]

    return await asyncio.to_thread(_parse)


def describe(events: list[dict], hours: float) -> str:
    """Render the agenda for the model — compact, and relative where it helps."""
    if not events:
        return f"nothing on the calendar in the next {hours:g}h"
    lines = []
    for e in events:
        mins = e["minutes_until"]
        if e["all_day"]:
            rel = "all day"
        elif mins < 0:
            rel = "in progress"
        elif mins < 60:
            rel = f"in {mins}m"
        else:
            rel = f"in {mins // 60}h{mins % 60:02d}m"
        where = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"- {e['when']} ({rel}): {e['summary']}{where}")
    return "\n".join(lines)
