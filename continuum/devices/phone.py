"""The phone — Continuum's second body.

Everything about *being* a device across a socket now lives in `remote.py`: the
connection lifecycle, the aged-signal store, the correlation-id command channel.
This module is only what makes a phone a phone — which signals it sends, how
fast each one goes stale, which commands the Android app actually implements,
and how to say all of that in one line the agent can afford on every turn.

Transport is the socket the app opens to `/phone/ws`, carried by Tailscale and
authenticated by it (see `app/security.py` — the tailnet stamps the identity,
we never mint a token for the phone to hold). If the socket is down, the phone
is simply absent, and absent is a legitimate answer to "where is he".
"""
from __future__ import annotations

import logging
from typing import Any

from ..bus import Bus
from .remote import (
    COMMAND_TIMEOUT, DEFAULT_FRESH, MAX_CAPABILITIES, MAX_KIND_LEN,
    MAX_NAME_LEN, MAX_UNKNOWN_SIGNALS, UNKNOWN_WARN_LIMIT, RemoteDevice, aged,
)

log = logging.getLogger("continuum.devices.phone")

# Re-exported so the constants keep their historical import site. Callers say
# `from ..devices.phone import COMMANDS as PHONE_COMMANDS`; the move to a base
# class is not a reason to make them learn a new module.
__all__ = [
    "COMMANDS", "COMMAND_TIMEOUT", "DEFAULT_FRESH", "FRESH_SECONDS",
    "KNOWN_SIGNALS", "MAX_CAPABILITIES", "MAX_KIND_LEN", "MAX_NAME_LEN",
    "MAX_UNKNOWN_SIGNALS", "UNKNOWN_WARN_LIMIT", "PhoneDevice",
]

# How long a reading stays quotable. Past this we still keep the value — it's
# the best we have — but every surface must mark it as aged rather than assert
# it. The windows differ because the signals decay at different speeds: charge
# state changes over minutes, where you are changes over tens of minutes, and
# which apps exist on the device barely changes at all.
FRESH_SECONDS = {
    "battery": 15 * 60,
    "location": 30 * 60,
    "connectivity": 15 * 60,
    "screen": 5 * 60,
    "foreground": 5 * 60,
    "motion": 20 * 60,
    "apps": 24 * 60 * 60,
}

KNOWN_SIGNALS = frozenset(FRESH_SECONDS) | {
    "steps", "weight", "workout", "health", "notification",
}

# Commands the app is expected to implement. Kept here, not in tools.py, so the
# fabric owns its own vocabulary and the agent layer just consumes it. This list
# is honest — a command only belongs here once the app actually carries it out.
# `ui_act` (tapping/typing into specific elements) is deliberately absent: the
# accessibility service can observe and press global buttons, not act inside apps.
COMMANDS = (
    "open_app",        # launch an installed app by package or friendly name
    "notify",          # raise a notification on the phone
    "list_apps",       # what's installed
    "ui_observe",      # read the on-screen accessibility text tree (read-only)
    "global_action",   # press a system button: back/home/recents/screenshot/…
    "routine_widget",  # push today's routine (next item + progress) to the home-screen widget
    "set_alarm",       # add/replace a wake-up alarm (the phone rings it via AlarmManager)
    "list_alarms",     # what alarms are set on the phone
    "cancel_alarm",    # remove an alarm by id
    # The phone is the only always-on machine on Jake's home LAN, which is the
    # one qualification that matters here: a magic packet is a layer-2 broadcast,
    # so it can only be sent by something already on the target's network. The
    # hub is a VPS in Germany and Tailscale carries no broadcast, so neither can
    # wake the desk however well-connected they are. Works when the phone is on
    # home Wi-Fi and fails honestly when it is on mobile data.
    "wake_lan",        # broadcast a Wake-on-LAN magic packet at the desk
)


class PhoneDevice(RemoteDevice):
    """One phone. Holds its last known state; relays commands to it."""

    label = "phone"
    topic_prefix = "phone"
    default_os = "Android"
    commands = COMMANDS
    fresh_seconds = FRESH_SECONDS
    known_signals = KNOWN_SIGNALS

    def __init__(self, bus: Bus, name: str = "phone") -> None:
        super().__init__(bus, name)

    def summary_text(self) -> str:
        """One compact line for the agent's context, or "" when there's nothing
        worth saying. Deliberately terse — this rides along with the desktop
        state on every single turn, so it earns its tokens or it stays quiet."""
        if not self._signals:
            return "Phone: not connected" if not self.connected else "Phone: connected, no readings yet"

        bits: list[str] = []

        batt = self.get("battery")
        if batt and batt.get("level") is not None:
            s = f"{batt['level']}%"
            if batt.get("charging"):
                s += " charging"
            elif isinstance(batt.get("level"), (int, float)) and batt["level"] <= 20:
                s += " (low)"
            bits.append(aged(s, batt))

        loc = self.get("location")
        if loc:
            where = loc.get("place") or loc.get("label")
            if not where and loc.get("lat") is not None:
                where = f"{loc['lat']:.3f},{loc['lon']:.3f}"
            if where:
                bits.append(aged(str(where), loc))

        scr = self.get("screen")
        if scr and scr.get("on") is not None and not scr.get("stale"):
            bits.append("screen on" if scr["on"] else "screen off")

        # Which app is in front, when the accessibility service is reporting it
        # and the screen is on — a stale foreground is just where he last was.
        fg = self.get("foreground")
        if fg and fg.get("app") and not fg.get("stale") and scr and scr.get("on"):
            bits.append(f"in {fg['app']}")

        net = self.get("connectivity")
        if net and not net.get("stale"):
            if net.get("ssid"):
                bits.append(f"wifi {net['ssid']}")
            elif net.get("type"):
                bits.append(str(net["type"]))

        if not self.connected:
            bits.append("offline")

        return "Phone: " + ", ".join(bits) if bits else ""


def _aged(text: str, sig: dict[str, Any]) -> str:
    """Kept as a module-level name for anything that imported it from here."""
    return aged(text, sig)
