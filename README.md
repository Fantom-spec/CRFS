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
# Format a new filesystem
python -m src.cli filesystem.img --format --size 16M

# Or use the CLI interactively
python -m src.cli filesystem.img
crfs> format 16M 128 64
crfs> mkdir /home/user
crfs> create /home/user/file.txt
crfs> write /home/user/file.txt "Hello, World!"
crfs> read /home/user/file.txt
crfs> ls /home/user
crfs> stat /home/user/file.txt
crfs> exit
```

## Requirements

- Python 3.11+
- No external dependencies (uses only standard library)

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test class
python -m pytest tests/test_filesystem.py::TestFileSystem -v
```

## Documentation

- [Architecture](docs/architecture.md) - Component architecture and module responsibilities
- [Disk Layout](docs/disk-layout.md) - Exact block regions, offsets, structures, and format constraints
- [Journaling and Recovery](docs/journaling-and-recovery.md) - Transaction protocol, commit protocol, write ordering, replay rules
- [Testing](docs/testing.md) - Test organization, crash points, invariants
- [Demo Script](docs/demo-script.md) - 5-8 minute presentation demonstration
- [Limitations](docs/limitations.md) - Unsupported functionality, durability assumptions, known issues

## License

Educational project for Operating Systems course.# CRFS
