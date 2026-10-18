"""
Directory Module

Manages directory entries and name-to-inode mappings.
"""

import struct
import time
from dataclasses import dataclass
from typing import List, Optional, Iterator, Tuple
from .virtual_disk import VirtualDisk
from .inode import Inode, InodeType, InodeTable, DIRECT_POINTERS
from .block_allocator import BlockAllocator


# Directory entry constants
DIR_ENTRY_SIZE = 64
ENTRIES_PER_BLOCK = 4096 // DIR_ENTRY_SIZE  # 64
MAX_NAME_LEN = 51  # 52 bytes for name, null-terminated

# Directory entry field offsets
DIR_INODE_NUM = 0
DIR_NAME_LEN = 8
DIR_FILE_TYPE = 10
DIR_RESERVED = 11
DIR_NAME = 12

# File type values for directory entries
DIR_TYPE_FILE = 1
DIR_TYPE_DIR = 2


class DirectoryError(Exception):
    """Base exception for directory errors."""
    pass


class EntryNotFoundError(DirectoryError):
    """Raised when directory entry not found."""
    pass


class EntryExistsError(DirectoryError):
    """Raised when directory entry already exists."""
    pass


class DirectoryFullError(DirectoryError):
    """Raised when directory has no space for new entries."""
    pass


class InvalidNameError(DirectoryError):
    """Raised when name is invalid."""
    pass


@dataclass
class DirEntry:
    """Directory entry representation."""
    inode_num: int
    name: str
    file_type: int  # DIR_TYPE_FILE or DIR_TYPE_DIR
    
    def __post_init__(self):
        if len(self.name) > MAX_NAME_LEN:
            raise InvalidNameError(f"Name too long: {len(self.name)} > {MAX_NAME_LEN}")
        # Allow inode_num=0 for empty entries
        if self.inode_num < 0:
            raise ValueError(f"Invalid inode number: {self.inode_num}")
    
    def to_bytes(self) -> bytes:
        """Serialize directory entry to 64 bytes."""
        name_bytes = self.name.encode('utf-8')
        if len(name_bytes) > MAX_NAME_LEN:
            name_bytes = name_bytes[:MAX_NAME_LEN]
        
        data = struct.pack(
            '<QHB',
            self.inode_num,      # 8
            len(name_bytes),     # 2
            self.file_type       # 1
        )
        data += b'\x00'  # reserved
        data += name_bytes
        data += b'\x00' * (MAX_NAME_LEN + 1 - len(name_bytes))  # null-terminated + padding
        
        assert len(data) == DIR_ENTRY_SIZE, f"DirEntry size {len(data)} != {DIR_ENTRY_SIZE}"
        return data
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'DirEntry':
        """Deserialize directory entry from 64 bytes."""
        if len(data) < DIR_ENTRY_SIZE:
            raise ValueError(f"DirEntry data too short: {len(data)} < {DIR_ENTRY_SIZE}")
        
        inode_num, name_len, file_type = struct.unpack('<QHB', data[:11])
        
        if inode_num == 0:
            return cls(0, "", 0)  # Empty entry
        
        # Extract name (null-terminated)
        name_bytes = data[12:12 + name_len]
        name = name_bytes.decode('utf-8', errors='replace')
        
        return cls(inode_num, name, file_type)
    
    @property
    def is_empty(self) -> bool:
        return self.inode_num == 0


