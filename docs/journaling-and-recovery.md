# Journaling and Recovery Protocol

## Transaction State Machine

```
IDLE -> PREPARING -> JOURNAL_WRITING -> JOURNAL_FLUSHED -> COMMIT_WRITING -> COMMIT_FLUSHED -> APPLYING -> HOME_FLUSHED -> CHECKPOINTING -> IDLE
                    |                  |                   |                  |              |               |
                    v                  v                   v                  v              v               v
                ABORTED            ABORTED             ABORTED            ABORTED        ABORTED       ABORTED
```

## Write Ordering Protocol

### Metadata Journaling (Redo Logging)

For each filesystem operation that modifies metadata:

1. **Begin Transaction**
   - Allocate new transaction ID (monotonically increasing)
   - Record start timestamp
   - Write TRANS_BEGIN record to journal

2. **Log Metadata Changes**
   - For each metadata block to be modified:
     - Compute after-image (entire block or modified portion)
     - Write TRANS_DATA record with target block, offset, length, after-image
   - Records are written sequentially in journal

3. **Flush Journal Records**
   - `fsync()` the journal region up to the last record
   - This ensures all TRANS_DATA records are durable before commit

4. **Write Commit Record**
   - Write TRANS_COMMIT record with transaction ID
   - This is the durability boundary - transaction is committed once this is durable

5. **Flush Commit Record**
   - `fsync()` the commit record
   - At this point, transaction is officially committed

6. **Apply to Home Locations**
   - Write each metadata after-image to its home block location
   - These writes can be in any order (idempotent replay handles crashes)

7. **Flush Home Locations**
   - `fsync()` all modified home blocks
   - Ensures metadata changes are durable at home locations

8. **Checkpoint**
   - Advance journal tail to after this transaction
   - Write CHECKPOINT record
   - `fsync()` checkpoint
   - Reclaim journal space

## Recovery Algorithm

### Phase 1: Journal Scan

1. Read superblock, validate magic and version
2. Read journal from checkpoint tail position (or journal start if no checkpoint)
3. Scan sequentially through journal blocks:
   - Read record header
   - Validate checksum (header + payload)
   - Validate record type and length
   - Track transaction state per transaction ID
   - Stop at first invalid/truncated record or journal head

### Phase 2: Transaction Classification

For each transaction ID encountered:
- **Committed**: Has valid TRANS_COMMIT record
- **Incomplete**: Has TRANS_BEGIN but no valid TRANS_COMMIT
- **Aborted**: Has TRANS_ABORT record

Only committed transactions are replayed.

### Phase 3: Replay

For each committed transaction (in chronological order):
1. Collect all TRANS_DATA records for this transaction
2. For each TRANS_DATA record:
   - Write after-image to target block at specified offset
3. Flush all written blocks

### Phase 4: Checkpoint Update

1. Set journal tail to after the last committed transaction
2. Write new CHECKPOINT record
3. Flush checkpoint
4. Update superblock journal tail

### Phase 5: Consistency Verification

Run consistency checker to validate recovered state.

## Idempotency Guarantees

- TRANS_DATA records contain complete after-images
- Applying the same after-image multiple times produces identical result
- Home block writes are idempotent
- Checkpoint advancement is idempotent (monotonic)

## Crash Scenarios and Outcomes

| Crash Point | Recovery Outcome |
|-------------|------------------|
| During TRANS_BEGIN write | Transaction not seen, no effect |
| During TRANS_DATA writes | Partial transaction, no commit record, discarded |
| After journal flush, before commit | No commit record, transaction discarded |
| During commit record write | Torn commit record (checksum fails), discarded |
| After commit flush, before apply | Commit record valid, replay applies changes |
| During home block writes | Partial apply, replay completes remaining |
| After home flush, before checkpoint | Committed, replayed, checkpoint advances |
| During checkpoint | Checkpoint may be incomplete, next recovery re-scans |

## Journal Wraparound

- Journal is a circular buffer
- Head advances on writes, tail advances on checkpoint
- Space between tail and head (circular) is used space
- Transaction must fit in available space (head to tail - 1 block)
- If insufficient space: force checkpoint or reject operation

## Checkpoint Protocol

Checkpoint record contains:
- Checkpoint transaction ID (last fully applied transaction)
- Journal head position at checkpoint time
- Journal tail position (should equal after last committed transaction)

On recovery, start scanning from checkpoint tail position.

## Durability Assumptions

1. **fsync() durability**: When `fsync()` returns, data is on stable storage
2. **Write ordering**: `fsync()` creates a barrier; writes before `fsync()` are ordered before writes after
3. **Block atomicity**: 4096-byte block writes are atomic (no torn blocks within a block)
4. **Checksum detection**: CRC64 detects corruption with probability 1 - 2^-64

## Limitations

- No data journaling (only metadata)
- File content writes are not transactional (metadata journaling only)
- Single-threaded operation (no concurrent transactions)
- Journal size fixed at format time
- No online resizing