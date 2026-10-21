"""Inbox scanner: recursive walk, filters, stability gate, language heuristic (§6.1).

Pure functions are kept testable without IO; the ScanState class carries the
(size, mtime, first_seen) memory between consecutive scans.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from config import MEDIA_EXTENSIONS, VIDEO_EXTENSIONS


def has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04FF" for ch in text)


def language_hint_for(relative_path: str) -> str:
    """'Russian' if any Cyrillic codepoint below the inbox root, else ''."""
    return "Russian" if has_cyrillic(relative_path) else ""


def is_candidate(path: Path) -> bool:
    """Extension/hidden/_done filters for a media file path."""
    if any(part.startswith(".") and part not in (".", "..") for part in path.parts):
        return False
    if path.suffix.lower() not in MEDIA_EXTENSIONS:
        return False
    if path.stem.endswith("_done"):
        return False
    return True


def job_kind(path: Path) -> str:
    """'convert' for video sources (must become mp3 first), else 'transcribe'."""
    return "convert" if path.suffix.lower() in VIDEO_EXTENSIONS else "transcribe"


@dataclass(frozen=True)
class Observed:
    size: int
    mtime: float


@dataclass(frozen=True)
class GateResult:
    stable: bool
    reason: str  # 'unchanged' | 'new' | 'changed' | 'too-fresh' | 'zero-size'


def stability_gate(prev: Observed | None, current: Observed,
                   stable_seconds: int, now: float) -> GateResult:
    """A file is enqueuable only when (size, mtime) unchanged across two
    consecutive scans AND age >= stable_seconds (§6.1)."""
    if current.size == 0:
        return GateResult(False, "zero-size")
    if prev is None:
        return GateResult(False, "new")
    if prev != current:
        return GateResult(False, "changed")
    if now - current.mtime < stable_seconds:
        return GateResult(False, "too-fresh")
    return GateResult(True, "unchanged")


class ScanState:
    """Remembers observations between scans; pending files become stable on
    the next pass. Not persisted — a restart just re-learns stability, and the
    jobs table dedup prevents double enqueue."""

    def __init__(self) -> None:
        self._seen: dict[str, Observed] = {}

    def scan_roots(self, roots: list[str], stable_seconds: int,
                   known_paths: set[str], now: float | None = None
                   ) -> list[Path]:
        """Return newly-stable, not-yet-enqueued candidate files."""
        now = time.time() if now is None else now
        ready: list[Path] = []
        current: dict[str, Observed] = {}
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.is_dir():
                continue
            for path in sorted(root_path.rglob("*")):
                if not path.is_file() or not is_candidate(path):
                    continue
                key = str(path.resolve())
                try:
                    stat = path.stat()
                except OSError:
                    continue
                obs = Observed(size=stat.st_size, mtime=stat.st_mtime)
                current[key] = obs
                if key in known_paths:
                    continue
                result = stability_gate(self._seen.get(key), obs, stable_seconds, now)
                if result.stable:
                    ready.append(path)
        self._seen = current
        return ready
