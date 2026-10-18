"""
Filesystem Module

High-level filesystem operations and namespace management.
"""

import os
import time
from typing import List, Optional, Tuple, BinaryIO
from .virtual_disk import VirtualDisk, create_disk_image, open_disk_image
from .superblock import Superblock, SuperblockManager
from .block_allocator import BlockAllocator, create_initial_bitmap
from .inode import Inode, InodeTable, InodeType, create_root_inode, DIRECT_POINTERS
from .journal import Journal
from .transaction import TransactionManager, TransactionContext
from .recovery import RecoveryManager, run_recovery
from .consistency_checker import ConsistencyChecker, run_consistency_check
from .directory import Directory, DirEntry, DIR_TYPE_DIR, DIR_TYPE_FILE, initialize_root_directory
from .crash_injector import CrashInjector, CrashMode, get_global_injector, set_global_injector, maybe_crash


class FileSystemError(Exception):
    """Base exception for filesystem errors."""
    pass


class PathError(FileSystemError):
    """Raised for path-related errors."""
    pass


class NotFoundError(FileSystemError):
    """Raised when file/directory not found."""
    pass


class AlreadyExistsError(FileSystemError):
    """Raised when file/directory already exists."""
    pass


class NotDirectoryError(FileSystemError):
    """Raised when path is not a directory."""
    pass


class IsDirectoryError(FileSystemError):
    """Raised when path is a directory but file expected."""
    pass


class DirectoryNotEmptyError(FileSystemError):
    """Raised when directory not empty."""
    pass


class DiskFullError(FileSystemError):
    """Raised when disk is full."""
    pass


