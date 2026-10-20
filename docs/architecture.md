# Architecture

```
            +-----------------------+     +-----------------+
 Browser -> | webui/app.py (HTTP)   |     | src/cli.py      |
            | token, Host check,    |     | shell / -c      |
            | typed confirmations   |     +--------+--------+
            +----------+------------+              |
                       |   (only public FileSystem API)
                       v                           v
                +--------------------------------------------+
                |  src/filesystem.py  FileSystem             |
                |  create mkdir write append read delete     |
                |  rename ls stat tree fsck recover overview |
                +--------+-----------------------+-----------+
                         |                       |
            +------------v---------+   +---------v-----------+
            | transaction.py       |   | consistency_checker |
            | TransactionManager   |   | (read-only fsck)    |
            | StagingIO overlay    |   +---------------------+
            +----+-------------+---+
                 |             |
        +--------v---+   +-----v--------+   +--------------+
        | journal.py |   | recovery.py  |   | events.py    |
        | records,   |   | scan+replay  |   | EventLog     |
        | ring, CRC  |   +--------------+   +--------------+
        +-----+------+
              v
        +-------------------------------+
        | virtual_disk.py               |
        | block I/O, volatile cache,    |
        | flush(), crash(), fire(point) |
        +---------------+---------------+
                        v
                  disk image file
```

## Modules

| Module | Responsibility |
|---|---|
| `virtual_disk.py` | Block device on a file. Two models: `volatile` (writes sit in a cache until `flush`, a crash drops them) and `writethrough`. Fires named crash points; can persist a torn prefix of one block. |
| `superblock.py` | Format metadata, journal tail/head, checkpoint txid, CRC64. |
| `block_allocator.py`, `inode.py`, `directory.py` | Bitmap, inode table, directory blocks. All reads/writes go through an `io` object so inside a transaction they hit the staging overlay. |
| `journal.py` | Record encoding, circular ring, scan with sequence validation, idempotent replay. |
| `transaction.py` | `begin/commit/abort`. `StagingIO` rejects metadata writes made outside a transaction. Drives the 5-step commit protocol and fires crash points. |
| `recovery.py` | Mount-time redo of committed transactions; `recover()` returns a structured `RecoveryResult`. |
| `consistency_checker.py` | fsck: superblock, journal, bitmap vs inodes, tree reachability, link counts; every finding has a `code` and a `why`. |
| `events.py` | Bounded in-memory event log with optional JSONL sink. Used by UI and lab. |
| `crash_injector.py` | `CrashScenario` table (14 scenarios) and the injector (`EXCEPTION`, `SUBPROCESS` = `SIGKILL`). |
| `crash_worker.py` / `crash_lab.py` | The lab: runs `setup`, `crash`, `inspect`, `verify` as **separate processes**; compares expected vs actual. |
| `webui/app.py` | Stdlib `ThreadingHTTPServer`; JSON API + static files. |

## UI/backend boundary

* The UI is static HTML/JS. It never reads or writes an image; every action is an HTTP call that ends in a
  public `FileSystem` method (or the lab, which spawns worker processes on a *temporary* image).
* Every number shown comes from `/api/state`, `/api/layout`, `/api/block`, `/api/journal`, `/api/check`,
  `/api/events`, `/api/lab/*`. There is no sample data in the dashboard.
* Engine exceptions become `{"ok": false, "error": {"type", "message"}}` with a 4xx/5xx status and appear
  as toasts.
* Long work (crash lab) runs in a background thread; the UI polls `/api/lab/job`.
* Security: binds to loopback only (non-loopback hosts are refused at startup), `Host` header must be a
  loopback name, every POST needs a per-process token (`X-CRFS-Token`), image names are restricted to
  `[A-Za-z0-9._-]{1,64}.img` inside one directory, destructive calls need an exact confirmation string
  (`FORMAT <name>`, `DELETE <name>`, `RUN CRASH LAB`), request bodies are capped at 256 KiB. There is no
  shell or arbitrary-path endpoint. The only endpoint that crashes the *mounted* image is `/api/demo/crash`
  (typed confirmation `CRASH THIS IMAGE`, runs in a separate process, leaves the image mounted read-only
  until recovery).
* The CLI and UI use the same engine and the same on-disk format; an image made by one opens in the other.
