"""
Journal Module

Write-ahead *redo* journal for metadata.

On-disk layout of one transaction inside the circular journal region
(block numbers are journal-relative, the ring wraps modulo journal_blocks):

    block s      : TRANS_BEGIN record, then TRANS_DATA records back to back
    ...            (records may straddle block boundaries; the tail of the
                    last block is zero padding)
    block s+m    : TRANS_COMMIT record (always starts on a fresh block, so
                    writing the commit never rewrites blocks that hold data
                    records)

Every record is  [txid u64 | type u16 | flags u16 | length u32 | crc64 u64]
followed by `length` payload bytes; the crc covers header fields + payload.

Recovery does NOT trust the superblock's journal_head.  It starts at
journal_tail, expects txid == checkpoint_txid + 1, and follows the chain
until a record is missing, corrupt, out of sequence or a transaction has no
valid commit record.  Stale records left over from earlier laps of the ring
are rejected because their txids are not the expected next txid.
"""

import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple

from .superblock import Superblock, crc64
from .virtual_disk import CrashPoint, VirtualDisk


class RecordType(IntEnum):
    TRANS_BEGIN = 0x0001
    TRANS_DATA = 0x0002
    TRANS_COMMIT = 0x0003
    TRANS_ABORT = 0x0004
    CHECKPOINT = 0x0005  # legacy: no longer written, still decodable


class JournalError(Exception):
    pass


class JournalFullError(JournalError):
    pass


class InvalidRecordError(JournalError):
    pass


class CorruptRecordError(JournalError):
    pass


JOURNAL_HEADER_SIZE = 24  # <QHHIQ


@dataclass
class JournalRecord:
    txid: int
    record_type: RecordType = RecordType.TRANS_DATA
    flags: int = 0
    payload: bytes = b""

    def compute_checksum(self) -> int:
        header = struct.pack("<QHHI", self.txid, self.record_type, self.flags, len(self.payload))
        return crc64(header + self.payload)

    def to_bytes(self) -> bytes:
        header = struct.pack("<QHHIQ", self.txid, self.record_type, self.flags,
                             len(self.payload), self.compute_checksum())
        return header + self.payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "JournalRecord":
        if len(data) < JOURNAL_HEADER_SIZE:
            raise InvalidRecordError(f"Record too short: {len(data)} < {JOURNAL_HEADER_SIZE}")
        if not any(data[:JOURNAL_HEADER_SIZE]):
            raise InvalidRecordError("Empty record (end of journal)")
        txid, rec_type, flags, length, checksum = struct.unpack(
            "<QHHIQ", data[:JOURNAL_HEADER_SIZE])
        if rec_type not in [rt.value for rt in RecordType]:
            raise InvalidRecordError(f"Invalid record type: {rec_type}")
        if len(data) < JOURNAL_HEADER_SIZE + length:
            raise InvalidRecordError(
                f"Record payload truncated: need {length}, have {len(data) - JOURNAL_HEADER_SIZE}")
        payload = data[JOURNAL_HEADER_SIZE:JOURNAL_HEADER_SIZE + length]
        record = JournalRecord(txid, RecordType(rec_type), flags, payload)
        if record.compute_checksum() != checksum:
            raise CorruptRecordError(f"Checksum mismatch for txid {txid}")
        return record

    @property
    def total_size(self) -> int:
        return JOURNAL_HEADER_SIZE + len(self.payload)


@dataclass
class TransBeginRecord(JournalRecord):
    timestamp: int = 0

    def __post_init__(self):
        self.record_type = RecordType.TRANS_BEGIN
        self.payload = struct.pack("<QQ", self.txid, self.timestamp)

    @classmethod
    def from_bytes(cls, data: bytes) -> "TransBeginRecord":
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.TRANS_BEGIN:
            raise InvalidRecordError("Not a TRANS_BEGIN record")
        if len(base.payload) != 16:
            raise InvalidRecordError(f"Invalid TRANS_BEGIN payload size: {len(base.payload)}")
        txid, timestamp = struct.unpack("<QQ", base.payload)
        if txid != base.txid:
            raise InvalidRecordError("TRANS_BEGIN txid mismatch")
        return cls(txid=txid, timestamp=timestamp)


