"""SQLite queue: schema (plan §5), per-thread connections, short transactions (WAL)."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS keys (
  key_id           INTEGER PRIMARY KEY,
  state            TEXT NOT NULL DEFAULT 'active',
  exhausted_until  TEXT,
  req_count        INTEGER NOT NULL DEFAULT 0,
  req_date         TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Job:
    id: int
    src_path: str
    stem: str
    status: str
    mode: str
    language_hint: str
    attempts: int
    next_attempt_at: str | None
    transcript_done: int
    playbook_done: int
    done_path: str | None
    engine: str | None = None
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"], src_path=row["src_path"], stem=row["stem"],
            status=row["status"], mode=row["mode"], language_hint=row["language_hint"],
            attempts=row["attempts"], next_attempt_at=row["next_attempt_at"],
            transcript_done=row["transcript_done"], playbook_done=row["playbook_done"],
            done_path=row["done_path"], engine=row["engine"],
            last_error=row["last_error"],
        )

    def needs_transcript(self) -> bool:
        return self.mode in ("transcript", "both") and not self.transcript_done

    def needs_playbook(self) -> bool:
        return self.mode in ("playbook", "both") and not self.playbook_done

    def complete(self) -> bool:
        return not self.needs_transcript() and not self.needs_playbook()


class Database:
    """Thread-safe wrapper: each thread gets its own connection (WAL)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.conn()
        conn.executescript(SCHEMA)
        conn.commit()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _touch(self, job_id: int, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (utcnow_iso(), job_id))

    # ---- jobs -----------------------------------------------------------
    def enqueue(self, src_path: str, stem: str, mode: str, language_hint: str,
                size_bytes: int | None) -> bool:
        """Insert a queued job; return False if src_path already known (dedup)."""
        now = utcnow_iso()
        conn = self.conn()
        try:
            with conn:  # short transaction
                conn.execute(
                    "INSERT INTO jobs (src_path, stem, mode, language_hint, size_bytes,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (src_path, stem, mode, language_hint, size_bytes, now, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_job(self, job_id: int) -> Job | None:
        row = self.conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def find_by_src(self, src_path: str) -> Job | None:
        row = self.conn().execute("SELECT * FROM jobs WHERE src_path=?", (src_path,)).fetchone()
        return Job.from_row(row) if row else None

    def jobs_by_status(self) -> dict[str, int]:
        rows = self.conn().execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def jobs_with_status(self, status: str) -> list[Job]:
        rows = self.conn().execute("SELECT * FROM jobs WHERE status=?", (status,)).fetchall()
        return [Job.from_row(r) for r in rows]

    def recover_running(self) -> int:
        """Startup recovery (§6.6): running -> queued."""
        conn = self.conn()
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET status='queued', updated_at=? WHERE status='running'",
                (utcnow_iso(),),
            )
        return cur.rowcount

    def claim_job(self, engine: str, whisper_capable: bool) -> Job | None:
        """Atomically claim the oldest eligible queued job for this engine.

        Gemini can produce both outputs; whisper only transcripts.
        """
        conn = self.conn()
        now = utcnow_iso()
        with conn:  # BEGIN IMMEDIATE via sqlite3 write lock inside transaction
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='queued'"
                " AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
                " ORDER BY created_at, id",
                (now,),
            ).fetchall()
            for row in rows:
                job = Job.from_row(row)
                if job.complete():
                    continue
                if whisper_capable and not job.needs_transcript():
                    continue  # whisper has nothing to contribute
                cur = conn.execute(
                    "UPDATE jobs SET status='running', engine=?, started_at=COALESCE"
                    "(started_at,?), updated_at=? WHERE id=? AND status='queued'",
                    (engine, now, now, job.id),
                )
                if cur.rowcount == 1:
                    return self.get_job(job.id)
        return None

    def set_output_flag(self, job_id: int, kind: str) -> None:
        assert kind in ("transcript", "playbook")
        conn = self.conn()
        with conn:
            conn.execute(
                f"UPDATE jobs SET {kind}_done=1, updated_at=? WHERE id=?",
                (utcnow_iso(), job_id),
            )

    def refresh_output_flags(self, job_id: int, transcript: bool, playbook: bool) -> None:
        conn = self.conn()
        with conn:
            conn.execute(
                "UPDATE jobs SET transcript_done=?, playbook_done=?, updated_at=? WHERE id=?",
                (int(transcript), int(playbook), utcnow_iso(), job_id),
            )

    def finish_job(self, job_id: int, done_path: str) -> None:
        conn = self.conn()
        with conn:
            conn.execute(
                "UPDATE jobs SET status='done', done_path=?, finished_at=?, updated_at=?"
                " WHERE id=?",
                (done_path, utcnow_iso(), utcnow_iso(), job_id),
            )

    def finish_job_unrenamed(self, job_id: int) -> None:
        """Done but rename failed: done_path stays NULL for the housekeeping sweep."""
        conn = self.conn()
        with conn:
            conn.execute(
                "UPDATE jobs SET status='done', finished_at=?, updated_at=? WHERE id=?",
                (utcnow_iso(), utcnow_iso(), job_id),
            )

    def release_job(self, job_id: int, attempts_delta: int, backoff_seconds: int | None,
                    error: str, terminal: bool) -> None:
        """Quota/transient release (queued, backoff) or terminal error state."""
        conn = self.conn()
        now = utcnow_iso()
        with conn:
            if terminal:
                conn.execute(
                    "UPDATE jobs SET status='error', attempts=attempts+?, last_error=?,"
                    " finished_at=?, updated_at=? WHERE id=?",
                    (attempts_delta, error, now, now, job_id),
                )
            else:
                next_at = None
                if backoff_seconds is not None:
                    from datetime import datetime, timedelta
                    dt = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
                    next_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "UPDATE jobs SET status='queued', attempts=attempts+?,"
                    " next_attempt_at=?, last_error=?, updated_at=? WHERE id=?",
                    (attempts_delta, next_at, error, now, job_id),
                )

    def requeue_as_queued(self, job_id: int) -> None:
        """Quota cooldown: release job WITHOUT counting an attempt (§6.2)."""
        conn = self.conn()
        with conn:
            conn.execute(
                "UPDATE jobs SET status='queued', updated_at=? WHERE id=?",
                (utcnow_iso(), job_id),
            )

    # ---- keys -----------------------------------------------------------
    def ensure_keys(self, n: int) -> None:
        conn = self.conn()
        with conn:
            for key_id in range(1, n + 1):
                conn.execute(
                    "INSERT OR IGNORE INTO keys (key_id, state) VALUES (?, 'active')",
                    (key_id,),
                )

    def get_key(self, key_id: int) -> sqlite3.Row | None:
        return self.conn().execute(
            "SELECT * FROM keys WHERE key_id=?", (key_id,)).fetchone()

    def set_cooldown(self, key_id: int, exhausted_until: str) -> None:
        conn = self.conn()
        with conn:
            conn.execute(
                "UPDATE keys SET state='cooldown', exhausted_until=? WHERE key_id=?",
                (exhausted_until, key_id),
            )

    def clear_cooldown(self, key_id: int) -> None:
        conn = self.conn()
        with conn:
            conn.execute(
                "UPDATE keys SET state='active', exhausted_until=NULL WHERE key_id=?",
                (key_id,),
            )

    def revive_if_expired(self, key_id: int) -> bool:
        """Expired cooldown -> active. Returns True if key is active afterwards."""
        row = self.get_key(key_id)
        if row is None or row["state"] != "cooldown":
            return True
        if row["exhausted_until"] and row["exhausted_until"] > utcnow_iso():
            return False
        self.clear_cooldown(key_id)
        return True

    def bump_req_count(self, key_id: int) -> None:
        conn = self.conn()
        today = utcnow_iso()[:10]
        with conn:
            conn.execute(
                "UPDATE keys SET req_count=CASE WHEN req_date=? THEN req_count+1 ELSE 1"
                " END, req_date=? WHERE key_id=?",
                (today, today, key_id),
            )

    def active_key_count(self, n_keys: int) -> int:
        return sum(1 for kid in range(1, n_keys + 1) if self.revive_if_expired(kid))

    # ---- misc -----------------------------------------------------------
    def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn().execute(
            "SELECT id, src_path, status, engine, attempts, last_error, updated_at"
            " FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def unrenamed_done_jobs(self) -> list[Job]:
        """Done jobs still missing done_path (housekeeping sweep, §6.5)."""
        rows = self.conn().execute(
            "SELECT * FROM jobs WHERE status='done' AND done_path IS NULL").fetchall()
        return [Job.from_row(r) for r in rows]
