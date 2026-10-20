"""
Directory Module

Manages directory entries and name-to-inode mappings.
"""

import struct
import time
from dataclasses import dataclass
from typing import List, Optional, Iterator, Tuple
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


class CorruptDirectoryError(DirectoryError):
    """Raised when a directory block cannot be decoded."""
    pass


def validate_name(name: str) -> None:
    if not name or name in (".", ".."):
        raise InvalidNameError(f"Invalid or reserved name: {name!r}")
    if "/" in name or "\x00" in name:
        raise InvalidNameError(f"Name may not contain '/' or NUL: {name!r}")
    if len(name.encode("utf-8")) > MAX_NAME_LEN:
        raise InvalidNameError(f"Name too long (max {MAX_NAME_LEN} bytes): {name!r}")


@dataclass
class DirEntry:
    """Directory entry representation."""
    inode_num: int
    name: str
    file_type: int  # DIR_TYPE_FILE or DIR_TYPE_DIR
    
    def __post_init__(self):
        if len(self.name.encode("utf-8")) > MAX_NAME_LEN:
            raise InvalidNameError(f"Name too long: {len(self.name.encode('utf-8'))} > {MAX_NAME_LEN} bytes")
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
        if name_len > MAX_NAME_LEN:
            raise InvalidNameError(f"corrupt directory entry: name length {name_len}")
        name_bytes = data[12:12 + name_len]
        name = name_bytes.decode('utf-8', errors='replace')
        
        return cls(inode_num, name, file_type)
    
    @property
    def is_empty(self) -> bool:
        return self.inode_num == 0


class Directory:
    """
    A single directory.  Entries are read from / written to the directory's
    data blocks through ``self.disk`` (the transaction-staging proxy in a
    mounted filesystem); there is no entry cache.
    """

    def __init__(self, inode: Inode, inode_table: InodeTable,
                 block_allocator: BlockAllocator, disk, tx_manager=None):
        self.inode = inode
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self.disk = disk
        self.tx_manager = tx_manager  # unused (kept for compatibility)
        self._parent_inode_num: int = 0

    # ---------------------------------------------------------------- reads
    def _read_raw_entries(self) -> List[DirEntry]:
        """All non-empty entries including '.' and '..'."""
        entries = []
        for block_ptr in self.inode.direct:
            if block_ptr == 0:
                continue
            block = self.disk.read_block(block_ptr)
            for i in range(ENTRIES_PER_BLOCK):
                chunk = block[i * DIR_ENTRY_SIZE:(i + 1) * DIR_ENTRY_SIZE]
                try:
                    entry = DirEntry.from_bytes(chunk)
                except (DirectoryError, ValueError) as e:
                    raise CorruptDirectoryError(
                        f"directory inode {self.inode.inode_num}, block {block_ptr}, slot {i}: {e}") from e
                if not entry.is_empty:
                    entries.append(entry)
        return entries

    def _load_entries(self) -> List[DirEntry]:
        return [e for e in self._read_raw_entries() if e.name not in (".", "..")]

    def _get_entries(self) -> List[DirEntry]:
        return self._load_entries()

    def _invalidate_cache(self) -> None:
        pass

    def _read_parent_inode(self) -> int:
        if self._parent_inode_num:
            return self._parent_inode_num
        if self.inode.inode_num == 1:
            return 1
        for e in self._read_raw_entries():
            if e.name == "..":
                self._parent_inode_num = e.inode_num
                return e.inode_num
        return self.inode.inode_num

    # --------------------------------------------------------------- writes
    def _write_entries(self, entries: List[DirEntry], parent_inode_num: Optional[int] = None) -> None:
        """Rewrite the whole directory (including '.' and '..') and its inode."""
        total = len(entries) + 2
        required = (total + ENTRIES_PER_BLOCK - 1) // ENTRIES_PER_BLOCK
        current = sum(1 for b in self.inode.direct if b != 0)
        if required > DIRECT_POINTERS:
            raise DirectoryFullError(
                f"directory would need {required} blocks (max {DIRECT_POINTERS})")
        parent = parent_inode_num if parent_inode_num else self._read_parent_inode()
        if required > current:
            try:
                new_blocks = self.block_allocator.allocate_any(required - current)
            except Exception as e:
                raise DirectoryFullError(f"no free blocks for directory expansion: {e}") from e
            it = iter(new_blocks)
            for i in range(DIRECT_POINTERS):
                if self.inode.direct[i] == 0:
                    try:
                        blk = next(it)
                    except StopIteration:
                        break
                    self.inode.direct[i] = self.block_allocator._get_absolute_block(blk)
                    self.inode.blocks_allocated += 1

        all_entries = [DirEntry(self.inode.inode_num, ".", DIR_TYPE_DIR),
                       DirEntry(parent, "..", DIR_TYPE_DIR)] + list(entries)
        idx = 0
        for ptr in self.inode.direct:
            if ptr == 0:
                continue
            block = bytearray(4096)
            for slot in range(ENTRIES_PER_BLOCK):
                if idx < len(all_entries):
                    block[slot * DIR_ENTRY_SIZE:(slot + 1) * DIR_ENTRY_SIZE] = all_entries[idx].to_bytes()
                    idx += 1
            self.disk.write_block(ptr, bytes(block))
        self.inode.size = len(entries) * DIR_ENTRY_SIZE
        self.inode.update_timestamps(mtime=True, ctime=True)
        self.inode_table.write_inode(self.inode)
        self._parent_inode_num = parent

    # ------------------------------------------------------------ operations
    def lookup(self, name: str) -> Optional[int]:
        if name == ".":
            return self.inode.inode_num
        if name == "..":
            return self._read_parent_inode()
        for entry in self._load_entries():
            if entry.name == name:
                return entry.inode_num
        return None

    def add_entry(self, name: str, inode_num: int, file_type: int) -> None:
        validate_name(name)
        entries = self._load_entries()
        if any(e.name == name for e in entries):
            raise EntryExistsError(f"Entry '{name}' already exists")
        entries.append(DirEntry(inode_num, name, file_type))
        self._write_entries(entries)

    def remove_entry(self, name: str) -> int:
        if name in (".", ".."):
            raise InvalidNameError(f"Cannot remove reserved name: {name}")
        entries = self._load_entries()
        for i, entry in enumerate(entries):
            if entry.name == name:
                del entries[i]
                self._write_entries(entries)
                return entry.inode_num
        raise EntryNotFoundError(f"Entry '{name}' not found")

    def rename_entry(self, old_name: str, new_name: str) -> None:
        if old_name in (".", "..") or new_name in (".", ".."):
            raise InvalidNameError("Cannot rename reserved names")
        validate_name(new_name)
        entries = self._load_entries()
        if any(e.name == new_name for e in entries):
            raise EntryExistsError(f"Entry '{new_name}' already exists")
        for entry in entries:
            if entry.name == old_name:
                entry.name = new_name
                self._write_entries(entries)
                return
        raise EntryNotFoundError(f"Entry '{old_name}' not found")

    def set_parent(self, new_parent_inode: int) -> None:
        """Re-point '..' (used when a directory is moved)."""
        self._write_entries(self._load_entries(), parent_inode_num=new_parent_inode)

    def list_entries(self) -> List[DirEntry]:
        return self._load_entries()

    def get_entry(self, name: str) -> Optional[DirEntry]:
        if name == ".":
            return DirEntry(self.inode.inode_num, ".", DIR_TYPE_DIR)
        if name == "..":
            return DirEntry(self._read_parent_inode(), "..", DIR_TYPE_DIR)
        for entry in self._load_entries():
            if entry.name == name:
                return entry
        return None

    def entry_count(self) -> int:
        return len(self._load_entries())

    def is_empty(self) -> bool:
        return self.entry_count() == 0


