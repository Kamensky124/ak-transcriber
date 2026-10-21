import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, load_config
from convert_worker import ConvertWorker
from db import Database
from scanner import is_candidate, job_kind


OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  src_path       TEXT NOT NULL UNIQUE,
  stem           TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'queued',
  mode           TEXT NOT NULL DEFAULT 'both',
  language_hint  TEXT NOT NULL DEFAULT '',
  engine         TEXT,
  key_id         INTEGER,
  attempts       INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  transcript_done INTEGER NOT NULL DEFAULT 0,
  playbook_done   INTEGER NOT NULL DEFAULT 0,
  size_bytes     INTEGER,
  done_path      TEXT,
  last_error     TEXT,
  created_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT,
  updated_at     TEXT NOT NULL
);
"""


def make_config(tmp: str, dry_run: bool = False, max_attempts: int = 2) -> object:
    return load_config(env={
        "TRANSCRIBE_INBOX": tmp,
        "TRANSCRIBE_DB": str(Path(tmp) / "queue.db"),
        "TRANSCRIBE_DRY_RUN": "1" if dry_run else "0",
        "MAX_ATTEMPTS": str(max_attempts),
        "STABLE_SECONDS": "0",
    })


class TestScannerKindSplit(unittest.TestCase):
    def test_job_kind_split(self) -> None:
        for ext in AUDIO_EXTENSIONS:
            self.assertEqual(job_kind(Path(f"/i/x{ext}")), "transcribe")
        for ext in VIDEO_EXTENSIONS:
            self.assertEqual(job_kind(Path(f"/i/x{ext}")), "convert")

    def test_video_files_are_candidates(self) -> None:
        self.assertTrue(is_candidate(Path("/i/clip.mp4")))
        self.assertTrue(is_candidate(Path("/i/clip.MKV")))
        self.assertFalse(is_candidate(Path("/i/.clip.mp4")))
        self.assertFalse(is_candidate(Path("/i/clip_done.mp4")))

    def test_scan_enqueues_by_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.mp3"
            video = root / "v.mp4"
            audio.write_bytes(b"x" * 10)
            video.write_bytes(b"x" * 10)
            os.utime(audio, (900, 900))
            os.utime(video, (900, 900))
            from scanner import ScanState
            state = ScanState()
            state.scan_roots([str(root)], 0, set(), now=1000.0)
            config = make_config(tmp)
            from transcriberd import scan_once
            from db import Database
            db = Database(str(root / "queue.db"))
            scan_once(config, db, state)  # noqa: just enqueue via daemon helper
            audio_job = db.find_by_src(str(audio.resolve()))
            video_job = db.find_by_src(str(video.resolve()))
            assert audio_job is not None and video_job is not None
            self.assertEqual(audio_job.kind, "transcribe")
            self.assertEqual(video_job.kind, "convert")
            # partitioned claims: transcribe engine never gets convert jobs
            self.assertIsNone(db.claim_job("gemini", False, kind="convert"))
            gem = db.claim_job("gemini", False)
            conv = db.claim_job("converter", False, kind="convert")
            assert gem is not None and conv is not None
            self.assertEqual(gem.kind, "transcribe")
            self.assertEqual(conv.kind, "convert")
            self.assertIsNone(db.claim_job("converter", False, kind="convert"))


class TestDbKindMigration(unittest.TestCase):
    def test_pre_kind_db_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "queue.db")
            conn = sqlite3.connect(path)
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO jobs (src_path, stem, created_at, updated_at)"
                " VALUES ('/x/old.mp3', 'old', '2024-01-01T00:00:00Z',"
                " '2024-01-01T00:00:00Z')")
            conn.commit()
            conn.close()
            db = Database(path)
            job = db.find_by_src("/x/old.mp3")
            assert job is not None
            self.assertEqual(job.kind, "transcribe")
            # migration is idempotent: reopen
            db2 = Database(path)
            self.assertEqual(db2.jobs_by_kind(), {"transcribe": 1})


FFMPEG = shutil.which("ffmpeg")


@unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
class TestConvertWorker(unittest.TestCase):
    def _make_video(self, root: Path, name: str = "clip.mp4") -> Path:
        src = root / name
        subprocess.run(
            [FFMPEG, "-nostdin", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=128x128:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-shortest", str(src)],
            check=True, capture_output=True)
        return src

    def _enqueue_convert(self, db: Database, src: Path) -> None:
        db.enqueue(str(src.resolve()), src.stem, "both", "", src.stat().st_size,
                   kind="convert")

    def test_success_path_real_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._make_video(root)
            config = make_config(tmp, dry_run=False)
            db = Database(str(root / "queue.db"))
            self._enqueue_convert(db, src)
            job = db.claim_job("converter", False, kind="convert")
            assert job is not None
            worker = ConvertWorker(config, db, threading.Event())
            worker.process(job)

            mp3 = root / "clip.mp3"
            self.assertTrue(mp3.is_file())
            self.assertGreater(mp3.stat().st_size, 1024)
            original = root / ".originals" / "clip.mp4"
            self.assertTrue(original.is_file())
            self.assertFalse(src.exists())
            done = db.get_job(job.id)
            assert done is not None
            self.assertEqual(done.status, "done")
            self.assertEqual(done.done_path, str(original))
            follow = db.find_by_src(str(mp3.resolve()))
            assert follow is not None
            self.assertEqual(follow.kind, "transcribe")
            self.assertEqual(follow.status, "queued")

    def test_failure_path_corrupt_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "bad.mp4"
            src.write_bytes(b"this is not a video" * 100)
            config = make_config(tmp, dry_run=False, max_attempts=2)
            db = Database(str(root / "queue.db"))
            self._enqueue_convert(db, src)
            job = db.claim_job("converter", False, kind="convert")
            assert job is not None
            worker = ConvertWorker(config, db, threading.Event())
            worker.process(job)
            fresh = db.get_job(job.id)
            assert fresh is not None
            self.assertEqual(fresh.status, "queued")
            self.assertEqual(fresh.attempts, 1)
            self.assertIn("ffmpeg", fresh.last_error or "")
            # second failure reaches terminal error
            job2 = db.claim_job("converter", False, kind="convert")
            if job2 is not None:  # backoff gate may block; force clear
                worker.process(job2)
            else:
                db.release_job(job.id, 0, None, "", False)
                job2 = db.claim_job("converter", False, kind="convert")
                assert job2 is not None
                worker.process(job2)
            fresh = db.get_job(job.id)
            assert fresh is not None
            self.assertEqual(fresh.status, "error")
            self.assertEqual(fresh.attempts, 2)
            self.assertFalse((root / "bad.mp3").exists())

    def test_dry_run_stub_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "clip.webm"
            src.write_bytes(b"x" * 50)
            config = make_config(tmp, dry_run=True)
            db = Database(str(root / "queue.db"))
            self._enqueue_convert(db, src)
            job = db.claim_job("converter", False, kind="convert")
            assert job is not None
            worker = ConvertWorker(config, db, threading.Event())
            worker.process(job)
            mp3 = root / "clip.mp3"
            self.assertGreater(mp3.stat().st_size, 1024)
            self.assertTrue((root / ".originals" / "clip.webm").is_file())
            follow = db.find_by_src(str(mp3.resolve()))
            assert follow is not None
            self.assertEqual(follow.kind, "transcribe")


if __name__ == "__main__":
    unittest.main()
