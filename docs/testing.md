# Testing Documentation

## Test Organization

The test suite is organized in `tests/test_filesystem.py` with the following test classes:

### Unit Tests
- **TestVirtualDisk**: Block I/O, bounds checking, flush operations
- **TestSuperblock**: Creation, validation, checksum verification
- **TestBlockAllocator**: Allocation, deallocation, contiguous allocation, free count
- **TestInode**: Serialization, validation, allocation, freeing
- **TestDirectory**: Entry serialization, add/remove/rename operations
- **TestJournal**: Transaction records, checksums, journal write/read

### Integration Tests
- **TestFileSystem**: Format/mount, mkdir/create, write/read/append, stat, rename, delete, rmdir, df, persistence, nested directories, large files, disk full behavior

### Recovery Tests
- **TestRecovery**: Clean recovery, recovery after commit

### Crash Injection Tests
- **TestCrashInjection**: Exception mode, disabled mode

### Consistency Checker Tests
- **TestConsistencyChecker**: Clean filesystem check, corruption detection

## Crash Points

The crash injector supports the following deterministic crash points:

| Crash Point | Description |
|-------------|-------------|
| `BEFORE_JOURNAL_WRITE` | Before writing journal records |
| `AFTER_JOURNAL_WRITE` | After writing journal records |
| `AFTER_JOURNAL_FLUSH` | After journal flush |
| `BEFORE_COMMIT_WRITE` | Before writing commit record |
| `AFTER_COMMIT_WRITE` | After writing commit record |
| `AFTER_COMMIT_FLUSH` | After commit record flush |
| `BEFORE_HOME_WRITE` | Before applying to home locations |
| `AFTER_HOME_WRITE` | After applying to home locations |
| `AFTER_HOME_FLUSH` | After home locations flush |
| `BEFORE_CHECKPOINT` | Before checkpoint write |
| `AFTER_CHECKPOINT` | After checkpoint write |
| `DURING_RECOVERY_SCAN` | During journal scan |
| `DURING_RECOVERY_REPLAY` | During transaction replay |

## Crash Test Scenarios

Predefined crash scenarios in `CrashInjector.SCENARIOS`:

1. **crash_before_journal_write** - Crash before writing journal records
2. **crash_during_journal_write** - Crash during journal record writing
3. **crash_after_journal_write_before_flush** - Crash after journal write but before flush
4. **crash_after_journal_flush_before_commit** - Crash after journal flush but before commit record
5. **crash_during_commit_write** - Crash during commit record write
6. **crash_after_commit_write_before_flush** - Crash after commit write but before commit flush
7. **crash_after_commit_flush_before_apply** - Crash after commit flush but before home block apply
8. **crash_during_home_apply** - Crash during home block application
9. **crash_after_home_apply_before_flush** - Crash after home apply but before home flush
10. **crash_after_home_flush_before_checkpoint** - Crash after home flush but before checkpoint
11. **crash_during_checkpoint** - Crash during checkpoint write
12. **crash_during_recovery_scan** - Crash during recovery journal scan
13. **crash_during_recovery_replay** - Crash during recovery replay

## Running Crash Tests

```bash
# Run all crash scenarios (requires manual operation trigger)
python -m src.cli filesystem.img
crfs> crash-test crash_after_commit_flush_before_apply
# Then perform an operation that will crash
```

## Invariants Verified by Consistency Checker

1. **Superblock validity** - Magic number, version, block size, region bounds
2. **Inode validity** - Type, size, block pointers, link count, timestamps
3. **Block allocation consistency** - Bitmap matches inode references, no duplicate ownership
4. **Directory hierarchy** - Valid entries, no duplicate names, valid file types, no cycles
5. **Journal integrity** - Valid record structure, checksums, commit records
6. **No orphans** - All allocated inodes reachable, no leaked blocks

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=src --cov-report=html

# Specific test
python -m pytest tests/test_filesystem.py::TestFileSystem::test_write_read -v
```