@dataclass
class TransDataRecord(JournalRecord):
    """Metadata after-image: bytes [offset, offset+length) of target_block."""
    target_block: int = 0
    offset: int = 0
    length: int = 0
    after_image: bytes = b""

    def __post_init__(self):
        self.record_type = RecordType.TRANS_DATA
        self.payload = struct.pack("<QII", self.target_block, self.offset, self.length) + self.after_image

    @classmethod
    def from_bytes(cls, data: bytes) -> "TransDataRecord":
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.TRANS_DATA:
            raise InvalidRecordError("Not a TRANS_DATA record")
        if len(base.payload) < 16:
            raise InvalidRecordError(f"Invalid TRANS_DATA payload size: {len(base.payload)}")
        target_block, offset, length = struct.unpack("<QII", base.payload[:16])
        after_image = base.payload[16:]
        if len(after_image) != length:
            raise InvalidRecordError(f"After-image length mismatch: {len(after_image)} != {length}")
        return cls(txid=base.txid, flags=base.flags, target_block=target_block,
                   offset=offset, length=length, after_image=after_image)


@dataclass
class TransCommitRecord(JournalRecord):
    """
    Commit record.  Payload: txid u64 + number of DATA records u32.  The
    count lets recovery detect a commit whose data records are missing.
    Legacy 8-byte payloads (no count) are still accepted (num_records = -1).
    """
    num_records: int = -1

    def __post_init__(self):
        self.record_type = RecordType.TRANS_COMMIT
        if self.num_records >= 0:
            self.payload = struct.pack("<QI", self.txid, self.num_records)
        else:
            self.payload = struct.pack("<Q", self.txid)

    @classmethod
    def from_bytes(cls, data: bytes) -> "TransCommitRecord":
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.TRANS_COMMIT:
            raise InvalidRecordError("Not a TRANS_COMMIT record")
        if len(base.payload) == 8:
            txid = struct.unpack("<Q", base.payload)[0]
            n = -1
        elif len(base.payload) == 12:
            txid, n = struct.unpack("<QI", base.payload)
        else:
            raise InvalidRecordError(f"Invalid TRANS_COMMIT payload size: {len(base.payload)}")
        if txid != base.txid:
            raise InvalidRecordError("TRANS_COMMIT txid mismatch")
        return cls(txid=txid, num_records=n)


@dataclass
class CheckpointRecord(JournalRecord):
    """Legacy checkpoint record (the superblock is now the checkpoint)."""
    checkpoint_txid: int = 0
    journal_head: int = 0
    journal_tail: int = 0

    def __post_init__(self):
        self.record_type = RecordType.CHECKPOINT
        self.payload = struct.pack("<QQQ", self.checkpoint_txid, self.journal_head, self.journal_tail)

    @classmethod
    def from_bytes(cls, data: bytes) -> "CheckpointRecord":
        base = JournalRecord.from_bytes(data)
        if base.record_type != RecordType.CHECKPOINT:
            raise InvalidRecordError("Not a CHECKPOINT record")
        if len(base.payload) != 24:
            raise InvalidRecordError(f"Invalid CHECKPOINT payload size: {len(base.payload)}")
        c, h, t = struct.unpack("<QQQ", base.payload)
        return cls(txid=base.txid, checkpoint_txid=c, journal_head=h, journal_tail=t)


@dataclass
class Transaction:
    """In-memory transaction representation."""
    txid: int
    records: List[TransDataRecord] = field(default_factory=list)
    committed: bool = False
    aborted: bool = False
    timestamp: int = field(default_factory=time.time_ns)
    start_block: int = -1       # journal-relative block of BEGIN (set on write/scan)
    commit_block: int = -1      # journal-relative block of COMMIT

    def add_data_record(self, target_block: int, offset: int, data: bytes) -> None:
        self.records.append(TransDataRecord(
            txid=self.txid, target_block=target_block, offset=offset,
            length=len(data), after_image=data))

    def get_begin_record(self) -> TransBeginRecord:
        return TransBeginRecord(txid=self.txid, timestamp=self.timestamp)

    def get_commit_record(self) -> TransCommitRecord:
        return TransCommitRecord(txid=self.txid, num_records=len(self.records))

    def get_journal_data_records(self) -> List[JournalRecord]:
        return [self.get_begin_record()] + list(self.records)

    def get_all_records(self) -> List[JournalRecord]:
        return [self.get_begin_record()] + list(self.records) + [self.get_commit_record()]

    def estimate_size(self) -> int:
        size = JOURNAL_HEADER_SIZE + 16
        size += sum(r.total_size for r in self.records)
        size += JOURNAL_HEADER_SIZE + 12
        return size

    def blocks_touched(self) -> List[int]:
        return sorted({r.target_block for r in self.records})


