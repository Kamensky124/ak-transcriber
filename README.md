# transcriber

Persistent rootless Podman queue daemon that watches `~/org/media/inbox` for
media files, transcribes them with the Gemini API (3 rotating free-tier keys)
and falls back to local faster-whisper only when all keys are quota-cooled.
On success it writes `X.md` / `X.guide.md` next to the source and renames
`X.mp3` → `X_done.mp3`.

## Layout

```
src/transcriberd.py    entrypoint: run | status | enqueue | reset-key | doctor
src/config.py          env parsing (plan §7 defaults)
src/db.py              SQLite WAL queue + key cooldown state (per-thread conns)
src/scanner.py         inbox walker, stability gate, Cyrillic language hint
src/keys.py            Pacific-midnight quota reset, error classification
src/renamer.py         *_done.* rename policy with collision suffixes
src/gemini_worker.py   Files-API upload + transcript/playbook (3 threads)
src/whisper_worker.py  fallback transcripts (timestamps bullet .md)
```

## Install

```sh
./setup.sh              # build + env + quadlet install + start
./setup.sh --env-only   # refresh keys only, then: systemctl --user restart transcriber
./setup.sh --no-start   # build and install without starting
```

API keys are extracted from `~/org/env-files/secrets` into
`~/org/transcriber/transcriber.env` (chmod 600). At least
`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3` must be present.

## Configuration (env)

| Var | Default |
|---|---|
| `TRANSCRIBE_INBOX` | `/home/andy/org/media/inbox` (`:`-separated multi-root) |
| `TRANSCRIBE_MODE` | `both` (`transcript` \| `playbook` \| `both`) |
| `TRANSCRIBE_MODEL` | `gemini-3.8-flash` (fallback `gemini-3.5-flash`) |
| `TRANSCRIBE_DB` | `/var/lib/transcriber/queue.db` |
| `SCAN_INTERVAL` / `STABLE_SECONDS` | `30` / `10` |
| `MAX_ATTEMPTS` | `5` |
| `FALLBACK_ENGINE` | `whisper` (`wait` disables fallback) |
| `WHISPER_MODEL` / `WHISPER_CPU_THREADS` | `small` / `3` |
| `TRANSCRIBE_DRY_RUN` | `0` (`1` = stub outputs, no network) |

## Ops

```sh
podman logs -f transcriber
podman exec transcriber python /app/transcriberd.py status
podman exec transcriber python /app/transcriberd.py reset-key --key 2
podman exec transcriber python /app/transcriberd.py enqueue /home/andy/org/media/inbox/foo.mp3
```

The queue and key cooldowns survive restarts; `running` jobs are requeued at
boot and finished outputs are never regenerated (idempotent resume).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Stdlib only; `google.genai` and `faster_whisper` are never imported in tests.
