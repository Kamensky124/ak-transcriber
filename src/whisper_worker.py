"""Whisper fallback thread: transcripts only while zero Gemini keys active (§6.4).

`faster_whisper` is imported lazily so tests run without it installed.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from config import Config
from db import Database

log = logging.getLogger("whisper")


def timestamp(seconds: float) -> str:
    """HH:MM:SS.mmm timestamp used for the bullet .md format."""
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{milliseconds:03}"


def segments_to_markdown(source_name: str, segments: list[tuple[float, str]]) -> str:
    """Timestamped bullet .md, matching the Track B whisper output format."""
    lines = [f"# Transcript: {source_name}", ""]
    for start, text in segments:
        lines.append(f"- **{timestamp(start)}** {text.strip()}")
    return "\n".join(lines) + "\n"


class WhisperWorker(threading.Thread):
    def __init__(self, config: Config, db: Database, stop_event: threading.Event,
                 whisper_wake: threading.Condition) -> None:
        super().__init__(name="whisper-fallback", daemon=True)
        self.config = config
        self.db = db
        self.stop_event = stop_event
        self.whisper_wake = whisper_wake
        self._model: Any = None

    def _get_model(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy
            self._model = WhisperModel(
                self.config.whisper_model, device="cpu", compute_type="int8",
                cpu_threads=self.config.whisper_cpu_threads,
            )
        return self._model

    def run(self) -> None:
        if self.config.fallback_engine != "whisper":
            return
        while not self.stop_event.is_set():
            if self.db.active_key_count(len(self.config.api_keys)) > 0:
                with self.whisper_wake:
                    self.whisper_wake.wait(timeout=5)
                continue
            job = self.db.claim_job(engine="whisper", whisper_capable=True)
            if job is None:
                self.stop_event.wait(5)
                continue
            src = Path(job.src_path)
            if not src.exists():
                log.warning("whisper source file missing: %s, dropping job", src)
                self.db.release_job(job.id, 1, 0, "file missing", True)
                continue
            try:
                self._process(job.id, job.src_path, job.stem)
            except Exception:  # defensive
                log.exception("whisper failed on %s", job.src_path)
                self.db.release_job(job.id, 1, 60, "whisper failure", False)
            # A revived Gemini key takes over again; loop back and wait while
            # any key is active (fallback resumes when all keys cool down).

    def _finish_if_complete(self, job_id: int) -> None:
        """Transcript-only jobs (mode='transcript') are complete after whisper;
        mode='both' stays queued for a Gemini worker to add the playbook."""
        fresh = self.db.get_job(job_id)
        if fresh is None or not fresh.complete():
            return
        from renamer import rename_done
        src = Path(fresh.src_path)
        final = rename_done(src)
        if final is None:
            log.error("rename failed for %s; sweep will retry", src)
            self.db.finish_job_unrenamed(job_id)
        else:
            self.db.finish_job(job_id, str(final))
            log.info("done: %s -> %s", src.name, final.name)

    def _process(self, job_id: int, src_path: str, stem: str) -> None:
        src = Path(src_path)
        if self.config.dry_run:
            time.sleep(2)
            from gemini_worker import atomic_write
            atomic_write(src.parent / f"{stem}.md",
                         f"# Transcript: {src.name}\n\n- **00:00:00.000** [whisper dry-run stub]")
            self.db.set_output_flag(job_id, "transcript")
            self._finish_if_complete(job_id)
            return
        segments_iter, _info = self._get_model().transcribe(str(src))
        segments = [(seg.start, seg.text) for seg in segments_iter]
        from gemini_worker import atomic_write
        if not atomic_write(src.parent / f"{stem}.md",
                            segments_to_markdown(src.name, segments)):
            raise RuntimeError("whisper produced no segments")
        self.db.set_output_flag(job_id, "transcript")
        self._finish_if_complete(job_id)
