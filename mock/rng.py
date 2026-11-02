"""Named, independent random streams.

Every stream is derived from (seed, *names) with SHA-256, so the draws for one
persona-day or one symbol-day never depend on what else was generated or in which
order. That is what keeps output byte-identical for the same seed.
"""

from __future__ import annotations

import hashlib

import numpy as np


def stream(seed: int, *names: object) -> np.random.Generator:
    key = "|".join([str(seed), *(str(n) for n in names)]).encode()
    digest = hashlib.sha256(key).digest()
    return np.random.default_rng(int.from_bytes(digest[:16], "big"))
