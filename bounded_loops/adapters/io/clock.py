"""
UtcClock — concrete `ClockPort` adapter.

The one permitted source of wall-clock time in the whole codebase.
Domain code never calls datetime.now(); every timestamp flows through
this adapter (or a test double) via dependency injection.
"""

from __future__ import annotations

import datetime


class UtcClock:
    """Implements ClockPort. Returns UTC timestamps as ISO8601 strings."""

    def now_iso(self) -> str:
        """Return the current UTC time as YYYY-MM-DDTHH:MM:SS.ffffffZ."""
        dt = datetime.datetime.now(tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
