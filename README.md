# CRFS — Crash-Recoverable File System Simulator

A small file system written in Python for an Operating Systems course. It lives in a disk-image file and
uses **write-ahead (redo) journaling of metadata** so that a crash at any point leaves the image either
with the whole operation or with none of it. It ships with a CLI, a deterministic crash-injection
framework that really kills processes, a consistency checker (fsck) and a local web monitor.

> **What this is not.** It is a simulator on top of an ordinary file. The guarantees below are
> proved against a *modelled* disk (see `docs/journaling-and-recovery.md`, "Durability model"); nothing
> here claims safety against real power loss on real hardware.

## Quick start

```bash
# CLI (unchanged entry point)
python -m src.cli demo.img --format --size 8M          # format
python -m src.cli demo.img -c "mkdir /docs"            # single command
python -m src.cli demo.img                             # interactive shell: help, ls, write, journal, check ...

# Web monitor (loopback only, no extra dependencies)
python -m webui --image-dir crfs_images --open         # http://127.0.0.1:8765/
```

Developed and tested on Python 3.13 (older 3.x versions are untested); the engine and UI use only the standard library. `pytest` is only needed for the legacy
test file; Playwright is only needed for the optional browser tests.

## Layout

```
src/            engine: virtual_disk, superblock, block_allocator, inode, directory, journal,
                transaction, recovery, consistency_checker, filesystem, events
                crash_injector, crash_worker, crash_lab (crash testing), cli
webui/          local HTTP API (app.py) + static dark UI (static/)
tests/          unit, engine-integrity, crash-recovery (subprocess SIGKILL), UI integration, browser E2E
docs/           architecture, disk layout, journaling & recovery, testing, UI guide, demo script, limitations
```

## How a metadata change is made durable

1. **PREPARE** – the operation runs against a staging overlay; no metadata block is touched.
2. **JOURNAL WRITE + FLUSH** – after-images (changed byte ranges) go to the circular log, then `flush`.
3. **COMMIT** – a commit record (with a record count and CRC) is written to its own block, then `flush`.
   This flush is the commit point.
4. **APPLY** – metadata blocks are written to their home locations, then `flush`.
5. **CHECKPOINT** – the superblock advances the journal tail / checkpoint txid, then `flush`.

File *data* blocks are copy-on-write and flushed before the commit ("ordered mode"), so a committed
inode never points at unwritten data. On mount, recovery replays committed-but-not-checkpointed
transactions in order and discards anything else. Replay is idempotent.

## Testing

```bash
python -m unittest tests.test_engine_integrity tests.test_crash_recovery tests.test_webui tests.test_webui_e2e -v
python -m pytest tests/test_filesystem.py            # legacy suite (needs pytest)
```

See `docs/testing.md` for what each suite proves and the exact results recorded for this revision.

## Documentation

| Doc | Content |
|---|---|
| `docs/architecture.md` | modules, layering, data flow, UI/backend boundary |
| `docs/disk-layout.md` | exact on-disk format |
| `docs/journaling-and-recovery.md` | protocol, recovery algorithm, durability model |
| `docs/testing.md` | test strategy, crash matrix, commands |
| `docs/crash-test-report.md` | the 14-scenario × 6-operation matrix |
| `docs/ui-guide.md` | the seven monitor screens |
| `docs/demo-script.md` | 5–7 minute viva demo |
| `docs/audit.md` | Phase 1 bug audit |
| `docs/limitations.md` | known limitations and unverified claims |
