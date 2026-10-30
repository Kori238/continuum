"""Continuum's desktop face — an interface layer that consumes the bus.

Architecturally this is just another interface (layer 6): it boots the same
fabric the supervisor does, subscribes to the bus to stream events to the UI,
and publishes task.request events when you submit work. The core knows nothing
about it. Swapping the transport to NATS later would not touch a line here.
"""
