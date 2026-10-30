"""Third-party integrations Continuum pulls from.

Each module here is a thin, self-contained client for an external service. They
are the only code that talks to the outside world on Jake's behalf, so they keep
credentials at arm's length (read from Config, never hardcoded) and fail loudly
rather than silently — a broken pull should surface, not corrupt the local store.
"""
