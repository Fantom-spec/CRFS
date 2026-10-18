"""
Superblock Module

Defines the on-disk superblock structure and validation logic.
"""

import struct
import time
from dataclasses import dataclass, field
from typing import Optional
from .virtual_disk import VirtualDisk

# CRC64-ECMA-182 polynomial
CRC64_POLY = 0x42F0E1EBA9EA3693

# Superblock constants
MAGIC_NUMBER = 0x43524653  # "CRFS"
FORMAT_VERSION = 1
SUPERBLOCK_BLOCK = 0
SUPERBLOCK_SIZE = 4096

# Superblock field offsets (matching disk-layout.md)
SB_MAGIC = 0
SB_VERSION = 4
SB_FLAGS = 6
SB_BLOCK_SIZE = 8
SB_TOTAL_BLOCKS = 12
SB_JOURNAL_START = 20
SB_JOURNAL_COUNT = 28
SB_BITMAP_START = 32
SB_BITMAP_COUNT = 40
SB_INODE_TABLE_START = 44
SB_INODE_TABLE_COUNT = 52
SB_DATA_START = 56
SB_DATA_COUNT = 64
SB_ROOT_INODE = 72
SB_NEXT_FREE_INODE = 80
SB_JOURNAL_HEAD = 88
SB_JOURNAL_TAIL = 96
SB_CHECKPOINT_TXID = 104
SB_TOTAL_INODES = 112
SB_FREE_INODES = 120
SB_FREE_BLOCKS = 128
SB_VOLUME_LABEL = 136
SB_RESERVED = 152
SB_PADDING = 168
SB_CHECKSUM = 208
SB_END = 216


