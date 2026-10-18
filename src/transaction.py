"""
Transaction Module

Manages transaction lifecycle: begin, metadata tracking, commit, abort.
"""

import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable
from .virtual_disk import VirtualDisk, CrashPoint
from .superblock import Superblock
from .journal import Journal, Transaction as JournalTransaction, TransDataRecord, RecordType
from .inode import Inode, InodeTable
from .block_allocator import BlockAllocator


class TransactionError(Exception):
    """Base exception for transaction errors."""
    pass


class TransactionAbortedError(TransactionError):
    """Raised when transaction is aborted."""
    pass


@dataclass
class MetadataChange:
    """Represents a single metadata block change."""
    block_num: int
    offset: int
    old_data: bytes
    new_data: bytes
    
    @property
    def length(self) -> int:
        return len(self.new_data)


class TransactionManager:
    """
    Manages filesystem transactions with write-ahead journaling.
    
    Protocol:
    1. begin() - Start new transaction
    2. Track metadata changes via add_metadata_change()
    3. commit() - Write to journal, flush, write commit record, flush, apply to home, flush, checkpoint
    4. abort() - Discard changes
    """
    
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
        
        self._current_tx: Optional[JournalTransaction] = None
        self._pending_changes: List[MetadataChange] = []
        self._crash_callback: Optional[Callable[[CrashPoint], None]] = None
        self._crash_points_enabled = True
    
    def set_crash_callback(self, callback: Optional[Callable[[CrashPoint], None]]) -> None:
        """Set crash injection callback."""
        self._crash_callback = callback
        self.journal.set_crash_callback(callback)
        self.disk.set_crash_callback(callback)
    
    def enable_crash_points(self, enabled: bool = True) -> None:
        """Enable/disable crash injection."""
        self._crash_points_enabled = enabled
        self.disk.enable_crash_points(enabled)
    
    def _maybe_crash(self, point: CrashPoint) -> None:
        if self._crash_points_enabled and self._crash_callback:
            self._crash_callback(point)
    
    def begin(self) -> JournalTransaction:
        """Begin a new transaction."""
        if self._current_tx is not None:
            raise TransactionError("Transaction already in progress")
        
        txid = self.journal.get_next_txid()
        self._current_tx = JournalTransaction(txid=txid)
        self._pending_changes = []
        self._maybe_crash(CrashPoint.BEFORE_JOURNAL_WRITE)
        return self._current_tx
    
    def add_metadata_change(self, block_num: int, offset: int, new_data: bytes) -> None:
        """
        Record a metadata change for the current transaction.
        
        Reads the current block content as old_data for potential abort/recovery.
        """
        if self._current_tx is None:
            raise TransactionError("No active transaction")
        
        # Read current block content
        old_data = self.disk.read_block(block_num)
        old_slice = old_data[offset:offset + len(new_data)]
        
        change = MetadataChange(
            block_num=block_num,
            offset=offset,
            old_data=old_slice,
            new_data=new_data
        )
        self._pending_changes.append(change)
        
        # Add to journal transaction
        self._current_tx.add_data_record(block_num, offset, new_data)
    
    def add_full_block_change(self, block_num: int, new_data: bytes) -> None:
        """Record a full block change."""
        if len(new_data) != self.disk.block_size:
            raise ValueError(f"Data size {len(new_data)} != block size {self.disk.block_size}")
        self.add_metadata_change(block_num, 0, new_data)
    
    def commit(self) -> int:
        """
        Commit the current transaction.
        
        Returns the transaction ID.
        
        Protocol:
        1. Write all transaction records to journal
        2. Flush journal
        3. Write commit record
        4. Flush commit record
        5. Apply changes to home locations
        6. Flush home locations
        7. Checkpoint
        """
        if self._current_tx is None:
            raise TransactionError("No active transaction")
        
        tx = self._current_tx
        txid = tx.txid
        
        try:
            # Phase 1: Write transaction records to journal
            self._maybe_crash(CrashPoint.BEFORE_JOURNAL_WRITE)
            self.journal.write_transaction(tx)
            self._maybe_crash(CrashPoint.AFTER_JOURNAL_WRITE)
            
            # Phase 2: Flush journal
            self.journal.flush_journal()
            self._maybe_crash(CrashPoint.AFTER_JOURNAL_FLUSH)
            
            # Phase 3: Write commit record
            self._maybe_crash(CrashPoint.BEFORE_COMMIT_WRITE)
            commit_record = tx.get_commit_record()
            self._write_commit_record(commit_record)
            self._maybe_crash(CrashPoint.AFTER_COMMIT_WRITE)
            
            # Phase 4: Flush commit record
            self.disk.flush()
            self._maybe_crash(CrashPoint.AFTER_COMMIT_FLUSH)
            
            # Phase 5: Apply changes to home locations
            self._maybe_crash(CrashPoint.BEFORE_HOME_WRITE)
            self._apply_changes()
            self._maybe_crash(CrashPoint.AFTER_HOME_WRITE)
            
            # Phase 6: Flush home locations
            self.disk.flush()
            self._maybe_crash(CrashPoint.AFTER_HOME_FLUSH)
            
            # Phase 7: Checkpoint
            self._maybe_crash(CrashPoint.BEFORE_CHECKPOINT)
            self.journal.write_checkpoint(txid)
            self._maybe_crash(CrashPoint.AFTER_CHECKPOINT)
            
            # Success
            self._current_tx = None
            self._pending_changes = []
            return txid
            
        except Exception as e:
            # On any error, abort
            self.abort()
            raise
    
    def _write_commit_record(self, commit_record) -> None:
        """Write commit record to journal at current head."""
        head = self.superblock.journal_head
        record_bytes = commit_record.to_bytes()
        
        if len(record_bytes) > self.disk.block_size:
            raise TransactionError("Commit record too large")
        
        # Write at current head position
        abs_block = self.journal.journal_start + head
        block_data = bytearray(self.disk.read_block(abs_block))
        block_data[0:len(record_bytes)] = record_bytes
        self.disk.write_block(abs_block, bytes(block_data))
        
        # Advance head
        self.superblock.journal_head = (head + 1) % self.journal.journal_blocks
        self.superblock._dirty = True
    
    def _apply_changes(self) -> None:
        """Apply all pending changes to home locations."""
        for change in self._pending_changes:
            # Read home block
            block_data = bytearray(self.disk.read_block(change.block_num))
            # Apply change
            end = change.offset + change.length
            if end > len(block_data):
                raise TransactionError(f"Change exceeds block boundary: {end} > {len(block_data)}")
            block_data[change.offset:end] = change.new_data
            # Write back
            self.disk.write_block(change.block_num, bytes(block_data))
    
    def abort(self) -> None:
        """Abort the current transaction, discarding all changes."""
        if self._current_tx is None:
            return  # No transaction to abort
        
        # Write abort record to journal (optional, for clarity)
        try:
            abort_record = Journal.Transaction(txid=self._current_tx.txid, aborted=True)
            # Just discard the transaction
        except Exception:
            pass
        
        self._current_tx = None
        self._pending_changes = []
    
    def is_active(self) -> bool:
        """Check if a transaction is in progress."""
        return self._current_tx is not None
    
    def get_current_txid(self) -> Optional[int]:
        """Get current transaction ID."""
        return self._current_tx.txid if self._current_tx else None
    
    def get_pending_changes_count(self) -> int:
        """Get number of pending metadata changes."""
        return len(self._pending_changes)


class TransactionContext:
    """Context manager for transactions."""
    
    def __init__(self, tx_manager: TransactionManager):
        self.tx_manager = tx_manager
        self.tx = None
    
    def __enter__(self) -> JournalTransaction:
        self.tx = self.tx_manager.begin()
        return self.tx
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self.tx_manager.commit()
        else:
            self.tx_manager.abort()
        return False  # Don't suppress exceptions


def create_transaction_manager(
    disk: VirtualDisk,
    superblock: Superblock,
    journal: Journal,
    inode_table: InodeTable,
    block_allocator: BlockAllocator
) -> TransactionManager:
    """Factory function to create transaction manager."""
    return TransactionManager(disk, superblock, journal, inode_table, block_allocator)