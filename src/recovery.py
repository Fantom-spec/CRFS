"""
Recovery Module

Crash recovery = redo of committed-but-not-checkpointed transactions.

    1. read + verify the superblock from disk (checksum)
    2. scan the journal from journal_tail, expecting txid checkpoint_txid+1
    3. replay every valid COMMITTED transaction in order (idempotent: each
       DATA record is an absolute after-image of a byte range)
    4. flush the home locations
    5. checkpoint: new tail / checkpoint_txid in the superblock, flush

Uncommitted, torn, corrupt or out-of-sequence transactions are never
replayed.  Running recovery twice (or crashing in the middle of it) yields
the same result.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .block_allocator import BlockAllocator
from .consistency_checker import ConsistencyChecker
from .inode import InodeTable
from .journal import Journal, JournalError
from .superblock import Superblock, SuperblockManager
from .virtual_disk import CrashPoint, VirtualDisk


class RecoveryError(Exception):
    pass


@dataclass
class RecoveryResult:
    success: bool
    transactions_replayed: int
    transactions_discarded: int
    errors: List[str]
    warnings: List[str]
    duration_ms: float
    replayed: List[Dict[str, Any]] = field(default_factory=list)
    discarded: List[Dict[str, Any]] = field(default_factory=list)
    checkpoint_txid: int = 0
    checker_errors: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return (f"Recovery {status}: {self.transactions_replayed} replayed, "
                f"{self.transactions_discarded} discarded, "
                f"{len(self.errors)} errors, {len(self.warnings)} warnings "
                f"in {self.duration_ms:.2f}ms")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "transactions_replayed": self.transactions_replayed,
            "transactions_discarded": self.transactions_discarded,
            "errors": self.errors, "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 3),
            "replayed": self.replayed, "discarded": self.discarded,
            "checkpoint_txid": self.checkpoint_txid,
            "checker_errors": self.checker_errors,
        }


class RecoveryManager:
    def __init__(self, disk: VirtualDisk, superblock: Superblock, journal: Journal,
                 inode_table: InodeTable, block_allocator: BlockAllocator):
        self.disk = disk
        self.superblock = superblock
        self.journal = journal
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self.events = None

    def _emit(self, kind, message="", **kw):
        if self.events is not None:
            self.events.emit(kind, message, **kw)

    def set_crash_callback(self, callback) -> None:
        self.disk.set_crash_callback(callback)

    def enable_crash_points(self, enabled: bool = True) -> None:
        self.disk.enable_crash_points(enabled)

    # ------------------------------------------------------------------
    def _reload_superblock(self) -> None:
        """Re-read block 0 from disk into the shared in-memory superblock object."""
        fresh = SuperblockManager(self.disk).read()   # validates magic + checksum
        for name in fresh.__dataclass_fields__:
            setattr(self.superblock, name, getattr(fresh, name))

    def needs_recovery(self) -> bool:
        """True when the journal holds committed transactions not yet checkpointed."""
        try:
            self._reload_superblock()
            return bool(self.journal.scan().committed)
        except Exception:
            return True

    def recover(self, run_checker: bool = False) -> RecoveryResult:
        start = time.time()
        errors: List[str] = []
        warnings: List[str] = []
        replayed: List[Dict[str, Any]] = []
        discarded: List[Dict[str, Any]] = []
        self._emit("recovery_start", "recovery started")
        try:
            self.disk.fire(CrashPoint.DURING_RECOVERY_SCAN)
            self._reload_superblock()
            j = self.journal
            j.tail = j.head = self.superblock.journal_tail
            j.checkpoint_txid = self.superblock.checkpoint_txid
            scan = j.scan()
            warnings.extend(scan.warnings)
            errors.extend(scan.errors)
            for inc in scan.incomplete:
                discarded.append(inc)
                self._emit("recovery_discard",
                           f"tx {inc['txid']} discarded: {inc['reason']}",
                           severity="warning", txid=inc["txid"], reason=inc["reason"])
            self._emit("recovery_scan",
                       f"journal scan: {len(scan.committed)} committed transaction(s) to replay, "
                       f"{len(discarded)} discarded", committed=[t.txid for t in scan.committed])
            if errors:
                raise RecoveryError("; ".join(errors))

            self.disk.fire(CrashPoint.DURING_RECOVERY_REPLAY)
            for tx in scan.committed:
                j.replay_transaction(tx, flush=False)
                blocks = sorted({r.target_block for r in tx.records})
                replayed.append({"txid": tx.txid, "blocks": blocks, "records": len(tx.records)})
                self._emit("recovery_replay",
                           f"replayed tx {tx.txid}: {len(tx.records)} record(s) -> blocks {blocks}",
                           txid=tx.txid, blocks=blocks)
            self.disk.flush("recovery")
            self.disk.fire(CrashPoint.AFTER_RECOVERY_REPLAY)

            # in-memory superblock must now reflect the replayed home blocks
            self._reload_superblock()
            if scan.committed:
                j.head = scan.end_block
                j.uncheckpointed = [t.txid for t in scan.committed]
                j.checkpoint(scan.committed[-1].txid)
            j.tail = j.head = self.superblock.journal_tail
            j.checkpoint_txid = self.superblock.checkpoint_txid
            j.uncheckpointed = []
            j._next_txid = max(j._next_txid, self.superblock.checkpoint_txid + 1)

            checker_errors: List[str] = []
            if run_checker:
                checker_errors = ConsistencyChecker(
                    self.disk, self.superblock, self.inode_table,
                    self.block_allocator, self.journal).check_all()
                errors.extend(checker_errors)

            res = RecoveryResult(
                success=not errors, transactions_replayed=len(replayed),
                transactions_discarded=len(discarded), errors=errors, warnings=warnings,
                duration_ms=(time.time() - start) * 1000, replayed=replayed, discarded=discarded,
                checkpoint_txid=self.superblock.checkpoint_txid, checker_errors=checker_errors)
            self._emit("recovery_done", str(res), severity="info" if res.success else "error",
                       replayed=len(replayed), discarded=len(discarded))
            return res
        except Exception as e:
            from .virtual_disk import CrashInjectedError
            if isinstance(e, CrashInjectedError):
                raise
            errors.append(f"Recovery failed: {e}")
            self._emit("recovery_done", f"recovery FAILED: {e}", severity="error")
            return RecoveryResult(False, len(replayed), len(discarded), errors, warnings,
                                  (time.time() - start) * 1000, replayed, discarded)

    def quick_recovery(self) -> RecoveryResult:
        return self.recover(run_checker=False)

    def get_journal_state(self) -> Dict:
        return self.journal.get_journal_stats()

    def force_checkpoint(self) -> None:
        if self.journal.uncheckpointed:
            self.journal.checkpoint()


def run_recovery(disk, superblock, journal, inode_table, block_allocator,
                 run_checker: bool = True) -> RecoveryResult:
    return RecoveryManager(disk, superblock, journal, inode_table,
                           block_allocator).recover(run_checker=run_checker)
