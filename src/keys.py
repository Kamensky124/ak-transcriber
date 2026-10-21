"""Key scheduler: Pacific-midnight quota reset, error classification (plan §6.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# Substrings that identify an exhausted free-tier quota on a Gemini key.
QUOTA_MARKERS = ("resource_exhausted", "429", "free_tier_requests", "quota")
# Substrings that identify an invalid/revoked API key (key-level fault, not job-level).
AUTH_MARKERS = (
    "401", "unauthenticated", "api_key_invalid", "api key not valid",
    "access_token_type_unsupported", "invalid_api_key",
)
# Transient network/5xx failures worth retrying with backoff.
TRANSIENT_MARKERS = (
    "503", "500", "502", "429", "unavailable", "disconnected",
    "timeout", "timed out", "protocol", "reset", "connection",
)


def next_pacific_midnight(now_utc: datetime) -> str:
    """ISO UTC instant of the next 00:00 America/Los_Angeles after now_utc.

    DST-safe: a midnight that does not exist (spring forward) resolves forward
    to 01:00 local via fold semantics of ZoneInfo arithmetic.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(PACIFIC)
    candidate = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    # Normalize invalid local midnight (DST spring-forward gap) forward.
    if candidate.astimezone(timezone.utc).astimezone(PACIFIC).hour != 0:
        candidate = candidate + timedelta(hours=1)
    return candidate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_class(exc: BaseException | str) -> str:
    """Classify a Gemini failure as 'auth' | 'quota' | 'transient' | 'permanent'."""
    text = str(exc).lower()
    if any(marker in text for marker in AUTH_MARKERS):
        return "auth"
    if any(marker in text for marker in QUOTA_MARKERS):
        return "quota"
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return "transient"
    return "permanent"


def backoff_seconds(attempts: int) -> int:
    """min(300, 30*2^attempts) per plan §6.3 (attempts counted after increment)."""
    return min(300, 30 * (2 ** max(1, attempts)))
