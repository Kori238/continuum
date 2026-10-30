"""Continuum — a personal operating system.

Layers (see README):
  events   — the vocabulary everything speaks
  bus      — the spine: publish/subscribe (in-process now, NATS next)
  context  — the digital twin (current state + history)
  router   — dynamic local/cloud model allocation
  agent    — the brain: consumes events, decides, acts
  devices  — the fabric: each device registers its capabilities
"""

__version__ = "0.0.1"
