# Disk Layout Specification

## Overview

This document specifies the exact on-disk format for the crash-recoverable file system simulator.

## Constants

- **Magic Number**: `0x43524653` ("CRFS" in ASCII)
- **Format Version**: 1
- **Block Size**: 4096 bytes (4 KiB)
- **Default Disk Size**: 16 MiB (4096 blocks)
- **Default Journal Blocks**: 128 blocks
- **Inode Size**: 256 bytes
- **Inodes per Block**: 16 (4096 / 256)
- **Default Inode Count**: 1024 (64 blocks)
- **Directory Entry Size**: 64 bytes
- **Entries per Block**: 64 (4096 / 64)

## Block Regions

### Block 0: Superblock (1 block)

```
Offset  Size  Field
------  ----  -----
0       4     Magic number (uint32, little-endian) = 0x43524653
4       2     Format version (uint16) = 1
6       2     Flags (uint16, reserved)
8       4     Block size (uint32) = 4096
12      8     Total blocks (uint64)
20      8     Journal start block (uint64)
28      4     Journal block count (uint32)
32      8     Bitmap start block (uint64)
40      4     Bitmap block count (uint32)
44      8     Inode table start block (uint64)
52      4     Inode table block count (uint32)
56      8     Data region start block (uint64)
64      8     Data region block count (uint64)
72      8     Root inode number (uint64) = 1
80      8     Next free inode (uint64)
88      8     Journal head (uint64) - next write position
96      8     Journal tail (uint64) - oldest valid transaction
104     8     Checkpoint transaction ID (uint64)
112     8     Total inodes (uint64)
120     8     Free inode count (uint64)
128     8     Free block count (uint64)
136     16    Volume label (bytes, null-padded)
152     16    Reserved for future use
168     40    Padding to 208 bytes
208     8     Checksum (uint64, CRC64 of bytes 0-207)
216     3880  Padding to fill block
```

### Journal Region (configurable, default 128 blocks)

Each journal block contains one or more journal records.

#### Journal Record Header (24 bytes)

```
Offset  Size  Field
------  ----  -----
0       8     Transaction ID (uint64)
8       2     Record type (uint16)
10      2     Flags (uint16)
12      4     Payload length (uint32)
16      8     Checksum (uint64, CRC64 of header + payload)
```

#### Record Types

- `0x0001` = TRANS_BEGIN
- `0x0002` = TRANS_DATA (metadata after-image)
- `0x0003` = TRANS_COMMIT
- `0x0004` = TRANS_ABORT
- `0x0005` = CHECKPOINT

#### TRANS_BEGIN Payload (16 bytes)

```
Offset  Size  Field
------  ----  -----
0       8     Transaction ID (uint64)
8       8     Timestamp (uint64, nanoseconds since epoch)
```

#### TRANS_DATA Payload (variable)

```
Offset  Size  Field
------  ----  -----
0       8     Target block number (uint64)
8       4     Offset within block (uint32)
12      4     Length (uint32)
16      N     After-image data (length bytes)
```

#### TRANS_COMMIT Payload (12 bytes; 8-byte legacy form still accepted)

```
Offset  Size  Field
------  ----  -----
0       8     Transaction ID (uint64)
8       4     Number of TRANS_DATA records in this transaction (uint32)
```

The record count lets recovery reject a commit whose data records are missing or torn.

#### Journal placement rules (v2 engine, format version unchanged)

* A transaction starts on a **block boundary**: `BEGIN`, then the `DATA` records packed back to back
  (a record may span block boundaries inside the ring).
* The `COMMIT` record is written **alone in the next block**, so that a torn write can only damage the
  commit record itself (its CRC then fails and the transaction is discarded).
* The ring is circular: positions wrap modulo the journal block count. A transaction is accepted only
  if its txid equals `checkpoint_txid + 1 + (number already accepted)`; stale blocks from earlier
  laps therefore never replay. `journal_head` in the superblock is advisory; recovery does not trust it.
* The legacy `CHECKPOINT` record type is still decodable but no longer written; the superblock is the
  checkpoint.

#### CHECKPOINT Payload (24 bytes)

```
Offset  Size  Field
------  ----  -----
0       8     Checkpoint transaction ID (uint64)
8       8     Journal head at checkpoint (uint64)
16      8     Journal tail at checkpoint (uint64)
```

### Bitmap Region (variable, 1 bit per data block)

- 1 bit per block in data region
- 0 = free, 1 = allocated
- Bitmap blocks cover the data region only
- Stored as little-endian 64-bit words

### Inode Table Region (fixed, default 64 blocks = 1024 inodes)

#### Inode Structure (256 bytes)

```
Offset  Size  Field
------  ----  -----
0       2     Type (uint16): 0=free, 1=file, 2=directory
2       2     Mode/permissions (uint16)
4       4     UID (uint32)
8       4     GID (uint32)
12      8     Size in bytes (uint64)
20      8     Blocks allocated (uint64)
28      8     Created time (uint64, ns since epoch)
36      8     Modified time (uint64)
44      8     Accessed time (uint64)
52      8     Change time (uint64)
60      96    Direct block pointers (12 × uint64)
156     8     Singly indirect block pointer (uint64)
164     8     Doubly indirect block pointer (uint64)
172     8     Triply indirect block pointer (uint64)
                     (the three indirect pointers are reserved and unused: maximum file size is 12 blocks = 48 KiB)
180     4     Link count (uint32)
184     4     Flags (uint32)
188     8     Generation number (uint64)
196     60    Reserved/extended attributes
256     -     End (exactly 256 bytes)
```

Inode 0 is reserved (null inode). Inode 1 is root directory.

### Data Region (remaining blocks)

Contains file data blocks and directory blocks.

#### Directory Entry (64 bytes)

```
Offset  Size  Field
------  ----  -----
0       8     Inode number (uint64)
8       2     Name length (uint16)
10      1     File type (uint8): 1=file, 2=directory
11      1     Reserved
12      52    Name (bytes, null-terminated, max 51 chars)
```

Directory entries are packed sequentially in directory blocks. Empty entries have inode=0. A directory
may own at most 12 blocks (768 entries). `.` and `..` are stored as normal entries.

## Checksum Algorithm

CRC64-ECMA-182 (polynomial 0x42F0E1EBA9EA3693) for all checksums.

## Byte Order

All multi-byte integers are little-endian.

## Alignment

All structures are naturally aligned. Blocks are 4096-byte aligned.