class Directory:
    """Manages a single directory's entries."""
    
    def __init__(self, inode: Inode, inode_table: InodeTable, 
                 block_allocator: BlockAllocator, disk: VirtualDisk):
        self.inode = inode
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self.disk = disk
        self._entries_cache: Optional[List[DirEntry]] = None
        self._cache_dirty = False
    
    def _load_entries(self) -> List[DirEntry]:
        """Load all directory entries from disk."""
        entries = []
        
        for block_ptr in self.inode.direct:
            if block_ptr == 0:
                break
            
            block_data = self.disk.read_block(block_ptr)
            
            for i in range(ENTRIES_PER_BLOCK):
                offset = i * DIR_ENTRY_SIZE
                entry_data = block_data[offset:offset + DIR_ENTRY_SIZE]
                entry = DirEntry.from_bytes(entry_data)
                
                if entry.is_empty:
                    continue
                
                # Skip . and .. entries
                if entry.name in (".", ".."):
                    continue
                
                entries.append(entry)
        
        return entries
    
    def _get_entries(self) -> List[DirEntry]:
        """Get entries, using cache if valid."""
        if self._entries_cache is None or self._cache_dirty:
            self._entries_cache = self._load_entries()
            self._cache_dirty = False
        return self._entries_cache
    
    def _invalidate_cache(self) -> None:
        """Invalidate the entries cache."""
        self._entries_cache = None
        self._cache_dirty = True
    
    def _write_entries(self, entries: List[DirEntry]) -> None:
        """Write entries to directory blocks."""
        # Calculate required blocks
        total_entries = len(entries) + 2  # +2 for . and ..
        required_blocks = (total_entries + ENTRIES_PER_BLOCK - 1) // ENTRIES_PER_BLOCK
        
        # Ensure we have enough blocks
        current_blocks = sum(1 for b in self.inode.direct if b != 0)
        
        if required_blocks > current_blocks:
            # Allocate new blocks
            for _ in range(required_blocks - current_blocks):
                new_blocks = self.block_allocator.allocate(1)
                if not new_blocks:
                    raise DirectoryFullError("No free blocks for directory expansion")
                
                # Find first empty direct pointer
                for i in range(DIRECT_POINTERS):
                    if self.inode.direct[i] == 0:
                        self.inode.direct[i] = new_blocks[0]
                        self.inode.blocks_allocated += 1
                        self.inode._dirty = True
                        break
        
        # Write entries to blocks
        all_entries = [
            DirEntry(self.inode.inode_num, ".", DIR_TYPE_DIR),
            DirEntry(self.inode_table.read_inode(self.inode.direct[0]).inode_num if self.inode.direct[0] else self.inode.inode_num, "..", DIR_TYPE_DIR)
        ] + entries
        
        entry_idx = 0
        for block_idx in range(DIRECT_POINTERS):
            block_ptr = self.inode.direct[block_idx]
            if block_ptr == 0:
                if entry_idx < len(all_entries):
                    # Need to allocate (should have been done above)
                    new_blocks = self.block_allocator.allocate(1)
                    if not new_blocks:
                        raise DirectoryFullError("No free blocks")
                    block_ptr = new_blocks[0]
                    self.inode.direct[block_idx] = block_ptr
                    self.inode.blocks_allocated += 1
                    self.inode._dirty = True
                else:
                    break
            
            block_data = bytearray(4096)
            for i in range(ENTRIES_PER_BLOCK):
                if entry_idx < len(all_entries):
                    entry_bytes = all_entries[entry_idx].to_bytes()
                    block_data[i * DIR_ENTRY_SIZE:(i + 1) * DIR_ENTRY_SIZE] = entry_bytes
                    entry_idx += 1
                else:
                    # Empty entry
                    block_data[i * DIR_ENTRY_SIZE:(i + 1) * DIR_ENTRY_SIZE] = b'\x00' * DIR_ENTRY_SIZE
            
            self.disk.write_block(block_ptr, bytes(block_data))
        
        # Update inode size
        self.inode.size = len(entries) * DIR_ENTRY_SIZE
        self.inode.update_timestamps(mtime=True, ctime=True)
        self.inode_table.write_inode(self.inode)
        
        self._entries_cache = entries
        self._cache_dirty = False
    
    def lookup(self, name: str) -> Optional[int]:
        """Look up a name, return inode number or None."""
        if name == ".":
            return self.inode.inode_num
        if name == "..":
            # Return parent inode (first block's inode or self for root)
            if self.inode.direct[0] != 0:
                # We'd need to read the parent directory to get its inode
                # For now, return self for root
                pass
            return self.inode.inode_num  # Simplified
        
        entries = self._get_entries()
        for entry in entries:
            if entry.name == name:
                return entry.inode_num
        return None
    
    def add_entry(self, name: str, inode_num: int, file_type: int) -> None:
        """Add a new entry."""
        if len(name) > MAX_NAME_LEN:
            raise InvalidNameError(f"Name too long: {name}")
        if name in (".", ".."):
            raise InvalidNameError(f"Reserved name: {name}")
        
        entries = self._get_entries()
        
        # Check for duplicate
        for entry in entries:
            if entry.name == name:
                raise EntryExistsError(f"Entry '{name}' already exists")
        
        # Add new entry
        new_entry = DirEntry(inode_num, name, file_type)
        entries.append(new_entry)
        self._write_entries(entries)
    
    def remove_entry(self, name: str) -> int:
        """Remove an entry, return its inode number."""
        if name in (".", ".."):
            raise InvalidNameError(f"Cannot remove reserved name: {name}")
        
        entries = self._get_entries()
        
        for i, entry in enumerate(entries):
            if entry.name == name:
                inode_num = entry.inode_num
                del entries[i]
                self._write_entries(entries)
                return inode_num
        
        raise EntryNotFoundError(f"Entry '{name}' not found")
    
    def rename_entry(self, old_name: str, new_name: str) -> None:
        """Rename an entry."""
        if old_name in (".", "..") or new_name in (".", ".."):
            raise InvalidNameError("Cannot rename reserved names")
        if len(new_name) > MAX_NAME_LEN:
            raise InvalidNameError(f"New name too long: {new_name}")
        
        entries = self._get_entries()
        
        # Check new name doesn't exist
        for entry in entries:
            if entry.name == new_name:
                raise EntryExistsError(f"Entry '{new_name}' already exists")
        
        # Find and rename
        for entry in entries:
            if entry.name == old_name:
                entry.name = new_name
                self._write_entries(entries)
                return
        
        raise EntryNotFoundError(f"Entry '{old_name}' not found")
    
    def list_entries(self) -> List[DirEntry]:
        """List all entries (excluding . and ..)."""
        return self._get_entries()
    
    def get_entry(self, name: str) -> Optional[DirEntry]:
        """Get a specific entry by name."""
        if name == ".":
            return DirEntry(self.inode.inode_num, ".", DIR_TYPE_DIR)
        if name == "..":
            return DirEntry(self.inode.inode_num, "..", DIR_TYPE_DIR)
        
        entries = self._get_entries()
        for entry in entries:
            if entry.name == name:
                return entry
        return None
    
    def entry_count(self) -> int:
        """Get number of entries (excluding . and ..)."""
        return len(self._get_entries())
    
    def is_empty(self) -> bool:
        """Check if directory is empty (only . and ..)."""
        return len(self._get_entries()) == 0
    
    def ensure_block_allocated(self) -> int:
        """Ensure directory has at least one data block, return it."""
        for block_ptr in self.inode.direct:
            if block_ptr != 0:
                return block_ptr
        
        # Allocate first block
        new_blocks = self.block_allocator.allocate(1)
        if not new_blocks:
            raise DirectoryFullError("No free blocks for directory")
        
        # Convert to absolute block number
        abs_block = self.block_allocator._get_absolute_block(new_blocks[0])
        self.inode.direct[0] = abs_block
        self.inode.blocks_allocated += 1
        self.inode._dirty = True
        self.inode_table.write_inode(self.inode)
        
        # Initialize block with . and .. entries
        block_data = bytearray(4096)
        dot_entry = DirEntry(self.inode.inode_num, ".", DIR_TYPE_DIR).to_bytes()
        dotdot_entry = DirEntry(self.inode.inode_num, "..", DIR_TYPE_DIR).to_bytes()
        block_data[0:DIR_ENTRY_SIZE] = dot_entry
        block_data[DIR_ENTRY_SIZE:2*DIR_ENTRY_SIZE] = dotdot_entry
        self.disk.write_block(abs_block, bytes(block_data))
        
        return abs_block


