"""Seeded PRNG, bit-exact port of betting-companion/src/engine/prng.ts.

JavaScript 32-bit semantics (``>>> 0``, ``Math.imul``, ToInt32 coercion on
``^``) are emulated with unsigned arithmetic masked to 32 bits — XOR, add,
and multiply are congruent mod 2**32 regardless of signedness, so masking
after every operation reproduces the JS sequence exactly. Same seed ⇒ same
floats as the app's risk check.
"""

from __future__ import annotations

from typing import Callable

_MASK = 0xFFFFFFFF


def mulberry32(seed: int) -> Callable[[], float]:
    """Return a generator of floats in [0, 1); same seed → same sequence."""
    state = seed & _MASK

    def next_float() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        t = state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t = (t ^ (t + (((t ^ (t >> 7)) * (t | 61)) & _MASK))) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    return next_float


def hash_to_seed(text: str) -> int:
    """FNV-1a 32-bit hash over UTF-16 code units (JS charCodeAt semantics)."""
    h = 0x811C9DC5
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        h ^= unit
        h = (h * 0x01000193) & _MASK
    return h
