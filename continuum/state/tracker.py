"""StateTracker — turns point-in-time probes into a live model of "right now".

Polling every few seconds, it maintains per-app durations (how long open, how
long focused) and the current foreground app, and exposes:

  * basic()        — structured snapshot for the UI (all open apps + metrics).
  * summary_text() — a compact, token-cheap version injected into agent context.
  * inspect(target)— deep detail about one app/process (the "request more" call).

Durations use each process's real create_time for "open for", and accumulate
focus time across polls for "focused for".
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional

from . import browser, probe

log = logging.getLogger("continuum.state")

# Process names we treat as "a browser" and enrich with page-level context.
_BROWSER_PROCS = {"firefox.exe"}


def humanize(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def ago(seconds: float) -> str:
    """Recency phrasing: how long since something last happened."""
    s = int(max(0, seconds))
    if s < 10:
        return "now"
    if s < 60:
        return f"{s}s ago"
    return humanize(s) + " ago"


class StateTracker:
    def __init__(self, poll_interval: float = 5.0) -> None:
        self.poll_interval = poll_interval
        self._apps: dict[int, dict[str, Any]] = {}   # pid -> tracked app
        self._fg_pid: Optional[int] = None
        self._last_poll: Optional[float] = None
        self._browser: Optional[dict[str, Any]] = None
        self._browser_focused: bool = False
        self._basic: dict[str, Any] = {"apps": [], "system": {}, "foreground": None}

        # Presence / rhythm.
        self._idle_threshold = 90.0    # no input this long => "away"
        self._present = True
        self._active_since: Optional[float] = None   # start of current at-desk session
        self._away_since: Optional[float] = None
        self._last_return: Optional[dict[str, Any]] = None   # {"at", "gap_s"} of last comeback
        # Rolling (timestamp, app_name, seconds) focus samples, ~2h window.
        self._focus_samples: deque[tuple[float, str, float]] = deque()
        self._presence: dict[str, Any] = {}

        # Throttled samples (spawn subprocesses / rarely change).
        self._gpu: Optional[dict[str, Any]] = None
        self._gpu_ts = 0.0
        self._wifi: Optional[str] = None
        self._wifi_ts = 0.0
        self._media: Optional[dict[str, Any]] = None
        self._media_ts = 0.0

    # ---- sampling -----------------------------------------------------------
    def poll(self) -> dict[str, Any]:
        now = time.time()
        self._update_presence(now)   # sets self._present + rhythm before we count focus
        windows = probe.list_windows()
        fg_pid = probe.foreground_pid()

        # Group visible windows by owning process.
        by_pid: dict[int, list[str]] = {}
        for win in windows:
            by_pid.setdefault(win["pid"], []).append(win["title"])

        seen: set[int] = set()
        for pid, titles in by_pid.items():
            brief = probe.process_brief(pid)
            if not brief:
                continue
            seen.add(pid)
            app = self._apps.get(pid)
            if app is None:
                app = {
                    "pid": pid, "name": brief["name"], "proc": brief["proc"],
                    "create_time": brief["create_time"],
                    "first_seen": now, "focus_seconds": 0.0,
                    "last_focused": None,
                }
                self._apps[pid] = app
            app["titles"] = titles
            app["last_seen"] = now

        # Accumulate focus time for whatever was in front during this interval —
        # but ONLY while Jake is actually present, so "focused 12m" means 12m of
        # real attention, not a window left in front while he walked away.
        if self._fg_pid in self._apps and self._last_poll is not None and self._present:
            dt = now - self._last_poll
            app = self._apps[self._fg_pid]
            app["focus_seconds"] += dt
            app["last_focused"] = now
            self._focus_samples.append((now, app["name"], dt))
        # Prune focus samples older than 2h.
        cutoff = now - 2 * 3600
        while self._focus_samples and self._focus_samples[0][0] < cutoff:
            self._focus_samples.popleft()

        # Drop apps whose windows are all gone (closed).
        for pid in [p for p in self._apps if p not in seen]:
            del self._apps[pid]

        self._fg_pid = fg_pid
        if fg_pid in self._apps and self._present:
            self._apps[fg_pid]["last_focused"] = now

        # Page-level context for the browser (what's actually on screen).
        fg_proc = self._apps.get(fg_pid, {}).get("proc", "").lower() if fg_pid else ""
        any_browser = any(a["proc"].lower() in _BROWSER_PROCS for a in self._apps.values())
        self._browser = browser.firefox_context() if any_browser else None
        self._browser_focused = fg_proc in _BROWSER_PROCS

        self._sample_throttled(now)

        self._last_poll = now
        self._basic = self._build_basic(now)
        return self._basic

    # ---- presence / throttled samples ---------------------------------------
    def _update_presence(self, now: float) -> None:
        idle = probe.idle_seconds()
        if self._active_since is None:
            self._active_since = now
        if idle is None:
            self._presence = {"idle_s": None, "present": True, "state": "active"}
            return
        away = idle >= self._idle_threshold
        if away and self._present:                       # just stepped away
            self._present = False
            self._away_since = now - idle
        elif not away and not self._present:             # just came back
            gap = now - (self._away_since or now)
            self._last_return = {"at": now, "gap_s": gap}
            self._present = True
            self._active_since = now
            self._away_since = None
        elif not away:
            self._present = True

        p: dict[str, Any] = {"idle_s": int(idle), "present": self._present}
        if not self._present:
            p["state"] = "away"
            p["away_for"] = humanize(now - (self._away_since or now))
        else:
            p["state"] = "active"
            p["session_for"] = humanize(now - (self._active_since or now))
            # Surface a fresh return for a little while so context can say "just back".
            if self._last_return and (now - self._last_return["at"]) < 300 \
                    and self._last_return["gap_s"] >= self._idle_threshold:
                p["just_back"] = True
                p["was_away"] = humanize(self._last_return["gap_s"])
        self._presence = p

    def _sample_throttled(self, now: float) -> None:
        # nvidia-smi / netsh spawn processes — sample them far less than every poll.
        if now - self._gpu_ts >= 15:
            self._gpu = probe.gpu()
            self._gpu_ts = now
        if now - self._wifi_ts >= 60:
            self._wifi = probe.wifi_ssid()
            self._wifi_ts = now
        if now - self._media_ts >= 5:
            self._media = probe.now_playing()
            self._media_ts = now

    def recent_activity(self, window_s: float = 3600, top: int = 5) -> list[dict[str, Any]]:
        """What Jake actually spent the last hour on, most time first."""
        now = time.time()
        cutoff = now - window_s
        by_name: dict[str, float] = {}
        for ts, name, dt in self._focus_samples:
            if ts >= cutoff:
                by_name[name] = by_name.get(name, 0.0) + dt
        ranked = sorted(by_name.items(), key=lambda kv: -kv[1])
        return [{"name": n, "seconds": int(s), "for": humanize(s)}
                for n, s in ranked[:top] if s >= 20]

    def _unread(self) -> list[dict[str, Any]]:
        """Cheap unread signal parsed from titles we already have — Gmail's tab
        and comms apps put their count right in the window title."""
        import re
        out: list[dict[str, Any]] = []
        # Gmail inbox count from an open Firefox tab.
        for pg in (self._browser or {}).get("recent", []):
            m = re.search(r"Inbox \(([\d,]+)\)", pg.get("title", ""))
            if m:
                out.append({"source": "Gmail", "count": int(m.group(1).replace(",", ""))})
                break
        # Discord/Slack numeric badge in the app's own window title.
        for app in self._apps.values():
            proc = app["proc"].lower()
            if proc not in ("discord.exe", "slack.exe"):
                continue
            for t in app.get("titles", []):
                m = re.match(r"\((\d+)\)", t.strip())
                if m:
                    out.append({"source": app["name"], "count": int(m.group(1))})
                    break
        return out

    # ---- views --------------------------------------------------------------
    def _app_view(self, app: dict[str, Any], now: float) -> dict[str, Any]:
        titles = app.get("titles", [])
        is_fg = app["pid"] == self._fg_pid
        last_focused = app.get("last_focused")
        title = titles[0] if titles else ""
        url = ""
        # For a browser, the page you're on says far more than the window title —
        # and its URL is what lets the activity store split time out by site.
        if app["proc"].lower() in _BROWSER_PROCS and self._browser and self._browser.get("active"):
            act = self._browser["active"]
            title = act.get("title") or title
            url = act.get("url") or ""
        return {
            "pid": app["pid"],
            "name": app["name"],
            "proc": app["proc"],
            "title": title,
            "url": url,
            "windows": len(titles),
            "open_for_s": int(now - app["create_time"]),
            "open_for": humanize(now - app["create_time"]),
            "focus_for": humanize(app["focus_seconds"]),
            "focus_seconds": int(app["focus_seconds"]),
            "focused_ago": "now" if is_fg else (ago(now - last_focused) if last_focused else "not yet"),
            "focused_ago_s": 0 if is_fg else (int(now - last_focused) if last_focused else None),
            "foreground": is_fg,
        }

    def _build_basic(self, now: float) -> dict[str, Any]:
        apps = [self._app_view(a, now) for a in self._apps.values()]
        # What matters is attention: foreground first, then most-recently focused,
        # then most-focused overall. Open time is a weak signal and not ranked on.
        apps.sort(key=lambda a: (not a["foreground"],
                                 a["focused_ago_s"] if a["focused_ago_s"] is not None else 1e12,
                                 -a["focus_seconds"]))
        fg = next((a for a in apps if a["foreground"]), None)
        network = {"wifi": self._wifi} if self._wifi else {}
        return {
            "time": datetime.now().strftime("%H:%M %a %d %b"),
            "foreground": fg,
            "apps": apps,
            "count": len(apps),
            "browser": self._browser,
            "presence": self._presence,
            "activity": self.recent_activity(),
            "gpu": self._gpu,
            "network": network,
            "media": self._media,
            "unread": self._unread(),
            "system": probe.system_metrics(),
        }

    def basic(self) -> dict[str, Any]:
        return self._basic

    def summary_text(self, max_apps: int = 6, max_pages: int = 4) -> str:
        """Compact state for injecting into the agent's context. Built around
        attention — what's in front, what page it is, and how recently other
        things were touched — rather than raw "how long open"."""
        b = self._basic
        if not b.get("apps"):
            return ""
        lines = [f"Current machine state ({b.get('time','now')}):"]

        # Presence — is Jake even here, and for how long / just back?
        pres = b.get("presence") or {}
        if pres.get("state") == "away":
            lines.append(f"- Jake is away from the desk (idle {pres.get('away_for','a while')}) — "
                         "these windows are just left open.")
        elif pres.get("just_back"):
            lines.append(f"- Jake just came back after {pres.get('was_away','a break')} away.")
        elif pres.get("session_for"):
            lines.append(f"- At the desk, this session {pres['session_for']}.")

        fg = b.get("foreground")
        if fg:
            line = f"- In focus: {fg['name']} — \"{fg['title']}\" (focused {fg['focus_for']} in total)"
            if fg["proc"].lower() in _BROWSER_PROCS:
                bc = b.get("browser") or {}
                act = bc.get("active") or {}
                if act.get("url"):
                    line += f"\n  page: {act['url']}"
            lines.append(line)

        # What the last hour actually went to.
        act = b.get("activity") or []
        if len(act) > 1:
            spent = ", ".join(f"{a['name']} {a['for']}" for a in act)
            lines.append(f"- Last hour on: {spent}")

        # Recently focused (not now) — recency is the interesting bit, so lead with it.
        others = [a for a in b["apps"] if not a["foreground"]][:max_apps]
        if others:
            listed = ", ".join(
                f"{a['name']} ({a['focused_ago']})" if a["focused_ago_s"] is not None
                else a["name"]
                for a in others)
            lines.append(f"- Recently used ({b['count']} open): {listed}")

        # What they've actually been looking at in the browser.
        bc = b.get("browser") or {}
        recent = bc.get("recent") or []
        if recent:
            shown = recent[:max_pages]
            lines.append(f"- Browser tabs ({bc.get('open_count', len(recent))} open, recent first):")
            for pg in shown:
                title = (pg.get("title") or pg.get("url") or "").strip()
                if len(title) > 70:
                    title = title[:67] + "..."
                lines.append(f"    · {title} — {pg.get('idle','')}")

        # Now playing (only when actually playing).
        media = b.get("media") or {}
        if media.get("playing") and media.get("title"):
            who = f" — {media['artist']}" if media.get("artist") else ""
            lines.append(f"- Playing: \"{media['title']}\"{who} ({media.get('app','')})")

        # Unread comms (only when there's something).
        unread = [u for u in (b.get("unread") or []) if u.get("count")]
        if unread:
            lines.append("- Unread: " + ", ".join(f"{u['source']} {u['count']}" for u in unread))

        # A GPU job worth noticing (Jake trains LoRAs — this catches active runs).
        gpu = b.get("gpu") or {}
        if gpu.get("util_pct", 0) >= 30 or (gpu.get("procs") and gpu.get("mem_used_mb", 0) > 2000):
            busy = f"- GPU: {gpu['util_pct']}% util, {gpu['mem_used_mb']}/{gpu['mem_total_mb']}MB"
            procs = gpu.get("procs") or []
            if procs:
                busy += " — " + ", ".join(f"{p['name']} ({p['mem_mb']}MB)" for p in procs[:3])
            lines.append(busy)

        sysm = b.get("system", {})
        if sysm:
            bit = f"- System: CPU {sysm.get('cpu_pct','?')}%, RAM {sysm.get('mem_pct','?')}%"
            if "battery_pct" in sysm:
                bit += f", battery {sysm['battery_pct']}%{' (unplugged)' if sysm.get('on_battery') else ''}"
            net = b.get("network") or {}
            if net.get("wifi"):
                bit += f", Wi-Fi \"{net['wifi']}\""
            lines.append(bit)
        return "\n".join(lines)

    # ---- request more -------------------------------------------------------
    def inspect(self, target: Any) -> dict[str, Any]:
        """Deep detail about one app/process. `target` may be a pid or a name
        substring (matched against currently-open apps)."""
        pid = self._resolve(target)
        if pid is None:
            return {"error": f"no open app matching {target!r}"}
        try:
            detail = probe.process_detail(pid)
        except Exception as exc:
            return {"error": f"could not inspect pid {pid}: {exc}"}
        app = self._apps.get(pid)
        now = time.time()
        if app:
            detail["titles"] = app.get("titles", [])
            detail["open_for"] = humanize(now - app["create_time"])
            detail["focus_for"] = humanize(app["focus_seconds"])
            lf = app.get("last_focused")
            detail["focused_ago"] = "now" if pid == self._fg_pid else (ago(now - lf) if lf else "not yet")
            detail["foreground"] = pid == self._fg_pid
            if app["proc"].lower() in _BROWSER_PROCS and self._browser:
                detail["browser"] = self._browser
        return detail

    def _resolve(self, target: Any) -> Optional[int]:
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            pid = int(target)
            return pid if pid in self._apps else pid  # allow inspecting any live pid
        if isinstance(target, str):
            t = target.strip().lower()
            # exact-ish first, then substring on name/title/proc.
            for a in self._apps.values():
                if a["name"].lower() == t or a["proc"].lower() == t:
                    return a["pid"]
            for a in self._apps.values():
                hay = " ".join([a["name"], a["proc"], *a.get("titles", [])]).lower()
                if t in hay:
                    return a["pid"]
        return None
