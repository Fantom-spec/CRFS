"""
Recovery Module

Implements crash recovery: journal scanning, validation, replay, and checkpoint management.
"""

import time
from dataclasses import dataclass
from typing import List, Dict, Set, Optional, Tuple
from .virtual_disk import VirtualDisk, CrashPoint
from .superblock import Superblock, SuperblockManager
from .journal import Journal, Transaction as JournalTransaction, RecordType, TransDataRecord
from .inode import InodeTable, Inode
from .block_allocator import BlockAllocator
from .consistency_checker import ConsistencyChecker


class RecoveryError(Exception):
    """Base exception for recovery errors."""
    pass


@dataclass
class RecoveryResult:
    """Result of recovery operation."""
    success: bool
    transactions_replayed: int
    transactions_discarded: int
    errors: List[str]
    warnings: List[str]
    duration_ms: float
    
    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return (f"Recovery {status}: {self.transactions_replayed} replayed, "
                f"{self.transactions_discarded} discarded, "
                f"{len(self.errors)} errors, {len(self.warnings)} warnings "
                f"in {self.duration_ms:.2f}ms")


class RecoveryManager:
    """Manages filesystem crash recovery."""
    
    def __init__(
        self,
        disk: VirtualDisk,
        superblock: Superblock,
        journal: Journal,
        inode_table: InodeTable,
        block_allocator: BlockAllocator
    ):
        self.disk = disk
        self.superblock = superblock
        self.journal = journal
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self._crash_callback = None
        self._crash_points_enabled = True
    
    def set_crash_callback(self, callback) -> None:
        """Set crash injection callback."""
        self._crash_callback = callback
    
    def enable_crash_points(self, enabled: bool = True) -> None:
        """Enable/disable crash injection."""
        self._crash_points_enabled = enabled
        self.disk.enable_crash_points(enabled)
    
    def _maybe_crash(self, point: CrashPoint) -> None:
        if self._crash_points_enabled and self._crash_callback:
            self._crash_callback(point)
    
    def recover(self, run_checker: bool = True) -> RecoveryResult:
        """
        Perform full crash recovery.
        
        Steps:
        1. Validate superblock
        2. Scan journal for committed transactions
        3. Replay committed transactions
        4. Flush recovered metadata
        5. Update checkpoint
        6. Run consistency checker (optional)
        """
        start_time = time.time()
        errors = []
        warnings = []
        transactions_replayed = 0
        transactions_discarded = 0
        
        try:
            # Step 1: Validate superblock
            self._maybe_crash(CrashPoint.DURING_RECOVERY_SCAN)
            self.superblock.validate()
            
            # Step 2: Scan journal
            committed_txs = self._scan_journal()
            
            # Count discarded (incomplete) transactions
            all_txs = self.journal.scan_transactions(from_tail=False)
            incomplete = [tx for tx in all_txs if not tx.committed]
            transactions_discarded = len(incomplete)
            
            if incomplete:
                warnings.append(f"Discarded {len(incomplete)} incomplete transactions")
            
            # Step 3: Replay committed transactions
            self._maybe_crash(CrashPoint.DURING_RECOVERY_REPLAY)
            for tx in committed_txs:
                try:
                    self.journal.replay_transaction(tx)
                    transactions_replayed += 1
                except Exception as e:
                    errors.append(f"Failed to replay transaction {tx.txid}: {e}")
            
            # Step 4: Flush recovered metadata
            self.disk.flush()
            
            # Step 5: Update checkpoint to last replayed transaction
            if committed_txs:
                last_txid = committed_txs[-1].txid
                self.journal.write_checkpoint(last_txid)
                self.superblock.checkpoint_txid = last_txid
                self.superblock._dirty = True
            
            # Write superblock
            self.superblock._checksum = self.superblock.compute_checksum()
            self.disk.write_block(0, self.superblock.to_bytes())
            
            # Step 6: Run consistency checker
            checker_errors = []
            if run_checker:
                checker = ConsistencyChecker(
                    self.disk, self.superblock, self.inode_table, 
                    self.block_allocator, self.journal
                )
                checker_errors = checker.check_all()
                if checker_errors:
                    errors.extend(checker_errors)
            
            success = len(errors) == 0
            duration_ms = (time.time() - start_time) * 1000
            
            return RecoveryResult(
                success=success,
                transactions_replayed=transactions_replayed,
                transactions_discarded=transactions_discarded,
                errors=errors,
                warnings=warnings,
                duration_ms=duration_ms
            )
            
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            errors.append(f"Recovery failed: {e}")
            return RecoveryResult(
                success=False,
                transactions_replayed=transactions_replayed,
                transactions_discarded=transactions_discarded,
                errors=errors,
                warnings=warnings,
                duration_ms=duration_ms
            )
    
    def _scan_journal(self) -> List[JournalTransaction]:
        """Scan journal for committed transactions."""
        return self.journal.scan_transactions(from_tail=True)
    
    def quick_recovery(self) -> RecoveryResult:
        """Quick recovery: only replay transactions after last checkpoint."""
        # This is the same as full recovery since we always scan from tail
        return self.recover(run_checker=False)
    
    def needs_recovery(self) -> bool:
        """Check if recovery is needed (journal has uncheckpointed transactions)."""
        return self.superblock.journal_head != self.superblock.journal_tail
    
    def get_journal_state(self) -> Dict:
        """Get journal state for diagnostics."""
        return self.journal.get_journal_stats()
    
    def force_checkpoint(self) -> None:
        """Force a checkpoint at current journal head."""
        self.journal.write_checkpoint(self.superblock.checkpoint_txid)
        self.disk.flush()


def run_recovery(
    disk: VirtualDisk,
    superblock: Superblock,
    journal: Journal,
    inode_table: InodeTable,
    block_allocator: BlockAllocator,
    run_checker: bool = True
) -> RecoveryResult:
    """Convenience function to run recovery."""
    manager = RecoveryManager(disk, superblock, journal, inode_table, block_allocator)
    return manager.recover(run_checker=run_checker)