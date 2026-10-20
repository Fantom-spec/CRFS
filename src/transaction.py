"""
Transaction Module

All filesystem METADATA changes go through here.

* ``StagingIO`` sits between the metadata code (inode table, bitmap allocator,
  directories, superblock image) and the raw disk.  While a transaction is
  active, block writes are *staged* in an in-memory overlay and reads see the
  staged version first.  Nothing touches a home location until the
  transaction's commit record is durable.  Outside a transaction metadata
  writes are rejected, so no code path can silently bypass the journal.
* File DATA blocks are written directly into newly allocated (previously free)
  blocks before the commit ("ordered" mode) and flushed together with the
  journal records, so a committed inode never points at unwritten data.
* ``TransactionManager.commit`` implements the protocol:

    1 PREPARE        diff staged blocks against disk -> redo records
    2 JOURNAL WRITE  BEGIN + DATA records into the ring
    3 FLUSH          journal (+ data blocks) durable
    4 COMMIT WRITE   commit record in its own block
    5 FLUSH          commit durable   <-- the transaction is now committed
    6 APPLY          staged blocks written to their home locations
    7 FLUSH          home locations durable
    8 CHECKPOINT     superblock tail/txid advanced, journal space reclaimed
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .block_allocator import BlockAllocator
from .inode import InodeTable
from .journal import Journal, Transaction as JournalTransaction
from .superblock import Superblock
from .virtual_disk import CrashInjectedError, CrashPoint, VirtualDisk


class TransactionError(Exception):
    pass


class TransactionAbortedError(TransactionError):
    pass


class ReadOnlyError(TransactionError):
    pass


class StagingIO:
    """Block I/O proxy: overlay while a transaction is active."""

    def __init__(self, disk: VirtualDisk):
        self.disk = disk
        self._overlay: Optional[Dict[int, bytes]] = None
        self.allow_raw_writes = False   # only set during format

    # -- passthrough properties
    @property
    def block_size(self) -> int:
        return self.disk.block_size

    @property
    def total_blocks(self) -> int:
        return self.disk.total_blocks

    @property
    def is_open(self) -> bool:
        return self.disk.is_open

    @property
    def in_transaction(self) -> bool:
        return self._overlay is not None

    def begin(self) -> None:
        self._overlay = {}

    def discard(self) -> None:
        self._overlay = None

    def staged(self) -> Dict[int, bytes]:
        return dict(self._overlay or {})

    # -- I/O
    def read_block(self, block_num: int) -> bytes:
        if self._overlay is not None and block_num in self._overlay:
            return self._overlay[block_num]
        return self.disk.read_block(block_num)

    def read_blocks(self, start: int, count: int) -> bytes:
        return b"".join(self.read_block(start + i) for i in range(count))

    def write_block(self, block_num: int, data: bytes) -> None:
        if len(data) != self.disk.block_size:
            raise ValueError(f"Data size {len(data)} != block size {self.disk.block_size}")
        if self._overlay is not None:
            self.disk._check_block(block_num)
            self._overlay[block_num] = bytes(data)
        elif self.allow_raw_writes:
            self.disk.write_block(block_num, data)
        else:
            raise TransactionError(
                f"metadata write to block {block_num} outside a transaction is not allowed")

    def write_blocks(self, start: int, data: bytes) -> None:
        bs = self.disk.block_size
        if len(data) % bs:
            raise ValueError("Data size must be multiple of block size")
        for i in range(len(data) // bs):
            self.write_block(start + i, data[i * bs:(i + 1) * bs])

    def flush(self, phase: Optional[str] = None) -> None:
        self.disk.flush(phase)


@dataclass
class MetadataChange:
    block_num: int
    offset: int
    old_data: bytes
    new_data: bytes

    @property
    def length(self) -> int:
        return len(self.new_data)


class TransactionManager:
    """Owns the transaction lifecycle: begin / stage / commit / abort."""

    def __init__(self, disk: VirtualDisk, superblock: Superblock, journal: Journal,
                 inode_table: InodeTable, block_allocator: BlockAllocator,
                 io: Optional[StagingIO] = None):
        self.disk = disk
        self.superblock = superblock
        self.journal = journal
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self.io = io if io is not None else StagingIO(disk)
        self.events = None
        self.read_only = False
        self.checkpoint_policy = "eager"       # or "lazy"
        self.lazy_threshold = 0.5              # fraction of the journal
        self._current_tx: Optional[JournalTransaction] = None
        self._sb_snapshot: Optional[Superblock] = None
        self._data_blocks: Set[int] = set()
        self._crashed = False
        self.last_tx_info: Optional[dict] = None

    # ---------------------------------------------------------------- misc
    def _emit(self, kind: str, message: str = "", **kw) -> None:
        if self.events is not None:
            self.events.emit(kind, message, **kw)

    def _fire(self, point: CrashPoint, info: Optional[dict] = None) -> None:
        self.disk.fire(point, info)

    def set_crash_callback(self, callback) -> None:
        self.disk.set_crash_callback(callback)

    def enable_crash_points(self, enabled: bool = True) -> None:
        self.disk.enable_crash_points(enabled)

    def is_active(self) -> bool:
        return self._current_tx is not None

    def get_current_txid(self) -> Optional[int]:
        return self._current_tx.txid if self._current_tx else None

    def get_pending_changes_count(self) -> int:
        return len(self.io.staged())

    # --------------------------------------------------------------- begin
    def begin(self) -> JournalTransaction:
        if self.read_only:
            raise ReadOnlyError("filesystem is mounted read-only (recovery has not run)")
        if self._current_tx is not None:
            raise TransactionError("Transaction already in progress")
        if self.disk.dead:
            raise TransactionError("disk has crashed; remount required")
        txid = self.journal.get_next_txid()
        self._current_tx = JournalTransaction(txid=txid)
        self._sb_snapshot = self.superblock.copy()
        self._data_blocks = set()
        self.block_allocator.avoid = set()
        self.block_allocator.fresh = set()
        self.io.begin()
        self._emit("tx_begin", f"tx {txid} started", txid=txid)
        return self._current_tx

    # ---------------------------------------------------- data block writes
    def write_data_block(self, block_num: int, data: bytes) -> None:
        """Write a FILE DATA block (ordered mode: before commit, flushed with the journal)."""
        if self._current_tx is None:
            raise TransactionError("No active transaction")
        if len(data) != self.disk.block_size:
            raise ValueError("data block must be exactly one block")
        sb = self.superblock
        if not (sb.data_start_block <= block_num < sb.data_start_block + sb.data_block_count):
            raise TransactionError(f"block {block_num} is not in the data region")
        if self.block_allocator.is_allocated_absolute(block_num) and \
                block_num not in self.block_allocator.fresh:
            raise TransactionError(
                f"refusing to overwrite block {block_num}: it is not newly allocated in this transaction")
        self.disk.write_block(block_num, data)
        self._data_blocks.add(block_num)

    # -------------------------------------------------------------- commit
    def _stage_superblock(self) -> None:
        sb = self.superblock
        sb._checksum = sb.compute_checksum()
        image = sb.to_bytes(include_checksum=True)
        base = self.disk.read_block(0)
        if image != base:
            self.io._overlay[0] = image

    def _build_records(self, tx: JournalTransaction) -> Dict[int, bytes]:
        """Diff every staged block against the disk; returns {block: full new image}."""
        images = {}
        for blk, new in sorted(self.io.staged().items()):
            old = self.disk.read_block(blk)
            if old == new:
                continue
            first = next(i for i in range(len(new)) if new[i] != old[i])
            last = len(new) - 1 - next(i for i in range(len(new)) if new[-1 - i] != old[-1 - i])
            tx.add_data_record(blk, first, new[first:last + 1])
            images[blk] = new
        return images

    def commit(self) -> int:
        if self._current_tx is None:
            raise TransactionError("No active transaction")
        tx = self._current_tx
        txid = tx.txid
        durable = False
        try:
            images: Dict[int, bytes] = {}
            for _attempt in (0, 1):
                tx.records = []
                self._stage_superblock()
                images = self._build_records(tx)
                if not images and not self._data_blocks:
                    self._finish(txid, committed=False)
                    return 0
                before = self.journal.checkpoint_txid
                self.journal.reserve(tx)      # may checkpoint to make room
                if self.journal.checkpoint_txid == before:
                    break                      # (else: superblock image changed, rebuild)
            self._emit("tx_prepare", f"tx {txid}: {len(images)} metadata block(s) staged, "
                       f"{len(self._data_blocks)} data block(s) written",
                       txid=txid, blocks=sorted(images), data_blocks=sorted(self._data_blocks))
            # --- 2/3: journal write + flush (data blocks are pending in the same flush)
            self.journal.write_transaction(tx)
            self.journal.flush_journal()
            # --- 4/5: commit record + flush  => durable commit point
            self.journal.write_commit(tx)
            self.journal.flush_commit()
            durable = True
            self.journal.mark_committed(tx)
            self._emit("commit_flush", f"tx {txid}: COMMITTED (commit record durable)", txid=txid)
            # --- 6/7: apply to home + flush
            self._fire(CrashPoint.BEFORE_HOME_WRITE)
            for blk, img in images.items():
                self.disk.write_block(blk, img)
            self._fire(CrashPoint.AFTER_HOME_WRITE)
            self._emit("home_apply", f"tx {txid}: applied {len(images)} block(s) to home locations "
                       f"{sorted(images)}", txid=txid, blocks=sorted(images))
            self._fire(CrashPoint.BEFORE_HOME_FLUSH)
            self.disk.flush("home")
            self._fire(CrashPoint.AFTER_HOME_FLUSH)
            self._emit("home_flush", f"tx {txid}: home blocks flushed", txid=txid)
            # --- 8: checkpoint
            used_frac = self.journal.used_blocks() / max(1, self.journal.journal_blocks)
            if self.checkpoint_policy == "eager" or used_frac >= self.lazy_threshold:
                self.journal.checkpoint(txid)
            self._finish(txid, committed=True, blocks=sorted(images))
            return txid
        except CrashInjectedError:
            self._crashed = True
            self._current_tx = None
            self.io.discard()
            raise
        except Exception:
            if durable:
                # committed but not fully applied: memory no longer matches disk
                self._current_tx = None
                self.io.discard()
                self.read_only = True
                self._emit("error", f"tx {txid} committed but apply/checkpoint failed; "
                           f"remount to recover", severity="error", txid=txid)
                raise
            self.abort()
            raise

    def _finish(self, txid: int, committed: bool, blocks=None) -> None:
        self.last_tx_info = {"txid": txid, "committed": committed, "blocks": blocks or []}
        self._current_tx = None
        self._sb_snapshot = None
        self._data_blocks = set()
        self.block_allocator.avoid = set()
        self.block_allocator.fresh = set()
        self.io.discard()
        if not committed:
            self.journal.release_txid(txid)

    def _crash_cleanup(self) -> None:
        """The 'process' died mid-transaction: drop in-memory state, touch no disk."""
        self._crashed = True
        self._current_tx = None
        self.io.discard()

    # --------------------------------------------------------------- abort
    def abort(self) -> None:
        """Discard the transaction: nothing was written to a home location."""
        if self._current_tx is None:
            return
        txid = self._current_tx.txid
        if self._sb_snapshot is not None:
            import dataclasses
            for f in dataclasses.fields(self.superblock):
                setattr(self.superblock, f.name, getattr(self._sb_snapshot, f.name))
        self._current_tx.aborted = True
        self._finish(txid, committed=False)
        self._emit("tx_abort", f"tx {txid} aborted; staged changes discarded", severity="warning", txid=txid)

    # ----------------------------------------------- legacy staging helpers
    def add_metadata_change(self, block_num: int, offset: int, new_data: bytes) -> None:
        if self._current_tx is None:
            raise TransactionError("No active transaction")
        blk = bytearray(self.io.read_block(block_num))
        blk[offset:offset + len(new_data)] = new_data
        self.io.write_block(block_num, bytes(blk))

    def add_full_block_change(self, block_num: int, new_data: bytes) -> None:
        self.io.write_block(block_num, new_data)

    def stage_inode_change(self, inode) -> None:
        self.inode_table.write_inode(inode)


class TransactionContext:
    """``with TransactionContext(tm):`` commits on success, aborts on error."""

    def __init__(self, tx_manager: TransactionManager):
        self.tx_manager = tx_manager
        self.tx = None

    def __enter__(self) -> JournalTransaction:
        self.tx = self.tx_manager.begin()
        return self.tx

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self.tx_manager.commit()
        elif isinstance(exc_val, CrashInjectedError):
            self.tx_manager._crash_cleanup()
        else:
            self.tx_manager.abort()
        return False


def create_transaction_manager(disk, superblock, journal, inode_table, block_allocator):
    return TransactionManager(disk, superblock, journal, inode_table, block_allocator)
