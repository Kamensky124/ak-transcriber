---
title: "transcriber — Documentation Index"
status: active
created: 2026-11-02
project: transcriber
tags:
  - project/transcriber
  - type/dashboard
---

# transcriber — Documentation Index

Documentation home for `transcriber` (Persistent Podman queue daemon for audio/video transcription).

## Project docs

- [Transcriber Service Implementation Plan](transcriber-service-plan.md)

## Live views

```base
filters:
  and:
    - file.inFolder("_pets-docs/transcriber")
properties:
  note.status:
    displayName: Status
views:
  - type: table
    name: All docs (recent first)
    limit: 40
    order:
      - file.name
      - note.status
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
```
