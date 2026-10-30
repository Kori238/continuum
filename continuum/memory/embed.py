"""The embedder — the single, fixed model that makes memory cross-model.

Every memory and every query is embedded by the SAME model, independent of which
chat model handled the task. That is the whole trick: a memory written while one
model was answering is recalled just as well when another is. The chat tier and
the embedding tier are deliberately decoupled.

This used to be `nomic-embed-text` on Ollama. It now runs IN-PROCESS via
fastembed (ONNX runtime — no torch, no daemon, no separate install), because:

  * Anthropic has no embeddings endpoint, so "route everything to Claude" could
    never cover this. Something local was always going to do the embedding.
  * The Ollama dependency made memory fail *silently* whenever the daemon was
    down — `embed()` had no fallback and every caller swallowed the exception,
    so you answered without recall and were never told.

`BAAI/bge-small-en-v1.5` was picked on measurement, not reputation. Against the
incumbent on Jake's real store it agreed on 0.71 of top-3 hits and 0.88 of top-1
(model2vec's static embeddings managed 0.46/0.25 — much faster, much worse), at
2.8 ms per embed, ~1 s cold start and ~280 MB resident. See docs/DATA.md.

CAUTION: changing the model changes the VECTOR SPACE. `store.py` deliberately
skips rows embedded by a different model rather than mixing incomparable vectors,
so every stored memory must be re-embedded on a change or recall silently returns
nothing. `_reembed.py` does that.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from ..config import Config

log = logging.getLogger("continuum.memory.embed")


class Embedder:
    # One model per process, shared by every Embedder instance. Loading is ~1 s
    # and ~280 MB; two copies would be pure waste, and several stores are built
    # across the app (server, supervisor, one-off scripts).
    _shared: dict[str, object] = {}
    _lock = threading.Lock()

    def __init__(self, cfg: Config) -> None:
        self.model = cfg.embed_model

    def _load(self):
        """Load (and on first ever run, download ~130 MB of weights). Blocking —
        always call this via a thread, never straight off the event loop."""
        with self._lock:
            if self.model not in self._shared:
                from fastembed import TextEmbedding
                log.info("loading embedder %s (first run downloads weights)", self.model)
                self._shared[self.model] = TextEmbedding(model_name=self.model)
                log.info("embedder %s ready", self.model)
            return self._shared[self.model]

    async def available(self) -> bool:
        """True when the embedder can actually embed. Unlike the old HTTP probe
        this is not a liveness check on someone else's daemon — it either loads
        in this process or it doesn't."""
        try:
            await asyncio.to_thread(self._load)
            return True
        except Exception:
            log.warning("embedder %s unavailable", self.model, exc_info=True)
            return False

    async def embed(self, text: str) -> list[float]:
        def _run() -> list[float]:
            model = self._load()
            # fastembed is batch-first and returns a generator of numpy arrays.
            return [float(x) for x in next(iter(model.embed([text])))]

        return await asyncio.to_thread(_run)
