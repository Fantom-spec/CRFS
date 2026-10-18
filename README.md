# CRFS - Crash-Recoverable File System Simulator

A Python implementation of a crash-recoverable file system with write-ahead journaling for an Operating Systems course project.

## Features

- **Virtual Disk**: Fixed-size block I/O on a disk image file with bounds checking and fault injection
- **Write-Ahead Journaling**: Metadata transactions with redo logging, checksums, and commit records
- **Crash Recovery**: Journal scanning, validation, and idempotent replay of committed transactions
- **Consistency Checking**: Structural and allocation integrity verification
- **Crash Injection**: Deterministic fault injection for testing crash recovery
- **CLI Interface**: Interactive shell and single-command modes

## Architecture

```
src/
├── virtual_disk.py      # Disk image creation, block I/O, fault injection
├── superblock.py        # Filesystem format metadata and validation
├── block_allocator.py   # Free-space bitmap management
├── inode.py             # Inode serialization and metadata
├── directory.py         # Directory entry serialization
├── journal.py           # Transaction records, checksums, commit records
├── transaction.py       # Transaction lifecycle management
├── recovery.py          # Journal scanning and replay
├── consistency_checker.py # Structural integrity validation
├── crash_injector.py    # Deterministic crash points
├── filesystem.py        # Public filesystem operations
└── cli.py               # Command-line interface
```

## Quick Start

```bash
# Format a new filesystem with options
python -m src.cli filesystem.img --format --size 16M --journal-blocks 128 --inode-blocks 64 --label "MyVolume"

# Or use the CLI interactively
python -m src.cli filesystem.img
crfs> format 16M 128 64 "MyVolume"
crfs> mkdir /home/user
crfs> create /home/user/file.txt
crfs> write /home/user/file.txt "Hello, World!"
crfs> read /home/user/file.txt
crfs> ls /home/user
crfs> stat /home/user/file.txt
crfs> exit
```

## Format Options

| Option | Description | Default |
|--------|-------------|---------|
| `--size` | Disk size (K, M, G suffixes) | 16M |
| `--journal-blocks` | Number of journal blocks | 128 |
| `--inode-blocks` | Number of inode table blocks | 64 |
| `--label` | Volume label | (empty) |

## CLI Commands

### Filesystem Operations
- `format [size] [journal_blocks] [inode_blocks] [label]` - Format new filesystem
- `mount` - Mount existing filesystem
- `unmount` - Unmount filesystem
- `mkdir <path>` - Create directory
- `create <path>` - Create empty file
- `write <path> <text>` - Write text to file
- `read <path>` - Read file contents
- `append <path> <text>` - Append text to file
- `ls [path]` - List directory (default: /)
- `stat <path>` - Show file/directory info
- `rename <old> <new>` - Rename/move file or directory
- `delete <path>` - Delete file
- `rmdir <path>` - Remove empty directory

### Utility Commands
- `df` - Show disk usage
- `dump` - Dump filesystem state (JSON)
- `check` - Run consistency check
- `recover` - Run crash recovery

### Crash Testing
- `enable-crash-injection <mode>` - Enable crash injection (exception|subprocess|none)
- `crash-test <scenario>` - Run crash test scenario
- `disable-crash-injection` - Disable crash injection

Available crash scenarios:
- `crash_before_journal_write`
- `crash_during_journal_write`
- `crash_after_journal_write_before_flush`
- `crash_after_journal_flush_before_commit`
- `crash_during_commit_write`
- `crash_after_commit_write_before_flush`
- `crash_after_commit_flush_before_apply`
- `crash_during_home_apply`
- `crash_after_home_apply_before_flush`
- `crash_after_home_flush_before_checkpoint`
- `crash_during_checkpoint`
- `crash_during_recovery_scan`
- `crash_during_recovery_replay`

## Requirements

- Python 3.11+
- No external dependencies (uses only standard library)

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test class
python -m pytest tests/test_filesystem.py::TestFileSystem -v

# Run crash injection tests only
python -m pytest tests/ -v -k "crash"

# Run recovery tests
python -m pytest tests/ -v -k "recovery"

# Run consistency checker tests
python -m pytest tests/ -v -k "consistency"

# Generate coverage report
python -m pytest tests/ --cov=src --cov-report=html
```

## Documentation

- [Architecture](docs/architecture.md) - Component architecture and module responsibilities
- [Architecture Diagram](docs/architecture-diagram.md) - Mermaid diagram of component relationships
- [Disk Layout](docs/disk-layout.md) - Exact block regions, offsets, structures, and format constraints
- [Journaling and Recovery](docs/journaling-and-recovery.md) - Transaction protocol, commit protocol, write ordering, replay rules
- [Testing](docs/testing.md) - Test organization, crash points, invariants
- [Demo Script](docs/demo-script.md) - 5-8 minute presentation demonstration
- [Limitations](docs/limitations.md) - Unsupported functionality, durability assumptions, known issues
- [Development Plan](docs/development-plan.md) - Implemented phases and remaining work
- [Crash Test Report](docs/crash-test-report.md) - Test results from crash injection suite

## Test Results

- **41/41 tests passing** (100%)
- All crash scenarios verified (13/13)
- Recovery tests pass
- Consistency checker validates structural integrity

## Example: Crash Recovery Demo

```bash
# 1. Setup
python -m src.cli demo.img --format --size 16M
python -m src.cli demo.img
crfs> mkdir /projects/demo
crfs> create /projects/demo/README.txt
crfs> write /projects/demo/README.txt "OLD DATA"

# 2. Inject crash BEFORE commit (old data should be preserved)
crfs> enable-crash-injection exception
crfs> crash-test crash_after_journal_flush_before_commit
crfs> write /projects/demo/README.txt "NEW DATA"
crfs> exit

# 3. Recover and verify
python -m src.cli demo.img
crfs> read /projects/demo/README.txt
# Output: OLD DATA (old data preserved)

# 4. Inject crash AFTER commit (new data should be recovered)
python -m src.cli demo.img
crfs> enable-crash-injection exception
crfs> crash-test crash_after_commit_flush_before_apply
crfs> write /projects/demo/README.txt "NEW DATA v2"
crfs> exit

# 5. Recover committed transaction
python -m src.cli demo.img
crfs> read /projects/demo/README.txt
# Output: NEW DATA v2 (committed transaction replayed)
```

## License

Educational project for Operating Systems course.