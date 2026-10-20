import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tempfile
import unittest
from pathlib import Path

from renamer import done_target, rename_done


class TestDoneTarget(unittest.TestCase):
    def test_simple(self) -> None:
        self.assertEqual(str(done_target(Path("/i/a.mp3"))), "/i/a_done.mp3")

    def test_collision_suffix_pure(self) -> None:
        src = Path("/i/a.mp3")
        self.assertEqual(
            str(done_target(src, {"/i/a_done.mp3"})), "/i/a_done_2.mp3")
        self.assertEqual(
            str(done_target(src, {"/i/a_done.mp3", "/i/a_done_2.mp3"})),
            "/i/a_done_3.mp3")

    def test_collision_with_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.mp3"
            src.write_bytes(b"x")
            (Path(tmp) / "a_done.mp3").write_bytes(b"old")
            self.assertEqual(done_target(src).name, "a_done_2.mp3")


class TestRenameDone(unittest.TestCase):
    def test_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.mp3"
            src.write_bytes(b"x")
            final = rename_done(src)
            assert final is not None
            self.assertEqual(final.name, "a_done.mp3")
            self.assertTrue(final.exists())
            self.assertFalse(src.exists())

    def test_missing_source_returns_none(self) -> None:
        self.assertIsNone(rename_done(Path("/nonexistent/a.mp3")))


if __name__ == "__main__":
    unittest.main()
