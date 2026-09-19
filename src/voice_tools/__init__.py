"""voice_tools: example external-tool integrations for an AI voice receptionist.

Every tool call runs through :class:`ToolRuntime`, which enforces the
project's hard rule:

    The assistant NEVER guesses. A failed tool call degrades to a fallback
    or escalates to a human -- it never invents names, times, or bookings.

All external systems sit behind adapter interfaces
(:mod:`voice_tools.adapters.base`) so the simulated adapters used here can be
swapped for real API clients without touching tool logic.
"""

from voice_tools.runtime import ToolResult, ToolRuntime

__all__ = ["ToolResult", "ToolRuntime"]
