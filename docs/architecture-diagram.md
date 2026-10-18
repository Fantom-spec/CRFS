# Architecture Diagram

```mermaid
graph TB
    subgraph CLI["Command Line Interface"]
        CLI_MAIN[cli.py]
    end

    subgraph FS["Filesystem Layer"]
        FS_API[filesystem.py: FileSystem]
        TX_MGR[transaction.py: TransactionManager]
        RECOVERY[recovery.py: RecoveryManager]
        CHECKER[consistency_checker.py: ConsistencyChecker]
        CRASH_INJ[crash_injector.py: CrashInjector]
    end

    subgraph CORE["Core Data Structures"]
        INODE[inode.py: InodeTable, Inode]
        DIR[directory.py: Directory, DirEntry]
        BLOCK_ALLOC[block_allocator.py: BlockAllocator]
        SB[superblock.py: SuperblockManager, Superblock]
        JOURNAL[journal.py: Journal, Transaction]
    end

    subgraph STORAGE["Storage Layer"]
        VDISK[virtual_disk.py: VirtualDisk]
        DISK_IMG[(disk image file)]
    end

    CLI_MAIN --> FS_API
    FS_API --> TX_MGR
    FS_API --> RECOVERY
    FS_API --> CHECKER
    FS_API --> CRASH_INJ
    FS_API --> INODE
    FS_API --> DIR
    FS_API --> BLOCK_ALLOC
    FS_API --> SB
    FS_API --> JOURNAL

    TX_MGR --> JOURNAL
    TX_MGR --> INODE
    TX_MGR --> BLOCK_ALLOC
    TX_MGR --> SB
    TX_MGR -.-> CRASH_INJ

    RECOVERY --> JOURNAL
    RECOVERY --> INODE
    RECOVERY --> BLOCK_ALLOC
    RECOVERY --> SB
    RECOVERY -.-> CRASH_INJ

    CHECKER --> SB
    CHECKER --> INODE
    CHECKER --> BLOCK_ALLOC
    CHECKER --> DIR
    CHECKER --> JOURNAL

    CRASH_INJ -.-> VDISK
    CRASH_INJ -.-> TX_MGR
    CRASH_INJ -.-> RECOVERY

    SB --> VDISK
    INODE --> VDISK
    DIR --> VDISK
    BLOCK_ALLOC --> VDISK
    JOURNAL --> VDISK

    VDISK --> DISK_IMG

    style CLI_MAIN fill:#e1f5fe
    style FS_API fill:#fff3e0
    style TX_MGR fill:#fff3e0
    style RECOVERY fill:#fff3e0
    style CHECKER fill:#fff3e0
    style CRASH_INJ fill:#ffebee
    style INODE fill:#e8eaf6
    style DIR fill:#e8eaf6
    style BLOCK_ALLOC fill:#e8eaf6
    style SB fill:#e8eaf6
    style JOURNAL fill:#e8eaf6
    style VDISK fill:#f3e5f5
    style DISK_IMG fill:#f1f8e9
```

## Component Relationships

### Data Flow (Normal Operation)
1. **CLI** → **FileSystem** → **TransactionManager** → **Journal** (WAL)
2. **TransactionManager** tracks metadata changes
3. **Commit**: Journal → flush → Commit Record → flush → Home Blocks → flush → Checkpoint
4. **Recovery**: On mount, scan journal → replay committed transactions

### Crash Injection Points
- **VirtualDisk**: Before/after read/write/flush
- **TransactionManager**: Before/after journal write/flush/commit/apply/checkpoint
- **RecoveryManager**: During scan/replay

### Recovery Process
1. Read superblock, validate
2. Scan journal from checkpoint tail
3. Validate records (checksum, length, type)
4. Identify committed transactions (valid TRANS_COMMIT)
5. Replay committed transactions (idempotent)
6. Flush recovered metadata
7. Update checkpoint
8. Run consistency checker

### Consistency Checks
1. Superblock validation (magic, version, bounds, checksum)
2. Inode validation (type, size, blocks, links, timestamps)
3. Block allocation (bitmap vs inode references, no leaks)
4. Directory hierarchy (valid entries, no cycles, reachable)
5. Journal integrity (record structure, checksums)
6. Orphan detection (unlinked inodes, unreachable directories)