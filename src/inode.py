"""
Inode Module

Manages inode serialization, metadata, and block mappings.
"""

import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import IntEnum
from .virtual_disk import VirtualDisk
from .superblock import Superblock


class InodeType(IntEnum):
    """Inode type enumeration."""
    FREE = 0
    FILE = 1
    DIRECTORY = 2


class InodeError(Exception):
    """Base exception for inode errors."""
    pass


class InodeNotFoundError(InodeError):
    """Raised when inode is not found."""
    pass


class InodeTableFullError(InodeError):
    """Raised when inode table is full."""
    pass


class InvalidInodeError(InodeError):
    """Raised when inode is invalid."""
    pass


# Inode structure constants
INODE_SIZE = 256
INODES_PER_BLOCK = 4096 // INODE_SIZE  # 16

# Inode field offsets
INODE_TYPE = 0
INODE_MODE = 2
INODE_UID = 4
INODE_GID = 8
INODE_SIZE_OFFSET = 12
INODE_BLOCKS = 20
INODE_CTIME = 28
INODE_MTIME = 36
INODE_ATIME = 44
INODE_CHTIME = 52
INODE_DIRECT = 60      # 12 * 8 = 96 bytes
INODE_INDIRECT1 = 156
INODE_INDIRECT2 = 164
INODE_INDIRECT3 = 172
INODE_LINK_COUNT = 180
INODE_FLAGS = 184
INODE_GENERATION = 188
INODE_RESERVED = 196   # 60 bytes
INODE_END = 256

DIRECT_POINTERS = 12
MAX_FILE_SIZE = DIRECT_POINTERS * 4096  # 48 KiB with direct pointers only


@dataclass
class Inode:
    """In-memory inode representation."""
    
    # Identification
    inode_num: int = 0
    
    # Type and permissions
    type: InodeType = InodeType.FREE
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    
    # Size and blocks
    size: int = 0
    blocks_allocated: int = 0
    
    # Timestamps (nanoseconds since epoch)
    ctime: int = 0
    mtime: int = 0
    atime: int = 0
    chtime: int = 0
    
    # Block pointers
    direct: List[int] = field(default_factory=lambda: [0] * DIRECT_POINTERS)
    indirect1: int = 0
    indirect2: int = 0
    indirect3: int = 0
    
    # Link count and metadata
    link_count: int = 0
    flags: int = 0
    generation: int = 0
    
    # Runtime state
    _dirty: bool = False
    
    def __post_init__(self):
        if len(self.direct) != DIRECT_POINTERS:
            self.direct = list(self.direct[:DIRECT_POINTERS]) + [0] * max(0, DIRECT_POINTERS - len(self.direct))
    
    @property
    def is_free(self) -> bool:
        return self.type == InodeType.FREE
    
    @property
    def is_file(self) -> bool:
        return self.type == InodeType.FILE
    
    @property
    def is_directory(self) -> bool:
        return self.type == InodeType.DIRECTORY
    
    def get_all_blocks(self) -> List[int]:
        """Get all data block pointers (direct only for now)."""
        return [b for b in self.direct if b != 0]
    
    def add_block(self, block_num: int) -> bool:
        """Add a block pointer. Returns True if added, False if no space."""
        for i in range(DIRECT_POINTERS):
            if self.direct[i] == 0:
                self.direct[i] = block_num
                self.blocks_allocated += 1
                self._dirty = True
                return True
        return False
    
    def remove_block(self, block_num: int) -> bool:
        """Remove a block pointer. Returns True if found and removed."""
        for i in range(DIRECT_POINTERS):
            if self.direct[i] == block_num:
                self.direct[i] = 0
                self.blocks_allocated -= 1
                self._dirty = True
                return True
        return False
    
    def update_timestamps(self, mtime: bool = True, atime: bool = False, ctime: bool = True) -> None:
        """Update timestamps."""
        now = time.time_ns()
        if mtime:
            self.mtime = now
        if atime:
            self.atime = now
        if ctime:
            self.ctime = now
        self.chtime = now
        self._dirty = True
    
    def to_bytes(self) -> bytes:
        """Serialize inode to 256 bytes."""
        # Pack all fields
        data = struct.pack(
            '<HHIIQQQQQQII',
            self.type.value,      # 2
            self.mode,            # 2
            self.uid,             # 4
            self.gid,             # 4
            self.size,            # 8
            self.blocks_allocated,# 8
            self.ctime,           # 8
            self.mtime,           # 8
            self.atime,           # 8
            self.chtime,          # 8
            self.link_count,      # 4
            self.flags,           # 4
        )
        
        # Direct pointers (12 * 8 = 96 bytes)
        for ptr in self.direct:
            data += struct.pack('<Q', ptr)
        
        # Indirect pointers
        data += struct.pack('<QQQ', self.indirect1, self.indirect2, self.indirect3)
        
        # Generation
        data += struct.pack('<Q', self.generation)
        
        # Reserved (60 bytes)
        data += b'\x00' * 60
        
        assert len(data) == INODE_SIZE, f"Inode size {len(data)} != {INODE_SIZE}"
        return data
    
    @classmethod
    def from_bytes(cls, data: bytes, inode_num: int = 0) -> 'Inode':
        """Deserialize inode from 256 bytes."""
        if len(data) < INODE_SIZE:
            raise ValueError(f"Inode data too short: {len(data)} < {INODE_SIZE}")
        
        # Unpack fixed fields
        fmt = '<HHIIQQQQQQII'
        fixed_size = struct.calcsize(fmt)
        fields = struct.unpack(fmt, data[:fixed_size])
        
        offset = fixed_size
        
        # Direct pointers
        direct = []
        for _ in range(DIRECT_POINTERS):
            ptr = struct.unpack('<Q', data[offset:offset+8])[0]
            direct.append(ptr)
            offset += 8
        
        # Indirect pointers
        indirect1, indirect2, indirect3 = struct.unpack('<QQQ', data[offset:offset+24])
        offset += 24
        
        # Generation
        generation = struct.unpack('<Q', data[offset:offset+8])[0]
        
        return cls(
            inode_num=inode_num,
            type=InodeType(fields[0]),
            mode=fields[1],
            uid=fields[2],
            gid=fields[3],
            size=fields[4],
            blocks_allocated=fields[5],
            ctime=fields[6],
            mtime=fields[7],
            atime=fields[8],
            chtime=fields[9],
            direct=direct,
            indirect1=indirect1,
            indirect2=indirect2,
            indirect3=indirect3,
            link_count=fields[10],
            flags=fields[11],
            generation=generation,
        )
    
    def validate(self) -> List[str]:
        """Validate inode consistency. Returns list of errors."""
        errors = []
        
        if self.inode_num < 0:
            errors.append(f"Invalid inode number: {self.inode_num}")
        
        if self.type not in InodeType:
            errors.append(f"Invalid inode type: {self.type}")
        
        if self.size < 0:
            errors.append(f"Negative size: {self.size}")
        
        if self.blocks_allocated < 0:
            errors.append(f"Negative blocks allocated: {self.blocks_allocated}")
        
        # Count non-zero direct pointers
        actual_blocks = sum(1 for b in self.direct if b != 0)
        if actual_blocks != self.blocks_allocated:
            errors.append(f"Block count mismatch: blocks_allocated={self.blocks_allocated}, actual={actual_blocks}")
        
        if self.link_count < 0:
            errors.append(f"Negative link count: {self.link_count}")
        
        # For directories, link count should be at least 2 (self + parent)
        if self.is_directory and self.link_count < 2:
            errors.append(f"Directory link count too low: {self.link_count}")
        
        return errors


