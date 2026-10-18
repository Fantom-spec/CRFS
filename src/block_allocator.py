"""
Block Allocator Module

Manages the free-space bitmap for data block allocation.
"""

import struct
from typing import List, Optional, Iterator
from .virtual_disk import VirtualDisk
from .superblock import Superblock


class BlockAllocatorError(Exception):
    """Base exception for block allocator errors."""
    pass


class NoFreeBlocksError(BlockAllocatorError):
    """Raised when no free blocks are available."""
    pass


class InvalidBlockError(BlockAllocatorError):
    """Raised when block number is invalid."""
    pass


class BlockAllocator:
    """
    Manages free-space bitmap for data block allocation.
    
    Bitmap is stored in dedicated blocks, 1 bit per data block.
    0 = free, 1 = allocated.
    """
    
    BITS_PER_BLOCK = 4096 * 8  # 32768 bits per 4KiB block
    
    def __init__(self, disk: VirtualDisk, superblock: Superblock):
        self.disk = disk
        self.superblock = superblock
        self._bitmap: Optional[bytearray] = None
        self._dirty = False
    
    def _load_bitmap(self) -> None:
        """Load bitmap from disk into memory."""
        if self._bitmap is not None:
            return
        
        sb = self.superblock
        bitmap_blocks = sb.bitmap_block_count
        bitmap_start = sb.bitmap_start_block
        
        # Read all bitmap blocks
        data = self.disk.read_blocks(bitmap_start, bitmap_blocks)
        self._bitmap = bytearray(data)
        self._dirty = False
    
    def _save_bitmap(self) -> None:
        """Write bitmap from memory to disk."""
        if self._bitmap is None or not self._dirty:
            return
        
        sb = self.superblock
        bitmap_start = sb.bitmap_start_block
        bitmap_blocks = sb.bitmap_block_count
        
        # Ensure we have exactly the right amount of data
        expected_size = bitmap_blocks * 4096
        if len(self._bitmap) != expected_size:
            # Pad or truncate
            self._bitmap = bytearray(self._bitmap[:expected_size].ljust(expected_size, b'\x00'))
        
        self.disk.write_blocks(bitmap_start, bytes(self._bitmap))
        self._dirty = False
    
    def _get_bit_index(self, data_block: int) -> tuple[int, int]:
        """
        Convert data block number to (byte_index, bit_mask).
        
        Args:
            data_block: Data block number (relative to data region start)
            
        Returns:
            Tuple of (byte_index, bit_mask)
        """
        if data_block < 0 or data_block >= self.superblock.data_block_count:
            raise InvalidBlockError(f"Data block {data_block} out of range [0, {self.superblock.data_block_count})")
        
        byte_index = data_block // 8
        bit_index = data_block % 8
        return byte_index, 1 << bit_index
    
    def _get_absolute_block(self, data_block: int) -> int:
        """Convert data block index to absolute block number."""
        return self.superblock.data_start_block + data_block
    
    def _get_data_block_index(self, absolute_block: int) -> int:
        """Convert absolute block number to data block index."""
        return absolute_block - self.superblock.data_start_block
    
    def is_allocated(self, data_block: int) -> bool:
        """Check if a data block is allocated."""
        self._load_bitmap()
        byte_index, bit_mask = self._get_bit_index(data_block)
        return (self._bitmap[byte_index] & bit_mask) != 0
    
    def is_allocated_absolute(self, absolute_block: int) -> bool:
        """Check if an absolute block is allocated."""
        data_block = self._get_data_block_index(absolute_block)
        return self.is_allocated(data_block)
    
    def allocate(self, count: int = 1) -> List[int]:
        """
        Allocate one or more contiguous free blocks.
        
        Args:
            count: Number of contiguous blocks to allocate
            
        Returns:
            List of allocated data block indices (relative to data region)
            
        Raises:
            NoFreeBlocksError: If insufficient contiguous free blocks
        """
        if count <= 0:
            raise ValueError("Count must be positive")
        
        self._load_bitmap()
        
        # Search for contiguous free blocks
        max_block = self.superblock.data_block_count
        byte_index = 0
        bit_index = 0
        current_run = 0
        start_block = 0
        
        for data_block in range(max_block):
            byte_idx, bit_mask = self._get_bit_index(data_block)
            allocated = (self._bitmap[byte_idx] & bit_mask) != 0
            
            if not allocated:
                if current_run == 0:
                    start_block = data_block
                current_run += 1
                if current_run >= count:
                    # Found enough contiguous blocks
                    allocated_blocks = list(range(start_block, start_block + count))
                    for blk in allocated_blocks:
                        b_idx, b_mask = self._get_bit_index(blk)
                        self._bitmap[b_idx] |= b_mask
                    
                    self._dirty = True
                    self.superblock.free_block_count -= count
                    self.superblock._dirty = True
                    return allocated_blocks
            else:
                current_run = 0
        
        raise NoFreeBlocksError(f"No {count} contiguous free blocks available")
    
    def allocate_any(self, count: int = 1) -> List[int]:
        """
        Allocate any free blocks (not necessarily contiguous).
        
        Args:
            count: Number of blocks to allocate
            
        Returns:
            List of allocated data block indices
        """
        if count <= 0:
            raise ValueError("Count must be positive")
        
        self._load_bitmap()
        
        if self.superblock.free_block_count < count:
            raise NoFreeBlocksError(f"Only {self.superblock.free_block_count} free blocks, need {count}")
        
        allocated = []
        for data_block in range(self.superblock.data_block_count):
            if len(allocated) >= count:
                break
            
            byte_idx, bit_mask = self._get_bit_index(data_block)
            if (self._bitmap[byte_idx] & bit_mask) == 0:
                self._bitmap[byte_idx] |= bit_mask
                allocated.append(data_block)
        
        if len(allocated) < count:
            raise NoFreeBlocksError("Failed to allocate requested blocks")
        
        self._dirty = True
        self.superblock.free_block_count -= count
        self.superblock._dirty = True
        return allocated
    
    def free(self, data_blocks: List[int]) -> None:
        """
        Free previously allocated blocks.
        
        Args:
            data_blocks: List of data block indices to free
        """
        self._load_bitmap()
        
        for data_block in data_blocks:
            byte_idx, bit_mask = self._get_bit_index(data_block)
            if (self._bitmap[byte_idx] & bit_mask) == 0:
                raise InvalidBlockError(f"Block {data_block} already free")
            self._bitmap[byte_idx] &= ~bit_mask
        
        self._dirty = True
        self.superblock.free_block_count += len(data_blocks)
        self.superblock._dirty = True
    
    def free_absolute(self, absolute_blocks: List[int]) -> None:
        """Free blocks by absolute block numbers."""
        data_blocks = [self._get_data_block_index(b) for b in absolute_blocks]
        self.free(data_blocks)
    
    def get_free_count(self) -> int:
        """Get number of free blocks."""
        return self.superblock.free_block_count
    
    def get_allocated_count(self) -> int:
        """Get number of allocated blocks."""
        return self.superblock.data_block_count - self.superblock.free_block_count
    
    def iter_allocated(self) -> Iterator[int]:
        """Iterate over allocated data block indices."""
        self._load_bitmap()
        for data_block in range(self.superblock.data_block_count):
            if self.is_allocated(data_block):
                yield data_block
    
    def iter_free(self) -> Iterator[int]:
        """Iterate over free data block indices."""
        self._load_bitmap()
        for data_block in range(self.superblock.data_block_count):
            if not self.is_allocated(data_block):
                yield data_block
    
    def flush(self) -> None:
        """Flush bitmap to disk."""
        self._save_bitmap()
        # Also flush superblock since free count changed
        if self.superblock._dirty:
            self.superblock._checksum = self.superblock.compute_checksum()
            self.disk.write_block(0, self.superblock.to_bytes())
            self.superblock._dirty = False
    
    def verify_consistency(self, allocated_by_inodes: set) -> List[str]:
        """
        Verify bitmap consistency against inode block references.
        
        Args:
            allocated_by_inodes: Set of absolute block numbers referenced by inodes
            
        Returns:
            List of error messages (empty if consistent)
        """
        errors = []
        self._load_bitmap()
        
        # Check each data block
        for data_block in range(self.superblock.data_block_count):
            absolute_block = self._get_absolute_block(data_block)
            in_bitmap = self.is_allocated(data_block)
            in_inodes = absolute_block in allocated_by_inodes
            
            if in_bitmap and not in_inodes:
                errors.append(f"Block {absolute_block} (data index {data_block}) marked allocated in bitmap but not referenced by any inode")
            elif not in_bitmap and in_inodes:
                errors.append(f"Block {absolute_block} (data index {data_block}) referenced by inode but marked free in bitmap")
        
        # Verify free count matches
        actual_free = sum(1 for i in range(self.superblock.data_block_count) if not self.is_allocated(i))
        if actual_free != self.superblock.free_block_count:
            errors.append(f"Free block count mismatch: bitmap={actual_free}, superblock={self.superblock.free_block_count}")
        
        return errors
    
    def get_bitmap_summary(self) -> dict:
        """Get bitmap statistics."""
        self._load_bitmap()
        allocated = sum(1 for i in range(self.superblock.data_block_count) if self.is_allocated(i))
        free = self.superblock.data_block_count - allocated
        return {
            'total_blocks': self.superblock.data_block_count,
            'allocated': allocated,
            'free': free,
            'superblock_free_count': self.superblock.free_block_count,
            'bitmap_blocks': self.superblock.bitmap_block_count,
        }


def create_initial_bitmap(disk: VirtualDisk, superblock: Superblock) -> None:
    """Initialize bitmap to all zeros (all blocks free)."""
    bitmap_start = superblock.bitmap_start_block
    bitmap_blocks = superblock.bitmap_block_count
    zero_data = b'\x00' * (bitmap_blocks * 4096)
    disk.write_blocks(bitmap_start, zero_data)