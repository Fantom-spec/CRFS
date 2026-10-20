# Phase 1 audit of the original repository

Method: read the original sources side by side with the rewritten engine, then confirm each item by a
test in this revision. Severity: **H** = can lose/corrupt committed data or makes a claim untestable,
**M** = wrong behaviour in edge cases, **L** = cosmetic/documentation.

| # | Sev | Finding (original code) | Root cause | Fix | Test |
|---|---|---|---|---|---|
| 1 | H | `VirtualDisk.write_block` wrote straight into the file; `flush()` was just `fsync`. | No volatile-cache model, so "flush before commit" ordering could not be violated or tested; crash tests could not distinguish durable from non-durable. | `virtual_disk.py` rewritten with a volatile write-back cache (default) + `writethrough`; `crash()` drops unflushed writes; torn-write support. | `test_commit_orders_flushes_correctly`, crash matrix |
| 2 | H | `crash-test` CLI command printed "Crash injection enabled…" and ran nothing; README claimed "13/13 crash scenarios verified". | Stub implementation, unverified claim. | Real subprocess lab (`crash_lab.py`, `crash_worker.py`); CLI `crash-test <scenario> <operation>` runs it. | `TestCrashMatrixVolatile` (6 ops × 14 scenarios) |
| 3 | H | `needs_recovery()` was `journal_head != journal_tail`; recovery trusted the superblock head. | Head is updated after commit, so it can lag or lead the durable journal. | Recovery scans from the tail using txid sequence + CRC + commit-record count; head ignored. | `test_stale_journal_records_from_earlier_laps_are_ignored`, wraparound tests |
| 4 | H | Metadata could be modified in memory/on disk outside a transaction; abort wrote an ABORT record into the journal. | Staging was advisory; allocator/inode caches held state across aborts. | `StagingIO` overlay; writes outside a tx raise; caches removed; abort writes nothing. | `test_metadata_writes_outside_transaction_are_rejected`, `test_abort_restores_...` |
| 5 | H | Commit record had no record count; a torn/missing DATA record could still look committed. | Commit not bound to its payload. | Commit payload carries the DATA record count; own block; CRC. | `test_missing_data_records_detected_by_commit_record_count`, torn-commit sweep |
| 6 | M | `delete`/`rmdir` freed an inode whose link count was still non-zero → `InvalidInodeError` / inconsistent state. | Wrong order of operations. | Set `link_count = 0` and write the inode before `free_inode`. | legacy delete tests, namespace tests |
| 7 | M | Superblock checkpoint rebuilt from in-memory (possibly uncommitted) counters. | Shared mutable superblock object. | Checkpoint built from the on-disk committed superblock. | `test_failed_operation_leaves_no_trace_and_txid_is_reused` |
| 8 | M | Journal full / disk full / inode full mid-operation left partial state. | No clean abort path. | Errors raised before commit; abort restores counters; old file content untouched (copy-on-write). | `TestResourceExhaustion` |
| 9 | M | CRC64 computed bitwise, per byte, in Python (slow); record validation incomplete (targets inside journal, past disk end). | – | Table-driven CRC64; target validation in scan/replay. | `test_record_targeting_journal_region_is_rejected`, `test_stale_pointer_beyond_disk_is_rejected` |
| 10 | M | fsck reported bare strings without a reason; missed duplicate block ownership, orphans, bad link counts. | – | Findings with `code` + `why`; added checks. | `TestFsckDetectsDamage` |
| 11 | L | README / development plan stated "41/41 tests passing" and "13/13 verified". | Stale claims. | Replaced with measured results in `docs/testing.md`. | – |
| 12 | L | `src/__init__` imported the CLI → `runpy` warning on `python -m src.cli`. | – | Removed import. | – |

Not changed (documented in `docs/limitations.md`): single-copy superblock, 48 KiB file limit, no hard links.
