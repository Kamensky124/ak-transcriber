"""transcriberd — queue daemon entrypoint (plan §6.7).

Commands: run (default) | status | enqueue | reset-key | doctor.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import Config, load_config
from db import Database
from keys import next_pacific_midnight
from scanner import ScanState, language_hint_for
from whisper_worker import WhisperWorker

log = logging.getLogger("transcriberd")


def known_paths(db: Database) -> set[str]:
    rows = db.conn().execute("SELECT src_path FROM jobs").fetchall()
    return {r["src_path"] for r in rows}


def language_hint_for_path(path: Path, roots: tuple[str, ...]) -> str:
    """Cyrillic heuristic applied to the path below its inbox root (§6.1)."""
    resolved = path.resolve()
    for root in roots:
        try:
            return language_hint_for(str(resolved.relative_to(Path(root).resolve())))
        except ValueError:
            continue
    return language_hint_for(path.name)


def scan_once(config: Config, db: Database, state: ScanState) -> int:
    """One inbox pass; enqueue stable candidates. Returns count enqueued."""
    candidates = state.scan_roots(
        list(config.inbox_roots), config.stable_seconds, known_paths(db))
    enqueued = 0
    for path in candidates:
        hint = language_hint_for_path(path, config.inbox_roots)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if db.enqueue(str(path.resolve()), path.stem, config.mode, hint, size):
            enqueued += 1
            log.info("enqueued %s (lang=%s)", path, hint or "auto")
    return enqueued


def housekeeping_sweep(db: Database) -> None:
    """Retry renames for done jobs whose done_path is still NULL (§6.5)."""
    from renamer import rename_done
    for job in db.unrenamed_done_jobs():
        src = Path(job.src_path)
        if not src.exists():
            continue  # already renamed manually or vanished
        final = rename_done(src)
        if final:
            db.finish_job(job.id, str(final))
            log.info("sweep renamed %s -> %s", src.name, final.name)


def recover_output_flags(config: Config, db: Database) -> None:
    """§6.6: recompute transcript/playbook flags of crashed jobs from disk
    (idempotent resume — outputs written before the crash are not re-made).
    Flags are only set for files that exist and are non-empty."""
    for job in db.jobs_with_status("running"):
        src = Path(job.src_path)
        transcript = src.parent / f"{job.stem}.md"
        playbook = src.parent / f"{job.stem}.guide.md"
        db.refresh_output_flags(
            job.id,
            job.mode in ("transcript", "both") and transcript.is_file()
            and transcript.stat().st_size > 0,
            job.mode in ("playbook", "both") and playbook.is_file()
            and playbook.stat().st_size > 0,
        )


def run_daemon(config: Config, db: Database) -> None:
    recover_output_flags(config, db)
    recovered = db.recover_running()
    if recovered:
        log.info("recovered %d running job(s) -> queued", recovered)
    db.ensure_keys(len(config.api_keys) or 1)
    state = ScanState()
    stop_event = threading.Event()
    whisper_wake = threading.Condition()

    def request_stop(signum: int, frame: object) -> None:
        log.info("signal %d received, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    from gemini_worker import GeminiWorker
    workers: list[threading.Thread] = [
        GeminiWorker(config, db, i + 1, key, stop_event, whisper_wake)
        for i, key in enumerate(config.api_keys)
    ]
    workers.append(WhisperWorker(config, db, stop_event, whisper_wake))
    for w in workers:
        w.start()

    log.info("daemon running: %d keys, inbox=%s, db=%s, dry_run=%s",
             len(config.api_keys), config.inbox_roots, config.db_path,
             config.dry_run)
    try:
        while not stop_event.is_set():
            scan_once(config, db, state)
            housekeeping_sweep(db)
            # short slices so SIGTERM/SIGINT is honored promptly
            for _ in range(config.scan_interval):
                if stop_event.is_set():
                    break
                stop_event.wait(1)
    finally:
        stop_event.set()
        for w in workers:
            w.join(timeout=10)


def cmd_status(config: Config, db: Database) -> int:
    counts = db.jobs_by_status()
    print("Queue by status:")
    for status in ("queued", "running", "done", "error"):
        print(f"  {status:8} {counts.get(status, 0)}")
    total_keys = len(config.api_keys) or 1
    db.ensure_keys(total_keys)
    print("\nKeys:")
    for key_id in range(1, total_keys + 1):
        row = db.get_key(key_id)
        active = db.revive_if_expired(key_id)
        state = row["state"] if row else "active"
        print(f"  key {key_id}: {state if not active else 'active'}"
              f" (req_count={row['req_count'] if row else 0})")
    print("\nLast events:")
    for ev in db.recent_jobs(10):
        print(f"  [{ev['status']:7}] #{ev['id']} {ev['src_path']}"
              f" engine={ev['engine']} err={ev['last_error'] or '-'}")
    return 0


def cmd_enqueue(config: Config, db: Database, paths: list[str]) -> int:
    added = 0
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 1
        hint = language_hint_for_path(path, config.inbox_roots)
        try:
            size = path.stat().st_size
        except OSError as exc:
            print(f"cannot stat {path}: {exc}", file=sys.stderr)
            return 1
        if db.enqueue(str(path), path.stem, config.mode, hint, size):
            added += 1
    print(f"enqueued {added} job(s)")
    return 0


def cmd_reset_key(db: Database, key_id: int) -> int:
    db.ensure_keys(3)
    db.clear_cooldown(key_id)
    print(f"key {key_id} -> active")
    return 0


def cmd_doctor(config: Config) -> int:
    ok = True
    for root in config.inbox_roots:
        exists = Path(root).is_dir()
        ok &= exists
        print(f"inbox {root}: {'OK' if exists else 'MISSING'}")
    db_dir = Path(config.db_path).parent
    writable = db_dir.exists() and os.access(db_dir, os.W_OK)
    ok &= writable
    print(f"db dir {db_dir}: {'OK' if writable else 'MISSING/UNWRITABLE'}")
    if config.api_keys:
        print(f"keys: {len(config.api_keys)} configured")
    else:
        print("keys: NONE CONFIGURED (whisper-only fallback will be used)")
        ok = False
    try:
        next_pacific_midnight(datetime.now(timezone.utc))
        print("zoneinfo America/Los_Angeles: OK")
    except Exception as exc:
        print(f"zoneinfo: FAIL ({exc})")
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcriberd")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "status", "enqueue", "reset-key", "doctor"))
    parser.add_argument("paths", nargs="*", help="paths for enqueue")
    parser.add_argument("--key", type=int, choices=(1, 2, 3),
                        help="key id for reset-key")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = load_config()
    db = Database(config.db_path)

    if args.command == "run":
        run_daemon(config, db)
        return 0
    if args.command == "status":
        return cmd_status(config, db)
    if args.command == "enqueue":
        if not args.paths:
            parser.error("enqueue requires at least one path")
        return cmd_enqueue(config, db, args.paths)
    if args.command == "reset-key":
        if not args.key:
            parser.error("reset-key requires --key 1|2|3")
        return cmd_reset_key(db, args.key)
    return cmd_doctor(config)


if __name__ == "__main__":
    raise SystemExit(main())
