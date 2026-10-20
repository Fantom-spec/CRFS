# Journaling and Recovery

## What is logged

CRFS uses **redo journaling of metadata** (superblock counters, bitmap, inode table, directory blocks).
File contents are written **copy-on-write** to freshly allocated blocks *before* the commit ("ordered
mode"), so a committed inode never points at unwritten data and old file contents are never overwritten
in place. Data is therefore not journaled.

Each journal `DATA` record is an absolute after-image of a byte range of one block, so replaying it twice
gives the same result (idempotence).

## Commit protocol (the order is the whole point)

| # | Step | Event kind | Disk call |
|---|---|---|---|
| 1 | PREPARE – operation runs on the staging overlay (`StagingIO`); data blocks written | `tx_begin`, `tx_prepare` | data blocks flushed |
| 2 | JOURNAL WRITE – BEGIN + DATA records into the ring | `journal_write` | – |
| 3 | FLUSH – journal blocks durable | `journal_flush` | `flush("journal")` |
| 4 | COMMIT – commit record written alone in its block | `commit_write` | – |
| 5 | COMMIT FLUSH – **commit point** | `commit_flush` | `flush("commit")` |
| 6 | APPLY – after-images written to home locations | `home_apply` | – |
| 7 | HOME FLUSH | `home_flush` | `flush("home")` |
| 8 | CHECKPOINT – superblock tail / checkpoint txid advanced | `checkpoint` | `flush("checkpoint")` |

`abort` (any exception before step 4) discards the overlay, restores in-memory counters and frees
nothing on disk, so nothing is left behind and the txid is reused.

Crash points are fired before/after/in the middle of every one of these steps
(`CrashPoint` in `src/virtual_disk.py`; the 14 scenarios are in `src/crash_injector.py`).

## Recovery (mount time, or the explicit `recover` command / UI button)

1. Read block 0 and verify magic + CRC64. A bad superblock is a mount error (it is single-copy; see limitations).
2. Scan the ring from `journal_tail`. Accept transaction *n* only if its txid is `checkpoint_txid + n`,
   its BEGIN/DATA records have valid CRCs and targets (not the journal region, within the disk), and a
   valid COMMIT record with the right record count follows. Stop at the first transaction that fails.
   `journal_head` is not trusted.
3. Replay accepted transactions in order (apply after-images). `flush`.
4. Checkpoint: write the superblock with the new tail / checkpoint txid. `flush`.
5. Anything else (no commit, torn commit, corrupt record, stale lap) is **discarded**, with a warning
   in the result and an event.

Running recovery again finds nothing to replay. Crashing *during* recovery is covered by two scenarios:
the next recovery redoes the same work and reaches the same state.

## Durability model (read this before trusting any claim)

The simulator models the disk as a **volatile write-back cache** (`cache_mode=volatile`, default):

* `write_block` goes to a cache; only `flush()` makes it durable.
* On a crash, unflushed writes are lost. Writes inside one flush may reach the disk in any prefix
  order (the "mid-flush" crash points), and one block can be torn to a prefix (`torn_bytes`).
* A block write is otherwise atomic; there is no reordering *between* flushes.

`writethrough` makes every write durable immediately; it is a weaker model that cannot exercise the
ordering rules, so the crash matrix is run mainly in volatile mode.

**What is and is not claimed.** Within this model, the tests show that committed work survives, uncommitted
work vanishes, recovery is idempotent and fsck is clean afterwards. This does **not** prove safety against
real power loss: real disks and `fsync` implementations may reorder, lie, or tear writes in ways not modelled
(e.g. torn writes of arbitrary sectors, bit rot, reordering without barriers). The image is an ordinary file;
the host OS page cache sits underneath it.

## Journal space

* `eager` checkpoint (default): every transaction is checkpointed immediately, so the ring is reused from
  the tail each time.
* `lazy` checkpoint: committed transactions accumulate; when the ring fills, the engine checkpoints
  automatically to reclaim space. If a single transaction still cannot fit, the operation fails with a
  journal-full error and the filesystem stays consistent.
* Wraparound: records may straddle the end of the ring; the scan wraps modulo the ring size and the
  txid-sequence rule rejects stale blocks from earlier laps.
