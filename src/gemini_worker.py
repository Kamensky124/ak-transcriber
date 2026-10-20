"""Gemini worker thread: Files-API upload, transcript/playbook generation (§6.3).

Prompts, symlink tmp-upload trick, model fallback chain and retry logic are
ported from scripts/transcribe-gemini.py. `google.genai` is imported lazily so
unit tests run without the package installed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keys import error_class as keys_error_class, next_pacific_midnight, backoff_seconds
from config import Config
from db import Database, Job

log = logging.getLogger("gemini")

TRANSCRIPT_PROMPT = """You are an expert audio transcriptionist.
Provide a faithful, accurate, and complete transcript of this recording.
Format rules:
1. Include millisecond/second timestamps for every paragraph or speaker turn in the format `- **HH:MM:SS** [text]`.
2. Clearly distinguish speakers (e.g. Speaker 1, Speaker 2) if multiple voices are present.
3. Preserve all technical terminology, numbers, proper nouns, and domain vocabulary.
4. Output clean GitHub-flavored Markdown. Do not include conversational filler or conversational preamble like 'Here is your transcript:'."""

PLAYBOOK_PROMPT = """You are an expert analyst and operational systems coach.
Listen carefully to this recording and transform it into a field-ready, highly actionable Playbook & Action Guide (руководство к действию).

Strict Output Format:
# [Topic / Video Title] — Action Guide

> **Core Thesis / Objective**: 1–2 sentence operational definition of the problem solved.

## 1. Antipatterns to Stop Immediately
- **Failure Mode**: What common mistake is made?
- **Root Cause & Impact**: Why does it fail? (psychological, physiological, or tactical reason).
- **Correction**: What to replace it with.

## 2. Core Mental Model & Strategic Rules
- Fundamental rule or framework to memorize before executing.

## 3. Step-by-Step Execution Protocol
1. **Step 1: [Phase Name]** — Specific action, timing, and goal.
2. **Step 2: [Phase Name]** — Diagnostic questions or actions to execute.
3. **Step 3: [Phase Name]** — Step-by-step closure and next milestone.

## 4. Key Phrasing, Quotes & Principles
> Exact phrasing, core principles, key distinctions, or formulas extracted directly from the audio.

## 5. Field Execution Checklist
- [ ] Task 1 (Pre-execution preparation)
- [ ] Task 2 (Execution cue)
- [ ] Task 3 (Follow-up / Verification)

