"""
Journal Module

Implements write-ahead journaling with transaction records, checksums, and commit records.
"""

import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Iterator, Tuple
from enum import IntEnum
from .virtual_disk import VirtualDisk
from .superblock import Superblock, crc64


class RecordType(IntEnum):
    """Journal record types."""
    TRANS_BEGIN = 0x0001
    TRANS_DATA = 0x0002
    TRANS_COMMIT = 0x0003
    TRANS_ABORT = 0x0004
    CHECKPOINT = 0x0005


class JournalError(Exception):
    """Base exception for journal errors."""
    pass


class JournalFullError(JournalError):
    """Raised when journal has no space."""
    pass


class InvalidRecordError(JournalError):
    """Raised when journal record is invalid."""
    pass


class CorruptRecordError(JournalError):
    """Raised when journal record checksum fails."""
    pass


# Journal record header format
JOURNAL_HEADER_SIZE = 24
# <QHHIIQ = txid(8) + type(2) + flags(2) + length(4) + checksum(8) = 24

@dataclass
@dataclass
class JournalRecord:
    """Base journal record."""
    txid: int
    record_type: RecordType = RecordType.TRANS_DATA
    flags: int = 0
    payload: bytes = b''
    
    def compute_checksum(self) -> int:
        """Compute checksum of header + payload."""
        header = struct.pack('<QHHI', self.txid, self.record_type, self.flags, len(self.payload))
        return crc64(header + self.payload)
    
    def to_bytes(self) -> bytes:
        """Serialize record to bytes."""
        checksum = self.compute_checksum()
        header = struct.pack('<QHHIQ', self.txid, self.record_type, self.flags, len(self.payload), checksum)
        return header + self.payload
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'JournalRecord':
        """Deserialize record from bytes."""
        if len(data) < JOURNAL_HEADER_SIZE:
            raise InvalidRecordError(f"Record too short: {len(data)} < {JOURNAL_HEADER_SIZE}")
        
        # Check for empty record (all zeros)
        if all(b == 0 for b in data[:JOURNAL_HEADER_SIZE]):
            raise InvalidRecordError("Empty record (end of journal)")
        
        txid, rec_type, flags, length, checksum = struct.unpack('<QHHIQ', data[:JOURNAL_HEADER_SIZE])
        
        # Validate record type
        if rec_type not in [rt.value for rt in RecordType]:
            raise InvalidRecordError(f"Invalid record type: {rec_type}")
        
        if len(data) < JOURNAL_HEADER_SIZE + length:
            raise InvalidRecordError(f"Record payload truncated: need {length}, have {len(data) - JOURNAL_HEADER_SIZE}")
        
        payload = data[JOURNAL_HEADER_SIZE:JOURNAL_HEADER_SIZE + length]
        
        # Verify checksum
        record = cls(txid, RecordType(rec_type), flags, payload)
        if record.compute_checksum() != checksum:
            raise CorruptRecordError(f"Checksum mismatch for txid {txid}")
        
        return record
    
    @property
    def total_size(self) -> int:
        return JOURNAL_HEADER_SIZE + len(self.payload)


@dataclass
class TransBeginRecord(JournalRecord):
    """Transaction begin record."""
    timestamp: int = 0
    
    def __post_init__(self):
        self.record_type = RecordType.TRANS_BEGIN
        self.payload = struct.pack('<QQ', self.txid, self.timestamp)
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'TransBeginRecord':
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.TRANS_BEGIN:
            raise InvalidRecordError("Not a TRANS_BEGIN record")
        if len(base.payload) != 16:
            raise InvalidRecordError(f"Invalid TRANS_BEGIN payload size: {len(base.payload)}")
        txid, timestamp = struct.unpack('<QQ', base.payload)
        return cls(txid=txid, timestamp=timestamp)


