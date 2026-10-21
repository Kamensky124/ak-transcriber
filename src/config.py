"""Environment configuration for the transcriber daemon (plan §7)."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_INBOX = "/home/andy/org/media/inbox"
DEFAULT_DB = "/var/lib/transcriber/queue.db"
DEFAULT_MODEL = "gemini-3.8-flash"
MODEL_FALLBACKS: dict[str, str] = {
    "gemini-3.8-flash": "gemini-3.5-flash",
    "gemini-3.5-flash": "gemini-3.8-flash",
}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
VALID_MODES = ("transcript", "playbook", "both")


@dataclass(frozen=True)
class Config:
    inbox_roots: tuple[str, ...]
    mode: str
    model: str
    db_path: str
    scan_interval: int
    stable_seconds: int
    max_attempts: int
    fallback_engine: str
    whisper_model: str
    whisper_cpu_threads: int
    api_keys: tuple[str, ...]
    dry_run: bool

    @property
    def model_fallback(self) -> str:
        return MODEL_FALLBACKS.get(self.model, "gemini-3.5-flash")


def _int_env(name: str, default: int, e: dict[str, str]) -> int:
    raw = e.get(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build Config from environment (or an explicit dict, for tests)."""
    e = env if env is not None else os.environ
    inbox = e.get("TRANSCRIBE_INBOX", DEFAULT_INBOX)
    roots = tuple(r for r in (part.strip() for part in inbox.split(":")) if r)
    mode = e.get("TRANSCRIBE_MODE", "both")
    if mode not in VALID_MODES:
        mode = "both"
    keys: list[str] = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        val = e.get(name, "").strip()
        if val:
            keys.append(val)
    return Config(
        inbox_roots=roots or (DEFAULT_INBOX,),
        mode=mode,
        model=e.get("TRANSCRIBE_MODEL", DEFAULT_MODEL),
        db_path=e.get("TRANSCRIBE_DB", DEFAULT_DB),
        scan_interval=_int_env("SCAN_INTERVAL", 30, e),
        stable_seconds=_int_env("STABLE_SECONDS", 10, e),
        max_attempts=_int_env("MAX_ATTEMPTS", 5, e),
        fallback_engine=e.get("FALLBACK_ENGINE", "whisper"),
        whisper_model=e.get("WHISPER_MODEL", "small"),
        whisper_cpu_threads=_int_env("WHISPER_CPU_THREADS", 3, e),
        api_keys=tuple(keys),
        dry_run=e.get("TRANSCRIBE_DRY_RUN", "0") == "1",
    )
