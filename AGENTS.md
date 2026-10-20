# AGENTS.md

- stdlib-only core (`src/`); `google.genai` and `faster_whisper` must be imported lazily inside functions.
- Tests are stdlib `unittest` only (`python3 -m unittest discover -s tests`); host has no pytest.
- Python 3.12-compatible (container), must also run on 3.14 (host tests). Type hints required.
- No secrets in code, tests, or DB. Keys come only from the env file via `setup.sh`.
- Host paths inside the container are identical to host paths (see `transcriber.container` volumes).
- Schema/behavior contract: SQLite WAL, per-thread connections, short transactions; DRY_RUN via `TRANSCRIBE_DRY_RUN=1`.
