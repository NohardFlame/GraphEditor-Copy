"""Shared normalization for canonical and extraction layers."""

import re

# Trivial punctuation to remove (per comparator prompt: keep letters/numbers).
_TRIVIAL_PUNCTUATION = re.compile(r"[.,;:!?\"'()\[\]{}]+")


def norm(s: str) -> str:
    """Normalize string: lowercase, strip, collapse spaces, remove trivial punctuation.
    Matches comparator prompt: keep letters/numbers only for comparison."""
    if not s:
        return ""
    t = s.lower().strip()
    t = " ".join(t.split())
    t = _TRIVIAL_PUNCTUATION.sub("", t)
    return t
