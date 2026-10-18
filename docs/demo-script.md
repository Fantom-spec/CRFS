# Demo Script

## 5-8 Minute Presentation Demonstration

This script demonstrates the crash-recoverable file system capabilities for an Operating Systems course presentation.

### Prerequisites

```bash
# Ensure we start clean
rm -f demo.img
python -m src.cli demo.img --format --size 16M --journal-blocks 128 --inode-blocks 64 --label "DemoVolume"
```

### Demo Sequence (Total: ~6 minutes)

#### 1. Format and Mount (30 seconds)

```bash
python -m src.cli demo.img
crfs> format 16M 128 64 "DemoVolume"
crfs> df
```

**Expected output**: Shows 16MB disk, ~4000 free blocks, 1024 inodes

#### 2. Create Directory Structure (45 seconds)

```bash
crfs> mkdir /projects
crfs> mkdir /projects/demo
crfs> mkdir /projects/demo/src
crfs> ls /projects/demo
```

**Expected output**: Empty directory listing

#### 3. Create and Write File (45 seconds)

```bash
crfs> create /projects/demo/README.txt
crfs> write /projects/demo/README.txt "OLD DATA - Initial version"
crfs> read /projects/demo/README.txt
crfs> stat /projects/demo/README.txt
```

**Expected output**: File content "OLD DATA - Initial version", size 26 bytes, 1 block

#### 4. Demonstrate Persistence (30 seconds)

```bash
crfs> exit
python -m src.cli demo.img
crfs> read /projects/demo/README.txt
```

**Expected output**: "OLD DATA - Initial version" - data persists across restarts

#### 5. Crash During File Replacement - BEFORE COMMIT (60 seconds)

```bash
crfs> enable-crash-injection exception
crfs> crash-test crash_after_journal_flush_before_commit
crfs> write /projects/demo/README.txt "NEW DATA - This should not appear"
# Operation crashes here
crfs> exit
```

**Expected output**: Crash injected during write operation

#### 6. Recovery After Crash (45 seconds)

```bash
python -m src.cli demo.img
crfs> read /projects/demo/README.txt
crfs> check
```

**Expected output**: "OLD DATA - Initial version" - old data preserved, consistency check passes

#### 7. Crash During File Replacement - AFTER COMMIT (60 seconds)

```bash
crfs> enable-crash-injection exception
crfs> crash-test crash_after_commit_flush_before_apply
crfs> write /projects/demo/README.txt "NEW DATA - This SHOULD appear after recovery"
# Operation crashes here
crfs> exit
```

**Expected output**: Crash injected after commit but before home apply

#### 8. Recovery of Committed Transaction (45 seconds)

```bash
python -m src.cli demo.img
crfs> read /projects/demo/README.txt
crfs> check
```

**Expected output**: "NEW DATA - This SHOULD appear after recovery" - committed transaction replayed

#### 9. Consistency Check and Disk Usage (30 seconds)

```bash
crfs> check
crfs> df
crfs> dump
```

**Expected output**: Consistency check passes, disk usage statistics, full dump

#### 10. Nested Operations and Cleanup (45 seconds)

```bash
crfs> mkdir /projects/demo/docs
crfs> create /projects/demo/docs/notes.txt
crfs> write /projects/demo/docs/notes.txt "Meeting notes"
crfs> ls /projects/demo
crfs> rename /projects/demo/README.txt /projects/demo/OLD_README.txt
crfs> delete /projects/demo/docs/notes.txt
crfs> rmdir /projects/demo/docs
crfs> ls /projects/demo
```

**Expected output**: Directory operations work correctly

#### 11. Run Automated Crash Test Suite (60 seconds)

```bash
crfs> exit
python -m pytest tests/ -v -k "crash"
```

**Expected output**: All crash injection tests pass

### Key Points to Highlight

1. **Write-Ahead Journaling**: Metadata changes are logged before applying
2. **Atomic Transactions**: Operations either fully commit or fully abort
3. **Idempotent Recovery**: Replay is safe to run multiple times
4. **Consistency Guarantees**: Checker validates structural integrity
5. **Deterministic Testing**: Crash injection enables reproducible testing

### Cleanup

```bash
rm -f demo.img
```

### Troubleshooting

- If crash doesn't trigger: Ensure `enable-crash-injection exception` is run before `crash-test`
- If recovery fails: Check `check` output for errors
- If disk full: Use larger disk size or fewer test files