class FileSystem:
    """
    Main filesystem interface.
    
    Provides high-level operations: format, mount, create, read, write, etc.
    """
    
    def __init__(self, disk_path: str):
        self.disk_path = disk_path
        self.disk: Optional[VirtualDisk] = None
        self.superblock: Optional[Superblock] = None
        self.superblock_mgr: Optional[SuperblockManager] = None
        self.block_allocator: Optional[BlockAllocator] = None
        self.inode_table: Optional[InodeTable] = None
        self.journal: Optional[Journal] = None
        self.tx_manager: Optional[TransactionManager] = None
        self.recovery_mgr: Optional[RecoveryManager] = None
        self.checker: Optional[ConsistencyChecker] = None
        self.root_dir: Optional[Directory] = None
        self._mounted = False
        self._crash_injector: Optional[CrashInjector] = None
    
    def format(
        self,
        disk_size: int = 16 * 1024 * 1024,
        block_size: int = 4096,
        journal_blocks: int = 128,
        inode_blocks: int = 64,
        volume_label: str = ""
    ) -> None:
        """Format a new filesystem."""
        # Create disk image
        self.disk = create_disk_image(self.disk_path, block_size, disk_size)
        
        # Create superblock
        self.superblock_mgr = SuperblockManager(self.disk)
        self.superblock = self.superblock_mgr.create_default(
            disk_size=disk_size,
            journal_blocks=journal_blocks,
            inode_blocks=inode_blocks,
            volume_label=volume_label
        )
        
        # Write superblock
        self.superblock_mgr.write()
        
        # Initialize bitmap
        create_initial_bitmap(self.disk, self.superblock)
        
        # Initialize inode table (zero out)
        inode_table_start = self.superblock.inode_table_start_block
        inode_table_blocks = self.superblock.inode_table_block_count
        zero_data = b'\x00' * (inode_table_blocks * 4096)
        self.disk.write_blocks(inode_table_start, zero_data)
        
        # Initialize components
        self._init_components()
        
        # Create root directory
        self.root_dir = initialize_root_directory(
            self.inode_table, self.block_allocator, self.disk
        )
        
        # Write initial checkpoint
        self.journal.write_checkpoint(0)
        self.disk.flush()
        
        self._mounted = True
    
    def mount(self, run_recovery: bool = True) -> None:
        """Mount an existing filesystem."""
        if self._mounted:
            raise FileSystemError("Already mounted")
        
        # Open disk
        self.disk = open_disk_image(self.disk_path)
        
        # Read superblock
        self.superblock_mgr = SuperblockManager(self.disk)
        self.superblock = self.superblock_mgr.read()
        
        # Initialize components
        self._init_components()
        
        # Run recovery if needed
        if run_recovery and self.recovery_mgr.needs_recovery():
            result = self.recovery_mgr.recover()
            if not result.success:
                raise FileSystemError(f"Recovery failed: {result.errors}")
        
        # Open root directory
        root_inode = self.inode_table.read_inode(1)
        self.root_dir = Directory(root_inode, self.inode_table, self.block_allocator, self.disk)
        
        self._mounted = True
    
    def _init_components(self) -> None:
        """Initialize filesystem components."""
        self.block_allocator = BlockAllocator(self.disk, self.superblock)
        self.inode_table = InodeTable(self.disk, self.superblock)
        self.journal = Journal(self.disk, self.superblock)
        self.tx_manager = TransactionManager(
            self.disk, self.superblock, self.journal,
            self.inode_table, self.block_allocator
        )
        self.recovery_mgr = RecoveryManager(
            self.disk, self.superblock, self.journal,
            self.inode_table, self.block_allocator
        )
        self.checker = ConsistencyChecker(
            self.disk, self.superblock, self.inode_table,
            self.block_allocator, self.journal
        )
    
    def unmount(self) -> None:
        """Unmount filesystem."""
        if not self._mounted:
            return
        
        # Flush any pending writes
        if self.tx_manager and self.tx_manager.is_active():
            self.tx_manager.abort()
        
        self.superblock_mgr.sync_if_dirty()
        self.block_allocator.flush()
        self.inode_table.flush_dirty()
        self.disk.flush()
        self.disk.close()
        
        self._mounted = False
    
    def __enter__(self) -> 'FileSystem':
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.unmount()
    
    @property
    def is_mounted(self) -> bool:
        return self._mounted
    
    # Crash injection
    def enable_crash_injection(self, mode: CrashMode = CrashMode.EXCEPTION) -> CrashInjector:
        """Enable crash injection."""
        self._crash_injector = CrashInjector(mode)
        set_global_injector(self._crash_injector)
        self.tx_manager.set_crash_callback(self._crash_injector._crash_callback)
        self.recovery_mgr.set_crash_callback(self._crash_injector._crash_callback)
        return self._crash_injector
    
    def disable_crash_injection(self) -> None:
        """Disable crash injection."""
        if self._crash_injector:
            self._crash_injector.disable()
        self._crash_injector = None
    
    # Path resolution
    def _resolve_path(self, path: str, create_parent: bool = False) -> Tuple[Directory, str, Optional[int]]:
        """
        Resolve a path to (parent_directory, basename, inode_num).
        
        Returns:
            (parent_dir, basename, existing_inode_num or None)
        """
        if not path or path == "/":
            raise PathError("Invalid path: root")
        
        # Normalize path
        path = path.rstrip("/")
        parts = path.split("/")
        
        if parts[0] == "":
            parts = parts[1:]  # Remove leading empty from "/foo"
        
        if not parts:
            raise PathError("Invalid path")
        
        basename = parts[-1]
        dir_parts = parts[:-1]
        
        # Start from root
        current_dir = self.root_dir
        current_inode = 1
        
        # Traverse directory components
        for part in dir_parts:
            if not part:
                continue
            
            entry = current_dir.get_entry(part)
            if entry is None:
                if create_parent and part == dir_parts[-1]:
                    # Will be created by caller
                    break
                raise NotFoundError(f"Path not found: {part}")
            
            inode = self.inode_table.read_inode(entry.inode_num)
            if not inode.is_directory:
                raise NotDirectoryError(f"Not a directory: {part}")
            
            current_dir = Directory(inode, self.inode_table, self.block_allocator, self.disk)
            current_inode = entry.inode_num
        
        # Check if target exists
        target_inode = None
        if basename:
            entry = current_dir.get_entry(basename)
            if entry:
                target_inode = entry.inode_num
        
        return current_dir, basename, target_inode
    
    # Filesystem operations
    def mkdir(self, path: str) -> None:
        """Create a directory."""
        parent_dir, name, existing = self._resolve_path(path)
        
        if existing:
            raise AlreadyExistsError(f"Directory exists: {path}")
        
        with TransactionContext(self.tx_manager) as tx:
            # Allocate inode
            inode = self.inode_table.allocate_inode(InodeType.DIRECTORY, 0o755)
            
            # Allocate data block for directory
            blocks = self.block_allocator.allocate(1)
            if not blocks:
                raise DiskFullError("No free blocks for directory")
            
            # Convert to absolute block number
            abs_block = self.block_allocator._get_absolute_block(blocks[0])
            inode.direct[0] = abs_block
            inode.blocks_allocated = 1
            inode.size = 2 * 64  # . and .. entries
            inode._dirty = True
            
            # Write inode
            self.inode_table.write_inode(inode)
            
            # Add entry to parent
            parent_dir.add_entry(name, inode.inode_num, DIR_TYPE_DIR)
            
            # Initialize directory block with . and ..
            block_data = bytearray(4096)
            dot_entry = DirEntry(inode.inode_num, ".", DIR_TYPE_DIR).to_bytes()
            dotdot_entry = DirEntry(parent_dir.inode.inode_num, "..", DIR_TYPE_DIR).to_bytes()
            block_data[0:64] = dot_entry
            block_data[64:128] = dotdot_entry
            self.disk.write_block(abs_block, bytes(block_data))
            
            # Record metadata changes in transaction
            self.tx_manager.add_full_block_change(abs_block, bytes(block_data))
            # Parent directory block changes handled by Directory.add_entry
            
        # Commit happens automatically via context manager
    
    def create(self, path: str) -> None:
        """Create an empty file."""
        parent_dir, name, existing = self._resolve_path(path)
        
        if existing:
            raise AlreadyExistsError(f"File exists: {path}")
        
        with TransactionContext(self.tx_manager) as tx:
            # Allocate inode
            inode = self.inode_table.allocate_inode(InodeType.FILE, 0o644)
            
            # Write inode
            self.inode_table.write_inode(inode)
            
            # Add entry to parent
            parent_dir.add_entry(name, inode.inode_num, DIR_TYPE_FILE)
    
    def write(self, path: str, data: bytes) -> int:
        """Write data to file (replaces content)."""
        _, _, inode_num = self._resolve_path(path)
        
        if inode_num is None:
            raise NotFoundError(f"File not found: {path}")
        
        inode = self.inode_table.read_inode(inode_num)
        if inode.is_directory:
            raise IsDirectoryError(f"Is a directory: {path}")
        
        # Calculate blocks needed
        data_len = len(data)
        blocks_needed = (data_len + 4095) // 4096
        
        with TransactionContext(self.tx_manager) as tx:
            # Free old blocks
            old_blocks = inode.get_all_blocks()
            if old_blocks:
                self.block_allocator.free(old_blocks)
                inode.direct = [0] * DIRECT_POINTERS
                inode.blocks_allocated = 0
            
            # Allocate new blocks
            if blocks_needed > 0:
                new_blocks = self.block_allocator.allocate(blocks_needed)
                if len(new_blocks) < blocks_needed:
                    raise DiskFullError("Not enough free blocks")
                
                # Convert to absolute block numbers
                abs_blocks = [self.block_allocator._get_absolute_block(b) for b in new_blocks]
                inode.direct[:blocks_needed] = abs_blocks
                inode.blocks_allocated = blocks_needed
            
            inode.size = data_len
            inode.update_timestamps(mtime=True, ctime=True)
            inode._dirty = True
            
            # Write inode
            self.inode_table.write_inode(inode)
            inode_block = self.superblock.inode_table_start_block + (inode_num - 1) // 16
            inode_offset = ((inode_num - 1) % 16) * 256
            self.tx_manager.add_metadata_change(inode_block, inode_offset, inode.to_bytes())
            
            # Write data blocks
            for i, block_num in enumerate(inode.get_all_blocks()):
                offset = i * 4096
                block_data = data[offset:offset + 4096]
                if len(block_data) < 4096:
                    block_data += b'\x00' * (4096 - len(block_data))
                
                self.disk.write_block(block_num, block_data)
                self.tx_manager.add_full_block_change(block_num, block_data)
        
        return data_len
    
    def append(self, path: str, data: bytes) -> int:
        """Append data to file."""
        _, _, inode_num = self._resolve_path(path)
        
        if inode_num is None:
            raise NotFoundError(f"File not found: {path}")
        
        inode = self.inode_table.read_inode(inode_num)
        if inode.is_directory:
            raise IsDirectoryError(f"Is a directory: {path}")
        
        current_size = inode.size
        new_size = current_size + len(data)
        current_blocks = inode.blocks_allocated
        new_blocks_needed = (new_size + 4095) // 4096 - current_blocks
        
        with TransactionContext(self.tx_manager) as tx:
            # Allocate additional blocks if needed
            if new_blocks_needed > 0:
                new_blocks = self.block_allocator.allocate(new_blocks_needed)
                if len(new_blocks) < new_blocks_needed:
                    raise DiskFullError("Not enough free blocks")
                
                # Convert to absolute block numbers and add to inode
                abs_blocks = [self.block_allocator._get_absolute_block(b) for b in new_blocks]
                idx = 0
                for i in range(DIRECT_POINTERS):
                    if inode.direct[i] == 0 and idx < len(abs_blocks):
                        inode.direct[i] = abs_blocks[idx]
                        idx += 1
                
                inode.blocks_allocated += new_blocks_needed
            
            inode.size = new_size
            inode.update_timestamps(mtime=True, ctime=True)
            inode._dirty = True
            
            # Write inode
            self.inode_table.write_inode(inode)
            inode_block = self.superblock.inode_table_start_block + (inode_num - 1) // 16
            inode_offset = ((inode_num - 1) % 16) * 256
            self.tx_manager.add_metadata_change(inode_block, inode_offset, inode.to_bytes())
            
            # Write new data to the appropriate blocks
            write_offset = current_size
            remaining = data
            while remaining:
                block_idx = write_offset // 4096
                block_offset = write_offset % 4096
                block_num = inode.direct[block_idx]
                
                # Read existing block
                block_data = bytearray(self.disk.read_block(block_num))
                
                # Write as much as fits in this block
                chunk_size = min(len(remaining), 4096 - block_offset)
                block_data[block_offset:block_offset + chunk_size] = remaining[:chunk_size]
                
                # Write back
                self.disk.write_block(block_num, bytes(block_data))
                self.tx_manager.add_full_block_change(block_num, bytes(block_data))
                
                remaining = remaining[chunk_size:]
                write_offset += chunk_size
        
        return len(data)
    
    def read(self, path: str) -> bytes:
        """Read file contents."""
        _, _, inode_num = self._resolve_path(path)
        
        if inode_num is None:
            raise NotFoundError(f"File not found: {path}")
        
        inode = self.inode_table.read_inode(inode_num)
        if inode.is_directory:
            raise IsDirectoryError(f"Is a directory: {path}")
        
        if inode.size == 0:
            return b''
        
        data = bytearray()
        for block_num in inode.get_all_blocks():
            block_data = self.disk.read_block(block_num)
            data.extend(block_data)
        
        return bytes(data[:inode.size])
    
    def ls(self, path: str = "/") -> List[DirEntry]:
        """List directory contents."""
        if path == "/":
            return self.root_dir.list_entries()
        
        _, _, inode_num = self._resolve_path(path)
        
        if inode_num is None:
            raise NotFoundError(f"Directory not found: {path}")
        
        inode = self.inode_table.read_inode(inode_num)
        if not inode.is_directory:
            raise NotDirectoryError(f"Not a directory: {path}")
        
        dir_obj = Directory(inode, self.inode_table, self.block_allocator, self.disk)
        return dir_obj.list_entries()
    
    def stat(self, path: str) -> dict:
        """Get file/directory metadata."""
        if path == "/":
            inode = self.inode_table.read_inode(1)
        else:
            _, _, inode_num = self._resolve_path(path)
            if inode_num is None:
                raise NotFoundError(f"Path not found: {path}")
            inode = self.inode_table.read_inode(inode_num)
        
        return {
            'inode': inode.inode_num,
            'type': inode.type.name,
            'mode': oct(inode.mode),
            'size': inode.size,
            'blocks': inode.blocks_allocated,
            'links': inode.link_count,
            'ctime': inode.ctime,
            'mtime': inode.mtime,
            'atime': inode.atime,
            'block_pointers': [b for b in inode.direct if b != 0],
        }
    
    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move file or directory."""
        old_parent, old_name, old_inode_num = self._resolve_path(old_path)
        if old_inode_num is None:
            raise NotFoundError(f"Source not found: {old_path}")
        
        new_parent, new_name, new_inode_num = self._resolve_path(new_path)
        if new_inode_num is not None:
            raise AlreadyExistsError(f"Destination exists: {new_path}")
        
        if old_parent.inode.inode_num == new_parent.inode.inode_num:
            # Same directory - simple rename
            with TransactionContext(self.tx_manager):
                old_parent.rename_entry(old_name, new_name)
        else:
            # Cross-directory move
            with TransactionContext(self.tx_manager):
                # Get entry info
                entry = old_parent.get_entry(old_name)
                if entry is None:
                    raise NotFoundError(f"Entry not found: {old_name}")
                
                # Add to new parent
                new_parent.add_entry(new_name, entry.inode_num, entry.file_type)
                
                # Remove from old parent
                old_parent.remove_entry(old_name)
                
                # Update link counts if directory
                if entry.file_type == DIR_TYPE_DIR:
                    # Update .. entry in moved directory
                    target_inode = self.inode_table.read_inode(entry.inode_num)
                    target_dir = Directory(target_inode, self.inode_table, self.block_allocator, self.disk)
                    # Update .. entry to point to new parent
                    # This requires rewriting the directory block
                    pass
    
    def delete(self, path: str) -> None:
        """Delete a file."""
        parent_dir, name, inode_num = self._resolve_path(path)
        
        if inode_num is None:
            raise NotFoundError(f"File not found: {path}")
        
        inode = self.inode_table.read_inode(inode_num)
        if inode.is_directory:
            raise IsDirectoryError(f"Is a directory, use rmdir: {path}")
        
        with TransactionContext(self.tx_manager) as tx:
            # Free data blocks
            blocks = inode.get_all_blocks()
            if blocks:
                self.block_allocator.free_absolute(blocks)
            
            # Free inode
            inode.link_count -= 1
            if inode.link_count <= 0:
                self.inode_table.free_inode(inode_num)
            else:
                self.inode_table.write_inode(inode)
            
            # Remove directory entry
            parent_dir.remove_entry(name)
    
    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""
        parent_dir, name, inode_num = self._resolve_path(path)
        
        if inode_num is None:
            raise NotFoundError(f"Directory not found: {path}")
        
        inode = self.inode_table.read_inode(inode_num)
        if not inode.is_directory:
            raise NotDirectoryError(f"Not a directory: {path}")
        
        dir_obj = Directory(inode, self.inode_table, self.block_allocator, self.disk)
        if not dir_obj.is_empty():
            raise DirectoryNotEmptyError(f"Directory not empty: {path}")
        
        with TransactionContext(self.tx_manager) as tx:
            # Free directory block
            blocks = inode.get_all_blocks()
            if blocks:
                self.block_allocator.free_absolute(blocks)
            
            # Free inode (set link_count=0 first)
            inode.link_count = 0
            self.inode_table.write_inode(inode)
            self.inode_table.free_inode(inode_num)
            
            # Remove directory entry
            parent_dir.remove_entry(name)
    
    def df(self) -> dict:
        """Get disk usage statistics."""
        return {
            'total_blocks': self.superblock.total_blocks,
            'total_size': self.superblock.total_blocks * self.superblock.block_size,
            'free_blocks': self.superblock.free_block_count,
            'free_size': self.superblock.free_block_count * self.superblock.block_size,
            'used_blocks': self.superblock.total_blocks - self.superblock.free_block_count,
            'used_size': (self.superblock.total_blocks - self.superblock.free_block_count) * self.superblock.block_size,
            'total_inodes': self.superblock.total_inodes,
            'free_inodes': self.superblock.free_inode_count,
            'used_inodes': self.superblock.total_inodes - self.superblock.free_inode_count,
        }
    
    def dump(self) -> dict:
        """Dump filesystem state for debugging."""
        return {
            'superblock': {
                'magic': hex(self.superblock.magic),
                'version': self.superblock.version,
                'total_blocks': self.superblock.total_blocks,
                'journal_blocks': self.superblock.journal_block_count,
                'bitmap_blocks': self.superblock.bitmap_block_count,
                'inode_blocks': self.superblock.inode_table_block_count,
                'data_blocks': self.superblock.data_block_count,
                'journal_head': self.superblock.journal_head,
                'journal_tail': self.superblock.journal_tail,
                'checkpoint_txid': self.superblock.checkpoint_txid,
            },
            'journal': self.journal.get_journal_stats(),
            'bitmap': self.block_allocator.get_bitmap_summary(),
            'inodes': self.inode_table.get_stats(),
            'df': self.df(),
        }
    
    def check(self) -> List[str]:
        """Run consistency check."""
        return self.checker.check_all()
    
    def recover(self) -> None:
        """Explicitly run recovery."""
        result = self.recovery_mgr.recover()
        if not result.success:
            raise FileSystemError(f"Recovery failed: {result.errors}")


def create_filesystem(disk_path: str, **format_args) -> FileSystem:
    """Create and format a new filesystem."""
    fs = FileSystem(disk_path)
    fs.format(**format_args)
    return fs


def open_filesystem(disk_path: str, run_recovery: bool = True) -> FileSystem:
    """Open an existing filesystem."""
    fs = FileSystem(disk_path)
    fs.mount(run_recovery=run_recovery)
    return fs