"""
Filesystem Module

High-level operations.  EVERY mutating operation runs inside a journaled
transaction (see transaction.py); an operation either commits completely or
leaves no trace.
"""

import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from .block_allocator import BlockAllocator, NoFreeBlocksError, create_initial_bitmap
from .consistency_checker import CheckResult, ConsistencyChecker
from .crash_injector import (CrashInjector, CrashMode, get_global_injector,
                             maybe_crash, set_global_injector)
from .directory import (DIR_ENTRY_SIZE, DIR_TYPE_DIR, DIR_TYPE_FILE, CorruptDirectoryError,
                        Directory, DirectoryError, DirectoryFullError, DirEntry,
                        InvalidNameError, initialize_root_directory, new_directory_block,
                        validate_name)
from .events import EventLog
from .inode import (DIRECT_POINTERS, INODE_SIZE, INODES_PER_BLOCK, MAX_FILE_SIZE, Inode,
                    InodeTable, InodeTableFullError, InodeType, InvalidInodeError)
from .journal import Journal, JournalError, JournalFullError
from .recovery import RecoveryManager, RecoveryResult, run_recovery
from .superblock import Superblock, SuperblockManager
from .transaction import (ReadOnlyError, StagingIO, TransactionContext, TransactionError,
                          TransactionManager)
from .virtual_disk import (CrashInjectedError, DiskNotOpenError, VirtualDisk, VirtualDiskError,
                           create_disk_image, open_disk_image)


class FileSystemError(Exception):
    """Base exception for filesystem errors."""


class PathError(FileSystemError):
    pass


class NotFoundError(FileSystemError):
    pass


class AlreadyExistsError(FileSystemError):
    pass


class NotDirectoryError(FileSystemError):
    pass


class IsDirectoryError(FileSystemError):
    pass


class DirectoryNotEmptyError(FileSystemError):
    pass


class DiskFullError(FileSystemError):
    pass


class FileTooLargeError(FileSystemError):
    pass


class MountError(FileSystemError):
    pass


class NotMountedError(FileSystemError):
    pass


class ReadOnlyFilesystemError(FileSystemError):
    pass


def _map_error(e: Exception) -> Exception:
    """Translate low-level engine errors into user-facing FileSystemErrors."""
    if isinstance(e, FileSystemError):
        return e
    if isinstance(e, NoFreeBlocksError):
        return DiskFullError(f"disk full: {e}")
    if isinstance(e, InodeTableFullError):
        return DiskFullError(f"no free inodes: {e}")
    if isinstance(e, DirectoryFullError):
        return DiskFullError(f"directory full: {e}")
    if isinstance(e, JournalFullError):
        return FileSystemError(f"journal full: {e}")
    if isinstance(e, InvalidNameError):
        return PathError(str(e))
    if isinstance(e, ReadOnlyError):
        return ReadOnlyFilesystemError(str(e))
    if isinstance(e, CorruptDirectoryError):
        return FileSystemError(f"corrupt directory: {e}")
    if isinstance(e, DiskNotOpenError):
        return NotMountedError(str(e))
    return e


