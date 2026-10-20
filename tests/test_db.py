import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import unittest

from db import Database, Job


def make_job(db: Database, path: str = "/x/a.mp3", **kw) -> bool:
    return db.enqueue(path, kw.pop("stem", "a"), kw.pop("mode", "both"),
                      kw.pop("language_hint", ""), kw.pop("size_bytes", 100))


class TestDb(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")

    def test_init_and_dedup_enqueue(self) -> None:
        self.assertTrue(make_job(self.db))
        self.assertFalse(make_job(self.db))  # UNIQUE src_path dedup
        self.assertEqual(self.db.jobs_by_status(), {"queued": 1})

    def test_claim_finish_transition(self) -> None:
        make_job(self.db)
        job = self.db.claim_job(engine="gemini", whisper_capable=False)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.status, "running")
        self.assertEqual(job.engine, "gemini")
        # running jobs are not claimable again
        self.assertIsNone(self.db.claim_job("gemini", False))
        self.db.set_output_flag(job.id, "transcript")
        self.db.set_output_flag(job.id, "playbook")
        self.db.finish_job(job.id, "/x/a_done.mp3")
        fresh = self.db.get_job(job.id)
        assert fresh is not None
        self.assertEqual(fresh.status, "done")
        self.assertEqual(fresh.done_path, "/x/a_done.mp3")

    def test_claim_respects_backoff_gate(self) -> None:
        make_job(self.db)
        job = self.db.claim_job("gemini", False)
        assert job is not None
        self.db.release_job(job.id, 1, 3600, "boom", False)
        self.assertIsNone(self.db.claim_job("gemini", False))
        self.db.release_job(job.id, 0, None, "", False)  # clear gate
        self.assertIsNotNone(self.db.claim_job("gemini", False))

    def test_whisper_claim_skips_playbook_only(self) -> None:
        make_job(self.db, path="/x/p.mp3", stem="p", mode="playbook")
        self.assertIsNone(self.db.claim_job("whisper", whisper_capable=True))
        self.assertIsNotNone(self.db.claim_job("gemini", False))

    def test_crash_recovery(self) -> None:
        make_job(self.db)
        job = self.db.claim_job("gemini", False)
        assert job is not None
        self.assertEqual(self.db.recover_running(), 1)
        fresh = self.db.get_job(job.id)
        assert fresh is not None
        self.assertEqual(fresh.status, "queued")

    def test_cooldown_persist_and_revive(self) -> None:
        self.db.ensure_keys(3)
        self.db.set_cooldown(2, "2099-01-01T00:00:00Z")
        row = self.db.get_key(2)
        assert row is not None
        self.assertEqual(row["state"], "cooldown")
        self.assertFalse(self.db.revive_if_expired(2))
        self.db.clear_cooldown(2)
        self.assertTrue(self.db.revive_if_expired(2))

    def test_quota_release_does_not_count_attempt(self) -> None:
        make_job(self.db)
        job = self.db.claim_job("gemini", False)
        assert job is not None
        self.db.requeue_as_queued(job.id)
        fresh = self.db.get_job(job.id)
        assert fresh is not None
        self.assertEqual(fresh.status, "queued")
        self.assertEqual(fresh.attempts, 0)

    def test_terminal_error(self) -> None:
        make_job(self.db)
        job = self.db.claim_job("gemini", False)
        assert job is not None
        self.db.release_job(job.id, 1, None, "bad media", True)
        fresh = self.db.get_job(job.id)
        assert fresh is not None
        self.assertEqual(fresh.status, "error")
        self.assertEqual(fresh.attempts, 1)

    def test_job_output_flags_and_complete(self) -> None:
        make_job(self.db, path="/x/b.mp3", stem="b", mode="both")
        job = self.db.claim_job("gemini", False)
        assert job is not None
        self.assertFalse(job.complete())
        self.assertTrue(job.needs_transcript() and job.needs_playbook())
        self.db.refresh_output_flags(job.id, True, True)
        fresh = self.db.get_job(job.id)
        assert fresh is not None
        self.assertTrue(fresh.complete())
        # fully-flagged job is not claimable again
        self.assertIsNone(self.db.claim_job("gemini", False))

    def test_unrenamed_sweep_list(self) -> None:
        make_job(self.db)
        job = self.db.claim_job("gemini", False)
        assert job is not None
        self.db.finish_job_unrenamed(job.id)
        self.assertEqual(len(self.db.unrenamed_done_jobs()), 1)
        self.db.finish_job(job.id, "/x/a_done.mp3")
        self.assertEqual(len(self.db.unrenamed_done_jobs()), 0)


if __name__ == "__main__":
    unittest.main()
