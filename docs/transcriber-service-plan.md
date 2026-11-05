---
title: Transcriber Service — Persistent Podman Queue Daemon (Implementation Plan)
created: 2026-10-20
project: transcriber
tags:
  - project/transcriber
  - domain/automation
  - domain/content
  - type/planning
status: active
---

# Transcriber Service — Implementation Plan

> **Status:** approved by owner 2026-10-20 ("сохрани детальный md план и реализуй с субагентами").
> **Code repo:** [`~/pets/transcriber/`](file:///home/andy/pets/transcriber) · **Guide:** [`tools/transcription-agent-guide.md`](../../tools/ai-agents/transcription-agent-guide.md)
> Reworks `~/org/scripts/transcribe-gemini.py` from a one-shot CLI into an always-on containerized queue service.

## 1. Goal

A persistent rootless Podman service on the local workstation that:

1. Watches an inbox root where the owner regularly drops folders of `.mp3` files (folder/file names in Russian or English).
2. Enqueues every new stable media file into a **SQLite-backed queue**.
3. Transcribes via **Gemini API using 3 keys** (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3` — different accounts, free-tier daily quotas) — keys always take **priority** over local Whisper.
4. Falls back to **local faster-whisper** only when ALL Gemini keys are quota-cooled down.
5. On success writes `X.md` / `X.guide.md` next to the source, then renames `X.mp3` → `X_done.mp3`.
6. Survives restarts (queue + key cooldown state persisted in SQLite).

## 2. Defaults taken without owner confirmation (veto-able)

| Decision | Default | Rationale / override |
|---|---|---|
| Inbox root | `~/org/media/inbox` (recursive; `:`-separated list supported via `TRANSCRIBE_INBOX`) | Was an open question; single root, arbitrary nesting of RU/EN folders |
| Output mode | `both` (transcript + playbook) | Guide pre-flight recommends `both`. Costs 2 requests/file ⇒ ~30 files/day on 3 free keys. Env `TRANSCRIBE_MODE=playbook` halves cost |
| Whisper fallback model | `small` (multilingual, INT8 CPU, 1 file at a time, `nice`-style low priority) | Matches Track B guide; no `.en` models ever (RU hallucination) |
| Concurrency | 1 worker per active key (≤3 parallel uploads), each pinned to its own key | No cross-key rate-limit interference |
| Ordering | FIFO by enqueue time | Predictable for the owner (size-based reordering rejected) |

## 3. Architecture

```
owner drops folders ──▶ ~/org/media/inbox/**            (host path == in-container path)
                          │
        ┌─────────────────▼──────────────────┐
        │  quadlet: transcriber.container    │  systemd --user, Restart=always
        │  transcriberd.py                   │
        │  ┌──────────┐   claim/finish       │
        │  │ scanner  │──────┐               │
        │  │ thread   │      ▼               │
        │  └──────────┘  SQLite (WAL)        │
        │                queue.db            │
        │  ┌──────────────────────────┐      │
        │  │ gemini worker ×3 (key1-3)│      │──▶ Files API + generate_content
        │  └──────────────────────────┘      │
        │  ┌──────────────────────────┐      │
        │  │ whisper fallback thread  │      │──▶ local CPU (only while 0 keys active)
        │  └──────────────────────────┘      │
        └────────────────────────────────────┘
             success: X.md + X.guide.md (atomic write) ──▶ os.rename(X.mp3 → X_done.mp3)
```

Host paths are mounted into the container **at identical paths** so DB rows are valid in both worlds.

## 4. Repository layout (`~/pets/transcriber/`)

```
~/pets/transcriber/
├── AGENTS.md               # project conventions (small)
├── README.md               # operational doc: build, install, ops, key rotation
├── Containerfile
├── transcriber.container   # quadlet unit (copied to ~/.config/containers/systemd/ by setup.sh)
├── setup.sh                # build + env extraction + quadlet install + enable
├── .gitignore              # __pycache__, .venv, data/, *.env
├── src/
│   ├── transcriberd.py     # entrypoint: run | status | enqueue | reset-key | doctor
│   ├── config.py           # env parsing, dataclass Config
│   ├── db.py               # schema, migrations, queue ops (thread-safe)
│   ├── scanner.py          # inbox walker + stability gate + language heuristic
│   ├── keys.py             # KeyScheduler: cooldowns, Pacific reset, pure functions
│   ├── renamer.py          # success rename policy (pure, testable)
│   ├── gemini_worker.py    # upload + transcript/playbook generation (lazy import)
│   └── whisper_worker.py   # faster-whisper transcript (lazy import)
└── tests/                  # stdlib unittest, zero third-party deps
    ├── test_db.py
    ├── test_scanner.py
    ├── test_keys.py
    ├── test_renamer.py
    └── test_end_to_end.py  # DRY_RUN pipeline through real SQLite tmpdir
```

## 5. SQLite schema (`/var/lib/transcriber/queue.db`, WAL mode)

```sql
CREATE TABLE IF NOT EXISTS jobs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  src_path       TEXT NOT NULL UNIQUE,             -- absolute path (host == container)
  stem           TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|error
  mode           TEXT NOT NULL DEFAULT 'both',     -- transcript|playbook|both
  language_hint  TEXT NOT NULL DEFAULT '',         -- '' | 'Russian' | ...
  engine         TEXT,                             -- gemini|whisper (last used)
  key_id         INTEGER,                          -- last gemini key 1..3
  attempts       INTEGER NOT NULL DEFAULT 0,       -- retryable failures only
  next_attempt_at TEXT,                            -- ISO UTC backoff gate
  transcript_done INTEGER NOT NULL DEFAULT 0,
  playbook_done   INTEGER NOT NULL DEFAULT 0,
  size_bytes     INTEGER,
  done_path      TEXT,                             -- final *_done.* path after rename
  last_error     TEXT,
  created_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keys (
  key_id           INTEGER PRIMARY KEY,            -- 1..3 (env GEMINI_API_KEY[_2|_3])
  state            TEXT NOT NULL DEFAULT 'active', -- active|cooldown
  exhausted_until  TEXT,                           -- ISO UTC
  req_count        INTEGER NOT NULL DEFAULT 0,     -- informational only
  req_date         TEXT                            -- YYYY-MM-DD UTC
);
```

No secret material in the DB — keys live only in the env file.

## 6. Behavior specification

### 6.1 Scanner (thread, every `SCAN_INTERVAL`=30 s)
- Recursive walk of each root in `TRANSCRIBE_INBOX` (default `/home/andy/org/media/inbox`).
- Media extensions: `.mp3 .wav .m4a .aac .flac .ogg .mp4 .mkv .webm .mov .avi`.
- **Skip**: hidden files (`.`-prefixed), any path segment `_done`-suffixed before extension (`*_done.*`), paths already in `jobs` (UNIQUE `src_path`), zero-byte files.
- **Stability gate**: file enqueued only when `(size, mtime)` unchanged across two consecutive scans AND age ≥ `STABLE_SECONDS`=10 — protects files still being copied.
- **Language heuristic**: any Cyrillic codepoint in the path below the inbox root ⇒ `language_hint='Russian'`, else `''` (Gemini auto-detects).

### 6.2 Key scheduler (`keys.py`)
- Pure function `next_pacific_midnight(now_utc) -> iso` — free tier resets 00:00 `America/Los_Angeles` (zoneinfo; tzdata installed in image).
- On `429` / `RESOURCE_EXHAUSTED` / `free_tier` error from a worker: key → `state=cooldown`, `exhausted_until=next_pacific_midnight(now)`, persisted. Job attempt NOT counted.
- Workers re-check `exhausted_until` before each request; expired cooldown ⇒ back to `active`.
- `req_count`/`req_date` incremented per successful generate call (observability only).

### 6.3 Gemini workers (3 threads, one per key)
- Loop: if my key cooling ⇒ sleep 30 s. Claim oldest queued job (`ORDER BY created_at`) that still lacks an output my engine can produce and is past `next_attempt_at` (`UPDATE ... SET status='running'` guarded transaction).
- Nothing to do and other keys active ⇒ sleep 5 s. **Zero keys active ⇒ notify whisper condition variable.**
- Per job: symlink-upload trick + Files API wait loop + `generate_content` with model `TRANSCRIBE_MODEL` (default `gemini-3.8-flash`, fallback `gemini-3.5-flash`), prompts and retry/fallback logic ported from `transcribe-gemini.py`. Delete uploaded file in `finally`.
- Outputs written atomically (`.tmp` + `os.replace`), verified non-empty.
- Error classes: quota → cooldown key, release job; transient (5xx/timeout/net) → `attempts+1`, backoff `min(300, 30·2^attempts)` s via `next_attempt_at`; permanent (upload FAILED, bad media, empty response after retries) → after `MAX_ATTEMPTS`=5 total ⇒ `status='error'`, keep file un-renamed for inspection.
- Job finished when all mode-requested outputs exist ⇒ `status='done'`, `finished_at`, then rename (6.5).

### 6.4 Whisper fallback (single thread)
- Runs **only while zero Gemini keys are active** (`FALLBACK_ENGINE=whisper`, set `wait` to disable).
- Produces transcripts only (`.md`, timestamped bullets like Track B); never playbooks. Jobs with mode `playbook`-only wait for Gemini.
- Lazy `from faster_whisper import WhisperModel`; model `WHISPER_MODEL=small`, `device=cpu`, `compute_type=int8`, `cpu_threads=WHISPER_CPU_THREADS=3`; between files checks whether a Gemini key revived ⇒ stops.
- Later, when a key resets, jobs with `transcript_done=1, playbook_done=0, mode=both` are picked up by Gemini workers for the playbook half only.

### 6.5 Rename policy (`renamer.py`, pure core + thin IO)
- Precondition: all mode outputs exist and are non-empty.
- `X.mp3` → `X_done.mp3`; if target exists ⇒ `X_done_2.mp3`, `_3`, … Record final path in `done_path`.
- Rename failure (vanished/EBUSY) ⇒ job stays `done`, error logged, retried by a housekeeping sweep on next scan.

### 6.6 Startup recovery
- Jobs stuck `status='running'` (crash mid-flight) ⇒ back to `queued` at boot; output flags recomputed from disk (idempotent resume).

### 6.7 CLI (`transcriberd.py`)
- `run` — daemon (default in container CMD).
- `status` — table of queue by status + per-key state + last 10 events.
- `enqueue <path>…` — manual add (validates mount-visible path).
- `reset-key <1|2|3>` — clear cooldown.
- `doctor` — config/key/dirs/mounts sanity report.
- Env `TRANSCRIBE_DRY_RUN=1`: workers skip network, write stub outputs after 2 s — E2E smoke without quota spend (used by integration test & owner sanity checks).

## 7. Configuration (env, all optional except keys)

| Var | Default | Notes |
|---|---|---|
| `TRANSCRIBE_INBOX` | `/home/andy/org/media/inbox` | `:`-separated multi-root |
| `TRANSCRIBE_MODE` | `both` | `transcript` \| `playbook` \| `both` |
| `TRANSCRIBE_MODEL` | `gemini-3.8-flash` | fallback 3.5 built-in |
| `TRANSCRIBE_DB` | `/var/lib/transcriber/queue.db` | |
| `SCAN_INTERVAL` | `30` | seconds |
| `STABLE_SECONDS` | `10` | |
| `MAX_ATTEMPTS` | `5` | |
| `FALLBACK_ENGINE` | `whisper` | `wait` disables fallback |
| `WHISPER_MODEL` / `WHISPER_CPU_THREADS` | `small` / `3` | |
| `GEMINI_API_KEY`, `_2`, `_3` | required ≥1 | from env file, never in DB/git |
| `TRANSCRIBE_DRY_RUN` | `0` | |

## 8. Container & deployment

- **Containerfile**: `python:3.12-slim` + `ffmpeg`, `tzdata` (apt) + `google-genai`, `faster-whisper` (pip). Copies `src/` to `/app`. CMD `python /app/transcriberd.py run`. HF model cache volume keeps the 460 MB model off every restart.
- **Quadlet** `transcriber.container` (single container; a literal `.pod` adds an infra container with no benefit for one service — can be added later if sidecars appear):

```ini
[Container]
Image=localhost/transcriber:latest
ContainerName=transcriber
EnvironmentFile=%h/org/transcriber/transcriber.env
Volume=%h/org/media/inbox:/home/andy/org/media/inbox:Z
Volume=%h/org/transcriber/data:/var/lib/transcriber:Z
Volume=%h/.cache/transcriber-models:/root/.cache:Z
NoNewPrivileges=true

[Service]
Restart=always

[Install]
WantedBy=default.target
```

- **setup.sh**: mkdir runtime dirs → extract 3 keys from `~/org/env-files/secrets` into `~/org/transcriber/transcriber.env` (chmod 600, strips `export`/quotes; fails loudly if any key missing) → `podman build` → install quadlet → `systemctl --user daemon-reload && enable --now transcriber.service` → print status/log hints. Flags: `--env-only`, `--no-start`.
- Key rotation flow: edit `~/org/env-files/secrets` → `setup.sh --env-only` → `systemctl --user restart transcriber`.
- Ops: `podman logs -f transcriber`; `podman exec transcriber python /app/transcriberd.py status`.

## 9. Tests (stdlib `unittest` — host has no pytest; CI-less)

- `test_db`: init, dedup enqueue, claim/finish transitions, crash-recovery reset, cooldown persist.
- `test_scanner`: extension/hidden/`_done` filters, stability gating, Cyrillic heuristic.
- `test_keys`: Pacific-midnight computation (incl. DST edges), quota classification, cooldown expiry.
- `test_renamer`: rename, collision suffixing, precondition refusal.
- `test_end_to_end`: DRY_RUN daemon threads over tmp SQLite + tmp inbox with generated dummy `.mp3` → asserts outputs + `_done` rename + DB terminal state.

## 10. Acceptance criteria

1. `python3 -m unittest discover -s tests` green on host (no third-party imports needed for tests).
2. `podman build -t localhost/transcriber:latest ~/pets/transcriber` succeeds.
3. DRY_RUN smoke: one-shot `podman run --rm -e TRANSCRIBE_DRY_RUN=1 …` over a tmp inbox with an ffmpeg-generated 1 s silent mp3 ⇒ outputs written, file renamed `_done`, DB rows `done`.
4. `systemctl --user enable --now transcriber.service` ⇒ running; `status` CLI shows 0 queued / 3 keys active.
5. Reboot-safety: `systemctl --user restart` mid-queue resumes without re-doing finished outputs.

## 11. Out of scope / future

- Web UI / metrics exporter; email/notify on completion; auto video→mp3 pre-conversion inside the service (use existing `convert-video-to-mp3.py` manually); a real `.pod` with sidecars; playbook generation via local LLM.
- Wiki guide gets a "Track C" section after implementation (owner-visible change log updated by orchestrator).

## 12. Implementation order (subagent plan)

1. **Builder child**: whole repo per this plan (modules, tests, Containerfile, quadlet, setup.sh, README, AGENTS.md). Acceptance = §10.1.
2. **Reviewer child** (fresh context): spec-vs-code review + fix findings + re-run tests. Acceptance = §10.1 + review findings resolved.
3. **Orchestrator (parent)**: podman build, DRY_RUN smoke, quadlet install, service start, wiki guide Track C section, git init + commits, `sync-kb.sh`, memory log.

#project/transcriber #domain/automation #type/planning