class FileSystem:
    def __init__(self, disk_path: str, cache_mode: str = "volatile",
                 checkpoint: str = "eager", events: Optional[EventLog] = None):
        self.disk_path = disk_path
        self.cache_mode = cache_mode
        self.checkpoint_policy = checkpoint
        self.events = events if events is not None else EventLog()
        self.disk: Optional[VirtualDisk] = None
        self.io: Optional[StagingIO] = None
        self.superblock: Optional[Superblock] = None
        self.superblock_mgr: Optional[SuperblockManager] = None
        self.block_allocator: Optional[BlockAllocator] = None
        self.inode_table: Optional[InodeTable] = None
        self.journal: Optional[Journal] = None
        self.tx_manager: Optional[TransactionManager] = None
        self.recovery_mgr: Optional[RecoveryManager] = None
        self.checker: Optional[ConsistencyChecker] = None
        self._mounted = False
        self._crash_injector: Optional[CrashInjector] = None
        self.last_recovery: Optional[RecoveryResult] = None
        self.last_check: Optional[Dict[str, Any]] = None

    # =================================================================== setup
    def _init_components(self) -> None:
        self.io = StagingIO(self.disk)
        self.block_allocator = BlockAllocator(self.io, self.superblock)
        self.inode_table = InodeTable(self.io, self.superblock)
        self.journal = Journal(self.disk, self.superblock)
        self.tx_manager = TransactionManager(self.disk, self.superblock, self.journal,
                                             self.inode_table, self.block_allocator, io=self.io)
        self.tx_manager.checkpoint_policy = self.checkpoint_policy
        self.recovery_mgr = RecoveryManager(self.disk, self.superblock, self.journal,
                                            self.inode_table, self.block_allocator)
        self.checker = ConsistencyChecker(self.disk, self.superblock, self.inode_table,
                                          self.block_allocator, self.journal)
        for c in (self.journal, self.tx_manager, self.recovery_mgr):
            c.events = self.events

    @property
    def root_dir(self) -> Directory:
        return self._dir(1)

    def _dir(self, inode_num: int) -> Directory:
        inode = self.inode_table.read_inode(inode_num)
        d = Directory(inode, self.inode_table, self.block_allocator, self.io)
        if inode_num == 1:
            d._parent_inode_num = 1
        return d

    def format(self, disk_size: int = 16 * 1024 * 1024, block_size: int = 4096,
               journal_blocks: int = 128, inode_blocks: int = 64, volume_label: str = "") -> None:
        if block_size != 4096:
            raise ValueError("only 4096-byte blocks are supported")
        if journal_blocks < 4:
            raise ValueError("journal needs at least 4 blocks")
        if inode_blocks < 1:
            raise ValueError("need at least 1 inode-table block")
        if disk_size // block_size < journal_blocks + inode_blocks + 8:
            raise ValueError("disk too small for the requested journal / inode table")
        if self.disk is not None and self.disk.is_open:
            self.disk.close()
        self.disk = create_disk_image(self.disk_path, block_size, disk_size, self.cache_mode)
        self.superblock_mgr = SuperblockManager(self.disk)
        self.superblock = self.superblock_mgr.create_default(
            disk_size=disk_size, journal_blocks=journal_blocks,
            inode_blocks=inode_blocks, volume_label=volume_label)
        self._init_components()
        self.io.allow_raw_writes = True
        self.superblock_mgr.write()
        create_initial_bitmap(self.disk, self.superblock)
        self.disk.flush()
        self.io.allow_raw_writes = False
        # the root directory is created through a normal journaled transaction
        with TransactionContext(self.tx_manager):
            initialize_root_directory(self.inode_table, self.block_allocator, self.io, self.tx_manager)
        self._mounted = True
        self.events.emit("mount", f"formatted and mounted {self.disk_path}", cache_mode=self.cache_mode)

    def mount(self, run_recovery: bool = True, pre_recovery_hook=None) -> None:
        if self._mounted:
            raise FileSystemError("Already mounted")
        try:
            self.disk = open_disk_image(self.disk_path, cache_mode=self.cache_mode)
        except FileNotFoundError as e:
            raise MountError(str(e)) from e
        try:
            self.superblock_mgr = SuperblockManager(self.disk)
            self.superblock = self.superblock_mgr.read()
        except (ValueError, VirtualDiskError) as e:
            self.disk.close()
            raise MountError(f"not a valid CRFS image: {e}") from e
        self._init_components()
        if pre_recovery_hook is not None:
            pre_recovery_hook(self)      # e.g. arm a crash injector before recovery runs
        if run_recovery:
            result = self.recovery_mgr.recover()
            self.last_recovery = result
            if not result.success:
                self.disk.close()
                raise FileSystemError(f"Recovery failed: {result.errors}")
        else:
            self.tx_manager.read_only = True
            self.events.emit("mount", "mounted READ-ONLY without recovery", severity="warning")
        try:
            self.inode_table.read_inode(1)
        except Exception as e:
            self.disk.close()
            raise MountError(f"root inode unreadable: {e}") from e
        self._mounted = True
        self.events.emit("mount", f"mounted {self.disk_path}", cache_mode=self.cache_mode,
                         recovered=bool(self.last_recovery and self.last_recovery.transactions_replayed))

    def unmount(self) -> None:
        if not self._mounted:
            return
        try:
            if self.disk is None or self.disk.dead or not self.disk.is_open:
                pass
            else:
                if self.tx_manager.is_active():
                    self.tx_manager.abort()
                if self.journal.uncheckpointed and not self.tx_manager.read_only:
                    self.journal.checkpoint()
                self.disk.flush()
                self.disk.close()
        finally:
            if self.disk is not None and self.disk.is_open:
                self.disk.close()
            self._mounted = False
            self.events.emit("unmount", "unmounted")

    def __enter__(self) -> "FileSystem":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.unmount()

    @property
    def is_mounted(self) -> bool:
        return self._mounted

    def _require_mounted(self) -> None:
        if not self._mounted:
            raise NotMountedError("filesystem is not mounted")

    # ========================================================== crash injection
    def enable_crash_injection(self, mode: CrashMode = CrashMode.EXCEPTION) -> CrashInjector:
        inj = CrashInjector(mode)
        inj.attach_disk(self.disk)
        inj.events = self.events
        self._crash_injector = inj
        set_global_injector(inj)
        self.disk.set_crash_callback(inj.maybe_crash)
        return inj

    def disable_crash_injection(self) -> None:
        if self._crash_injector:
            self._crash_injector.disable()
        if self.disk is not None:
            self.disk.set_crash_callback(None)
        self._crash_injector = None

    # ======================================================== path resolution
    @staticmethod
    def _split(path: str) -> List[str]:
        if not isinstance(path, str):
            raise PathError("path must be a string")
        parts = [p for p in path.strip().split("/") if p != ""]
        for p in parts:
            if p in (".", ".."):
                raise PathError("'.' and '..' are not supported in paths")
            if "\x00" in p:
                raise PathError("NUL in path")
        return parts

    def _resolve_dir(self, parts: List[str], shown: str) -> Directory:
        d = self._dir(1)
        walked = ""
        for part in parts:
            walked += "/" + part
            e = d.get_entry(part)
            if e is None:
                raise NotFoundError(f"No such directory: {walked}")
            inode = self.inode_table.read_inode(e.inode_num)
            if not inode.is_directory:
                raise NotDirectoryError(f"Not a directory: {walked}")
            d = self._dir(e.inode_num)
        return d

    def _resolve_path(self, path: str) -> Tuple[Directory, str, Optional[int]]:
        """(parent directory, final name, inode number or None)."""
        parts = self._split(path)
        if not parts:
            raise PathError("Invalid path: root")
        parent = self._resolve_dir(parts[:-1], path)
        name = parts[-1]
        return parent, name, parent.lookup(name)

    def _lookup_inode(self, path: str) -> Inode:
        parts = self._split(path)
        if not parts:
            return self.inode_table.read_inode(1)
        _, _, num = self._resolve_path(path)
        if num is None:
            raise NotFoundError(f"No such file or directory: {path}")
        return self.inode_table.read_inode(num)

    # ================================================================ txn glue
    @contextmanager
    def _txn(self, op: str, path: str = ""):
        self._require_mounted()
        try:
            with TransactionContext(self.tx_manager):
                yield
        except CrashInjectedError:
            raise
        except Exception as e:
            mapped = _map_error(e)
            self.events.emit("fs_op_failed", f"{op} {path} failed: {mapped}", severity="warning",
                             op=op, path=path, error=type(mapped).__name__)
            if mapped is e:
                raise
            raise mapped from e
        else:
            info = self.tx_manager.last_tx_info or {}
            self.events.emit("fs_op", f"{op} {path}", op=op, path=path, txid=info.get("txid"))

    @contextmanager
    def _reading(self):
        self._require_mounted()
        try:
            yield
        except CrashInjectedError:
            raise
        except Exception as e:
            mapped = _map_error(e)
            if mapped is e:
                raise
            raise mapped from e

    # ============================================================ operations
    def mkdir(self, path: str) -> None:
        with self._reading():
            parent, name, existing = self._resolve_path(path)
            validate_name(name)
        if existing:
            raise AlreadyExistsError(f"Already exists: {path}")
        with self._txn("mkdir", path):
            inode = self.inode_table.allocate_inode(InodeType.DIRECTORY, 0o755)
            blk = self.block_allocator.allocate_any(1)[0]
            absb = self.block_allocator._get_absolute_block(blk)
            inode.direct[0] = absb
            inode.blocks_allocated = 1
            self.io.write_block(absb, new_directory_block(inode.inode_num, parent.inode.inode_num))
            self.inode_table.write_inode(inode)
            parent.add_entry(name, inode.inode_num, DIR_TYPE_DIR)
            parent.inode.link_count += 1            # the new '..'
            self.inode_table.write_inode(parent.inode)

    def create(self, path: str) -> None:
        with self._reading():
            parent, name, existing = self._resolve_path(path)
            validate_name(name)
        if existing:
            raise AlreadyExistsError(f"Already exists: {path}")
        with self._txn("create", path):
            inode = self.inode_table.allocate_inode(InodeType.FILE, 0o644)
            parent.add_entry(name, inode.inode_num, DIR_TYPE_FILE)

    def _set_file_data(self, inode: Inode, data: bytes) -> None:
        """Copy-on-write replacement of a file's contents (inside a transaction)."""
        n = (len(data) + 4095) // 4096
        old = inode.get_all_blocks()
        new_abs: List[int] = []
        if n:
            new_abs = [self.block_allocator._get_absolute_block(b)
                       for b in self.block_allocator.allocate_any(n)]
            for i, b in enumerate(new_abs):
                chunk = data[i * 4096:(i + 1) * 4096]
                self.tx_manager.write_data_block(b, chunk + b"\x00" * (4096 - len(chunk)))
        if old:
            self.block_allocator.free_absolute(old)
        inode.direct = new_abs + [0] * (DIRECT_POINTERS - len(new_abs))
        inode.blocks_allocated = n
        inode.size = len(data)
        inode.update_timestamps(mtime=True, ctime=True)
        self.inode_table.write_inode(inode)

    def _file_for_write(self, path: str, data_len: int) -> Inode:
        with self._reading():
            _, _, num = self._resolve_path(path)
            if num is None:
                raise NotFoundError(f"File not found: {path}")
            inode = self.inode_table.read_inode(num)
        if inode.is_directory:
            raise IsDirectoryError(f"Is a directory: {path}")
        if data_len > MAX_FILE_SIZE:
            raise FileTooLargeError(
                f"File would be {data_len} bytes; the maximum is {MAX_FILE_SIZE} "
                f"({DIRECT_POINTERS} direct blocks, no indirect blocks)")
        return inode

    def write(self, path: str, data: bytes) -> int:
        inode = self._file_for_write(path, len(data))
        with self._txn("write", path):
            self._set_file_data(inode, bytes(data))
        return len(data)

    def append(self, path: str, data: bytes) -> int:
        with self._reading():
            current = self.read(path)
        inode = self._file_for_write(path, len(current) + len(data))
        with self._txn("append", path):
            self._set_file_data(inode, current + bytes(data))
        return len(data)

    def read(self, path: str) -> bytes:
        with self._reading():
            inode = self._lookup_inode(path)
            if inode.is_directory:
                raise IsDirectoryError(f"Is a directory: {path}")
            out = bytearray()
            for b in inode.get_all_blocks():
                out.extend(self.io.read_block(b))
            return bytes(out[:inode.size])

    def ls(self, path: str = "/") -> List[DirEntry]:
        with self._reading():
            inode = self._lookup_inode(path)
            if not inode.is_directory:
                raise NotDirectoryError(f"Not a directory: {path}")
            return self._dir(inode.inode_num).list_entries()

    def stat(self, path: str) -> dict:
        with self._reading():
            inode = self._lookup_inode(path)
            return self._inode_dict(inode)

    @staticmethod
    def _inode_dict(inode: Inode) -> dict:
        return {
            "inode": inode.inode_num, "type": inode.type.name, "mode": oct(inode.mode),
            "size": inode.size, "blocks": inode.blocks_allocated, "links": inode.link_count,
            "ctime": inode.ctime, "mtime": inode.mtime, "atime": inode.atime,
            "generation": inode.generation,
            "block_pointers": [b for b in inode.direct if b != 0],
        }

    def rename(self, old_path: str, new_path: str) -> None:
        with self._reading():
            old_parent, old_name, old_num = self._resolve_path(old_path)
            if old_num is None:
                raise NotFoundError(f"Source not found: {old_path}")
            new_parent, new_name, new_num = self._resolve_path(new_path)
            validate_name(new_name)
            src = self.inode_table.read_inode(old_num)
        if new_num is not None:
            raise AlreadyExistsError(f"Destination exists: {new_path}")
        oparts, nparts = self._split(old_path), self._split(new_path)
        if src.is_directory and nparts[:len(oparts)] == oparts:
            raise PathError("cannot move a directory into itself")
        with self._txn("rename", f"{old_path} -> {new_path}"):
            if old_parent.inode.inode_num == new_parent.inode.inode_num:
                old_parent.rename_entry(old_name, new_name)
            else:
                ftype = DIR_TYPE_DIR if src.is_directory else DIR_TYPE_FILE
                new_parent.add_entry(new_name, old_num, ftype)
                old_parent.remove_entry(old_name)
                if src.is_directory:
                    self._dir(old_num).set_parent(new_parent.inode.inode_num)
                    old_parent.inode.link_count -= 1
                    new_parent.inode.link_count += 1
                    self.inode_table.write_inode(old_parent.inode)
                    self.inode_table.write_inode(new_parent.inode)

    def delete(self, path: str) -> None:
        with self._reading():
            parent, name, num = self._resolve_path(path)
            if num is None:
                raise NotFoundError(f"File not found: {path}")
            inode = self.inode_table.read_inode(num)
        if inode.is_directory:
            raise IsDirectoryError(f"Is a directory, use rmdir: {path}")
        with self._txn("delete", path):
            blocks = inode.get_all_blocks()
            if blocks:
                self.block_allocator.free_absolute(blocks)
            inode.link_count -= 1
            if inode.link_count <= 0:
                inode.link_count = 0
                self.inode_table.write_inode(inode)
                self.inode_table.free_inode(inode.inode_num)
            else:
                self.inode_table.write_inode(inode)
            parent.remove_entry(name)

    def rmdir(self, path: str) -> None:
        with self._reading():
            parent, name, num = self._resolve_path(path)
            if num is None:
                raise NotFoundError(f"Directory not found: {path}")
            inode = self.inode_table.read_inode(num)
            if not inode.is_directory:
                raise NotDirectoryError(f"Not a directory: {path}")
            if not self._dir(num).is_empty():
                raise DirectoryNotEmptyError(f"Directory not empty: {path}")
        with self._txn("rmdir", path):
            blocks = inode.get_all_blocks()
            if blocks:
                self.block_allocator.free_absolute(blocks)
            inode.link_count = 0
            self.inode_table.write_inode(inode)
            self.inode_table.free_inode(inode.inode_num)
            parent.remove_entry(name)
            parent.inode.link_count -= 1
            self.inode_table.write_inode(parent.inode)

    # ============================================================ reporting
    def df(self) -> dict:
        sb = self.superblock
        used = sb.total_blocks - sb.free_block_count
        return {
            "total_blocks": sb.total_blocks, "total_size": sb.total_blocks * sb.block_size,
            "free_blocks": sb.free_block_count, "free_size": sb.free_block_count * sb.block_size,
            "used_blocks": used, "used_size": used * sb.block_size,
            "data_blocks": sb.data_block_count,
            "data_used": sb.data_block_count - sb.free_block_count,
            "total_inodes": sb.total_inodes, "free_inodes": sb.free_inode_count,
            "used_inodes": sb.total_inodes - sb.free_inode_count,
        }

    def dump(self) -> dict:
        sb = self.superblock
        return {
            "superblock": {
                "magic": hex(sb.magic), "version": sb.version, "total_blocks": sb.total_blocks,
                "journal_blocks": sb.journal_block_count, "bitmap_blocks": sb.bitmap_block_count,
                "inode_blocks": sb.inode_table_block_count, "data_blocks": sb.data_block_count,
                "journal_head": sb.journal_head, "journal_tail": sb.journal_tail,
                "checkpoint_txid": sb.checkpoint_txid,
            },
            "journal": self.journal.get_journal_stats(),
            "bitmap": self.block_allocator.get_bitmap_summary(),
            "inodes": self.inode_table.get_stats(),
            "df": self.df(),
        }

    def check(self) -> List[str]:
        self._require_mounted()
        return self.check_report().errors

    def check_report(self) -> CheckResult:
        self._require_mounted()
        res = self.checker.check_and_report()
        self.last_check = {"at": time.time(), **res.to_dict()}
        self.events.emit("check_result",
                         f"consistency check {'PASSED' if res.passed else 'FAILED'}: "
                         f"{len(res.errors)} error(s), {len(res.warnings)} warning(s)",
                         severity="info" if res.passed else "error",
                         errors=len(res.errors), warnings=len(res.warnings))
        return res

    def recover(self) -> RecoveryResult:
        self._require_mounted()
        result = self.recovery_mgr.recover()
        self.last_recovery = result
        if not result.success:
            raise FileSystemError(f"Recovery failed: {result.errors}")
        self.tx_manager.read_only = False
        return result

    def checkpoint(self) -> None:
        """Force a checkpoint (reclaim journal space)."""
        self._require_mounted()
        if self.journal.uncheckpointed:
            self.journal.checkpoint()

    # ======================================================= introspection
    def layout(self) -> List[Dict[str, Any]]:
        """Disk regions with real offsets and usage (all values read from the superblock/bitmap)."""
        sb = self.superblock
        bs = sb.block_size
        j = self.journal
        used_inodes = sb.total_inodes - sb.free_inode_count

        def region(name, start, count, used=None, note=""):
            return {"name": name, "start_block": start, "block_count": count,
                    "end_block": start + count - 1, "offset_bytes": start * bs,
                    "size_bytes": count * bs, "used_blocks": used, "note": note}

        return [
            region("superblock", 0, 1, 1, "format metadata, counters, journal tail & checkpoint txid"),
            region("journal", sb.journal_start_block, sb.journal_block_count, j.used_blocks(),
                   "circular redo log; 'used' = blocks between tail and head (live, un-checkpointed)"),
            region("bitmap", sb.bitmap_start_block, sb.bitmap_block_count, sb.bitmap_block_count,
                   "1 bit per data block"),
            region("inode table", sb.inode_table_start_block, sb.inode_table_block_count,
                   (used_inodes + INODES_PER_BLOCK - 1) // INODES_PER_BLOCK,
                   f"{used_inodes}/{sb.total_inodes} inodes in use, {INODES_PER_BLOCK} per block"),
            region("data", sb.data_start_block, sb.data_block_count,
                   sb.data_block_count - sb.free_block_count,
                   "file contents and directory blocks"),
        ]

    def _region_of(self, n: int) -> str:
        sb = self.superblock
        if n == 0:
            return "superblock"
        if sb.journal_start_block <= n < sb.journal_start_block + sb.journal_block_count:
            return "journal"
        if sb.bitmap_start_block <= n < sb.bitmap_start_block + sb.bitmap_block_count:
            return "bitmap"
        if sb.inode_table_start_block <= n < sb.inode_table_start_block + sb.inode_table_block_count:
            return "inode table"
        if sb.data_start_block <= n < sb.data_start_block + sb.data_block_count:
            return "data"
        return "outside"

    def read_raw_block(self, n: int) -> bytes:
        self._require_mounted()
        return self.disk.read_block(n)

    def inode_info(self, n: int) -> Dict[str, Any]:
        self._require_mounted()
        inode = self.inode_table.read_inode(n)
        blk, off = self.inode_table._get_inode_location(n)
        d = self._inode_dict(inode)
        d.update({"in_use": not inode.is_free, "location": {"block": blk, "offset": off, "size": INODE_SIZE},
                  "uid": inode.uid, "gid": inode.gid, "flags": inode.flags,
                  "indirect": [inode.indirect1, inode.indirect2, inode.indirect3]})
        return d

    def block_info(self, n: int) -> Dict[str, Any]:
        self._require_mounted()
        sb = self.superblock
        if n < 0 or n >= sb.total_blocks:
            raise PathError(f"block {n} out of range [0, {sb.total_blocks})")
        raw = self.disk.read_block(n)
        region = self._region_of(n)
        info: Dict[str, Any] = {"block": n, "region": region, "offset_bytes": n * sb.block_size,
                                "all_zero": not any(raw), "decoded": None, "decoded_kind": None}
        if region == "superblock":
            s = Superblock.from_bytes(raw)
            info["decoded_kind"] = "superblock"
            info["decoded"] = {k: (v.decode(errors="replace") if isinstance(v, bytes) else v)
                               for k, v in s.__dict__.items() if not k.startswith("_") and k != "reserved"}
            info["decoded"]["checksum_ok"] = s.verify_checksum()
        elif region == "inode table":
            base = (n - sb.inode_table_start_block) * INODES_PER_BLOCK + 1
            ins = []
            for i in range(INODES_PER_BLOCK):
                try:
                    ino = Inode.from_bytes(raw[i * INODE_SIZE:(i + 1) * INODE_SIZE], base + i)
                except ValueError:
                    ins.append({"inode": base + i, "type": "CORRUPT"})
                    continue
                ins.append({"inode": base + i, "type": ino.type.name, "size": ino.size,
                            "links": ino.link_count, "blocks": [b for b in ino.direct if b]})
            info["decoded_kind"] = "inodes"
            info["decoded"] = ins
        elif region == "bitmap":
            bits = sum(bin(b).count("1") for b in raw)
            info["decoded_kind"] = "bitmap"
            info["decoded"] = {"bits_set": bits, "bits_per_block": 4096 * 8}
        elif region == "journal":
            rel = n - sb.journal_start_block
            info["decoded_kind"] = "journal"
            txs = [t for t in self.journal.scan_region()
                   if rel in [(t["start_block"] + i) % sb.journal_block_count for i in range(t["blocks"])]]
            info["decoded"] = {"journal_block": rel, "transactions": txs,
                               "is_live": self._journal_block_live(rel)}
        elif region == "data":
            owner = None
            for ino in self.inode_table.iter_inodes():
                if n in ino.get_all_blocks():
                    owner = ino
                    break
            info["owner_inode"] = owner.inode_num if owner else None
            allocated = self.block_allocator.is_allocated_absolute(n)
            info["allocated"] = allocated
            if owner is not None and owner.is_directory:
                info["decoded_kind"] = "directory"
                ents = []
                for i in range(4096 // DIR_ENTRY_SIZE):
                    try:
                        e = DirEntry.from_bytes(raw[i * DIR_ENTRY_SIZE:(i + 1) * DIR_ENTRY_SIZE])
                    except Exception:
                        ents.append({"slot": i, "error": "corrupt"})
                        continue
                    if not e.is_empty:
                        ents.append({"slot": i, "inode": e.inode_num, "name": e.name, "type": e.file_type})
                info["decoded"] = ents
            elif owner is not None:
                info["decoded_kind"] = "file-data"
                chunk = raw[:max(0, min(4096, owner.size - owner.get_all_blocks().index(n) * 4096))]
                info["decoded"] = {"text_preview": chunk[:256].decode("utf-8", errors="replace")}
        return info

    def _journal_block_live(self, rel: int) -> bool:
        j = self.journal
        used = j.used_blocks()
        return ((rel - j.tail) % j.journal_blocks) < used

    def journal_view(self) -> Dict[str, Any]:
        self._require_mounted()
        return {"stats": self.journal.get_journal_stats(), "transactions": self.journal.scan_region()}

    def overview(self) -> Dict[str, Any]:
        self._require_mounted()
        sb = self.superblock
        d = self.df()
        txs = self.journal.scan_region()
        latest = txs[-1] if txs else None
        js = self.journal.get_journal_stats()
        rec = self.last_recovery
        return {
            "mounted": True, "read_only": self.tx_manager.read_only,
            "image": self.disk_path, "volume_label": sb.volume_label.decode(errors="replace"),
            "disk_size": self.disk.disk_size, "block_size": sb.block_size, "cache_mode": self.cache_mode,
            "checkpoint_policy": self.tx_manager.checkpoint_policy,
            "blocks": {"total": sb.total_blocks, "data_total": sb.data_block_count,
                       "data_used": d["data_used"], "data_free": sb.free_block_count},
            "inodes": {"total": sb.total_inodes, "used": d["used_inodes"], "free": sb.free_inode_count},
            "journal": {"capacity_blocks": sb.journal_block_count, "used_blocks": js["used_blocks"],
                        "head": js["head"], "tail": js["tail"], "checkpoint_txid": js["checkpoint_txid"],
                        "pending_tx": js["uncheckpointed"]},
            "latest_tx": ({"txid": latest["txid"], "state": latest["state"]} if latest else None),
            "recovery": (None if rec is None else
                         {"success": rec.success, "replayed": rec.transactions_replayed,
                          "discarded": rec.transactions_discarded, "warnings": rec.warnings}),
            "last_check": (None if not self.last_check else
                           {"at": self.last_check["at"], "passed": self.last_check["passed"],
                            "errors": len(self.last_check["errors"]),
                            "warnings": len(self.last_check["warnings"])}),
            "io": dict(self.disk.stats),
        }

    def tree(self, path: str = "/", depth: int = 8) -> Dict[str, Any]:
        """Recursive listing for the UI file browser."""
        self._require_mounted()
        inode = self._lookup_inode(path)
        node = {"name": path.rstrip("/").split("/")[-1] or "/", "path": path or "/",
                "inode": inode.inode_num, "type": inode.type.name, "size": inode.size}
        if inode.is_directory and depth > 0:
            node["children"] = []
            for e in self._dir(inode.inode_num).list_entries():
                child = path.rstrip("/") + "/" + e.name
                node["children"].append(self.tree(child, depth - 1))
        return node


def create_filesystem(disk_path: str, cache_mode: str = "volatile", checkpoint: str = "eager",
                      **format_args) -> FileSystem:
    fs = FileSystem(disk_path, cache_mode=cache_mode, checkpoint=checkpoint)
    fs.format(**format_args)
    return fs


def open_filesystem(disk_path: str, run_recovery: bool = True, cache_mode: str = "volatile",
                    checkpoint: str = "eager") -> FileSystem:
    fs = FileSystem(disk_path, cache_mode=cache_mode, checkpoint=checkpoint)
    fs.mount(run_recovery=run_recovery)
    return fs
