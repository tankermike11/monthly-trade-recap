"""Sending (out of scope for this build, SPEC 5.3). The monthly run ends at the archive
and its summary; nothing is ever sent automatically. This is the interface a sender
will implement.
"""

from __future__ import annotations

from pathlib import Path


def send(run_dir: Path, customer_ids: list[str]) -> None:
    """Send the recaps of an archived, reviewed run to the given customers."""
    raise NotImplementedError("Sending is out of scope for this build; recaps stay in the archive.")
