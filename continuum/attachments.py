"""Attachments — images Jake sends into the chat (a recipe, a label, a photo).

The one store that holds *bytes*, not rows. An upload lands here from
`POST /api/upload`, keyed by a random id; the id is stashed in the user
message's meta (so a reopened thread can re-render the picture) and handed to
the model as context. The model reads what's in the image with the `view_image`
tool, which routes the bytes through the same vision path as `see_screen`.

Kept deliberately dumb: a file on disk plus a tiny JSON sidecar for metadata,
under `data/uploads/`. No database, no dependency — the sidecar is enough to
serve the file back with the right content-type and remember its name.

Trust boundary: ids are validated to bare hex so a fetch/read can never escape
the uploads directory, and only real image bytes (checked by magic number, not
by the client's say-so) are ever written.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("continuum.attachments")

# Only these land on disk, and only when the *bytes* prove it — the client's
# declared mime is never trusted. mime + canonical extension per signature.
_SIGATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
]

_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — a phone photo is a few MB; this is slack


def _sniff(data: bytes) -> Optional[tuple[str, str]]:
    """Return (mime, ext) if the bytes are a supported image, else None."""
    for sig, mime, ext in _SIGATURES:
        if data.startswith(sig):
            return mime, ext
    # WEBP is 'RIFF' <size> 'WEBP'
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


class AttachmentStore:
    def __init__(self, uploads_dir: str) -> None:
        self._dir = Path(uploads_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, name: str = "") -> dict[str, Any]:
        """Validate and persist an uploaded image. Raises ValueError if it is not
        a supported image or is too large. Returns the metadata dict."""
        if not data:
            raise ValueError("empty upload")
        if len(data) > _MAX_BYTES:
            raise ValueError(f"image too large ({len(data)} bytes, max {_MAX_BYTES})")
        sniffed = _sniff(data)
        if not sniffed:
            raise ValueError("unsupported file type (only PNG, JPEG, GIF, WEBP images)")
        mime, ext = sniffed
        aid = uuid.uuid4().hex[:16]
        (self._dir / f"{aid}.{ext}").write_bytes(data)
        meta = {"id": aid, "name": (name or f"image.{ext}")[:120], "mime": mime,
                "ext": ext, "size": len(data), "ts": time.time()}
        (self._dir / f"{aid}.json").write_text(json.dumps(meta), encoding="utf-8")
        log.info("stored attachment %s (%s, %d bytes)", aid, mime, len(data))
        return meta

    def path(self, aid: str) -> Optional[Path]:
        """The stored image file for an id, or None. Jailed to the uploads dir —
        an id that isn't bare hex can't name a path outside it."""
        if not _ID_RE.match(aid or ""):
            return None
        meta = self.meta(aid)
        if meta:
            p = self._dir / f"{aid}.{meta['ext']}"
            return p if p.exists() else None
        # No sidecar (older/partial write): fall back to any matching file.
        for p in self._dir.glob(f"{aid}.*"):
            if p.suffix != ".json":
                return p
        return None

    def meta(self, aid: str) -> Optional[dict[str, Any]]:
        if not _ID_RE.match(aid or ""):
            return None
        side = self._dir / f"{aid}.json"
        if not side.exists():
            return None
        try:
            return json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            return None

    def read(self, aid: str) -> Optional[bytes]:
        p = self.path(aid)
        return p.read_bytes() if p else None
