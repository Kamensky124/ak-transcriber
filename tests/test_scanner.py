import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os
import tempfile
import unittest
from pathlib import Path

from scanner import (
    Observed,
    ScanState,
    has_cyrillic,
    is_candidate,
    language_hint_for,
    stability_gate,
)
from transcriberd import language_hint_for_path


class TestPureFilters(unittest.TestCase):
    def test_is_candidate(self) -> None:
        self.assertTrue(is_candidate(Path("/i/rec.mp3")))
        self.assertTrue(is_candidate(Path("/i/rec.MP3")))
        self.assertFalse(is_candidate(Path("/i/.hidden.mp3")))
        self.assertFalse(is_candidate(Path("/i/rec.txt")))
        self.assertFalse(is_candidate(Path("/i/rec_done.mp3")))
        self.assertFalse(is_candidate(Path("/i/rec_done.guide.md")))  # irrelevant ext

    def test_cyrillic_heuristic(self) -> None:
        self.assertTrue(has_cyrillic("/i/встреча/rec.mp3"))
        self.assertFalse(has_cyrillic("/i/meeting/rec.mp3"))
        self.assertEqual(language_hint_for("встреча/rec.mp3"), "Russian")
        self.assertEqual(language_hint_for("meeting/rec.mp3"), "")

    def test_hint_uses_own_root_per_file(self) -> None:
        # multi-root inbox: hint must be relative to the root the file is under
        with tempfile.TemporaryDirectory() as tmp:
            en = Path(tmp) / "en" / "meeting"
            ru = Path(tmp) / "ru"
            en.mkdir(parents=True)
            ru.mkdir()
            f_en = en / "rec.mp3"
            f_ru = ru / "встреча.mp3"
            roots = (str(en), str(ru))
            self.assertEqual(language_hint_for_path(f_en, roots), "")
            self.assertEqual(language_hint_for_path(f_ru, roots), "Russian")

    def test_stability_gate(self) -> None:
        now = 1000.0
        cur = Observed(size=10, mtime=now - 60)
        self.assertEqual(stability_gate(None, cur, 10, now).reason, "new")
        self.assertEqual(
            stability_gate(Observed(9, cur.mtime), cur, 10, now).reason, "changed")
        self.assertEqual(
            stability_gate(Observed(10, now - 1), Observed(10, now - 1), 10, now).reason,
            "too-fresh")
        res = stability_gate(Observed(10, cur.mtime), cur, 10, now)
        self.assertTrue(res.stable)
        zero = Observed(size=0, mtime=now - 60)
        self.assertEqual(stability_gate(None, zero, 10, now).reason, "zero-size")
        self.assertEqual(stability_gate(None, zero, 10, now).stable, False)


class TestScanState(unittest.TestCase):
    def test_two_scan_stability_and_dedup_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp3").write_bytes(b"x" * 10)
            (root / ".hidden.mp3").write_bytes(b"x")
            (root / "b_done.mp3").write_bytes(b"x")
            sub = root / "встреча"
            sub.mkdir()
            (sub / "c.mp3").write_bytes(b"x" * 10)
            state = ScanState()
            # mtimes well before the mocked scan clock (now=1000.0)
            os.utime(root / "a.mp3", (900, 900))
            os.utime(sub / "c.mp3", (900, 900))

            ready = state.scan_roots([str(root)], 10, set(), now=1000.0)
            # nothing stable on the first pass
            self.assertEqual(ready, [])

            ready = state.scan_roots([str(root)], 10, set(), now=2000.0)
            names = sorted(p.name for p in ready)
            self.assertEqual(names, ["a.mp3", "c.mp3"])

            # already-known paths are skipped
            known = {str((root / "a.mp3").resolve()), str((sub / "c.mp3").resolve())}
            ready = state.scan_roots([str(root)], 10, known, now=3000.0)
            self.assertEqual(ready, [])

    def test_changed_file_resets_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.mp3"
            f.write_bytes(b"x")
            state = ScanState()
            os.utime(f, (900, 900))
            state.scan_roots([tmp], 0, set(), now=1000.0)
            f.write_bytes(b"yy")  # changed between scans
            os.utime(f, (999, 999))
            ready = state.scan_roots([tmp], 0, set(), now=1000.5)
            self.assertEqual(ready, [])
            # change observed at 1000.5; unchanged at 1001.0 -> stable now
            ready = state.scan_roots([tmp], 0, set(), now=1001.0)
            self.assertEqual([p.name for p in ready], ["a.mp3"])


if __name__ == "__main__":
    unittest.main()
