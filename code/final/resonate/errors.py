"""The two ways a store or queue refuses. They demand opposite responses."""

from __future__ import annotations


class Conflict(Exception):
    """The write lost a race: the object is not at the generation the write
    required. The decision was made against state that no longer exists, so it
    must be re-decided, never replayed."""


class Unavailable(Exception):
    """No answer. Nothing is known about whether the write landed."""
