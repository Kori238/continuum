"""Read Windows toast notifications via the UserNotificationListener API.

This is the system-wide feed of everything that pops a toast — Slack, Discord,
mail, calendar — captured the moment it arrives, whether or not the app is
focused. Each notification carries its app, a title line (often the channel or
sender), and a body ("Author: message" for chat apps).

Official WinRT API through the optional `winsdk` package; the async calls run on
a throwaway thread with their own event loop, safe to call from Continuum's
async loops. Degrades to [] without winsdk, off Windows, or if access is denied
(the user can grant it under Settings → Privacy → Notifications).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("continuum.state.notifications")


def _read_sync() -> list[dict[str, Any]]:
    import asyncio

    box: dict[str, Any] = {}

    def _worker() -> None:
        async def _go():
            from winsdk.windows.ui.notifications.management import (
                UserNotificationListener, UserNotificationListenerAccessStatus)
            from winsdk.windows.ui.notifications import NotificationKinds

            listener = UserNotificationListener.current
            status = await listener.request_access_async()
            if UserNotificationListenerAccessStatus(status) != UserNotificationListenerAccessStatus.ALLOWED:
                return []
            notes = await listener.get_notifications_async(NotificationKinds.TOAST)
            out: list[dict[str, Any]] = []
            for n in notes:
                try:
                    app = n.app_info.display_info.display_name
                except Exception:
                    app = ""
                texts: list[str] = []
                try:
                    binding = None
                    vis = n.notification.visual
                    if vis.bindings.size:
                        binding = vis.bindings[0]
                    if binding is not None:
                        for el in binding.get_text_elements():
                            t = (el.text or "").strip()
                            if t:
                                texts.append(t)
                except Exception:
                    pass
                if not texts:
                    continue
                out.append({"id": int(n.id), "app": app,
                            "title": texts[0], "body": " ".join(texts[1:]).strip()})
            return out

        # new_event_loop() belongs INSIDE the guard. It can fail by itself — no
        # selector available, a policy that refuses, a teardown race — and when
        # it threw out here the worker thread died unhandled, `box` was never
        # written, and the caller fell through to the `[]` default. An empty
        # tray and a listener that never ran look identical from the outside,
        # so the read layer happily reported "no notifications" while nothing
        # had been read at all. Now every failure path writes `box` and logs.
        loop = None
        try:
            loop = asyncio.new_event_loop()
            box["v"] = loop.run_until_complete(_go())
        except Exception:
            log.debug("notification read failed", exc_info=True)
            box["v"] = []
        finally:
            if loop is not None:  # nothing to close if creation is what failed
                loop.close()

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=8)
    return box.get("v", [])


def poll_notifications() -> list[dict[str, Any]]:
    """Current toast notifications: [{id, app, title, body}], newest-in-tray."""
    try:
        import winsdk  # noqa: F401
    except Exception:
        return []
    return _read_sync()
