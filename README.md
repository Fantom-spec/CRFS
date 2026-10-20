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

## Command cheat sheet

Run everything from the project folder.

### Web monitor
```bash
python -m webui --image-dir crfs_images --open      # start the UI at http://127.0.0.1:8765/
python -m webui --image-dir crfs_images --port 8800 # use another port if 8765 is busy
```
Ctrl+C stops it. In the UI: **New image…** creates a disk, **File browser → Crash demo…** crashes your own file,
**Run recovery** (red bar) repairs it, **Crash lab** runs the 14 scripted scenarios on a throw-away disk.

### CLI
```bash
python -m src.cli demo.img --format --size 8M            # create and format an image
python -m src.cli demo.img                               # interactive shell (type help)
python -m src.cli demo.img -c "mkdir /docs"              # one command
python -m src.cli demo.img -c "create /docs/a.txt"
python -m src.cli demo.img -c "write /docs/a.txt hello"
python -m src.cli demo.img -c "read /docs/a.txt"
python -m src.cli demo.img -c "ls /docs"
python -m src.cli demo.img -c "journal"                  # journal head/tail and every transaction
python -m src.cli demo.img -c "check"                    # fsck
python -m src.cli demo.img --no-recovery -c "ls /"       # open WITHOUT recovery (read-only, shows the raw crashed disk)
python -m src.cli demo.img --cache-mode writethrough     # weaker disk model (default is volatile)
python -m src.cli demo.img --checkpoint lazy             # checkpoint only when the journal fills
```

### Crash demo in the terminal (shows the crash with your own eyes)
```bash
rm -f demo.img
python -m src.crash_worker '{"phase":"setup","image":"demo.img","operation":"write_file"}'
python -m src.cli demo.img --no-recovery -c "read /lab/data.txt"      # OLD DATA
python -m src.crash_worker '{"phase":"crash","image":"demo.img","operation":"write_file","point":"AFTER_COMMIT_FLUSH"}'
echo $?                                                               # 137 = killed by SIGKILL
python -m src.cli demo.img --no-recovery -c "read /lab/data.txt"      # still OLD DATA (crashed disk)
python -m src.cli demo.img --no-recovery -c "journal"                 # last tx: committed-pending-replay
python -m src.cli demo.img -c "read /lab/data.txt"                    # NEW DATA (recovery ran)
python -m src.cli demo.img -c "check"                                 # Consistency check PASSED
```
Contrast (update is lost, old data intact): repeat with `"point":"AFTER_JOURNAL_FLUSH"`.
For a file that disappears and comes back, use `"operation":"create_file"` and `ls /lab` instead of `read`.
Operations: `create_file`, `write_file`, `mkdir`, `delete_file`, `rename_file`, `multi_block_write`.
Crash points: see `python -m src.cli x.img -c "crash-test"` for the 14 scenario names.

### One-shot scripted scenario (temporary image, prints PASS/FAIL)
```bash
python -m src.cli demo.img -c "crash-test crash_after_commit_flush_before_apply write_file"
```

### Tests
```bash
python -m unittest tests.test_engine_integrity tests.test_crash_recovery tests.test_webui tests.test_webui_e2e -v
python -m pytest tests/test_filesystem.py                # original suite (needs pytest)
```

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