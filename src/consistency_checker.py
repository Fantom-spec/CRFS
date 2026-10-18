"""
Consistency Checker Module

Validates filesystem structural and allocation integrity.
"""

from dataclasses import dataclass
from typing import List, Dict, Set, Optional, Tuple
from .virtual_disk import VirtualDisk
from .superblock import Superblock
from .journal import Journal, RecordType
from .inode import Inode, InodeTable, InodeType, DIRECT_POINTERS
from .block_allocator import BlockAllocator
from .directory import Directory, DirEntry, DIR_TYPE_DIR, DIR_TYPE_FILE, initialize_root_directory


@dataclass
class CheckResult:
    """Result of a consistency check."""
    passed: bool
    errors: List[str]
    warnings: List[str]
    stats: Dict
    
    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"Consistency Check {status}: {len(self.errors)} errors, {len(self.warnings)} warnings"


class ConsistencyChecker:
    """Performs comprehensive filesystem consistency checks."""
    
    def __init__(
        self,
        disk: VirtualDisk,
        superblock: Superblock,
        inode_table: InodeTable,
        block_allocator: BlockAllocator,
        journal: Journal
    ):
        self.disk = disk
        self.superblock = superblock
        self.inode_table = inode_table
        self.block_allocator = block_allocator
        self.journal = journal
    
    def check_all(self) -> List[str]:
        """Run all consistency checks. Returns list of error messages."""
        errors = []
        warnings = []
        
        # 1. Superblock validation
        errors.extend(self._check_superblock())
        
        # 2. Inode validation
        errors.extend(self._check_inodes())
        
        # 3. Block allocation consistency
        errors.extend(self._check_block_allocation())
        
        # 4. Directory hierarchy
        errors.extend(self._check_directories())
        
        # 5. Journal integrity
        errors.extend(self._check_journal())
        
        # 6. Orphaned inodes/blocks
        errors.extend(self._check_orphans())
        
        return errors
    
    def _check_superblock(self) -> List[str]:
        """Validate superblock."""
        errors = []
        
        # Read superblock from disk for validation
        from .superblock import SuperblockManager
        sb_mgr = SuperblockManager(self.disk)
        try:
            disk_superblock = sb_mgr.read()
            disk_superblock.validate()
        except ValueError as e:
            errors.append(f"Superblock validation failed: {e}")
        
        return errors
    
    def _check_inodes(self) -> List[str]:
        """Validate all inodes."""
        errors = []
        allocated_inodes = []
        inode_blocks = set()
        
        for inode_num in range(1, self.superblock.total_inodes + 1):
            inode = self.inode_table.read_inode(inode_num, use_cache=False)
            
            # Validate inode structure
            inode_errors = inode.validate()
            for err in inode_errors:
                errors.append(f"Inode {inode_num}: {err}")
            
            if not inode.is_free:
                allocated_inodes.append((inode_num, inode))
                
                # Collect referenced blocks
                for blk in inode.get_all_blocks():
                    if blk != 0:
                        inode_blocks.add(blk)
                        
                        # Check block is in data region
                        if blk < self.superblock.data_start_block:
                            errors.append(f"Inode {inode_num} references block {blk} before data region")
                        if blk >= self.superblock.data_start_block + self.superblock.data_block_count:
                            errors.append(f"Inode {inode_num} references block {blk} beyond data region")
        
        # Check allocated inode count matches superblock
        if len(allocated_inodes) != self.superblock.total_inodes - self.superblock.free_inode_count:
            errors.append(
                f"Inode count mismatch: found {len(allocated_inodes)} allocated, "
                f"superblock says {self.superblock.total_inodes - self.superblock.free_inode_count}"
            )
        
        # Store for later checks
        self._allocated_inodes = allocated_inodes
        self._inode_blocks = inode_blocks
        
        return errors
    
    def _check_block_allocation(self) -> List[str]:
        """Check bitmap consistency against inode references."""
        errors = []
        
        # Get bitmap-allocated blocks
        bitmap_allocated = set()
        for data_idx in self.block_allocator.iter_allocated():
            abs_block = self.block_allocator._get_absolute_block(data_idx)
            bitmap_allocated.add(abs_block)
        
        # Check each inode-referenced block is in bitmap
        for blk in self._inode_blocks:
            if blk not in bitmap_allocated:
                errors.append(f"Block {blk} referenced by inode but not allocated in bitmap")
        
        # Check each bitmap-allocated block is referenced by some inode
        for blk in bitmap_allocated:
            if blk not in self._inode_blocks:
                # Check if it's a directory block (might not be directly referenced)
                # For now, flag as potential leak
                errors.append(f"Block {blk} allocated in bitmap but not referenced by any inode (possible leak)")
        
        # Check free count
        actual_free = sum(1 for i in range(self.superblock.data_block_count) 
                         if not self.block_allocator.is_allocated(i))
        if actual_free != self.superblock.free_block_count:
            errors.append(
                f"Free block count mismatch: bitmap={actual_free}, "
                f"superblock={self.superblock.free_block_count}"
            )
        
        return errors
    
    def _check_directories(self) -> List[str]:
        """Validate directory structure."""
        errors = []
        visited_dirs = set()
        dir_children: Dict[int, List[Tuple[str, int]]] = {}
        
        # Find all directories
        for inode_num, inode in self._allocated_inodes:
            if inode.is_directory:
                visited_dirs.add(inode_num)
        
        # Check each directory
        for inode_num, inode in self._allocated_inodes:
            if not inode.is_directory:
                continue
            
            # Read directory entries
            try:
                dir_obj = Directory(inode, self.inode_table, self.block_allocator, self.disk)
                entries = dir_obj.list_entries()
            except Exception as e:
                errors.append(f"Directory {inode_num}: failed to read entries: {e}")
                continue
            
            # Check for duplicate names
            names = [e.name for e in entries]
            if len(names) != len(set(names)):
                errors.append(f"Directory {inode_num}: duplicate entry names")
            
            # Validate each entry
            for entry in entries:
                # Check inode exists and is allocated
                if entry.inode_num <= 0 or entry.inode_num > self.superblock.total_inodes:
                    errors.append(f"Directory {inode_num}: entry '{entry.name}' has invalid inode {entry.inode_num}")
                    continue
                
                target_inode = self.inode_table.read_inode(entry.inode_num, use_cache=False)
                if target_inode.is_free:
                    errors.append(f"Directory {inode_num}: entry '{entry.name}' points to free inode {entry.inode_num}")
                
                # Check file type matches
                expected_type = DIR_TYPE_DIR if target_inode.is_directory else DIR_TYPE_FILE
                if entry.file_type != expected_type:
                    errors.append(
                        f"Directory {inode_num}: entry '{entry.name}' type mismatch: "
                        f"dir says {entry.file_type}, inode is {target_inode.type.name}"
                    )
                
                # Track parent-child
                if target_inode.is_directory:
                    dir_children.setdefault(inode_num, []).append((entry.name, entry.inode_num))
        
        # Check for cycles in directory hierarchy
        errors.extend(self._check_directory_cycles(dir_children))
        
        # Check root directory
        root_inode = self.inode_table.read_inode(1, use_cache=False)
        if not root_inode.is_directory:
            errors.append("Root inode (1) is not a directory")
        
        return errors
    
    def _check_directory_cycles(self, dir_children: Dict[int, List[Tuple[str, int]]]) -> List[str]:
        """Check for cycles in directory hierarchy."""
        errors = []
        visited = set()
        rec_stack = set()
        
        def dfs(inode_num: int, path: List[int]) -> None:
            if inode_num in rec_stack:
                cycle = path[path.index(inode_num):] + [inode_num]
                errors.append(f"Directory cycle detected: {' -> '.join(str(x) for x in cycle)}")
                return
            
            if inode_num in visited:
                return
            
            visited.add(inode_num)
            rec_stack.add(inode_num)
            
            for name, child_inode in dir_children.get(inode_num, []):
                dfs(child_inode, path + [inode_num])
            
            rec_stack.remove(inode_num)
        
        dfs(1, [])  # Start from root
        return errors
    
    def _check_journal(self) -> List[str]:
        """Validate journal structure."""
        errors = []
        
        # Check journal bounds
        if self.superblock.journal_head >= self.superblock.journal_block_count:
            errors.append(f"Journal head out of bounds: {self.superblock.journal_head}")
        if self.superblock.journal_tail >= self.superblock.journal_block_count:
            errors.append(f"Journal tail out of bounds: {self.superblock.journal_tail}")
        
        # Scan journal for structural issues
        try:
            journal_data = self.journal._read_journal_bytes(0, self.journal.journal_blocks)
            offset = 0
            record_count = 0
            
            while offset + 24 <= len(journal_data):
                try:
                    record = Journal.JournalRecord.from_bytes(journal_data[offset:])
                    record_count += 1
                    offset += record.total_size
                    
                    # Check for wraparound
                    if offset > len(journal_data):
                        errors.append("Journal record extends past journal end")
                        break
                        
                except Exception:
                    # Invalid record - could be empty space or corruption
                    break
            
        except Exception as e:
            errors.append(f"Journal scan failed: {e}")
        
        return errors
    
    def _check_orphans(self) -> List[str]:
        """Check for orphaned inodes and blocks."""
        errors = []
        
        # Find inodes with link_count == 0 but not free
        for inode_num, inode in self._allocated_inodes:
            if inode.link_count == 0:
                errors.append(f"Inode {inode_num} has link_count=0 but is not free")
        
        # Find directories not reachable from root
        reachable = self._find_reachable_directories()
        for inode_num, inode in self._allocated_inodes:
            if inode.is_directory and inode_num not in reachable and inode_num != 1:
                errors.append(f"Directory {inode_num} not reachable from root (orphaned)")
        
        return errors
    
    def _find_reachable_directories(self) -> Set[int]:
        """Find all directories reachable from root."""
        reachable = set()
        to_visit = [1]  # Start from root
        
        while to_visit:
            current = to_visit.pop()
            if current in reachable:
                continue
            reachable.add(current)
            
            inode = self.inode_table.read_inode(current, use_cache=False)
            if not inode.is_directory:
                continue
            
            try:
                dir_obj = Directory(inode, self.inode_table, self.block_allocator, self.disk)
                entries = dir_obj.list_entries()
                for entry in entries:
                    target_inode = self.inode_table.read_inode(entry.inode_num, use_cache=False)
                    if target_inode.is_directory:
                        to_visit.append(entry.inode_num)
            except Exception:
                pass
        
        return reachable
    
    def get_stats(self) -> Dict:
        """Get filesystem statistics."""
        stats = {
            'superblock': {
                'total_blocks': self.superblock.total_blocks,
                'total_inodes': self.superblock.total_inodes,
                'free_inodes': self.superblock.free_inode_count,
                'free_blocks': self.superblock.free_block_count,
            },
            'journal': self.journal.get_journal_stats(),
            'bitmap': self.block_allocator.get_bitmap_summary(),
        }
        
        # Count inodes by type
        file_count = 0
        dir_count = 0
        for inode_num in range(1, self.superblock.total_inodes + 1):
            inode = self.inode_table.read_inode(inode_num, use_cache=False)
            if inode.is_file:
                file_count += 1
            elif inode.is_directory:
                dir_count += 1
        
        stats['inodes'] = {
            'files': file_count,
            'directories': dir_count,
            'total_allocated': file_count + dir_count,
        }
        
        return stats
    
    def check_and_report(self, verbose: bool = False) -> CheckResult:
        """Run checks and return structured result."""
        errors = self.check_all()
        stats = self.get_stats()
        
        return CheckResult(
            passed=len(errors) == 0,
            errors=errors,
            warnings=[],  # Warnings not collected in check_all
            stats=stats
        )


def run_consistency_check(
    disk: VirtualDisk,
    superblock: Superblock,
    inode_table: InodeTable,
    block_allocator: BlockAllocator,
    journal: Journal
) -> CheckResult:
    """Convenience function to run consistency check."""
    checker = ConsistencyChecker(disk, superblock, inode_table, block_allocator, journal)
    return checker.check_and_report()