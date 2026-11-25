"""Structured errors for the service layer: ``{code, message, hint}``.

The code is stable and machine-readable; the message says what went wrong; the hint says
what to do about it (the parts on a net, the pins of a part, the layers of a result...).
"""

from __future__ import annotations

from typing import Dict, Optional


class ServiceError(Exception):
    """An error the caller (a person, a script, an agent) can act on."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint or ""

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}

    def __str__(self) -> str:
        return self.message + (f" ({self.hint})" if self.hint else "")


# Codes. Keep them stable: scripts and agents branch on them.
FILE_NOT_FOUND = "file_not_found"
FILE_EXISTS = "file_exists"
BOARD_UNREADABLE = "board_unreadable"
BOARD_NOT_OPEN = "board_not_open"
INVALID_PROJECT = "invalid_project"
INVALID_INPUT = "invalid_input"
NET_NOT_FOUND = "net_not_found"
NO_PARTS_ON_NET = "no_parts_on_net"
PART_NOT_ON_NET = "part_not_on_net"
PIN_NOT_ON_NET = "pin_not_on_net"
MISSING_SOURCE = "missing_source"
MISSING_LOAD = "missing_load"
LAYER_NOT_FOUND = "layer_not_found"
RESULT_NOT_FOUND = "result_not_found"
RESULT_EVICTED = "result_evicted"
CUT_MISSES_COPPER = "cut_misses_copper"
INVALID_BOARD_FIELD = "invalid_board_field"
SOLVE_FAILED = "solve_failed"
CANCELLED = "cancelled"
INTERNAL_ERROR = "internal_error"
