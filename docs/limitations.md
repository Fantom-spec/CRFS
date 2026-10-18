# Limitations and Known Issues

## Functional Limitations

### File Size Limits
- **Maximum file size**: 48 KiB (12 direct blocks × 4 KiB)
- No indirect block support implemented
- Large files (>48 KiB) will fail with "Disk full" or truncation

### Concurrency
- **Single-threaded only**: No support for concurrent operations
- No locking or transaction isolation between operations
- Not suitable for multi-process or multi-threaded use

### Directory Features
- No symbolic links support
- No hard links support (link count tracked but not enforced for directories)
- No extended attributes or ACLs
- Maximum filename length: 51 characters
- Maximum entries per directory: ~64 per block (limited by direct blocks)

### Filesystem Features
- No filesystem resizing (fixed at format time)
- No defragmentation or online maintenance
- No quota support
- No snapshots or clones
- No compression or deduplication
- No encryption

## Durability Assumptions

### Host Filesystem
- **fsync() durability**: Assumes `os.fsync()` on the disk image file provides durability
- **Write ordering**: Assumes `fsync()` creates proper write barriers
- **Atomic block writes**: Assumes 4 KiB block writes are atomic (no torn blocks within a block)
- **No silent corruption**: Host filesystem must not silently corrupt data

### Storage Device
- **Power-loss protection**: Assumes storage device honors flush commands
- **No write reordering**: Device must not reorder writes across flush boundaries
- **Bad block handling**: No bad block management implemented

### Crash Model
- **Fail-stop crashes**: Only models sudden power loss / process termination
- **No Byzantine faults**: Does not handle malicious or arbitrary corruption
- **Single disk**: No RAID or replication support

## Known Issues

### Journal
- **Fixed journal size**: Cannot grow journal if needed
- **No journal compression**: Redundant records not compacted
- **Wraparound policy**: Simple circular buffer, may lose uncommitted transactions under heavy load

### Recovery
- **No partial transaction recovery**: Incomplete transactions are always discarded
- **No interactive recovery**: Fully automatic, no user intervention options
- **No lost+found**: Orphaned inodes/blocks not recovered to special directory

### Consistency Checker
- **Read-only**: Does not repair corruption (repair mode not implemented)
- **No data verification**: Does not verify file content checksums
- **Performance**: Full scan is O(n) in inodes and blocks

### Performance
- **No caching**: All reads/writes go directly to disk image
- **No read-ahead**: Sequential reads not optimized
- **Synchronous writes**: All metadata writes are synchronous (fsync after each transaction)
- **No batching**: Multiple operations not batched into single transaction

### Crash Injection
- **Exception mode only**: Subprocess/SIGKILL mode not fully implemented
- **Limited crash points**: Only metadata transaction boundaries covered
- **No data crash points**: File data writes not crash-injected

## Platform Limitations

### Python-Specific
- **GIL**: Single-threaded due to Python GIL
- **Memory**: Disk image fully managed in Python objects during operations
- **Signal handling**: Limited signal handling for graceful shutdown

### Portability
- **Linux-focused**: Tested on Linux; Windows/macOS may have different fsync behavior
- **Endianness**: Little-endian only (x86/ARM)
- **Block size**: Fixed at 4096 bytes

## Future Improvements

### High Priority
1. Indirect block support for larger files
2. Concurrent transaction support with locking
3. Online fsck/repair mode
4. Journal compression and dynamic sizing

### Medium Priority
1. Extended attributes support
2. Symbolic and hard links
3. Subprocess crash injection mode
4. Read caching and write buffering
5. Online resize support

### Low Priority
1. Snapshot/clone support
2. Compression and deduplication
3. Encryption
4. Quota management
4. RAID/replication awareness

## Security Considerations

- **No authentication**: No access control beyond basic mode bits
- **No encryption**: Data stored in plaintext
- **No integrity verification**: No Merkle trees or content checksums
- **No audit logging**: No operation audit trail
- **Crash injection**: Could be used for DoS if exposed

## Testing Limitations

- **Unit tests only**: No integration with real storage devices
- **No power-loss simulation**: Uses exception injection, not real power loss
- **No concurrent testing**: No multi-threaded test scenarios
- **Limited stress testing**: No long-running or high-load tests