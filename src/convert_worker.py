"""Converter thread: video inbox files -> MP3 via ffmpeg, then transcribe job.

Profile ported from ~/org/wiki/scripts/convert-video-to-mp3.py
(ffmpeg -vn -c:a libmp3lame -q:a 2, VBR ~190kbps). Originals are moved to a
hidden sibling .originals/ directory; the produced mp3 is enqueued as a normal
kind='transcribe' job.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from config import Config
from db import Database, Job
from keys import backoff_seconds

log = logging.getLogger("converter")

FFMPEG_TIMEOUT = 1800  # 30 minutes
MIN_MP3_BYTES = 1024


class ConvertWorker(threading.Thread):
    def __init__(self, config: Config, db: Database,
                 stop_event: threading.Event) -> None:
        super().__init__(name="converter", daemon=True)
        self.config = config
        self.db = db
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            job = self.db.claim_job(engine="converter", whisper_capable=False,
                                    kind="convert")
            if job is None:
                self.stop_event.wait(5)
                continue
            try:
                self.process(job)
            except Exception as exc:  # defensive: never kill the thread
                log.exception("convert failed on %s", job.src_path)
                self._release(job, exc)

    # -- pipeline ----------------------------------------------------------
    def process(self, job: Job) -> None:
        src = Path(job.src_path)
        out = src.with_suffix(".mp3")
        try:
            if self.config.dry_run:
                # stub "mp3" payload well above the 1KB acceptance threshold
                out.write_bytes(b"\xff\xfb" + b"\x00" * 2048)
            else:
                self._ffmpeg(src, out)
            if not (out.is_file() and out.stat().st_size > MIN_MP3_BYTES):
                raise RuntimeError(
                    f"ffmpeg produced missing or undersized mp3 (<{MIN_MP3_BYTES}B):"
                    f" {out.name}")
        except Exception as exc:
            self._release(job, exc)
            return
        moved = self._move_original(src)
        self.db.finish_job(job.id, str(moved))
        self.db.enqueue(str(out.resolve()), out.stem, self.config.mode,
                        job.language_hint, out.stat().st_size, kind="transcribe")
        log.info("converted %s -> %s (original in %s)", src.name, out.name,
                 moved.parent.name)

    def _ffmpeg(self, src: Path, out: Path) -> None:
        part = out.with_suffix(out.suffix + ".part")
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
               "-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(part)]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg timed out after {FFMPEG_TIMEOUT}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg binary not found") from exc
        if proc.returncode != 0:
            part.unlink(missing_ok=True)
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise RuntimeError("ffmpeg failed: "
                               + (" | ".join(tail) or f"exit {proc.returncode}"))
        os.replace(part, out)

    def _move_original(self, src: Path) -> Path:
        dest_dir = src.parent / ".originals"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / src.name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        os.replace(src, dest)
        return dest

    def _release(self, job: Job, exc: Exception) -> None:
        """Retry/backoff -> terminal error, same policy as other workers."""
        attempts = job.attempts + 1
        terminal = attempts >= self.config.max_attempts
        self.db.release_job(job.id, 1,
                            None if terminal else backoff_seconds(attempts),
                            str(exc)[:500], terminal)
        log.error("convert error on %s (attempt %d): %s",
                  job.src_path, attempts, exc)