@dataclass
class ScanResult:
    committed: List[Transaction] = field(default_factory=list)
    end_block: int = 0                      # journal-relative block after last committed tx
    incomplete: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class Journal:
    """Manages the circular write-ahead journal."""

    def __init__(self, disk: VirtualDisk, superblock: Superblock):
        self.disk = disk
        self.superblock = superblock
        self.journal_start = superblock.journal_start_block
        self.journal_blocks = superblock.journal_block_count
        self.block_size = superblock.block_size
        self.journal_size = self.journal_blocks * self.block_size
        # in-memory ring state (persisted tail/checkpoint live in the superblock)
        self.tail = superblock.journal_tail
        self.head = superblock.journal_tail
        self.checkpoint_txid = superblock.checkpoint_txid
        self.uncheckpointed: List[int] = []
        self._next_txid = superblock.checkpoint_txid + 1
        self.events = None  # optional EventLog

    # ----------------------------------------------------------- utilities
    def _emit(self, kind: str, message: str = "", **kw) -> None:
        if self.events is not None:
            self.events.emit(kind, message, **kw)

    def _fire(self, point: CrashPoint, info: Optional[dict] = None) -> None:
        self.disk.fire(point, info)

    def set_crash_callback(self, callback) -> None:  # kept for API compat
        self.disk.set_crash_callback(callback)

    def _abs(self, rel_block: int) -> int:
        return self.journal_start + (rel_block % self.journal_blocks)

    # -------------------------------------------------------------- txids
    def get_next_txid(self) -> int:
        txid = self._next_txid
        self._next_txid += 1
        return txid

    def release_txid(self, txid: int) -> None:
        """Give back the most recent txid after an aborted transaction."""
        if self._next_txid == txid + 1:
            self._next_txid = txid

    def set_next_txid(self, txid: int) -> None:
        """Ensure the next txid is greater than ``txid``."""
        self._next_txid = max(self._next_txid, txid + 1)

    # -------------------------------------------------------------- space
    def used_blocks(self) -> int:
        return (self.head - self.tail) % self.journal_blocks

    def free_blocks(self) -> int:
        return self.journal_blocks - 1 - self.used_blocks()   # one block reserved

    def get_free_space(self) -> int:
        return self.free_blocks() * self.block_size

    def _data_stream(self, tx: Transaction) -> bytes:
        stream = b"".join(r.to_bytes() for r in tx.get_journal_data_records())
        pad = (-len(stream)) % self.block_size
        return stream + b"\x00" * pad

    def blocks_needed(self, tx: Transaction) -> int:
        n = len(self._data_stream(tx)) // self.block_size
        return n + 1  # + commit block

    def reserve(self, tx: Transaction) -> None:
        """Make sure the transaction fits, checkpointing if that helps."""
        need = self.blocks_needed(tx)
        if need > self.journal_blocks - 1:
            raise JournalFullError(
                f"Transaction needs {need} journal blocks but the journal only holds "
                f"{self.journal_blocks - 1}")
        if need > self.free_blocks() and self.uncheckpointed:
            self.checkpoint()
        if need > self.free_blocks():
            raise JournalFullError(
                f"Transaction needs {need} journal blocks, only {self.free_blocks()} free")

    # --------------------------------------------------------- write path
    def write_transaction(self, transaction: Transaction) -> Tuple[int, int]:
        """
        Write BEGIN + DATA records (no commit) at the journal head.
        Returns (commit_block, 0): where the commit record belongs.
        """
        need = self.blocks_needed(transaction)
        if need > self.free_blocks():
            raise JournalFullError(
                f"Transaction needs {need} blocks, only {self.free_blocks()} free")
        self._fire(CrashPoint.BEFORE_JOURNAL_WRITE)
        stream = self._data_stream(transaction)
        nblocks = len(stream) // self.block_size
        transaction.start_block = self.head
        for i in range(nblocks):
            self.disk.write_block(self._abs(self.head + i),
                                  stream[i * self.block_size:(i + 1) * self.block_size])
        transaction.commit_block = (self.head + nblocks) % self.journal_blocks
        self._fire(CrashPoint.AFTER_JOURNAL_WRITE)
        self._emit("journal_write",
                   f"tx {transaction.txid}: {len(transaction.records)} data record(s) written "
                   f"to journal blocks {transaction.start_block}..{(transaction.commit_block - 1) % self.journal_blocks}",
                   txid=transaction.txid, start_block=transaction.start_block,
                   blocks=nblocks, records=len(transaction.records))
        return transaction.commit_block, 0

    def flush_journal(self) -> None:
        self._fire(CrashPoint.BEFORE_JOURNAL_FLUSH)
        self.disk.flush("journal")
        self._fire(CrashPoint.AFTER_JOURNAL_FLUSH)
        self._emit("journal_flush", "journal blocks flushed to stable storage")

    def write_commit(self, transaction: Transaction) -> None:
        """Write the commit record into its own block (not yet durable)."""
        if transaction.commit_block < 0:
            raise JournalError("write_commit before write_transaction")
        self._fire(CrashPoint.BEFORE_COMMIT_WRITE)
        raw = transaction.get_commit_record().to_bytes()
        block = raw + b"\x00" * (self.block_size - len(raw))
        self.disk.write_block(self._abs(transaction.commit_block), block)
        self._fire(CrashPoint.AFTER_COMMIT_WRITE)
        self._emit("commit_write", f"tx {transaction.txid}: commit record written to journal block "
                   f"{transaction.commit_block} (not yet durable)", txid=transaction.txid,
                   block=transaction.commit_block)

    def flush_commit(self) -> None:
        self._fire(CrashPoint.BEFORE_COMMIT_FLUSH)
        self.disk.flush("commit")
        self._fire(CrashPoint.AFTER_COMMIT_FLUSH)

    def mark_committed(self, transaction: Transaction) -> None:
        """Called once the commit record is durable: the ring head advances."""
        transaction.committed = True
        self.head = (transaction.commit_block + 1) % self.journal_blocks
        self.uncheckpointed.append(transaction.txid)

    def checkpoint(self, txid: Optional[int] = None, phase: str = "checkpoint") -> None:
        """
        Persist the new journal tail / checkpoint txid in the superblock.

        PRECONDITION (the caller's job): every committed transaction up to
        ``txid`` has been applied to its home location AND those home blocks
        have been flushed.  Only then may journal space be reclaimed.
        """
        if txid is None:
            txid = self.uncheckpointed[-1] if self.uncheckpointed else self.checkpoint_txid
        self._fire(CrashPoint.BEFORE_CHECKPOINT)
        # Build the image from the COMMITTED on-disk superblock (not the in-memory
        # one, which may already contain the uncommitted counts of a running tx).
        disk_sb = Superblock.from_bytes(self.disk.read_block(0))
        disk_sb.journal_tail = self.head
        disk_sb.journal_head = self.head
        disk_sb.checkpoint_txid = txid
        disk_sb._checksum = disk_sb.compute_checksum()
        self.disk.write_block(0, disk_sb.to_bytes(include_checksum=True))
        self.disk.flush(phase)
        sb = self.superblock
        sb.journal_tail = self.head
        sb.journal_head = self.head
        sb.checkpoint_txid = txid
        self.tail = self.head
        self.checkpoint_txid = txid
        self.uncheckpointed = [t for t in self.uncheckpointed if t > txid]
        self._fire(CrashPoint.AFTER_CHECKPOINT)
        self._emit("checkpoint", f"checkpoint: txid <= {txid} safely applied; journal tail -> block {self.tail}",
                   txid=txid, tail=self.tail)

    write_checkpoint = checkpoint  # legacy name

    # ----------------------------------------------------------- scanning
    def _ring_bytes(self) -> bytes:
        return self.disk.read_blocks(self.journal_start, self.journal_blocks)

    def _ring_read(self, ring: bytes, off: int, n: int) -> bytes:
        total = len(ring)
        off %= total
        if off + n <= total:
            return ring[off:off + n]
        return ring[off:] + ring[:(off + n) - total]

    def _try_record(self, ring: bytes, off: int) -> Tuple[Optional[JournalRecord], str]:
        """Parse one record at a ring byte offset.  Returns (record, '') or (None, reason)."""
        hdr = self._ring_read(ring, off, JOURNAL_HEADER_SIZE)
        if not any(hdr):
            return None, "empty"
        try:
            length = struct.unpack("<QHHIQ", hdr)[3]
        except struct.error:
            return None, "corrupt"
        if length > len(ring):
            return None, "corrupt"
        try:
            rec = JournalRecord.from_bytes(self._ring_read(ring, off, JOURNAL_HEADER_SIZE + length))
        except CorruptRecordError:
            return None, "checksum"
        except InvalidRecordError:
            return None, "invalid"
        return rec, ""

    def _validate_data_record(self, rec: TransDataRecord) -> Optional[str]:
        sb = self.superblock
        if rec.length <= 0:
            return "zero-length after-image"
        if rec.offset + rec.length > self.block_size:
            return f"after-image [{rec.offset},{rec.offset + rec.length}) exceeds block size"
        if rec.target_block >= sb.total_blocks:
            return f"target block {rec.target_block} beyond end of disk"
        if self.journal_start <= rec.target_block < self.journal_start + self.journal_blocks:
            return f"target block {rec.target_block} lies inside the journal region"
        return None

    def _parse_tx_at(self, ring: bytes, block: int, expect_txid: Optional[int]
                     ) -> Tuple[Optional[Transaction], str, int]:
        """
        Parse one transaction chain starting at ring block ``block``.
        Returns (tx, status, blocks_consumed); status is 'committed', 'no-begin',
        'out-of-sequence' (benign), or a description of why the tx is incomplete/corrupt.
        """
        bs = self.block_size
        off = block * bs
        rec, why = self._try_record(ring, off)
        if rec is None:
            return None, ("end" if why == "empty" else f"bad-begin:{why}"), 0
        if rec.record_type != RecordType.TRANS_BEGIN:
            return None, "no-begin", 0
        if expect_txid is not None and rec.txid != expect_txid:
            return None, "out-of-sequence", 0
        try:
            begin = TransBeginRecord.from_bytes(self._ring_read(ring, off, rec.total_size))
        except JournalError:
            return None, "bad-begin:invalid", 0
        tx = Transaction(txid=begin.txid, timestamp=begin.timestamp, start_block=block)
        off += rec.total_size
        total = len(ring)
        limit = off + total  # never walk further than one lap
        while True:
            if off >= limit:
                return tx, "incomplete: ran past one journal lap", 0
            if off % bs != 0:
                rest = bs - (off % bs)
                if not any(self._ring_read(ring, off, rest)):
                    off += rest  # zero padding -> next block boundary
            rec, why = self._try_record(ring, off)
            if rec is None:
                reason = {"empty": "no commit record (journal ends)",
                          "checksum": "corrupt record (checksum mismatch)",
                          "invalid": "corrupt record (invalid header)",
                          "corrupt": "corrupt record"}[why]
                return tx, f"incomplete: {reason}", 0
            if rec.txid != tx.txid:
                return tx, f"incomplete: record of tx {rec.txid} inside tx {tx.txid}", 0
            if rec.record_type == RecordType.TRANS_DATA:
                try:
                    data = TransDataRecord.from_bytes(self._ring_read(ring, off, rec.total_size))
                except JournalError as e:
                    return tx, f"incomplete: bad data record ({e})", 0
                problem = self._validate_data_record(data)
                if problem:
                    return tx, f"invalid: {problem}", 0
                tx.records.append(data)
                off += rec.total_size
                continue
            if rec.record_type == RecordType.TRANS_COMMIT:
                try:
                    commit = TransCommitRecord.from_bytes(self._ring_read(ring, off, rec.total_size))
                except JournalError as e:
                    return tx, f"incomplete: bad commit record ({e})", 0
                if commit.num_records >= 0 and commit.num_records != len(tx.records):
                    return tx, (f"incomplete: commit says {commit.num_records} data records, "
                                f"found {len(tx.records)}"), 0
                tx.committed = True
                tx.commit_block = (off // bs) % self.journal_blocks
                end_off = off + rec.total_size
                consumed_blocks = (end_off - 1) // bs + 1 - block
                return tx, "committed", consumed_blocks
            return tx, f"incomplete: unexpected record type {rec.record_type.name}", 0

    def scan(self) -> ScanResult:
        """Find every valid committed, not-yet-checkpointed transaction."""
        res = ScanResult()
        ring = self._ring_bytes()
        pos = self.superblock.journal_tail
        expect = self.superblock.checkpoint_txid + 1
        consumed = 0
        res.end_block = pos
        while consumed < self.journal_blocks - 1:
            tx, status, blocks = self._parse_tx_at(ring, pos, expect)
            if status == "committed":
                res.committed.append(tx)
                pos = (tx.commit_block + 1) % self.journal_blocks
                consumed += blocks
                expect += 1
                res.end_block = pos
                continue
            if status.startswith("invalid:"):
                res.errors.append(f"tx {tx.txid} at journal block {pos}: {status[8:].strip()}")
                res.incomplete.append({"txid": tx.txid, "start_block": pos, "reason": status})
            elif status.startswith("incomplete"):
                res.incomplete.append({"txid": tx.txid, "start_block": pos, "reason": status})
                if "corrupt" in status:
                    res.warnings.append(
                        f"tx {tx.txid} at journal block {pos}: {status} - transaction discarded")
            elif status.startswith("bad-begin"):
                res.warnings.append(
                    f"journal block {pos}: unreadable record where tx {expect} was expected "
                    f"({status.split(':')[1]}) - treated as end of journal")
            # 'end', 'no-begin', 'out-of-sequence' => normal end of the live log
            break
        return res

    def scan_transactions(self, from_tail: bool = True) -> List[Transaction]:
        """Committed, not-yet-checkpointed transactions in order (API kept for callers)."""
        return self.scan().committed

    # ------------------------------------------------------------- replay
    def replay_transaction(self, transaction: Transaction, flush: bool = True) -> None:
        """Redo a committed transaction's patches onto the home locations (idempotent)."""
        for rec in transaction.records:
            problem = self._validate_data_record(rec)
            if problem:
                raise JournalError(f"tx {transaction.txid}: {problem}")
            block = bytearray(self.disk.read_block(rec.target_block))
            block[rec.offset:rec.offset + rec.length] = rec.after_image
            self.disk.write_block(rec.target_block, bytes(block))
        if flush:
            self.disk.flush("recovery")

    # ------------------------------------------------------- introspection
    def scan_region(self) -> List[Dict[str, Any]]:
        """
        Forensic decode of the whole journal region: every transaction chain
        that parses, whether or not it is live.  Pure read of on-disk bytes.
        """
        ring = self._ring_bytes()
        out: List[Dict[str, Any]] = []
        covered = set()
        for b in range(self.journal_blocks):
            if b in covered:
                continue
            tx, status, blocks = self._parse_tx_at(ring, b, None)
            if tx is None:
                continue
            if status == "committed":
                nblocks = blocks
            else:
                nblocks = max(1, len(self._data_stream(tx)) // self.block_size)
            for i in range(nblocks):
                covered.add((b + i) % self.journal_blocks)
            ckpt = self.superblock.checkpoint_txid
            if status == "committed":
                state = "checkpointed" if tx.txid <= ckpt else "committed-pending-replay"
            else:
                state = "uncommitted"
            out.append({
                "txid": tx.txid,
                "start_block": b,
                "blocks": nblocks,
                "committed": status == "committed",
                "status": status,
                "state": state,
                "timestamp": tx.timestamp,
                "records": [{"type": "TRANS_BEGIN", "crc_ok": True}] + [
                    {"type": "TRANS_DATA", "target_block": r.target_block, "offset": r.offset,
                     "length": r.length, "crc_ok": True} for r in tx.records
                ] + ([{"type": "TRANS_COMMIT", "crc_ok": True, "block": tx.commit_block}]
                     if status == "committed" else []),
            })
        out.sort(key=lambda t: t["txid"])
        return out

    def get_journal_stats(self) -> dict:
        used = self.used_blocks()
        return {
            "journal_blocks": self.journal_blocks,
            "used_blocks": used,
            "free_blocks": self.journal_blocks - used - 1,
            "head": self.head,
            "tail": self.tail,
            "checkpoint_txid": self.checkpoint_txid,
            "next_txid": self._next_txid,
            "uncheckpointed": list(self.uncheckpointed),
        }


def parse_record(data: bytes) -> JournalRecord:
    return JournalRecord.from_bytes(data)
