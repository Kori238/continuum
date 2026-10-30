"""Continuum's memory — the context store (the digital twin).

Model-agnostic by construction. Memories are plain text + metadata that any
chat model reads as injected prompt context, and semantic recall runs on vectors
from a single fixed local embedder — never the chat tier. Swap llama for Claude
for anything else and memory persists and stays usable. That is "cross-model."
"""
from .reason import Extractor, MemoryReasoner
from .store import Memory, MemoryStore, RememberResult

__all__ = ["Memory", "MemoryStore", "RememberResult", "MemoryReasoner", "Extractor"]
