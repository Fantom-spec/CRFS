"""
Virtual Disk Module

Provides low-level block I/O operations on a disk image file.
Supports fault injection for crash testing.
"""

import os
import struct
import threading
from typing import Optional, Callable
from enum import Enum


class CrashPoint(Enum):
    """Crash injection points for testing."""
    NONE = 0
    BEFORE_READ = 1
    AFTER_READ = 2
    BEFORE_WRITE = 3
    AFTER_WRITE = 3
    BEFORE_FLUSH = 4
    AFTER_FLUSH = 5
    BEFORE_JOURNAL_WRITE = 6
    AFTER_JOURNAL_WRITE = 7
    BEFORE_JOURNAL_FLUSH = 8
    AFTER_JOURNAL_FLUSH = 9
    BEFORE_COMMIT_WRITE = 10
    AFTER_COMMIT_WRITE = 11
    BEFORE_COMMIT_FLUSH = 12
    AFTER_COMMIT_FLUSH = 13
    BEFORE_HOME_WRITE = 14
    AFTER_HOME_WRITE = 15
    BEFORE_HOME_FLUSH = 16
    AFTER_HOME_FLUSH = 17
    BEFORE_CHECKPOINT = 18
    AFTER_CHECKPOINT = 19
    DURING_RECOVERY_SCAN = 20
    DURING_RECOVERY_REPLAY = 21


class VirtualDiskError(Exception):
    """Base exception for virtual disk errors."""
    pass


class DiskNotOpenError(VirtualDiskError):
    """Raised when disk operations are attempted on closed disk."""
    pass


class InvalidBlockError(VirtualDiskError):
    """Raised when block number is out of bounds."""
    pass


class CrashInjectedError(VirtualDiskError):
    """Raised when a crash is injected."""
    pass


