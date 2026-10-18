# Crash-Recoverable File System Simulator - Architecture

## Overview

This document describes the architecture of a crash-recoverable file system simulator implemented in Python. The system implements a custom file system on top of a virtual disk image with write-ahead journaling for crash recovery.

## Module Responsibilities

### 1. virtual_disk.py
- **Purpose**: Low-level disk image management
- **Responsibilities**:
  - Create, open, and close disk image files
  - Fixed-size block reads/writes (4096 bytes default)
  - Flush/sync operations for persistence guarantees
  - Bounds checking for all block accesses
  - Fault injection points for crash testing

### 2. superblock.py
- **Purpose**: Filesystem format metadata and validation
- **Responsibilities**:
  - Define on-disk superblock structure
  - Magic number validation
  - Format version checking
  - Disk layout parameters (block size, total blocks, region offsets)
  - Serialization/deserialization

### 3. block_allocator.py
- **Purpose**: Free-space bitmap management
- **Responsibilities**:
  - Bitmap allocation and deallocation
  - Find free blocks
  - Allocation verification
  - Bitmap persistence

### 4. inode.py
- **Purpose**: Inode management and serialization
- **Responsibilities**:
  - Inode structure definition (type, permissions, timestamps, size, block pointers)
  - Inode serialization/deserialization
  - Direct block pointer management
  - Inode allocation and reclamation

### 5. directory.py
- **Purpose**: Directory entry management
- **Responsibilities**:
  - Directory entry structure (name, inode number)
  - Directory entry serialization
  - Name-to-inode mapping within directory blocks
  - Directory operations (add, remove, lookup, list)

### 6. journal.py
- **Purpose**: Write-ahead journal management
- **Responsibilities**:
  - Transaction record format (header, metadata after-images, checksums)
  - Commit record format
  - Journal bounds and wraparound
  - Record validation with checksums
  - Journal space management

### 7. transaction.py
- **Purpose**: Transaction lifecycle management
- **Responsibilities**:
  - Transaction begin/commit/abort
  - Ordered writes to journal
  - Metadata change tracking
  - Flush synchronization points
  - Crash point injection

### 8. recovery.py
- **Purpose**: Crash recovery and journal replay
- **Responsibilities**:
  - Journal scanning and validation
  - Committed transaction identification
  - Incomplete transaction detection and discard
  - Idempotent replay of committed transactions
  - Checkpoint management

### 9. consistency_checker.py
- **Purpose**: Filesystem integrity validation
- **Responsibilities**:
  - Superblock validation
  - Inode and bitmap consistency
  - Directory hierarchy validation
  - Block ownership verification
  - Journal structure validation

### 10. crash_injector.py
- **Purpose**: Deterministic crash injection for testing
- **Responsibilities**:
  - Crash point definitions
  - In-process crash simulation (exception-based)
  - Subprocess crash mode (SIGKILL)
  - Crash scenario orchestration

### 11. filesystem.py
- **Purpose**: High-level filesystem operations
- **Responsibilities**:
  - Path resolution
  - File/directory creation, deletion, renaming
  - Read/write/append operations
  - Mount/unmount with recovery
  - Namespace management

### 12. cli.py
- **Purpose**: Command-line interface
- **Responsibilities**:
  - Interactive shell and single-command modes
  - Command parsing and dispatch
  - User-facing error messages
  - Help system

## Disk Layout

```
+------------------+  Block 0
|    Superblock    |  (1 block)
+------------------+  Block 1
|    Journal       |  N blocks (configurable, default 128)
+------------------+  Block 1 + N
|    Bitmap        |  M blocks (1 bit per data block)
+------------------+  Block 1 + N + M
|    Inode Table   |  K blocks (fixed-size inode array)
+------------------+  Block 1 + N + M + K
|    Data Blocks   |  Remaining blocks
|  (files/dirs)    |
+------------------+
```

## Transaction Protocol

### Write-Ahead Journaling (Redo Logging)

1. **Prepare**: Collect all metadata changes for the operation
2. **Journal Write**: Write transaction records with after-images to journal
3. **Journal Flush**: `fsync` journal records to disk
4. **Commit Record**: Write commit record with transaction ID
5. **Commit Flush**: `fsync` commit record
6. **Apply**: Write metadata after-images to home locations
7. **Home Flush**: `fsync` home locations
8. **Checkpoint**: Advance journal head, reclaim space

### Crash Recovery

1. Validate superblock and journal bounds
2. Scan journal from last checkpoint
3. Validate records using checksums and lengths
4. Identify committed transactions (valid commit record)
5. Replay committed transactions (idempotent)
6. Flush recovered metadata
7. Update checkpoint
8. Run consistency checker

## Durability Assumptions

- Host filesystem provides `fsync` durability for the disk image file
- Block writes are atomic at the block level (4096 bytes)
- Write ordering is preserved by `fsync` barriers
- Power loss during write may cause torn blocks (handled by checksums)
- Journal records include checksums to detect corruption

## Testing Strategy

- Unit tests for each module
- Integration tests for filesystem operations
- Crash injection tests at every protocol stage
- Consistency checker validation after each recovery
- Automated test suite with deterministic seeds