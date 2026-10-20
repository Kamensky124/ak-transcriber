import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

"""End-to-end DRY_RUN pipeline: real SQLite tmpdir + tmp inbox, daemon threads.

TRANSCRIBE_DRY_RUN=1 makes workers write stub outputs after ~2s with no
network, so this exercises scanner -> queue -> gemini worker -> renamer.
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from config import load_config
from db import Database
from gemini_worker import GeminiWorker
from transcriberd import scan_once
from whisper_worker import WhisperWorker
from scanner import ScanState


def make_config(tmp: str, **overrides):
    env = {
        "TRANSCRIBE_INBOX": tmp,
        "TRANSCRIBE_MODE": "both",
        "TRANSCRIBE_DB": os.path.join(tmp, "queue.db"),
        "TRANSCRIBE_DRY_RUN": "1",
        "GEMINI_API_KEY": "fake-key-not-a-secret",
        "SCAN_INTERVAL": "1",
        "STABLE_SECONDS": "0",
    }
    env.update(overrides)
    return load_config(env)


class TestEndToEnd(unittest.TestCase):
    def test_dry_run_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            src = inbox / "запись.mp3"
            src.write_bytes(b"\0" * 32)
            old = time.time() - 3600
            os.utime(src, (old, old))

            config = make_config(str(inbox))
            db = Database(config.db_path)
            db.ensure_keys(1)

            # first scan observes; a later scan (file unchanged) enqueues
            state = ScanState()
            enq = 0
            for _ in range(5):
                enq = scan_once(config, db, state)
                if enq:
                    break
                time.sleep(0.05)
            self.assertEqual(enq, 1)
            job = db.claim_job("gemini", False)
            self.assertIsNotNone(job)
            # release it back for the worker
            self.db_release(db, job.id)  # type: ignore[union-attr]

            stop = threading.Event()
            wake = threading.Condition()
            worker = GeminiWorker(config, db, 1, config.api_keys[0], stop, wake)
            worker.start()
            try:
                deadline = time.time() + 20
                while time.time() < deadline:
                    row = db.find_by_src(str(src.resolve()))
                    if row and row.status == "done":
                        break
                    time.sleep(0.2)
                row = db.find_by_src(str(src.resolve()))
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row.status, "done", f"last_error={row.last_error}")
            finally:
                stop.set()
                worker.join(timeout=10)

            self.assertTrue((inbox / "запись.md").exists())
            self.assertTrue((inbox / "запись.guide.md").exists())
            self.assertTrue((inbox / "запись_done.mp3").exists())
            self.assertFalse(src.exists())
            self.assertEqual(row.done_path, str(inbox / "запись_done.mp3"))
            # language heuristic fired on Cyrillic path
            self.assertEqual(row.language_hint, "Russian")

    @staticmethod
    def db_release(db: Database, job_id: int) -> None:
        db.requeue_as_queued(job_id)

    def test_crash_recovery_recomputes_flags_from_disk(self) -> None:
        """§6.6: a crash after writing X.md but before flagging it must not
        re-generate the transcript after restart."""
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            src = inbox / "talk.mp3"
            src.write_bytes(b"\0" * 8)
            config = make_config(str(inbox))
            db = Database(config.db_path)
            db.ensure_keys(1)
            self.assertTrue(db.enqueue(str(src.resolve()), "talk", "both", "", 8))
            job = db.claim_job("gemini", False)
            assert job is not None
            # crash simulation: transcript already on disk, flag not yet set
            (inbox / "talk.md").write_text("- **00:00:00** hi", encoding="utf-8")
            from transcriberd import recover_output_flags
            recover_output_flags(config, db)
            self.assertEqual(db.recover_running(), 1)
            fresh = db.get_job(job.id)
            assert fresh is not None
            self.assertEqual(fresh.transcript_done, 1)
            self.assertEqual(fresh.playbook_done, 0)
            self.assertEqual(fresh.status, "queued")
            # whisper never runs again for the playbook half; only gemini may claim
            self.assertIsNone(db.claim_job("whisper", whisper_capable=True))

    def test_whisper_dry_run_transcript_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            src = inbox / "talk.mp3"
            src.write_bytes(b"\0" * 16)
            config = make_config(str(inbox), GEMINI_API_KEY="")
            db2 = Database(config.db_path)
            self.assertEqual(db2.enqueue(str(src.resolve()), "talk", "both", "", 16), True)
            job = db2.claim_job("whisper", whisper_capable=True)
            self.assertIsNotNone(job)
            db2.requeue_as_queued(job.id)  # type: ignore[union-attr]
            stop = threading.Event()
            wake = threading.Condition()
            w = WhisperWorker(config, db2, stop, wake)
            w.start()
            try:
                deadline = time.time() + 20
                while time.time() < deadline:
                    row = db2.find_by_src(str(src.resolve()))
                    if row and row.transcript_done:
                        break
                    time.sleep(0.2)
                row = db2.find_by_src(str(src.resolve()))
                assert row is not None
                self.assertEqual(row.transcript_done, 1)
                self.assertEqual(row.playbook_done, 0)  # whisper never writes guides
            finally:
                stop.set()
                w.join(timeout=10)
            self.assertTrue((inbox / "talk.md").exists())
            self.assertFalse((inbox / "talk.guide.md").exists())

    def test_whisper_finishes_transcript_only_job(self) -> None:
        """mode='transcript': whisper itself completes + renames the job even
        while all Gemini keys are cooling down (§6.4/§6.5)."""
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            src = inbox / "solo.mp3"
            src.write_bytes(b"\0" * 16)
            config = make_config(str(inbox), TRANSCRIBE_MODE="transcript")
            db2 = Database(config.db_path)
            db2.ensure_keys(1)
            db2.set_cooldown(1, "2099-01-01T00:00:00Z")  # all keys cooling
            self.assertTrue(db2.enqueue(str(src.resolve()), "solo", "transcript", "", 16))
            stop = threading.Event()
            wake = threading.Condition()
            w = WhisperWorker(config, db2, stop, wake)
            w.start()
            try:
                deadline = time.time() + 20
                while time.time() < deadline:
                    row = db2.find_by_src(str(src.resolve()))
                    if row and row.status == "done":
                        break
                    time.sleep(0.2)
            finally:
                stop.set()
                w.join(timeout=10)
            row = db2.find_by_src(str(src.resolve()))
            assert row is not None
            self.assertEqual(row.status, "done")
            self.assertTrue((inbox / "solo_done.mp3").exists())
            self.assertFalse(src.exists())


if __name__ == "__main__":
    unittest.main()