@dataclass
class Superblock:
    """In-memory superblock representation."""
    
    # Fixed fields
    magic: int = MAGIC_NUMBER
    version: int = FORMAT_VERSION
    flags: int = 0
    block_size: int = 4096
    total_blocks: int = 0
    
    # Region layout
    journal_start_block: int = 0
    journal_block_count: int = 0
    bitmap_start_block: int = 0
    bitmap_block_count: int = 0
    inode_table_start_block: int = 0
    inode_table_block_count: int = 0
    data_start_block: int = 0
    data_block_count: int = 0
    
    # Filesystem state
    root_inode: int = 1
    next_free_inode: int = 2
    journal_head: int = 0  # Relative to journal start
    journal_tail: int = 0  # Relative to journal start
    checkpoint_txid: int = 0
    total_inodes: int = 0
    free_inode_count: int = 0
    free_block_count: int = 0
    
    # Metadata
    volume_label: bytes = b''
    reserved: bytes = field(default_factory=lambda: b'\x00' * 16)
    
    # Runtime (not persisted)
    _checksum: int = 0
    _dirty: bool = False
    
    def compute_checksum(self) -> int:
        """Compute CRC64 checksum of superblock (excluding checksum field)."""
        # Get the 208-byte data without checksum and without final padding
        label = self.volume_label[:16].ljust(16, b'\x00')
        data = struct.pack(
            '<IHHIQQIQIQIQQQQQQQQQQ16s16s40s',
            self.magic,
            self.version,
            self.flags,
            self.block_size,
            self.total_blocks,
            self.journal_start_block,
            self.journal_block_count,
            self.bitmap_start_block,
            self.bitmap_block_count,
            self.inode_table_start_block,
            self.inode_table_block_count,
            self.data_start_block,
            self.data_block_count,
            self.root_inode,
            self.next_free_inode,
            self.journal_head,
            self.journal_tail,
            self.checkpoint_txid,
            self.total_inodes,
            self.free_inode_count,
            self.free_block_count,
            label,
            self.reserved,
            b'\x00' * 40
        )
        return crc64(data)
    
    def verify_checksum(self) -> bool:
        """Verify superblock checksum."""
        return self._checksum == self.compute_checksum()
    
    def to_bytes(self, include_checksum: bool = True) -> bytes:
        """Serialize superblock to bytes."""
        # Volume label: 16 bytes, null-padded
        label = self.volume_label[:16].ljust(16, b'\x00')
        
        # Pack all fields except checksum (208 bytes)
        data = struct.pack(
            '<IHHIQQIQIQIQQQQQQQQQQ16s16s40s',
            self.magic,           # 4 (I)
            self.version,         # 2 (H)
            self.flags,           # 2 (H)
            self.block_size,      # 4 (I)
            self.total_blocks,    # 8 (Q)
            self.journal_start_block,  # 8 (Q)
            self.journal_block_count,  # 4 (I)
            self.bitmap_start_block,   # 8 (Q)
            self.bitmap_block_count,   # 4 (I)
            self.inode_table_start_block,  # 8 (Q)
            self.inode_table_block_count,  # 4 (I)
            self.data_start_block,   # 8 (Q)
            self.data_block_count,   # 8 (Q)
            self.root_inode,       # 8 (Q)
            self.next_free_inode,  # 8 (Q)
            self.journal_head,     # 8 (Q)
            self.journal_tail,     # 8 (Q)
            self.checkpoint_txid,  # 8 (Q)
            self.total_inodes,     # 8 (Q)
            self.free_inode_count, # 8 (Q)
            self.free_block_count, # 8 (Q)
            label,                 # 16 (16s)
            self.reserved,         # 16 (16s)
            b'\x00' * 40           # 40 (40s) padding to 208
        )
        
        if include_checksum:
            checksum = crc64(data)
            data += struct.pack('<Q', checksum)
            data += b'\x00' * (SUPERBLOCK_SIZE - len(data))
        else:
            # Return just the 208-byte data (for checksum computation)
            return data
        
        return data
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'Superblock':
        """Deserialize superblock from bytes."""
        if len(data) < SUPERBLOCK_SIZE:
            raise ValueError(f"Superblock data too short: {len(data)} < {SUPERBLOCK_SIZE}")
        
        # Unpack fields (excluding checksum)
        fmt = '<IHHIQQIQIQIQQQQQQQQQQ16s16s40s'
        size = struct.calcsize(fmt)
        if len(data) < size + 8:
            raise ValueError("Superblock data too short for all fields")
        
        fields = struct.unpack(fmt, data[:size])
        stored_checksum = struct.unpack('<Q', data[size:size+8])[0]
        
        sb = cls(
            magic=fields[0],
            version=fields[1],
            flags=fields[2],
            block_size=fields[3],
            total_blocks=fields[4],
            journal_start_block=fields[5],
            journal_block_count=fields[6],
            bitmap_start_block=fields[7],
            bitmap_block_count=fields[8],
            inode_table_start_block=fields[9],
            inode_table_block_count=fields[10],
            data_start_block=fields[11],
            data_block_count=fields[12],
            root_inode=fields[13],
            next_free_inode=fields[14],
            journal_head=fields[15],
            journal_tail=fields[16],
            checkpoint_txid=fields[17],
            total_inodes=fields[18],
            free_inode_count=fields[19],
            free_block_count=fields[20],
            volume_label=fields[21].rstrip(b'\x00'),
            reserved=fields[22],
        )
        sb._checksum = stored_checksum
        return sb
    
    def validate(self) -> None:
        """Validate superblock fields for consistency."""
        if self.magic != MAGIC_NUMBER:
            raise ValueError(f"Invalid magic number: 0x{self.magic:08x} != 0x{MAGIC_NUMBER:08x}")
        
        if self.version != FORMAT_VERSION:
            raise ValueError(f"Unsupported format version: {self.version} != {FORMAT_VERSION}")
        
        if self.block_size != 4096:
            raise ValueError(f"Unsupported block size: {self.block_size}")
        
        if self.total_blocks <= 0:
            raise ValueError("Total blocks must be positive")
        
        # Validate region bounds
        expected_end = (
            self.journal_start_block + self.journal_block_count +
            self.bitmap_block_count + self.inode_table_block_count +
            self.data_block_count
        )
        
        # Journal starts at block 1
        if self.journal_start_block != 1:
            raise ValueError(f"Journal must start at block 1, got {self.journal_start_block}")
        
        # Regions must not overlap and must fit
        bitmap_start = self.journal_start_block + self.journal_block_count
        if self.bitmap_start_block != bitmap_start:
            raise ValueError(f"Bitmap start mismatch: {self.bitmap_start_block} != {bitmap_start}")
        
        inode_start = bitmap_start + self.bitmap_block_count
        if self.inode_table_start_block != inode_start:
            raise ValueError(f"Inode table start mismatch: {self.inode_table_start_block} != {inode_start}")
        
        data_start = inode_start + self.inode_table_block_count
        if self.data_start_block != data_start:
            raise ValueError(f"Data start mismatch: {self.data_start_block} != {data_start}")
        
        if data_start + self.data_block_count != self.total_blocks:
            raise ValueError("Region layout doesn't match total blocks")
        
        # Validate inode count
        expected_inodes = self.inode_table_block_count * 16  # 16 inodes per block
        if self.total_inodes != expected_inodes:
            raise ValueError(f"Inode count mismatch: {self.total_inodes} != {expected_inodes}")
        
        # Validate root inode
        if self.root_inode != 1:
            raise ValueError(f"Root inode must be 1, got {self.root_inode}")
        
        # Validate journal pointers
        if self.journal_head >= self.journal_block_count:
            raise ValueError(f"Journal head out of bounds: {self.journal_head} >= {self.journal_block_count}")
        if self.journal_tail >= self.journal_block_count:
            raise ValueError(f"Journal tail out of bounds: {self.journal_tail} >= {self.journal_block_count}")
        
        if not self.verify_checksum():
            raise ValueError("Superblock checksum mismatch")