class VirtualDisk:
    """
    Virtual disk image with fixed-size block I/O.
    
    Provides controlled read/write/flush operations with bounds checking
    and optional crash injection points.
    """
    
    DEFAULT_BLOCK_SIZE = 4096
    DEFAULT_DISK_SIZE = 16 * 1024 * 1024  # 16 MiB
    
    def __init__(
        self,
        path: str,
        block_size: int = DEFAULT_BLOCK_SIZE,
        disk_size: int = DEFAULT_DISK_SIZE,
        create: bool = False
    ):
        """
        Initialize virtual disk.
        
        Args:
            path: Path to disk image file
            block_size: Block size in bytes (must be power of 2)
            disk_size: Total disk size in bytes (ignored when opening existing disk)
            create: If True, create new disk image
        """
        self.path = path
        self.block_size = block_size
        self._fd: Optional[int] = None
        self._lock = threading.RLock()
        self._crash_callback: Optional[Callable[[CrashPoint], None]] = None
        self._crash_points_enabled = True
        
        # Validate block size
        if block_size <= 0 or (block_size & (block_size - 1)) != 0:
            raise ValueError("Block size must be a power of 2")
        
        if create:
            self.disk_size = disk_size
            if disk_size % block_size != 0:
                raise ValueError("Disk size must be multiple of block size")
            self.total_blocks = disk_size // block_size
            self._create_disk()
        else:
            self._open_disk()
    
    def _create_disk(self) -> None:
        """Create a new disk image file."""
        # Create directory if needed
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        
        # Create file with exact size
        with open(self.path, 'wb') as f:
            f.truncate(self.disk_size)
        
        self._fd = os.open(self.path, os.O_RDWR | os.O_SYNC)
    
    def _open_disk(self) -> None:
        """Open existing disk image file."""
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Disk image not found: {self.path}")
        
        # Get actual file size
        stat = os.stat(self.path)
        self.disk_size = stat.st_size
        if self.disk_size % self.block_size != 0:
            raise VirtualDiskError(
                f"Disk size {self.disk_size} is not a multiple of block size {self.block_size}"
            )
        self.total_blocks = self.disk_size // self.block_size
        
        self._fd = os.open(self.path, os.O_RDWR | os.O_SYNC)
    
    def close(self) -> None:
        """Close the disk image."""
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
    
    def __enter__(self) -> 'VirtualDisk':
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
    
    @property
    def is_open(self) -> bool:
        """Check if disk is open."""
        return self._fd is not None
    
    def _check_block(self, block_num: int, count: int = 1) -> None:
        """Validate block number and count."""
        if block_num < 0 or block_num + count > self.total_blocks:
            raise InvalidBlockError(
                f"Block {block_num} (count={count}) out of range [0, {self.total_blocks})"
            )
    
    def _maybe_crash(self, point: CrashPoint) -> None:
        """Invoke crash callback if enabled and set."""
        if self._crash_points_enabled and self._crash_callback:
            self._crash_callback(point)
    
    def set_crash_callback(self, callback: Optional[Callable[[CrashPoint], None]]) -> None:
        """Set callback for crash injection."""
        self._crash_callback = callback
    
    def enable_crash_points(self, enabled: bool = True) -> None:
        """Enable or disable crash injection points."""
        self._crash_points_enabled = enabled
    
    def read_block(self, block_num: int) -> bytes:
        """
        Read a single block from disk.
        
        Args:
            block_num: Block number to read
            
        Returns:
            Block data as bytes
        """
        with self._lock:
            if self._fd is None:
                raise DiskNotOpenError("Disk not open")
            
            self._check_block(block_num)
            self._maybe_crash(CrashPoint.BEFORE_READ)
            
            offset = block_num * self.block_size
            data = os.pread(self._fd, self.block_size, offset)
            
            if len(data) != self.block_size:
                raise VirtualDiskError(
                    f"Short read: expected {self.block_size}, got {len(data)}"
                )
            
            self._maybe_crash(CrashPoint.AFTER_READ)
            return data
    
    def write_block(self, block_num: int, data: bytes) -> None:
        """
        Write a single block to disk.
        
        Args:
            block_num: Block number to write
            data: Block data (must be exactly block_size bytes)
        """
        with self._lock:
            if self._fd is None:
                raise DiskNotOpenError("Disk not open")
            
            self._check_block(block_num)
            
            if len(data) != self.block_size:
                raise ValueError(
                    f"Data size {len(data)} != block size {self.block_size}"
                )
            
            self._maybe_crash(CrashPoint.BEFORE_WRITE)
            
            offset = block_num * self.block_size
            written = os.pwrite(self._fd, data, offset)
            
            if written != self.block_size:
                raise VirtualDiskError(
                    f"Short write: expected {self.block_size}, wrote {written}"
                )
            
            self._maybe_crash(CrashPoint.AFTER_WRITE)
    
    def read_blocks(self, start_block: int, count: int) -> bytes:
        """
        Read multiple consecutive blocks.
        
        Args:
            start_block: Starting block number
            count: Number of blocks to read
            
        Returns:
            Concatenated block data
        """
        with self._lock:
            if self._fd is None:
                raise DiskNotOpenError("Disk not open")
            
            self._check_block(start_block, count)
            self._maybe_crash(CrashPoint.BEFORE_READ)
            
            offset = start_block * self.block_size
            total_size = count * self.block_size
            data = os.pread(self._fd, total_size, offset)
            
            if len(data) != total_size:
                raise VirtualDiskError(
                    f"Short read: expected {total_size}, got {len(data)}"
                )
            
            self._maybe_crash(CrashPoint.AFTER_READ)
            return data
    
    def write_blocks(self, start_block: int, data: bytes) -> None:
        """
        Write multiple consecutive blocks.
        
        Args:
            start_block: Starting block number
            data: Block data (must be count * block_size bytes)
        """
        with self._lock:
            if self._fd is None:
                raise DiskNotOpenError("Disk not open")
            
            count = len(data) // self.block_size
            if len(data) % self.block_size != 0:
                raise ValueError("Data size must be multiple of block size")
            
            self._check_block(start_block, count)
            self._maybe_crash(CrashPoint.BEFORE_WRITE)
            
            offset = start_block * self.block_size
            written = os.pwrite(self._fd, data, offset)
            
            if written != len(data):
                raise VirtualDiskError(
                    f"Short write: expected {len(data)}, wrote {written}"
                )
            
            self._maybe_crash(CrashPoint.AFTER_WRITE)
    
    def flush(self) -> None:
        """Flush all pending writes to disk."""
        with self._lock:
            if self._fd is None:
                raise DiskNotOpenError("Disk not open")
            
            self._maybe_crash(CrashPoint.BEFORE_FLUSH)
            os.fsync(self._fd)
            self._maybe_crash(CrashPoint.AFTER_FLUSH)
    
    def flush_range(self, start_block: int, count: int) -> None:
        """
        Flush a specific range of blocks.
        
        Note: On most systems, fsync flushes the entire file.
        This is provided for semantic clarity in the protocol.
        """
        self.flush()
    
    def zero_blocks(self, start_block: int, count: int) -> None:
        """Zero out a range of blocks."""
        zero_data = b'\x00' * (count * self.block_size)
        self.write_blocks(start_block, zero_data)
    
    def get_size(self) -> int:
        """Get total disk size in bytes."""
        return self.disk_size
    
    def get_block_size(self) -> int:
        """Get block size in bytes."""
        return self.block_size
    
    def get_total_blocks(self) -> int:
        """Get total number of blocks."""
        return self.total_blocks


def create_disk_image(
    path: str,
    block_size: int = VirtualDisk.DEFAULT_BLOCK_SIZE,
    disk_size: int = VirtualDisk.DEFAULT_DISK_SIZE
) -> VirtualDisk:
    """
    Create and return a new virtual disk.
    
    Args:
        path: Path for disk image
        block_size: Block size in bytes
        disk_size: Total disk size in bytes
        
    Returns:
        Open VirtualDisk instance
    """
    return VirtualDisk(path, block_size, disk_size, create=True)


def open_disk_image(
    path: str,
    block_size: int = VirtualDisk.DEFAULT_BLOCK_SIZE
) -> VirtualDisk:
    """
    Open an existing virtual disk.
    
    Args:
        path: Path to disk image
        block_size: Expected block size
        
    Returns:
        Open VirtualDisk instance
    """
    return VirtualDisk(path, block_size, create=False)