Rules:
- Strip all conversational filler, sponsor plugs, and intro banter.
- Write in direct, prescriptive imperative mode ("Do this", "Never say X, say Y"). Do NOT write passively ("The speaker explains...").
- Preserve all domain terminology, numbers, benchmarks, and verbatim scripts.
- Output in clean Markdown matching the language of the audio (or Russian if requested)."""


def lang_instruction(language_hint: str) -> str:
    if not language_hint:
        return ""
    return (f"\nNote: The spoken language is primarily {language_hint}. "
            f"Transcribe verbatim in {language_hint} without translating to English.")


def atomic_write(path: Path, text: str) -> bool:
    """Atomic .tmp + os.replace write; refuses empty output (§6.3)."""
    text = text.strip()
    if not text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def symlink_upload_path(src: Path) -> Path:
    """Unique /tmp symlink so the Files API sees a plain file path."""
    suffix = src.suffix.lower()
    return Path(f"/tmp/gemini_upload_{os.getpid()}_{threading.get_ident()}"
                f"_{abs(hash(src.name)) % 1000000}{suffix}")


class GeminiWorker(threading.Thread):
    def __init__(self, config: Config, db: Database, key_id: int,
                 api_key: str, stop_event: threading.Event,
                 whisper_wake: threading.Condition) -> None:
        super().__init__(name=f"gemini-key{key_id}", daemon=True)
        self.config = config
        self.db = db
        self.key_id = key_id
        self.api_key = api_key
        self.stop_event = stop_event
        self.whisper_wake = whisper_wake
        self._client: Any = None

    # -- client -----------------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai  # lazy: absent on test host
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            if not self.db.revive_if_expired(self.key_id):
                if self.db.active_key_count(len(self.config.api_keys)) == 0:
                    with self.whisper_wake:
                        self.whisper_wake.notify_all()
                self.stop_event.wait(30)
                continue
            job = self.db.claim_job(engine="gemini", whisper_capable=False)
            if job is None:
                self.stop_event.wait(5)
                continue
            try:
                self.process(job)
            except Exception:  # defensive: never kill the thread
                log.exception("unhandled error on job %s", job.src_path)
                self.db.release_job(job.id, 0, 60, "unhandled worker error", False)
            if self.db.active_key_count(len(self.config.api_keys)) == 0:
                with self.whisper_wake:
                    self.whisper_wake.notify_all()

    # -- per-job pipeline --------------------------------------------------
    def process(self, job: Job) -> None:
        src = Path(job.src_path)
        if self.config.dry_run:
            self._dry_run(job, src)
            return
        try:
            uploaded = self._upload(src)
        except Exception as exc:
            self._classify_and_release(job, exc)
            return
        try:
            if uploaded.state.name == "FAILED":
                raise RuntimeError(f"Google Files API failed to process {src.name}")
            wait_cycles = 0
            while uploaded.state.name == "PROCESSING":
                time.sleep(2)
                wait_cycles += 1
                if wait_cycles > 180:
                    raise TimeoutError("File processing timed out in Google Files API")
                uploaded = self._get_client().files.get(name=uploaded.name)

            if job.needs_transcript():
                text = self._generate(
                    uploaded, TRANSCRIPT_PROMPT + lang_instruction(job.language_hint))
                if not atomic_write(src.parent / f"{job.stem}.md", text):
                    raise RuntimeError("empty transcript response after retries")
                self.db.set_output_flag(job.id, "transcript")
            if job.needs_playbook():
                text = self._generate(
                    uploaded, PLAYBOOK_PROMPT + lang_instruction(job.language_hint))
                if not atomic_write(src.parent / f"{job.stem}.guide.md", text):
                    raise RuntimeError("empty playbook response after retries")
                self.db.set_output_flag(job.id, "playbook")
            self.db.bump_req_count(self.key_id)
            self._maybe_finish(job, src)
        except Exception as exc:
            self._classify_and_release(job, exc)
        finally:
            try:
                self._get_client().files.delete(name=uploaded.name)
            except Exception:
                pass

    def _maybe_finish(self, job: Job, src: Path) -> None:
        fresh = self.db.get_job(job.id)
        if fresh is None or not fresh.complete():
            return
        from renamer import rename_done
        final = rename_done(src)
        if final is None:
            log.error("rename failed for %s; sweep will retry", src)
            self.db.finish_job_unrenamed(job.id)
        else:
            self.db.finish_job(job.id, str(final))
            log.info("done: %s -> %s", src.name, final.name)

    # -- uploads & generation ----------------------------------------------
    def _upload(self, src: Path) -> Any:
        tmp_link = symlink_upload_path(src)
        try:
            tmp_link.unlink(missing_ok=True)
            tmp_link.symlink_to(src)
            return self._get_client().files.upload(file=str(tmp_link))
        finally:
            tmp_link.unlink(missing_ok=True)

    def _generate(self, uploaded: Any, prompt: str) -> str:
        from google.genai import types  # lazy
        gen_config = types.GenerateContentConfig(max_output_tokens=65536)
        models = [self.config.model]
        if self.config.model_fallback not in models:
            models.append(self.config.model_fallback)
        last_err: Exception | None = None
        for model in models:
            for attempt in range(1, 5):
                try:
                    response = self._get_client().models.generate_content(
                        model=model, contents=[uploaded, prompt], config=gen_config)
                    return response.text.strip()
                except Exception as exc:
                    last_err = exc
                    kind = keys_error_class(exc)
                    if kind == "quota":
                        raise
                    if kind == "transient" and attempt < 4:
                        time.sleep(3 * attempt)
                        continue
                    break
        assert last_err is not None
        raise last_err

    # -- error handling ------------------------------------------------------
    def _classify_and_release(self, job: Job, exc: Exception) -> None:
        kind = keys_error_class(exc)
        if kind == "quota":
            until = next_pacific_midnight(datetime.now(timezone.utc))
            self.db.set_cooldown(self.key_id, until)
            self.db.requeue_as_queued(job.id)
            log.warning("key %d quota exhausted until %s", self.key_id, until)
        elif kind == "transient":
            self.db.release_job(job.id, 1, backoff_seconds(job.attempts + 1),
                                str(exc)[:500], False)
            log.warning("transient error on %s: %s", job.src_path, exc)
        else:
            attempts = job.attempts + 1
            terminal = attempts >= self.config.max_attempts
            # non-terminal permanent errors still back off to avoid a hot loop
            self.db.release_job(job.id, 1,
                                None if terminal else backoff_seconds(attempts),
                                str(exc)[:500], terminal)
            log.error("%s error on %s (attempt %d): %s",
                      kind, job.src_path, attempts, exc)

    # -- dry run ---------------------------------------------------------
    def _dry_run(self, job: Job, src: Path) -> None:
        time.sleep(2)
        if job.needs_transcript():
            atomic_write(src.parent / f"{job.stem}.md",
                         f"- **00:00:00** [dry-run transcript stub for {src.name}]")
            self.db.set_output_flag(job.id, "transcript")
        if job.needs_playbook():
            atomic_write(src.parent / f"{job.stem}.guide.md",
                         f"# {src.stem} — Action Guide (dry-run stub)")
            self.db.set_output_flag(job.id, "playbook")
        self._maybe_finish(job, src)
