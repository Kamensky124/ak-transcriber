import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import unittest
from datetime import datetime, timezone

from keys import backoff_seconds, error_class, next_pacific_midnight


class TestPacificMidnight(unittest.TestCase):
    def test_basic_next_midnight(self) -> None:
        now = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)  # 20:00 PDT Oct 19
        self.assertEqual(next_pacific_midnight(now), "2026-10-20T07:00:00Z")

    def test_exactly_midnight_returns_next_day(self) -> None:
        now = datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)  # 00:00 PDT
        self.assertEqual(next_pacific_midnight(now), "2026-10-21T07:00:00Z")

    def test_dst_winter_offset(self) -> None:
        now = datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)  # 21:00 PST Jan 14
        self.assertEqual(next_pacific_midnight(now), "2026-01-15T08:00:00Z")

    def test_naive_input_treated_as_utc(self) -> None:
        naive = datetime(2026, 10, 20, 3, 0)
        self.assertEqual(next_pacific_midnight(naive), "2026-10-20T07:00:00Z")

    def test_dst_spring_transition_day(self) -> None:
        # 2026-03-08: DST starts 02:00 local; next midnight is PST (-8) -> 08:00Z
        now = datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(next_pacific_midnight(now), "2026-03-08T08:00:00Z")


class TestErrorClassification(unittest.TestCase):
    def test_quota(self) -> None:
        for msg in ("429 RESOURCE_EXHAUSTED", "free_tier_requests exceeded",
                    "quota exceeded for project"):
            self.assertEqual(error_class(msg), "quota")

    def test_transient(self) -> None:
        for msg in ("503 unavailable", "connection reset by peer",
                    "Request timed out"):
            self.assertEqual(error_class(msg), "transient")

    def test_permanent(self) -> None:
        self.assertEqual(error_class("Google Files API failed to process x.mp3"),
                         "permanent")
        self.assertEqual(error_class(RuntimeError("bad media")), "permanent")

    def test_quota_wins_over_transient(self) -> None:
        # a 429 is quota, not generic transient
        self.assertEqual(error_class("429 too many requests"), "quota")

    def test_backoff(self) -> None:
        self.assertEqual(backoff_seconds(1), 60)
        self.assertEqual(backoff_seconds(2), 120)
        self.assertEqual(backoff_seconds(5), 300)
        self.assertEqual(backoff_seconds(50), 300)


if __name__ == "__main__":
    unittest.main()