def initialize_root_directory(inode_table: InodeTable, block_allocator: BlockAllocator, disk: VirtualDisk) -> Directory:
    """Initialize the root directory with . and .. entries."""
    root_inode = inode_table.read_inode(1)
    
    # Initialize root inode if it's free
    if root_inode.is_free:
        now = time.time_ns()
        root_inode.type = InodeType.DIRECTORY
        root_inode.mode = 0o755
        root_inode.uid = 0
        root_inode.gid = 0
        root_inode.size = 0
        root_inode.blocks_allocated = 0
        root_inode.ctime = now
        root_inode.mtime = now
        root_inode.atime = now
        root_inode.chtime = now
        root_inode.direct = [0] * DIRECT_POINTERS
        root_inode.indirect1 = 0
        root_inode.indirect2 = 0
        root_inode.indirect3 = 0
        root_inode.link_count = 2
        root_inode.flags = 0
        root_inode.generation = 1
        root_inode._dirty = True
        
        inode_table.write_inode(root_inode)
        
        # Update superblock
        inode_table.superblock.free_inode_count -= 1
        inode_table.superblock.next_free_inode = 2
        inode_table.superblock._dirty = True
    
    # Ensure root has a block
    if root_inode.direct[0] == 0:
        new_blocks = block_allocator.allocate(1)
        if not new_blocks:
            raise DirectoryFullError("No free blocks for root directory")
        
        # Convert to absolute block number
        abs_block = block_allocator._get_absolute_block(new_blocks[0])
        root_inode.direct[0] = abs_block
        root_inode.blocks_allocated = 1
        root_inode._dirty = True
        inode_table.write_inode(root_inode)
        
        # Write . and .. entries
        block_data = bytearray(4096)
        dot_entry = DirEntry(1, ".", DIR_TYPE_DIR).to_bytes()
        dotdot_entry = DirEntry(1, "..", DIR_TYPE_DIR).to_bytes()
        block_data[0:DIR_ENTRY_SIZE] = dot_entry
        block_data[DIR_ENTRY_SIZE:2*DIR_ENTRY_SIZE] = dotdot_entry
        disk.write_block(abs_block, bytes(block_data))
    
    return Directory(root_inode, inode_table, block_allocator, disk)