def new_directory_block(self_inode: int, parent_inode: int) -> bytes:
    """A fresh directory data block containing only '.' and '..'."""
    block = bytearray(4096)
    block[0:DIR_ENTRY_SIZE] = DirEntry(self_inode, ".", DIR_TYPE_DIR).to_bytes()
    block[DIR_ENTRY_SIZE:2 * DIR_ENTRY_SIZE] = DirEntry(parent_inode, "..", DIR_TYPE_DIR).to_bytes()
    return bytes(block)


def initialize_root_directory(inode_table: InodeTable, block_allocator: BlockAllocator,
                              disk, tx_manager=None) -> Directory:
    """
    Create the root directory (inode 1 + one data block).  Must be called
    inside a transaction.
    """
    sb = inode_table.superblock
    root = inode_table.read_inode(1)
    if root.is_free:
        now = time.time_ns()
        root = Inode(inode_num=1, type=InodeType.DIRECTORY, mode=0o755, ctime=now, mtime=now,
                     atime=now, chtime=now, link_count=2, generation=1)
        sb.free_inode_count -= 1
        sb.next_free_inode = 2
        sb._dirty = True
    if root.direct[0] == 0:
        blk = block_allocator.allocate_any(1)[0]
        root.direct[0] = block_allocator._get_absolute_block(blk)
        root.blocks_allocated = 1
        root.size = 0
        disk.write_block(root.direct[0], new_directory_block(1, 1))
    inode_table.write_inode(root)
    d = Directory(root, inode_table, block_allocator, disk, tx_manager)
    d._parent_inode_num = 1
    return d
