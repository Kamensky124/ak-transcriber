"""Success rename policy: X.mp3 -> X_done.mp3 with collision suffixes (§6.5)."""

from __future__ import annotations

import os
from pathlib import Path


def done_target(src: Path, existing: set[str] | None = None) -> Path:
    """Pure: final *_done.* path, appending _2, _3, ... on collisions.

    `existing` may list absolute path strings already taken (from disk or DB);
    when omitted, the filesystem itself is consulted.
    """
    taken = existing if existing is not None else _disk_existing(src)
    candidate = src.with_name(f"{src.stem}_done{src.suffix}")
    n = 2
    while str(candidate) in taken:
        candidate = src.with_name(f"{src.stem}_done_{n}{src.suffix}")
        n += 1
    return candidate


def _disk_existing(src: Path) -> set[str]:
    taken: set[str] = set()
    candidate = src.with_name(f"{src.stem}_done{src.suffix}")
    n = 2
    while candidate.exists():
        taken.add(str(candidate))
        candidate = src.with_name(f"{src.stem}_done_{n}{src.suffix}")
        n += 1
    return taken


def rename_done(src: Path) -> Path | None:
    """Perform the rename; return final path or None on failure (§6.5).

    Failure (source vanished, EBUSY, ...) is logged by the caller; the job
    stays `done` with done_path NULL and is retried by the sweep.
    """
    if not src.exists():
        return None
    target = done_target(src)
    try:
        os.rename(src, target)
    except OSError:
        return None
    return target
