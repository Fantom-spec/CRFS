"""
Virtual Disk Module

Block I/O on a disk-image file, with an explicit DURABILITY MODEL and
fault-injection hooks.

Durability model
----------------
``cache_mode="volatile"`` (default)
    Models a disk with a volatile write-back cache.  ``write_block`` only
    places the block in an in-memory pending set; the data reaches the image
    file only during ``flush()``.  If the process dies (or ``crash()`` is
    called) before a flush, the pending writes are LOST.  ``flush()`` writes
    pending blocks one at a time (ascending block number) and then fsyncs, so
    a crash in the middle of a flush leaves an arbitrary prefix of the pending
    blocks on disk (a "torn" multi-block write).  A single block write is
    atomic, except when a torn-sector write is injected on purpose.
    This is the strictest model the simulator supports: the journaling
    protocol is only correct if it calls ``flush()`` at the right moments.

``cache_mode="writethrough"``
    Every ``write_block`` is written straight to the image file with
    O_SYNC (the behaviour of the original simulator).  A write is durable the
    moment ``write_block`` returns; ``flush()`` is just an fsync.

What is NOT modelled: reordering of block writes *between* flushes (all
pending blocks are flushed ascending), bit rot, lost fsync, torn superblock
writes in normal operation (the injector can force one for testing).
"""

import os
import threading
from enum import Enum
from typing import Callable, Dict, Optional


class CrashPoint(Enum):
    """Crash injection points."""
    NONE = 0
    BEFORE_READ = 1
    AFTER_READ = 2
    BEFORE_WRITE = 3
    AFTER_WRITE = 4
    BEFORE_FLUSH = 5
    AFTER_FLUSH = 6
    BEFORE_JOURNAL_WRITE = 7
    AFTER_JOURNAL_WRITE = 8
    BEFORE_JOURNAL_FLUSH = 9
    AFTER_JOURNAL_FLUSH = 10
    BEFORE_COMMIT_WRITE = 11
    AFTER_COMMIT_WRITE = 12
    BEFORE_COMMIT_FLUSH = 13
    AFTER_COMMIT_FLUSH = 14
    BEFORE_HOME_WRITE = 15
    AFTER_HOME_WRITE = 16
    BEFORE_HOME_FLUSH = 17
    AFTER_HOME_FLUSH = 18
    BEFORE_CHECKPOINT = 19
    AFTER_CHECKPOINT = 20
    DURING_RECOVERY_SCAN = 21
    DURING_RECOVERY_REPLAY = 22
    # "Mid-flush" points: fire between the physical block writes of a flush,
    # so a crash here leaves only some of the pending blocks on disk.
    DURING_JOURNAL_WRITE = 23
    DURING_COMMIT_WRITE = 24
    DURING_HOME_WRITE = 25
    DURING_CHECKPOINT_WRITE = 26
    AFTER_RECOVERY_REPLAY = 27


# flush phase -> mid-flush crash point
PHASE_POINTS = {
    "journal": CrashPoint.DURING_JOURNAL_WRITE,
    "commit": CrashPoint.DURING_COMMIT_WRITE,
    "home": CrashPoint.DURING_HOME_WRITE,
    "checkpoint": CrashPoint.DURING_CHECKPOINT_WRITE,
    "recovery": CrashPoint.DURING_RECOVERY_REPLAY,
}


class VirtualDiskError(Exception):
    """Base exception for virtual disk errors."""


class DiskNotOpenError(VirtualDiskError):
    """Raised when disk operations are attempted on closed/crashed disk."""


class InvalidBlockError(VirtualDiskError):
    """Raised when block number is out of bounds."""


class CrashInjectedError(VirtualDiskError):
    """Raised when a crash is injected (exception mode)."""

    def __init__(self, message: str = "", point: Optional[CrashPoint] = None):
        super().__init__(message)
        self.point = point


CrashHook = Callable[[CrashPoint, Optional[dict]], None]