def crc64(data: bytes) -> int:
    """Compute CRC64-ECMA-182 checksum."""
    crc = 0
    for byte in data:
        crc ^= byte << 56
        for _ in range(8):
            if crc & (1 << 63):
                crc = (crc << 1) ^ CRC64_POLY
            else:
                crc = (crc << 1)
            crc &= (1 << 64) - 1
    return crc


class SuperblockManager:
    """Manages superblock read/write operations."""
    
    def __init__(self, disk: VirtualDisk):
        self.disk = disk
        self.superblock: Optional[Superblock] = None
    
    def read(self) -> Superblock:
        """Read and validate superblock from disk."""
        data = self.disk.read_block(SUPERBLOCK_BLOCK)
        sb = Superblock.from_bytes(data)
        sb.validate()
        self.superblock = sb
        return sb
    
    def write(self, sb: Optional[Superblock] = None) -> None:
        """Write superblock to disk."""
        if sb is None:
            sb = self.superblock
        if sb is None:
            raise ValueError("No superblock to write")
        
        sb._checksum = sb.compute_checksum()
        data = sb.to_bytes(include_checksum=True)
        self.disk.write_block(SUPERBLOCK_BLOCK, data)
        sb._dirty = False
    
    def create_default(self, disk_size: int = 16 * 1024 * 1024, 
                       journal_blocks: int = 128,
                       inode_blocks: int = 64,
                       volume_label: str = "") -> Superblock:
        """Create a default superblock for a new filesystem."""
        block_size = 4096
        total_blocks = disk_size // block_size
        
        # Layout: superblock(1) + journal + bitmap + inode_table + data
        journal_start = 1
        bitmap_start = journal_start + journal_blocks
        
        # Bitmap: 1 bit per data block
        # We need to compute data blocks to know bitmap size
        # data_blocks = total_blocks - (1 + journal_blocks + bitmap_blocks + inode_blocks)
        # bitmap_blocks = ceil(data_blocks / (block_size * 8))
        # Solve: bitmap_blocks = ceil((total_blocks - 1 - journal_blocks - bitmap_blocks - inode_blocks) / 32768)
        
        # Approximate: each bitmap block covers 32768 data blocks
        remaining = total_blocks - 1 - journal_blocks - inode_blocks
        bitmap_blocks = max(1, (remaining + 32767) // 32768)
        
        # Recompute data blocks
        data_start = bitmap_start + bitmap_blocks + inode_blocks
        data_blocks = total_blocks - data_start
        
        # Verify bitmap is sufficient
        max_data_for_bitmap = bitmap_blocks * block_size * 8
        if data_blocks > max_data_for_bitmap:
            # Need more bitmap blocks
            bitmap_blocks = (data_blocks + block_size * 8 - 1) // (block_size * 8)
            data_start = bitmap_start + bitmap_blocks + inode_blocks
            data_blocks = total_blocks - data_start
        
        total_inodes = inode_blocks * 16
        
        sb = Superblock(
            total_blocks=total_blocks,
            journal_start_block=journal_start,
            journal_block_count=journal_blocks,
            bitmap_start_block=bitmap_start,
            bitmap_block_count=bitmap_blocks,
            inode_table_start_block=bitmap_start + bitmap_blocks,
            inode_table_block_count=inode_blocks,
            data_start_block=data_start,
            data_block_count=data_blocks,
            total_inodes=total_inodes,
            free_inode_count=total_inodes,  # All inodes free initially
            free_block_count=data_blocks,
            volume_label=volume_label.encode('utf-8')[:16],
        )
        
        sb._checksum = sb.compute_checksum()
        self.superblock = sb
        return sb
    
    def mark_dirty(self) -> None:
        """Mark superblock as needing write."""
        if self.superblock:
            self.superblock._dirty = True
    
    def sync_if_dirty(self) -> None:
        """Write superblock if dirty."""
        if self.superblock and self.superblock._dirty:
            self.write()