@dataclass
class TransDataRecord(JournalRecord):
    """Transaction data record (metadata after-image)."""
    target_block: int = 0
    offset: int = 0
    length: int = 0
    after_image: bytes = b''
    
    def __post_init__(self):
        self.record_type = RecordType.TRANS_DATA
        self.payload = struct.pack('<QII', self.target_block, self.offset, self.length) + self.after_image
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'TransDataRecord':
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.TRANS_DATA:
            raise InvalidRecordError("Not a TRANS_DATA record")
        if len(base.payload) < 16:
            raise InvalidRecordError(f"Invalid TRANS_DATA payload size: {len(base.payload)}")
        target_block, offset, length = struct.unpack('<QII', base.payload[:16])
        after_image = base.payload[16:]
        if len(after_image) != length:
            raise InvalidRecordError(f"After-image length mismatch: {len(after_image)} != {length}")
        return cls(
            txid=base.txid,
            flags=base.flags,
            target_block=target_block,
            offset=offset,
            length=length,
            after_image=after_image
        )


@dataclass
class TransCommitRecord(JournalRecord):
    """Transaction commit record."""
    
    def __post_init__(self):
        self.record_type = RecordType.TRANS_COMMIT
        self.payload = struct.pack('<Q', self.txid)
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'TransCommitRecord':
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.TRANS_COMMIT:
            raise InvalidRecordError("Not a TRANS_COMMIT record")
        if len(base.payload) != 8:
            raise InvalidRecordError(f"Invalid TRANS_COMMIT payload size: {len(base.payload)}")
        txid = struct.unpack('<Q', base.payload)[0]
        return cls(txid=txid)


@dataclass
class CheckpointRecord(JournalRecord):
    """Checkpoint record."""
    checkpoint_txid: int = 0
    journal_head: int = 0
    journal_tail: int = 0
    
    def __post_init__(self):
        self.record_type = RecordType.CHECKPOINT
        self.payload = struct.pack('<QQQ', self.checkpoint_txid, self.journal_head, self.journal_tail)
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'CheckpointRecord':
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.CHECKPOINT:
            raise InvalidRecordError("Not a CHECKPOINT record")
        if len(base.payload) != 24:
            raise InvalidRecordError(f"Invalid CHECKPOINT payload size: {len(base.payload)}")
        checkpoint_txid, journal_head, journal_tail = struct.unpack('<QQQ', base.payload)
        return cls(
            txid=base.txid,
            checkpoint_txid=checkpoint_txid,
            journal_head=journal_head,
            journal_tail=journal_tail
        )


@dataclass
class Transaction:
    """In-memory transaction representation."""
    txid: int
    records: List[JournalRecord] = field(default_factory=list)
    committed: bool = False
    aborted: bool = False
    timestamp: int = field(default_factory=time.time_ns)
    
    def add_data_record(self, target_block: int, offset: int, data: bytes) -> None:
        """Add a metadata after-image record."""
        record = TransDataRecord(
            txid=self.txid,
            target_block=target_block,
            offset=offset,
            length=len(data),
            after_image=data
        )
        self.records.append(record)
    
    def get_begin_record(self) -> TransBeginRecord:
        return TransBeginRecord(txid=self.txid, timestamp=self.timestamp)
    
    def get_commit_record(self) -> TransCommitRecord:
        return TransCommitRecord(txid=self.txid)
    
    def get_all_records(self) -> List[JournalRecord]:
        """Get all records in order: BEGIN, DATA..., COMMIT."""
        result = [self.get_begin_record()]
        result.extend(self.records)
        result.append(self.get_commit_record())
        return result
    
    def estimate_size(self) -> int:
        """Estimate total journal space needed."""
        size = JOURNAL_HEADER_SIZE + 16  # BEGIN
        for rec in self.records:
            size += rec.total_size
        size += JOURNAL_HEADER_SIZE + 8  # COMMIT
        return size


