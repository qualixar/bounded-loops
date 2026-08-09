"""Stable typed failures for graph contracts."""

from __future__ import annotations


class GraphError(Exception):
    """Base error for graph-engineering contracts."""


class GraphValidationError(GraphError):
    """A closed authoring-contract violation with a machine-readable code."""

    def __init__(self, code: str, pointer: str, message: str) -> None:
        super().__init__(f"{code} at {pointer}: {message}")
        self.code = code
        self.pointer = pointer
        self.message = message


class GraphIntegrityError(GraphError):
    """A controller event/artifact stream is corrupt or inconsistent."""