class VirtualDisk:
    DEFAULT_BLOCK_SIZE = 4096
    DEFAULT_DISK_SIZE = 16 * 1024 * 1024

    def __init__(self, path: str, block_size: int = DEFAULT_BLOCK_SIZE,
                 disk_size: int = DEFAULT_DISK_SIZE, create: bool = False,
                 cache_mode: str = "volatile"):
        if cache_mode not in ("volatile", "writethrough"):
            raise ValueError("cache_mode must be 'volatile' or 'writethrough'")
        self.path = path
        self.block_size = block_size
        self.cache_mode = cache_mode
        self._fd: Optional[int] = None
        self._lock = threading.RLock()
        self._crash_callback: Optional[CrashHook] = None
        self._crash_points_enabled = True
        self._pending: Dict[int, bytes] = {}
        self.dead = False
        self.stats = {"reads": 0, "writes": 0, "flushes": 0, "blocks_flushed": 0}

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

    # ------------------------------------------------------------ lifecycle
    def _open_flags(self) -> int:
        flags = os.O_RDWR
        if self.cache_mode == "writethrough":
            flags |= os.O_SYNC
        return flags

    def _create_disk(self) -> None:
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(self.path, "wb") as f:
            f.truncate(self.disk_size)
        self._fd = os.open(self.path, self._open_flags())

    def _open_disk(self) -> None:
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Disk image not found: {self.path}")
        self.disk_size = os.stat(self.path).st_size
        if self.disk_size % self.block_size != 0:
            raise VirtualDiskError(
                f"Disk size {self.disk_size} is not a multiple of block size {self.block_size}")
        self.total_blocks = self.disk_size // self.block_size
        self._fd = os.open(self.path, self._open_flags())

    def close(self) -> None:
        """Orderly close.  Pending (unflushed) writes are NOT written."""
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._pending.clear()

    def crash(self) -> None:
        """Simulate sudden power loss: drop every unflushed write, close."""
        with self._lock:
            self._pending.clear()
            self.dead = True
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None

    def __enter__(self) -> "VirtualDisk":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    @property
    def pending_blocks(self) -> int:
        return len(self._pending)

    # ---------------------------------------------------------------- hooks
    def _check_block(self, block_num: int, count: int = 1) -> None:
        if block_num < 0 or count < 0 or block_num + count > self.total_blocks:
            raise InvalidBlockError(
                f"Block {block_num} (count={count}) out of range [0, {self.total_blocks})")

    def fire(self, point: CrashPoint, info: Optional[dict] = None) -> None:
        """Invoke the crash hook if enabled."""
        if self._crash_points_enabled and self._crash_callback:
            self._crash_callback(point, info)

    _maybe_crash = fire  # backwards-compatible alias

    def set_crash_callback(self, callback: Optional[CrashHook]) -> None:
        self._crash_callback = callback

    def enable_crash_points(self, enabled: bool = True) -> None:
        self._crash_points_enabled = enabled

    # ------------------------------------------------------------------ I/O
    def _require_open(self) -> None:
        if self._fd is None:
            raise DiskNotOpenError("Disk not open (closed or crashed)")

    def read_block(self, block_num: int) -> bytes:
        with self._lock:
            self._require_open()
            self._check_block(block_num)
            self.fire(CrashPoint.BEFORE_READ)
            data = self._pending.get(block_num)
            if data is None:
                data = os.pread(self._fd, self.block_size, block_num * self.block_size)
                if len(data) != self.block_size:
                    raise VirtualDiskError(
                        f"Short read: expected {self.block_size}, got {len(data)}")
            self.stats["reads"] += 1
            self.fire(CrashPoint.AFTER_READ)
            return data

    def write_block(self, block_num: int, data: bytes) -> None:
        with self._lock:
            self._require_open()
            self._check_block(block_num)
            if len(data) != self.block_size:
                raise ValueError(f"Data size {len(data)} != block size {self.block_size}")
            self.fire(CrashPoint.BEFORE_WRITE)
            if self.cache_mode == "volatile":
                self._pending[block_num] = bytes(data)
            else:
                written = os.pwrite(self._fd, data, block_num * self.block_size)
                if written != self.block_size:
                    raise VirtualDiskError(
                        f"Short write: expected {self.block_size}, wrote {written}")
            self.stats["writes"] += 1
            self.fire(CrashPoint.AFTER_WRITE)

    def read_blocks(self, start_block: int, count: int) -> bytes:
        with self._lock:
            self._require_open()
            self._check_block(start_block, count)
            return b"".join(self.read_block(start_block + i) for i in range(count))

    def write_blocks(self, start_block: int, data: bytes) -> None:
        with self._lock:
            self._require_open()
            if len(data) % self.block_size != 0:
                raise ValueError("Data size must be multiple of block size")
            count = len(data) // self.block_size
            self._check_block(start_block, count)
            for i in range(count):
                self.write_block(start_block + i,
                                 data[i * self.block_size:(i + 1) * self.block_size])

    def flush(self, phase: Optional[str] = None) -> None:
        """
        Make all pending writes durable.

        ``phase`` ('journal', 'commit', 'home', 'checkpoint', 'recovery')
        labels the flush so a crash can be injected *between* the physical
        block writes of that particular flush.
        """
        with self._lock:
            self._require_open()
            self.fire(CrashPoint.BEFORE_FLUSH, {"phase": phase})
            if self._pending:
                blocks = sorted(self._pending)
                point = PHASE_POINTS.get(phase or "")
                for i, blk in enumerate(blocks):
                    if point is not None:
                        self.fire(point, {"phase": phase, "block": blk,
                                          "index": i + 1, "total": len(blocks)})
                    os.pwrite(self._fd, self._pending[blk], blk * self.block_size)
                    self.stats["blocks_flushed"] += 1
                    del self._pending[blk]
            os.fsync(self._fd)
            self.stats["flushes"] += 1
            self.fire(CrashPoint.AFTER_FLUSH, {"phase": phase})

    def flush_range(self, start_block: int, count: int) -> None:
        self.flush()

    def torn_write(self, block_num: int, nbytes: int) -> None:
        """
        Fault injection: persist only the first ``nbytes`` of the pending
        version of ``block_num`` over the old on-disk contents (a torn sector
        write), directly to the image file.
        """
        with self._lock:
            if self._fd is None:
                return
            new = self._pending.get(block_num)
            if new is None:
                return
            os.pwrite(self._fd, new[:nbytes], block_num * self.block_size)
            os.fsync(self._fd)

    def zero_blocks(self, start_block: int, count: int) -> None:
        self.write_blocks(start_block, b"\x00" * (count * self.block_size))

    def get_size(self) -> int:
        return self.disk_size

    def get_block_size(self) -> int:
        return self.block_size

    def get_total_blocks(self) -> int:
        return self.total_blocks


def create_disk_image(path: str, block_size: int = VirtualDisk.DEFAULT_BLOCK_SIZE,
                      disk_size: int = VirtualDisk.DEFAULT_DISK_SIZE,
                      cache_mode: str = "volatile") -> VirtualDisk:
    return VirtualDisk(path, block_size, disk_size, create=True, cache_mode=cache_mode)


def open_disk_image(path: str, block_size: int = VirtualDisk.DEFAULT_BLOCK_SIZE,
                    cache_mode: str = "volatile") -> VirtualDisk:
    return VirtualDisk(path, block_size, create=False, cache_mode=cache_mode)
