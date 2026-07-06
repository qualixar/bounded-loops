"""
api.py — a small public API surface (and one function missing annotations
the checker should flag).
"""
from __future__ import annotations


def get_user(user_id: int) -> dict:
    """Return a user record by id."""
    return {"id": user_id, "name": "placeholder"}


def create_user(name, email: str) -> dict:
    """Create a new user record."""
    return {"name": name, "email": email}


def _internal_helper(payload):
    """Private helper — not part of the public contract, may stay bare."""
    return payload


def delete_user(user_id: int) -> None:
    """Delete a user record by id."""
    return None
