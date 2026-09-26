"""ADK callback handlers.

Callback functions that gate tool calls: the pre-execution risk gate and the
exit policy it shares with the risk-limit checks. In ADK, these are
implemented as tool wrapper functions rather than framework-level hooks.
"""
