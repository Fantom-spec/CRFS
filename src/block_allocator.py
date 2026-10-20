"""
Block Allocator Module

Free-space bitmap for data blocks (1 bit per data block, 0 = free).

The bitmap is always read from and written through the block I/O object it
was given (the transaction-staging proxy in a mounted filesystem), and it is
never cached in memory, so an aborted transaction cannot leave stale
allocation state behind.  Free-block accounting lives in the superblock
(which the transaction layer snapshots/restores on abort).
"""

from typing import Iterator, List, Optional, Set, Tuple

from .superblock import Superblock


class BlockAllocatorError(Exception):
    pass


class NoFreeBlocksError(BlockAllocatorError):
    pass


class InvalidBlockError(BlockAllocatorError):
    pass


class BlockAllocator:
    BITS_PER_BLOCK = 4096 * 8

    def __init__(self, disk, superblock: Superblock):
        self.disk = disk          # raw disk or StagingIO
        self.superblock = superblock
        # blocks freed in the running transaction must not be re-allocated by it
        self.avoid: Set[int] = set()
        # blocks allocated in the running transaction (absolute numbers)
        self.fresh: Set[int] = set()

    # ------------------------------------------------------------- helpers
    def _read_bitmap(self) -> bytearray:
        sb = self.superblock
        return bytearray(self.disk.read_blocks(sb.bitmap_start_block, sb.bitmap_block_count))

    def _write_bitmap(self, bitmap: bytearray, changed_bytes: List[int]) -> None:
        """Write back only the bitmap blocks that contain a changed byte."""
        sb = self.superblock
        bs = self.disk.block_size
        for bi in sorted({i // bs for i in changed_bytes}):
            self.disk.write_block(sb.bitmap_start_block + bi, bytes(bitmap[bi * bs:(bi + 1) * bs]))

    def _check(self, data_block: int) -> None:
        if data_block < 0 or data_block >= self.superblock.data_block_count:
            raise InvalidBlockError(
                f"Data block {data_block} out of range [0, {self.superblock.data_block_count})")

    def _get_bit_index(self, data_block: int) -> Tuple[int, int]:
        self._check(data_block)
        return data_block // 8, 1 << (data_block % 8)

    def _get_absolute_block(self, data_block: int) -> int:
        return self.superblock.data_start_block + data_block

    def _get_data_block_index(self, absolute_block: int) -> int:
        return absolute_block - self.superblock.data_start_block

    # -------------------------------------------------------------- queries
    def is_allocated(self, data_block: int) -> bool:
        self._check(data_block)
        bs = self.disk.block_size
        byte_index, mask = self._get_bit_index(data_block)
        blk = self.disk.read_block(self.superblock.bitmap_start_block + byte_index // bs)
        return (blk[byte_index % bs] & mask) != 0

    def is_allocated_absolute(self, absolute_block: int) -> bool:
        return self.is_allocated(self._get_data_block_index(absolute_block))

    def get_bitmap_data(self) -> List[Tuple[int, bytes]]:
        bm = self._read_bitmap()
        bs = self.disk.block_size
        sb = self.superblock
        return [(sb.bitmap_start_block + i, bytes(bm[i * bs:(i + 1) * bs]))
                for i in range(sb.bitmap_block_count)]

    # ------------------------------------------------------------ allocation
    def _mark(self, blocks: List[int], bm: bytearray) -> None:
        changed = []
        for b in blocks:
            idx, mask = self._get_bit_index(b)
            bm[idx] |= mask
            changed.append(idx)
        self._write_bitmap(bm, changed)
        self.superblock.free_block_count -= len(blocks)
        self.superblock._dirty = True
        self.fresh.update(self._get_absolute_block(b) for b in blocks)

    def allocate(self, count: int = 1) -> List[int]:
        """Allocate ``count`` CONTIGUOUS blocks. Returns data-region indices."""
        if count <= 0:
            raise ValueError("Count must be positive")
        bm = self._read_bitmap()
        run = 0
        start = 0
        for b in range(self.superblock.data_block_count):
            idx, mask = b // 8, 1 << (b % 8)
            used = (bm[idx] & mask) != 0 or self._get_absolute_block(b) in self.avoid
            if used:
                run = 0
                continue
            if run == 0:
                start = b
            run += 1
            if run >= count:
                blocks = list(range(start, start + count))
                self._mark(blocks, bm)
                return blocks
        raise NoFreeBlocksError(f"No {count} contiguous free blocks available")

    def allocate_any(self, count: int = 1) -> List[int]:
        """Allocate ``count`` free blocks, contiguous or not (all-or-nothing)."""
        if count <= 0:
            raise ValueError("Count must be positive")
        bm = self._read_bitmap()
        found: List[int] = []
        for b in range(self.superblock.data_block_count):
            idx, mask = b // 8, 1 << (b % 8)
            if (bm[idx] & mask) == 0 and self._get_absolute_block(b) not in self.avoid:
                found.append(b)
                if len(found) == count:
                    self._mark(found, bm)
                    return found
        raise NoFreeBlocksError(f"Only {len(found)} free blocks, need {count}")

    def free(self, data_blocks: List[int]) -> None:
        if not data_blocks:
            return
        bm = self._read_bitmap()
        changed = []
        seen = set()
        for b in data_blocks:
            idx, mask = self._get_bit_index(b)
            if b in seen or (bm[idx] & mask) == 0:
                raise InvalidBlockError(f"Block {b} already free")
            seen.add(b)
            bm[idx] &= ~mask & 0xFF
            changed.append(idx)
        self._write_bitmap(bm, changed)
        self.superblock.free_block_count += len(data_blocks)
        self.superblock._dirty = True
        self.avoid.update(self._get_absolute_block(b) for b in data_blocks)

    def free_absolute(self, absolute_blocks: List[int]) -> None:
        self.free([self._get_data_block_index(b) for b in absolute_blocks])

    def stage_free_absolute(self, absolute_blocks: List[int], tx_manager=None) -> None:
        """Kept for API compatibility; staging is implicit now."""
        self.free_absolute(absolute_blocks)

    # ------------------------------------------------------------ accounting
    def get_free_count(self) -> int:
        return self.superblock.free_block_count

    def get_allocated_count(self) -> int:
        return self.superblock.data_block_count - self.superblock.free_block_count

    def iter_allocated(self) -> Iterator[int]:
        bm = self._read_bitmap()
        for b in range(self.superblock.data_block_count):
            if bm[b // 8] & (1 << (b % 8)):
                yield b

    def iter_free(self) -> Iterator[int]:
        bm = self._read_bitmap()
        for b in range(self.superblock.data_block_count):
            if not bm[b // 8] & (1 << (b % 8)):
                yield b

    def count_allocated(self) -> int:
        return sum(1 for _ in self.iter_allocated())

    def flush(self) -> None:
        """No-op (kept for compatibility): the bitmap is never cached."""

    def verify_consistency(self, allocated_by_inodes: set) -> List[str]:
        errors = []
        alloc = set(self.iter_allocated())
        for b in range(self.superblock.data_block_count):
            absolute = self._get_absolute_block(b)
            in_bitmap = b in alloc
            in_inodes = absolute in allocated_by_inodes
            if in_bitmap and not in_inodes:
                errors.append(f"Block {absolute} allocated in bitmap but not referenced by any inode")
            elif in_inodes and not in_bitmap:
                errors.append(f"Block {absolute} referenced by inode but marked free in bitmap")
        free = self.superblock.data_block_count - len(alloc)
        if free != self.superblock.free_block_count:
            errors.append(f"Free block count mismatch: bitmap={free}, superblock={self.superblock.free_block_count}")
        return errors

    def get_bitmap_summary(self) -> dict:
        allocated = self.count_allocated()
        return {
            "total_blocks": self.superblock.data_block_count,
            "allocated": allocated,
            "free": self.superblock.data_block_count - allocated,
            "superblock_free_count": self.superblock.free_block_count,
            "bitmap_blocks": self.superblock.bitmap_block_count,
        }


def create_initial_bitmap(disk, superblock: Superblock) -> None:
    """Initialise the bitmap to all-free (raw write; used by format only)."""
    disk.write_blocks(superblock.bitmap_start_block,
                      b"\x00" * (superblock.bitmap_block_count * 4096))