class Journal:
    """Manages the write-ahead journal."""
    
    def __init__(self, disk: VirtualDisk, superblock: Superblock):
        self.disk = disk
        self.superblock = superblock
        self.journal_start = superblock.journal_start_block
        self.journal_blocks = superblock.journal_block_count
        self.block_size = superblock.block_size
        self.journal_size = self.journal_blocks * self.block_size
        self._next_txid = 1
        self._crash_callback = None
    
    def set_crash_callback(self, callback) -> None:
        """Set crash injection callback."""
        self._crash_callback = callback
    
    def _maybe_crash(self, point) -> None:
        if self._crash_callback:
            self._crash_callback(point)
    
    def _get_journal_offset(self, relative_block: int, offset: int) -> int:
        """Convert journal-relative position to absolute disk offset."""
        abs_block = self.journal_start + relative_block
        return abs_block * self.block_size + offset
    
    def _read_journal_bytes(self, start_block: int, count: int) -> bytes:
        """Read raw bytes from journal region."""
        abs_start = self.journal_start + start_block
        return self.disk.read_blocks(abs_start, count)
    
    def _write_journal_bytes(self, start_block: int, data: bytes) -> None:
        """Write raw bytes to journal region."""
        abs_start = self.journal_start + start_block
        self.disk.write_blocks(abs_start, data)
    
    def get_next_txid(self) -> int:
        """Get next transaction ID."""
        txid = self._next_txid
        self._next_txid += 1
        return txid
    
    def set_next_txid(self, txid: int) -> None:
        """Set next transaction ID (for recovery)."""
        self._next_txid = max(self._next_txid, txid + 1)
    
    def write_transaction(self, transaction: Transaction) -> None:
        """
        Write a complete transaction to the journal.
        
        This writes all records sequentially, handling block boundaries.
        """
        records = transaction.get_all_records()
        
        # Check space
        total_size = sum(r.total_size for r in records)
        free_space = self.get_free_space()
        if total_size > free_space:
            raise JournalFullError(f"Transaction needs {total_size} bytes, only {free_space} available")
        
        # Write records
        head = self.superblock.journal_head
        offset = 0
        
        for record in records:
            record_bytes = record.to_bytes()
            rec_size = len(record_bytes)
            
            while rec_size > 0:
                # Space in current block
                block_offset = head * self.block_size + offset
                space_in_block = self.block_size - offset
                
                if rec_size <= space_in_block:
                    # Fits in current block
                    self._write_partial_record(head, offset, record_bytes)
                    offset += rec_size
                    rec_size = 0
                else:
                    # Fill current block, move to next
                    self._write_partial_record(head, offset, record_bytes[:space_in_block])
                    rec_size -= space_in_block
                    record_bytes = record_bytes[space_in_block:]
                    
                    # Move to next block
                    head = (head + 1) % self.journal_blocks
                    offset = 0
                    
                    # Check for wraparound collision with tail
                    if head == self.superblock.journal_tail:
                        raise JournalFullError("Journal wraparound would overwrite uncheckpointed transactions")
        
        # Update journal head
        self.superblock.journal_head = head
        self.superblock._dirty = True
        
        self._maybe_crash('after_journal_write')
    
    def _write_partial_record(self, rel_block: int, offset: int, data: bytes) -> None:
        """Write partial record data to journal block."""
        abs_block = self.journal_start + rel_block
        block_data = bytearray(self.disk.read_block(abs_block))
        block_data[offset:offset + len(data)] = data
        self.disk.write_block(abs_block, bytes(block_data))
    
    def flush_journal(self) -> None:
        """Flush journal writes to disk."""
        self.disk.flush()
        self._maybe_crash('after_journal_flush')
    
    def write_checkpoint(self, checkpoint_txid: int) -> None:
        """Write a checkpoint record."""
        record = CheckpointRecord(
            txid=0,  # Checkpoints don't have txid
            checkpoint_txid=checkpoint_txid,
            journal_head=self.superblock.journal_head,
            journal_tail=self.superblock.journal_tail
        )
        
        # Write at current head
        head = self.superblock.journal_head
        record_bytes = record.to_bytes()
        
        if len(record_bytes) > self.block_size:
            raise JournalError("Checkpoint record too large")
        
        self._write_partial_record(head, 0, record_bytes)
        self.superblock.journal_head = (head + 1) % self.journal_blocks
        self.superblock.journal_tail = self.superblock.journal_head
        self.superblock.checkpoint_txid = checkpoint_txid
        self.superblock._dirty = True
        
        self.disk.flush()
        self._maybe_crash('after_checkpoint')
    
    def get_free_space(self) -> int:
        """Get free space in journal (bytes)."""
        head = self.superblock.journal_head
        tail = self.superblock.journal_tail
        
        if head >= tail:
            used = (head - tail) * self.block_size
        else:
            used = (head + self.journal_blocks - tail) * self.block_size
        
        return self.journal_size - used - self.block_size  # Reserve 1 block
    
    def scan_transactions(self, from_tail: bool = True) -> List[Transaction]:
        """
        Scan journal for complete transactions.
        
        Args:
            from_tail: If True, start from journal_tail (checkpoint). 
                      If False, start from beginning of journal.
        
        Returns:
            List of committed transactions in order.
        """
        start_block = self.superblock.journal_tail if from_tail else 0
        start_offset = 0
        
        # Read entire journal
        journal_data = self._read_journal_bytes(0, self.journal_blocks)
        
        transactions = []
        current_tx: Optional[Transaction] = None
        offset = start_block * self.block_size + start_offset
        
        # Scan through journal
        while offset < len(journal_data):
            # Check if we have enough for a header
            if offset + JOURNAL_HEADER_SIZE > len(journal_data):
                break
            
            # Try to read record
            try:
                record = JournalRecord.from_bytes(journal_data[offset:])
            except (InvalidRecordError, CorruptRecordError, struct.error):
                # Invalid/truncated record - stop scanning
                break
            
            offset += record.total_size
            
            # Handle record
            if record.record_type == RecordType.TRANS_BEGIN:
                if current_tx is not None:
                    # Previous transaction incomplete - discard
                    pass
                begin_rec = TransBeginRecord.from_bytes(journal_data[offset - record.total_size:offset])
                current_tx = Transaction(txid=begin_rec.txid, timestamp=begin_rec.timestamp)
                self.set_next_txid(begin_rec.txid)
            
            elif record.record_type == RecordType.TRANS_DATA:
                if current_tx is not None:
                    data_rec = TransDataRecord.from_bytes(journal_data[offset - record.total_size:offset])
                    current_tx.records.append(data_rec)
            
            elif record.record_type == RecordType.TRANS_COMMIT:
                if current_tx is not None:
                    commit_rec = TransCommitRecord.from_bytes(journal_data[offset - record.total_size:offset])
                    if commit_rec.txid == current_tx.txid:
                        current_tx.committed = True
                        transactions.append(current_tx)
                    current_tx = None
            
            elif record.record_type == RecordType.TRANS_ABORT:
                if current_tx is not None:
                    current_tx.aborted = True
                    current_tx = None
            
            elif record.record_type == RecordType.CHECKPOINT:
                # Update scan position
                cp_rec = CheckpointRecord.from_bytes(journal_data[offset - record.total_size:offset])
                self.superblock.checkpoint_txid = cp_rec.checkpoint_txid
        
        return transactions
    
    def replay_transaction(self, transaction: Transaction) -> None:
        """Replay a committed transaction's data records to home locations."""
        for record in transaction.records:
            if record.record_type == RecordType.TRANS_DATA:
                data_rec = record  # type: TransDataRecord
                # Read home block
                block_data = bytearray(self.disk.read_block(data_rec.target_block))
                # Apply after-image
                end = data_rec.offset + data_rec.length
                if end > len(block_data):
                    raise JournalError(f"After-image exceeds block: {end} > {len(block_data)}")
                block_data[data_rec.offset:end] = data_rec.after_image
                # Write back
                self.disk.write_block(data_rec.target_block, bytes(block_data))
        
        # Flush home blocks
        self.disk.flush()
    
    def get_journal_stats(self) -> dict:
        """Get journal statistics."""
        head = self.superblock.journal_head
        tail = self.superblock.journal_tail
        
        if head >= tail:
            used_blocks = head - tail
        else:
            used_blocks = head + self.journal_blocks - tail
        
        return {
            'journal_blocks': self.journal_blocks,
            'used_blocks': used_blocks,
            'free_blocks': self.journal_blocks - used_blocks - 1,
            'head': head,
            'tail': tail,
            'checkpoint_txid': self.superblock.checkpoint_txid,
            'next_txid': self._next_txid,
        }


def parse_record(data: bytes) -> JournalRecord:
    """Parse a journal record from bytes (generic)."""
    return JournalRecord.from_bytes(data)