class InodeTable:
    """Manages the inode table on disk."""
    
    def __init__(self, disk: VirtualDisk, superblock: Superblock):
        self.disk = disk
        self.superblock = superblock
        self._inode_cache: dict[int, Inode] = {}
        self._cache_dirty: set[int] = set()
    
    def _get_inode_location(self, inode_num: int) -> Tuple[int, int]:
        """
        Get block number and offset for an inode.
        
        Returns:
            (block_num, offset_within_block)
        """
        if inode_num <= 0 or inode_num > self.superblock.total_inodes:
            raise InvalidInodeError(f"Inode number {inode_num} out of range [1, {self.superblock.total_inodes}]")
        
        # Inode 1 is at index 0 in the table
        index = inode_num - 1
        block_offset = index // INODES_PER_BLOCK
        inode_offset = index % INODES_PER_BLOCK
        
        block_num = self.superblock.inode_table_start_block + block_offset
        offset = inode_offset * INODE_SIZE
        
        return block_num, offset
    
    def read_inode(self, inode_num: int, use_cache: bool = True) -> Inode:
        """Read an inode from disk."""
        if use_cache and inode_num in self._inode_cache:
            return self._inode_cache[inode_num]
        
        block_num, offset = self._get_inode_location(inode_num)
        block_data = self.disk.read_block(block_num)
        inode_data = block_data[offset:offset + INODE_SIZE]
        inode = Inode.from_bytes(inode_data, inode_num)
        
        if use_cache:
            self._inode_cache[inode_num] = inode
        
        return inode
    
    def write_inode(self, inode: Inode) -> None:
        """Write an inode to disk."""
        if inode.inode_num <= 0 or inode.inode_num >= self.superblock.total_inodes:
            raise InvalidInodeError(f"Inode number {inode.inode_num} out of range")
        
        block_num, offset = self._get_inode_location(inode.inode_num)
        
        # Read the block, modify inode, write back
        block_data = bytearray(self.disk.read_block(block_num))
        inode_bytes = inode.to_bytes()
        block_data[offset:offset + INODE_SIZE] = inode_bytes
        self.disk.write_block(block_num, bytes(block_data))
        
        # Update cache
        self._inode_cache[inode.inode_num] = inode
        inode._dirty = False
    
    def allocate_inode(self, inode_type: InodeType, mode: int = 0o644) -> Inode:
        """Allocate a new inode."""
        if self.superblock.free_inode_count <= 0:
            raise InodeTableFullError("No free inodes available")
        
        # Search from next_free_inode
        start = self.superblock.next_free_inode
        inode_num = start
        while True:
            inode = self.read_inode(inode_num, use_cache=False)
            if inode.is_free:
                # Found free inode
                now = time.time_ns()
                inode.type = inode_type
                inode.mode = mode
                inode.uid = 0
                inode.gid = 0
                inode.size = 0
                inode.blocks_allocated = 0
                inode.ctime = now
                inode.mtime = now
                inode.atime = now
                inode.chtime = now
                inode.direct = [0] * DIRECT_POINTERS
                inode.indirect1 = 0
                inode.indirect2 = 0
                inode.indirect3 = 0
                inode.link_count = 1 if inode_type == InodeType.FILE else 2
                inode.flags = 0
                inode.generation += 1
                inode._dirty = True
                
                self.write_inode(inode)
                
                # Update superblock
                self.superblock.free_inode_count -= 1
                self.superblock.next_free_inode = inode_num + 1
                if self.superblock.next_free_inode > self.superblock.total_inodes:
                    self.superblock.next_free_inode = 1
                self.superblock._dirty = True
                
                return inode
            
            # Move to next inode
            inode_num += 1
            if inode_num > self.superblock.total_inodes:
                inode_num = 1
            
            # Check if we've wrapped around completely
            if inode_num == start:
                break
        
        raise InodeTableFullError("No free inodes found (should not happen)")
    
    def free_inode(self, inode_num: int) -> None:
        """Free an inode."""
        inode = self.read_inode(inode_num)
        
        if inode.is_free:
            raise InvalidInodeError(f"Inode {inode_num} already free")
        
        if inode.link_count > 0:
            raise InvalidInodeError(f"Inode {inode_num} still has {inode.link_count} links")
        
        # Clear inode
        inode.type = InodeType.FREE
        inode.mode = 0
        inode.uid = 0
        inode.gid = 0
        inode.size = 0
        inode.blocks_allocated = 0
        inode.ctime = 0
        inode.mtime = 0
        inode.atime = 0
        inode.chtime = 0
        inode.direct = [0] * DIRECT_POINTERS
        inode.indirect1 = 0
        inode.indirect2 = 0
        inode.indirect3 = 0
        inode.link_count = 0
        inode.flags = 0
        inode._dirty = True
        
        self.write_inode(inode)
        
        # Update superblock
        self.superblock.free_inode_count += 1
        self.superblock._dirty = True
        
        # Remove from cache
        self._inode_cache.pop(inode_num, None)
    
    def flush_dirty(self) -> None:
        """Flush all dirty cached inodes."""
        for inode_num in list(self._cache_dirty):
            inode = self._inode_cache.get(inode_num)
            if inode and inode._dirty:
                self.write_inode(inode)
        self._cache_dirty.clear()
    
    def get_inode(self, inode_num: int) -> Inode:
        """Get inode from cache or disk."""
        return self.read_inode(inode_num)
    
    def iter_inodes(self) -> List[Inode]:
        """Iterate over all allocated inodes."""
        result = []
        for inode_num in range(1, self.superblock.total_inodes + 1):
            inode = self.read_inode(inode_num, use_cache=False)
            if not inode.is_free:
                result.append(inode)
        return result
    
    def get_stats(self) -> dict:
        """Get inode table statistics."""
        allocated = 0
        files = 0
        dirs = 0
        for inode_num in range(1, self.superblock.total_inodes + 1):
            inode = self.read_inode(inode_num, use_cache=False)
            if not inode.is_free:
                allocated += 1
                if inode.is_file:
                    files += 1
                elif inode.is_directory:
                    dirs += 1
        return {
            'total_inodes': self.superblock.total_inodes,
            'free_inodes': self.superblock.free_inode_count,
            'allocated': allocated,
            'files': files,
            'directories': dirs,
        }


def create_root_inode(inode_table: InodeTable) -> Inode:
    """Create and initialize the root directory inode (inode 1)."""
    # Root inode should already exist at inode 1
    inode = inode_table.read_inode(1)
    
    if not inode.is_free:
        return inode  # Already initialized
    
    now = time.time_ns()
    inode.type = InodeType.DIRECTORY
    inode.mode = 0o755
    inode.uid = 0
    inode.gid = 0
    inode.size = 0
    inode.blocks_allocated = 0
    inode.ctime = now
    inode.mtime = now
    inode.atime = now
    inode.chtime = now
    inode.direct = [0] * DIRECT_POINTERS
    inode.indirect1 = 0
    inode.indirect2 = 0
    inode.indirect3 = 0
    inode.link_count = 2  # . and ..
    inode.flags = 0
    inode.generation = 1
    inode._dirty = True
    
    inode_table.write_inode(inode)
    return inode