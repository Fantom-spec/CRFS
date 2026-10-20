"""
Consistency Checker (fsck-style)

Read-only verification of structural and allocation integrity.  Every problem
is reported as a *finding* with a severity, a stable code, the affected object
and a plain-language explanation of why it matters.  ``check_all()`` keeps the
original contract (a list of error strings; empty == consistent).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .block_allocator import BlockAllocator
from .directory import (DIR_TYPE_DIR, DIR_TYPE_FILE, DirectoryError, Directory,
                        ENTRIES_PER_BLOCK)
from .inode import Inode, InodeTable, InodeType, InvalidInodeError
from .journal import Journal
from .superblock import Superblock, SuperblockManager


@dataclass
class Finding:
    severity: str          # 'error' | 'warning' | 'info'
    code: str
    obj: str
    message: str
    why: str

    def to_dict(self) -> Dict[str, str]:
        return {"severity": self.severity, "code": self.code, "object": self.obj,
                "message": self.message, "why": self.why}


@dataclass
class CheckResult:
    passed: bool
    errors: List[str]
    warnings: List[str]
    stats: Dict
    findings: List[Dict[str, str]] = field(default_factory=list)

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"Consistency Check {status}: {len(self.errors)} errors, {len(self.warnings)} warnings"

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "errors": self.errors, "warnings": self.warnings,
                "findings": self.findings, "stats": self.stats}


class ConsistencyChecker:
    def __init__(self, disk, superblock: Superblock, inode_table: InodeTable,
                 block_allocator: BlockAllocator, journal: Journal):
        self.disk = disk
        self.superblock = superblock
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self.journal = journal
        self.findings: List[Finding] = []
        self.stats: Dict[str, Any] = {}

    # ------------------------------------------------------------- plumbing
    def _add(self, severity: str, code: str, obj: str, message: str, why: str) -> None:
        self.findings.append(Finding(severity, code, obj, message, why))

    def check_all(self) -> List[str]:
        return [f.message for f in self.run() if f.severity == "error"]

    def run(self) -> List[Finding]:
        self.findings = []
        self.stats = {}
        self._check_superblock()
        inodes = self._check_inodes()
        self._check_bitmap(inodes)
        self._check_directories(inodes)
        self._check_journal()
        return self.findings

    def check_and_report(self, verbose: bool = False) -> CheckResult:
        findings = self.run()
        errors = [f.message for f in findings if f.severity == "error"]
        warnings = [f.message for f in findings if f.severity == "warning"]
        return CheckResult(passed=not errors, errors=errors, warnings=warnings,
                           stats=self.get_stats(), findings=[f.to_dict() for f in findings])

    check_report = check_and_report

    # ----------------------------------------------------------- superblock
    def _check_superblock(self) -> None:
        raw = self.inode_table.disk.read_block(0)
        try:
            sb = Superblock.from_bytes(raw)
            sb.validate()
        except ValueError as e:
            self._add("error", "SB_INVALID", "superblock",
                      f"Superblock validation failed: {e}",
                      "The superblock describes the whole disk layout; if it is wrong nothing "
                      "else on the disk can be trusted.")
            return
        if sb.journal_head >= sb.journal_block_count or sb.journal_tail >= sb.journal_block_count:
            self._add("error", "JOURNAL_BOUNDS", "superblock",
                      f"Journal head/tail out of bounds: head={sb.journal_head}, tail={sb.journal_tail}",
                      "Recovery would start scanning outside the journal region.")

    # --------------------------------------------------------------- inodes
    def _check_inodes(self) -> Dict[int, Inode]:
        sb = self.superblock
        allocated: Dict[int, Inode] = {}
        owners: Dict[int, List[int]] = {}
        for n in range(1, sb.total_inodes + 1):
            try:
                inode = self.inode_table.read_inode(n)
            except InvalidInodeError as e:
                self._add("error", "INODE_CORRUPT", f"inode {n}", str(e),
                          "The inode cannot be decoded, so its file is unreadable.")
                continue
            if inode.is_free:
                if any(inode.direct) or inode.size or inode.link_count:
                    self._add("warning", "FREE_INODE_DIRTY", f"inode {n}",
                              f"Free inode {n} still holds stale fields (size={inode.size}, "
                              f"links={inode.link_count})",
                              "Harmless but means a free did not clear the inode.")
                continue
            allocated[n] = inode
            for err in inode.validate():
                self._add("error", "INODE_INVALID", f"inode {n}", f"Inode {n}: {err}",
                          "The inode's own fields contradict each other.")
            for blk in inode.get_all_blocks():
                if blk < sb.data_start_block or blk >= sb.data_start_block + sb.data_block_count:
                    self._add("error", "BLOCK_OUT_OF_RANGE", f"inode {n}",
                              f"Inode {n} references block {blk} outside the data region",
                              "Reading or writing that block would corrupt metadata regions.")
                else:
                    owners.setdefault(blk, []).append(n)
            if inode.is_file:
                need = (inode.size + 4095) // 4096
                if inode.blocks_allocated < need:
                    self._add("error", "SIZE_EXCEEDS_BLOCKS", f"inode {n}",
                              f"Inode {n}: size {inode.size} needs {need} blocks but has "
                              f"{inode.blocks_allocated}",
                              "Reading the file would return data that is not backed by blocks.")
        for blk, who in owners.items():
            if len(who) > 1:
                self._add("error", "DUP_BLOCK", f"block {blk}",
                          f"Block {blk} is owned by multiple inodes: {who}",
                          "Two files share storage; writing one silently corrupts the other.")
        expected_alloc = sb.total_inodes - sb.free_inode_count
        if len(allocated) != expected_alloc:
            self._add("error", "INODE_COUNT", "superblock",
                      f"Inode count mismatch: {len(allocated)} allocated inodes found, "
                      f"superblock implies {expected_alloc}",
                      "The free-inode counter is wrong, so inodes may be leaked or double-allocated.")
        self._owners = owners
        self.stats["inodes_allocated"] = len(allocated)
        return allocated

    # --------------------------------------------------------------- bitmap
    def _check_bitmap(self, inodes: Dict[int, Inode]) -> None:
        sb = self.superblock
        alloc = self.block_allocator
        bitmap_abs = {alloc._get_absolute_block(d) for d in alloc.iter_allocated()}
        referenced = set(self._owners)
        for blk in sorted(referenced - bitmap_abs):
            self._add("error", "REF_BUT_FREE", f"block {blk}",
                      f"Block {blk} is referenced by inode(s) {self._owners[blk]} but marked free in the bitmap",
                      "The allocator could hand this block to another file and overwrite live data.")
        for blk in sorted(bitmap_abs - referenced):
            self._add("error", "LEAKED_BLOCK", f"block {blk}",
                      f"Block {blk} is marked allocated but no inode references it (leak)",
                      "The block is lost: it can never be reused until fsck reclaims it.")
        free = sb.data_block_count - len(bitmap_abs)
        if free != sb.free_block_count:
            self._add("error", "FREE_COUNT", "superblock",
                      f"Free block count mismatch: bitmap says {free}, superblock says {sb.free_block_count}",
                      "df and the allocator disagree about free space.")
        self.stats["blocks_allocated"] = len(bitmap_abs)

    # ---------------------------------------------------------- directories
    def _check_directories(self, inodes: Dict[int, Inode]) -> None:
        sb = self.superblock
        root = inodes.get(1)
        if root is None or not root.is_directory:
            self._add("error", "NO_ROOT", "inode 1", "Root inode (1) is missing or not a directory",
                      "Without a root directory nothing is reachable.")
            return
        refs: Dict[int, int] = {}                 # inode -> number of entries pointing at it
        subdirs: Dict[int, int] = {}              # dir -> number of child directories
        parent_of: Dict[int, int] = {}
        reachable: Set[int] = set()
        stack = [(1, 1)]
        while stack:
            dnum, parent = stack.pop()
            if dnum in reachable:
                continue
            reachable.add(dnum)
            parent_of[dnum] = parent
            dino = inodes.get(dnum)
            if dino is None or not dino.is_directory:
                continue
            d = Directory(dino, self.inode_table, self.block_allocator, self.inode_table.disk)
            try:
                raw = d._read_raw_entries()
            except DirectoryError as e:
                self._add("error", "DIR_CORRUPT", f"directory inode {dnum}",
                          f"Directory inode {dnum} is unreadable: {e}",
                          "Every file below this directory is unreachable.")
                continue
            dot = next((e for e in raw if e.name == "."), None)
            dotdot = next((e for e in raw if e.name == ".."), None)
            if dot is None or dot.inode_num != dnum:
                self._add("error", "DOT_BAD", f"directory inode {dnum}",
                          f"Directory {dnum}: '.' entry is missing or does not point to itself",
                          "Path resolution and tools rely on '.'.")
            if dotdot is None or dotdot.inode_num != parent:
                self._add("error", "DOTDOT_BAD", f"directory inode {dnum}",
                          f"Directory {dnum}: '..' is {dotdot.inode_num if dotdot else 'missing'}, "
                          f"expected {parent}",
                          "'..' must point to the real parent or upward navigation breaks.")
            names = [e.name for e in raw if e.name not in (".", "..")]
            if len(names) != len(set(names)):
                self._add("error", "DUP_NAME", f"directory inode {dnum}",
                          f"Directory {dnum}: duplicate entry names",
                          "Lookups by name would be ambiguous.")
            for e in raw:
                if e.name in (".", ".."):
                    continue
                if e.inode_num <= 0 or e.inode_num > sb.total_inodes:
                    self._add("error", "DIRENT_BAD_INODE", f"directory inode {dnum}",
                              f"Directory {dnum}: entry '{e.name}' has invalid inode {e.inode_num}",
                              "The entry points outside the inode table.")
                    continue
                target = inodes.get(e.inode_num)
                if target is None:
                    self._add("error", "DIRENT_FREE_INODE", f"directory inode {dnum}",
                              f"Directory {dnum}: entry '{e.name}' points to free inode {e.inode_num}",
                              "Dangling reference: opening the name would fail or read garbage.")
                    continue
                want = DIR_TYPE_DIR if target.is_directory else DIR_TYPE_FILE
                if e.file_type != want:
                    self._add("error", "DIRENT_TYPE", f"directory inode {dnum}",
                              f"Directory {dnum}: entry '{e.name}' type {e.file_type} "
                              f"does not match inode type {target.type.name}",
                              "Tools trust the entry type to decide how to open the object.")
                refs[e.inode_num] = refs.get(e.inode_num, 0) + 1
                if target.is_directory:
                    subdirs[dnum] = subdirs.get(dnum, 0) + 1
                    if e.inode_num in reachable:
                        self._add("error", "DIR_CYCLE", f"directory inode {dnum}",
                                  f"Directory {dnum}: entry '{e.name}' creates a cycle or second parent "
                                  f"for directory {e.inode_num}",
                                  "Directory hierarchy must be a tree.")
                    else:
                        stack.append((e.inode_num, dnum))
                else:
                    reachable.add(e.inode_num)
        # link counts
        for n, ino in inodes.items():
            if n not in reachable:
                self._add("error", "ORPHAN_INODE", f"inode {n}",
                          f"Inode {n} ({ino.type.name}, {ino.blocks_allocated} blocks) is allocated "
                          f"but not reachable from the root directory",
                          "Orphan: it consumes an inode and blocks but no path can reach it.")
                continue
            if ino.is_directory:
                want = 2 + subdirs.get(n, 0)
                if ino.link_count != want:
                    self._add("error", "LINK_COUNT", f"inode {n}",
                              f"Directory inode {n}: link_count {ino.link_count}, expected {want}",
                              "Link counts decide when an inode may be freed.")
            else:
                if ino.link_count != refs.get(n, 0):
                    self._add("error", "LINK_COUNT", f"inode {n}",
                              f"File inode {n}: link_count {ino.link_count}, "
                              f"but {refs.get(n, 0)} directory entries reference it",
                              "A wrong count causes premature free (data loss) or a leak.")
        self.stats["reachable_inodes"] = len(reachable)

    # -------------------------------------------------------------- journal
    def _check_journal(self) -> None:
        try:
            scan = self.journal.scan()
        except Exception as e:
            self._add("error", "JOURNAL_SCAN", "journal", f"Journal scan failed: {e}",
                      "Recovery would not be able to read the journal.")
            return
        for m in scan.errors:
            self._add("error", "JOURNAL_INVALID", "journal", f"Journal: {m}",
                      "A committed transaction contains records recovery refuses to replay.")
        for m in scan.warnings:
            self._add("warning", "JOURNAL_CORRUPT_RECORD", "journal", f"Journal: {m}",
                      "A damaged or torn record ends the live log; the affected transaction is discarded.")
        if scan.committed:
            ids = [t.txid for t in scan.committed]
            self._add("warning", "JOURNAL_PENDING", "journal",
                      f"{len(ids)} committed transaction(s) {ids} are in the journal and not yet "
                      f"checkpointed",
                      "Normal with lazy checkpointing; after a crash it means recovery has not run yet.")
        self.stats["journal_pending_tx"] = len(scan.committed)

    # ---------------------------------------------------------------- stats
    def get_stats(self) -> Dict:
        files = dirs = 0
        for ino in self.inode_table.iter_inodes():
            if ino.is_file:
                files += 1
            elif ino.is_directory:
                dirs += 1
        out = {
            "superblock": {
                "total_blocks": self.superblock.total_blocks,
                "total_inodes": self.superblock.total_inodes,
                "free_inodes": self.superblock.free_inode_count,
                "free_blocks": self.superblock.free_block_count,
            },
            "journal": self.journal.get_journal_stats(),
            "inodes": {"files": files, "directories": dirs, "total_allocated": files + dirs},
        }
        out.update(self.stats)
        return out


def run_consistency_check(disk, superblock, inode_table, block_allocator, journal) -> CheckResult:
    return ConsistencyChecker(disk, superblock, inode_table, block_allocator, journal).check_and_report()
