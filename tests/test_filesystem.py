"""
Test Suite for CRFS Filesystem Simulator

Comprehensive tests for all components.
"""

import os
import tempfile
import shutil
import pytest
from src import (
    VirtualDisk, Superblock, SuperblockManager,
    BlockAllocator, Inode, InodeTable, InodeType,
    Directory, DirEntry, Journal, Transaction, RecordType,
    TransactionManager, RecoveryManager, ConsistencyChecker,
    FileSystem, create_filesystem, open_filesystem,
    CrashInjector, CrashMode, CrashPoint,
    create_disk_image, open_disk_image,
    TransBeginRecord,
    DirectoryNotEmptyError, TransactionContext
)


# Test fixtures
@pytest.fixture
def temp_disk():
    """Create a temporary disk image for testing."""
    temp_dir = tempfile.mkdtemp()
    disk_path = os.path.join(temp_dir, "test.img")
    yield disk_path
    shutil.rmtree(temp_dir)


@pytest.fixture
def formatted_fs(temp_disk):
    """Create a formatted filesystem."""
    fs = create_filesystem(temp_disk, disk_size=4*1024*1024)  # 4MB for faster tests
    yield fs
    fs.unmount()


@pytest.fixture
def mounted_fs(temp_disk):
    """Create and mount a filesystem."""
    fs = create_filesystem(temp_disk, disk_size=4*1024*1024)
    # create_filesystem already mounts
    yield fs
    fs.unmount()


# Virtual Disk Tests
class TestVirtualDisk:
    def test_create_and_open(self, temp_disk):
        disk = create_disk_image(temp_disk, disk_size=1024*1024)
        assert disk.is_open
        assert disk.get_total_blocks() == 256  # 1MB / 4KB
        disk.close()
        
        # Reopen
        disk2 = open_disk_image(temp_disk)
        assert disk2.get_total_blocks() == 256
        disk2.close()
    
    def test_read_write_block(self, temp_disk):
        disk = create_disk_image(temp_disk, disk_size=1024*1024)
        test_data = b"Hello, World!" + b"\x00" * (4096 - 13)
        disk.write_block(10, test_data)
        read_data = disk.read_block(10)
        assert read_data == test_data
        disk.close()
    
    def test_bounds_check(self, temp_disk):
        disk = create_disk_image(temp_disk, disk_size=1024*1024)
        with pytest.raises(Exception):
            disk.read_block(1000)  # Out of bounds
        with pytest.raises(Exception):
            disk.write_block(1000, b"x" * 4096)
        disk.close()
    
    def test_flush(self, temp_disk):
        disk = create_disk_image(temp_disk)
        disk.write_block(5, b"test" + b"\x00" * 4092)
        disk.flush()  # Should not raise
        disk.close()


# Superblock Tests
class TestSuperblock:
    def test_create_default(self, temp_disk):
        disk = create_disk_image(temp_disk, disk_size=4*1024*1024)
        mgr = SuperblockManager(disk)
        sb = mgr.create_default(disk_size=4*1024*1024, journal_blocks=64, inode_blocks=32)
        mgr.write()
        
        assert sb.magic == 0x43524653
        assert sb.version == 1
        assert sb.total_blocks == 1024  # 4MB / 4KB
        assert sb.journal_block_count == 64
        assert sb.inode_table_block_count == 32
        assert sb.total_inodes == 512  # 32 * 16
        disk.close()
    
    def test_validate(self, temp_disk):
        disk = create_disk_image(temp_disk, disk_size=4*1024*1024)
        mgr = SuperblockManager(disk)
        sb = mgr.create_default(disk_size=4*1024*1024)
        mgr.write()
        
        # Read back and validate
        sb2 = mgr.read()
        assert sb2.magic == sb.magic
        assert sb2.total_blocks == sb.total_blocks
        disk.close()
    
    def test_checksum(self, temp_disk):
        disk = create_disk_image(temp_disk, disk_size=4*1024*1024)
        mgr = SuperblockManager(disk)
        sb = mgr.create_default(disk_size=4*1024*1024)
        mgr.write()
        
        # Corrupt the superblock
        disk.write_block(0, b"\x00" * 4096)
        
        # Reading should fail validation
        with pytest.raises(ValueError):
            mgr.read()
        disk.close()


# Block Allocator Tests
class TestBlockAllocator:
    def test_allocate_free(self, mounted_fs):
        allocator = mounted_fs.block_allocator
        # metadata writes are only legal inside a transaction
        mounted_fs.tx_manager.begin()
        
        # Allocate single block
        blocks = allocator.allocate(1)
        assert len(blocks) == 1
        assert allocator.is_allocated(blocks[0])
        
        # Allocate multiple blocks
        blocks2 = allocator.allocate(3)
        assert len(blocks2) == 3
        for b in blocks2:
            assert allocator.is_allocated(b)
        
        # Free blocks
        allocator.free(blocks)
        assert not allocator.is_allocated(blocks[0])
        
        allocator.free(blocks2)
        for b in blocks2:
            assert not allocator.is_allocated(b)
        mounted_fs.tx_manager.abort()
    
    def test_allocate_contiguous(self, mounted_fs):
        allocator = mounted_fs.block_allocator
        mounted_fs.tx_manager.begin()
        
        # Allocate contiguous blocks
        blocks = allocator.allocate(5)
        assert len(blocks) == 5
        # Check they're contiguous
        for i in range(1, 5):
            assert blocks[i] == blocks[i-1] + 1
        mounted_fs.tx_manager.abort()
    
    def test_free_count(self, mounted_fs):
        allocator = mounted_fs.block_allocator
        initial_free = allocator.get_free_count()
        mounted_fs.tx_manager.begin()
        
        blocks = allocator.allocate(10)
        assert allocator.get_free_count() == initial_free - 10
        
        allocator.free(blocks)
        assert allocator.get_free_count() == initial_free
        mounted_fs.tx_manager.abort()


# Inode Tests
class TestInode:
    def test_inode_serialization(self):
        inode = Inode(
            inode_num=42,
            type=InodeType.FILE,
            mode=0o644,
            size=1024,
            blocks_allocated=1,
            direct=[100, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        )
        
        data = inode.to_bytes()
        assert len(data) == 256
        
        inode2 = Inode.from_bytes(data, 42)
        assert inode2.inode_num == 42
        assert inode2.type == InodeType.FILE
        assert inode2.size == 1024
        assert inode2.direct[0] == 100
    
    def test_inode_validation(self):
        inode = Inode(inode_num=1, type=InodeType.FILE, size=100, blocks_allocated=1)
        inode.direct[0] = 50
        errors = inode.validate()
        assert len(errors) == 0
        
        # Mismatched block count
        inode.blocks_allocated = 5
        errors = inode.validate()
        assert len(errors) > 0
    
    def test_allocate_inode(self, mounted_fs):
        inode_table = mounted_fs.inode_table
        mounted_fs.tx_manager.begin()
        
        inode = inode_table.allocate_inode(InodeType.FILE, 0o644)
        assert inode.type == InodeType.FILE
        assert inode.link_count == 1
        assert inode.inode_num > 1  # Not root
        
        # Read it back
        inode2 = inode_table.read_inode(inode.inode_num)
        assert inode2.type == InodeType.FILE
        assert inode2.inode_num == inode.inode_num
        mounted_fs.tx_manager.abort()
    
    def test_free_inode(self, mounted_fs):
        inode_table = mounted_fs.inode_table
        mounted_fs.tx_manager.begin()
        
        inode = inode_table.allocate_inode(InodeType.FILE)
        inode_num = inode.inode_num
        
        # Set link_count to 0 to allow freeing
        inode.link_count = 0
        inode_table.write_inode(inode)
        
        inode_table.free_inode(inode_num)
        
        # Should be free now
        inode2 = inode_table.read_inode(inode_num)
        assert inode2.is_free
        mounted_fs.tx_manager.abort()


# Directory Tests
class TestDirectory:
    def test_dir_entry_serialization(self):
        entry = DirEntry(inode_num=42, name="test.txt", file_type=1)
        data = entry.to_bytes()
        assert len(data) == 64
        
        entry2 = DirEntry.from_bytes(data)
        assert entry2.inode_num == 42
        assert entry2.name == "test.txt"
        assert entry2.file_type == 1
    
    def test_empty_entry(self):
        entry = DirEntry(0, "", 0)
        data = entry.to_bytes()
        entry2 = DirEntry.from_bytes(data)
        assert entry2.is_empty
    
    def test_add_remove_entries(self, mounted_fs):
        root_dir = mounted_fs.root_dir
        mounted_fs.tx_manager.begin()
        
        # Add entries
        root_dir.add_entry("file1.txt", 10, 1)  # File
        root_dir.add_entry("dir1", 11, 2)       # Directory
        
        entries = root_dir.list_entries()
        assert len(entries) == 2
        
        # Lookup
        assert root_dir.lookup("file1.txt") == 10
        assert root_dir.lookup("dir1") == 11
        assert root_dir.lookup("nonexistent") is None
        
        # Remove
        inode_num = root_dir.remove_entry("file1.txt")
        assert inode_num == 10
        
        entries = root_dir.list_entries()
        assert len(entries) == 1
        assert entries[0].name == "dir1"
        mounted_fs.tx_manager.abort()
    
    def test_rename(self, mounted_fs):
        root_dir = mounted_fs.root_dir
        mounted_fs.tx_manager.begin()
        root_dir.add_entry("old.txt", 20, 1)
        
        root_dir.rename_entry("old.txt", "new.txt")
        
        assert root_dir.lookup("old.txt") is None
        assert root_dir.lookup("new.txt") == 20
        mounted_fs.tx_manager.abort()


# Journal Tests
class TestJournal:
    def test_transaction_records(self):
        tx = Transaction(txid=123)
        tx.add_data_record(100, 0, b"test data" + b"\x00" * (4096 - 9))
        tx.add_data_record(101, 0, b"more data" + b"\x00" * (4096 - 9))
        
        records = tx.get_all_records()
        assert len(records) == 4  # BEGIN, DATA, DATA, COMMIT
        assert records[0].record_type == RecordType.TRANS_BEGIN
        assert records[1].record_type == RecordType.TRANS_DATA
        assert records[2].record_type == RecordType.TRANS_DATA
        assert records[3].record_type == RecordType.TRANS_COMMIT
    
    def test_record_checksum(self):
        record = Transaction(txid=1).get_begin_record()
        data = record.to_bytes()
        
        # Verify checksum
        parsed = TransBeginRecord.from_bytes(data)
        assert parsed.txid == 1
        
        # Corrupt data
        corrupted = bytearray(data)
        corrupted[10] ^= 0xFF
        with pytest.raises(Exception):
            TransBeginRecord.from_bytes(bytes(corrupted))
    
    def test_journal_write_read(self, mounted_fs):
        journal = mounted_fs.journal
        
        tx = Transaction(txid=journal.get_next_txid())
        tx.add_data_record(mounted_fs.superblock.data_start_block, 0, b"x" * 4096)
        
        journal.write_transaction(tx)
        journal.flush_journal()
        
        # Scan should find it
        txs = journal.scan_transactions()
        # Note: transaction not committed yet, so won't appear in committed list
        # This tests the write path


# Filesystem Integration Tests
class TestFileSystem:
    def test_format_and_mount(self, temp_disk):
        fs = create_filesystem(temp_disk, disk_size=4*1024*1024)
        assert fs.is_mounted
        fs.unmount()
        
        # Remount
        fs2 = open_filesystem(temp_disk)
        assert fs2.is_mounted
        fs2.unmount()
    
    def test_mkdir_create(self, mounted_fs):
        mounted_fs.mkdir("/dir1")
        mounted_fs.mkdir("/dir1/subdir")
        mounted_fs.create("/dir1/file.txt")
        
        entries = mounted_fs.ls("/dir1")
        names = [e.name for e in entries]
        assert "subdir" in names
        assert "file.txt" in names
    
    def test_write_read(self, mounted_fs):
        mounted_fs.create("/test.txt")
        mounted_fs.write("/test.txt", b"Hello, World!")
        
        data = mounted_fs.read("/test.txt")
        assert data == b"Hello, World!"
    
    def test_append(self, mounted_fs):
        mounted_fs.create("/test.txt")
        mounted_fs.write("/test.txt", b"Hello")
        mounted_fs.append("/test.txt", b" World")
        
        data = mounted_fs.read("/test.txt")
        assert data == b"Hello World"
    
    def test_stat(self, mounted_fs):
        mounted_fs.create("/test.txt")
        mounted_fs.write("/test.txt", b"Test data")
        
        stat = mounted_fs.stat("/test.txt")
        assert stat['type'] == 'FILE'
        assert stat['size'] == 9
        assert stat['inode'] > 1
    
    def test_rename(self, mounted_fs):
        mounted_fs.create("/old.txt")
        mounted_fs.write("/old.txt", b"content")
        
        mounted_fs.rename("/old.txt", "/new.txt")
        
        # Old should not exist
        with pytest.raises(Exception):
            mounted_fs.read("/old.txt")
        
        # New should exist
        data = mounted_fs.read("/new.txt")
        assert data == b"content"
    
    def test_delete(self, mounted_fs):
        mounted_fs.create("/test.txt")
        mounted_fs.write("/test.txt", b"data")
        
        mounted_fs.delete("/test.txt")
        
        with pytest.raises(Exception):
            mounted_fs.read("/test.txt")
    
    def test_rmdir(self, mounted_fs):
        mounted_fs.mkdir("/emptydir")
        mounted_fs.rmdir("/emptydir")
        
        with pytest.raises(Exception):
            mounted_fs.ls("/emptydir")
    
    def test_rmdir_not_empty(self, mounted_fs):
        mounted_fs.mkdir("/dir")
        mounted_fs.create("/dir/file.txt")
        
        with pytest.raises(DirectoryNotEmptyError):
            mounted_fs.rmdir("/dir")
    
    def test_df(self, mounted_fs):
        df = mounted_fs.df()
        assert df['total_blocks'] > 0
        assert df['free_blocks'] >= 0
        assert df['total_inodes'] > 0
    
    def test_persistence(self, temp_disk):
        # Create and write
        fs1 = create_filesystem(temp_disk, disk_size=4*1024*1024)
        fs1.create("/persist.txt")
        fs1.write("/persist.txt", b"Persistent data")
        fs1.unmount()
        
        # Reopen and verify
        fs2 = open_filesystem(temp_disk)
        data = fs2.read("/persist.txt")
        assert data == b"Persistent data"
        fs2.unmount()
    
    def test_nested_directories(self, mounted_fs):
        mounted_fs.mkdir("/a")
        mounted_fs.mkdir("/a/b")
        mounted_fs.mkdir("/a/b/c")
        mounted_fs.create("/a/b/c/deep.txt")
        mounted_fs.write("/a/b/c/deep.txt", b"deep")
        
        data = mounted_fs.read("/a/b/c/deep.txt")
        assert data == b"deep"
    
    def test_large_file(self, mounted_fs):
        """Test file larger than one block."""
        mounted_fs.create("/large.txt")
        # Write 10KB (3 blocks)
        data = b"X" * (10 * 1024)
        mounted_fs.write("/large.txt", data)
        
        read_data = mounted_fs.read("/large.txt")
        assert read_data == data
    
    def test_disk_full(self, temp_disk):
        """Test disk full behavior."""
        # Create very small filesystem (1MB with small journal and inode table)
        fs = create_filesystem(temp_disk, disk_size=1024*1024, journal_blocks=16, inode_blocks=8)
        
        # Fill up disk
        block_size = 4096
        for i in range(20):
            fs.create(f"/f{i}.txt")
            try:
                fs.write(f"/f{i}.txt", b"X" * block_size)
            except DiskFullError:
                break  # Expected
        
        fs.unmount()


# Recovery Tests
class TestRecovery:
    def test_recovery_no_crash(self, temp_disk):
        """Test recovery on clean filesystem."""
        fs = create_filesystem(temp_disk, disk_size=4*1024*1024)
        fs.create("/test.txt")
        fs.write("/test.txt", b"data")
        fs.unmount()
        
        # Mount with recovery
        fs2 = open_filesystem(temp_disk)
        data = fs2.read("/test.txt")
        assert data == b"data"
        fs2.unmount()
    
    def test_recovery_after_commit(self, temp_disk):
        """Test that committed transactions survive crash."""
        fs = create_filesystem(temp_disk, disk_size=4*1024*1024)
        fs.create("/test.txt")
        fs.write("/test.txt", b"committed")
        fs.unmount()
        
        # Simulate crash after commit by not unmounting cleanly
        # (In real test, we'd kill the process)
        # For now, just verify recovery runs
        fs2 = open_filesystem(temp_disk)
        data = fs2.read("/test.txt")
        assert data == b"committed"
        fs2.unmount()


# Crash Injection Tests
class TestCrashInjection:
    def test_crash_injector_exception_mode(self, mounted_fs):
        injector = CrashInjector(CrashMode.EXCEPTION)
        injector.enable(CrashPoint.BEFORE_JOURNAL_WRITE)
        
        crash_triggered = False
        try:
            injector.maybe_crash(CrashPoint.BEFORE_JOURNAL_WRITE)
        except Exception as e:
            crash_triggered = True
            assert "Crash injected" in str(e)
        
        assert crash_triggered
    
    def test_crash_injector_disabled(self, mounted_fs):
        injector = CrashInjector(CrashMode.EXCEPTION)
        # Not enabled
        
        crash_triggered = False
        try:
            injector.maybe_crash(CrashPoint.BEFORE_JOURNAL_WRITE)
        except Exception:
            crash_triggered = True
        
        assert not crash_triggered


# Crash Point Tests
class TestCrashPoints:
    """Test all crash points with transaction commit protocol."""
    
    EARLY_CRASH_POINTS = [
        CrashPoint.BEFORE_JOURNAL_WRITE,
        CrashPoint.AFTER_JOURNAL_WRITE,
        CrashPoint.BEFORE_JOURNAL_FLUSH,
        CrashPoint.AFTER_JOURNAL_FLUSH,
        CrashPoint.BEFORE_COMMIT_WRITE,
    ]
    
    MID_CRASH_POINTS = [
        CrashPoint.AFTER_COMMIT_WRITE,
        CrashPoint.BEFORE_COMMIT_FLUSH,
        CrashPoint.AFTER_COMMIT_FLUSH,
        CrashPoint.BEFORE_HOME_WRITE,
    ]
    
    LATE_CRASH_POINTS = [
        CrashPoint.AFTER_HOME_WRITE,
        CrashPoint.BEFORE_HOME_FLUSH,
        CrashPoint.AFTER_HOME_FLUSH,
        CrashPoint.BEFORE_CHECKPOINT,
        CrashPoint.AFTER_CHECKPOINT,
    ]
    
    def _run_crash_test(self, temp_disk, crash_point, is_early, is_mid, is_late):
        """Helper: create file, crash during write, verify recovery."""
        # Step 1: Format and create a file (no crash injection)
        # (this legacy matrix assumes the ORIGINAL write-through disk model; the strict
        #  volatile-cache matrix lives in tests/test_crash_recovery.py)
        fs = create_filesystem(temp_disk, disk_size=4*1024*1024, cache_mode="writethrough")
        fs.create("/test.txt")
        fs.unmount()
        
        # Step 2: Mount, enable crash injection, attempt write
        fs = open_filesystem(temp_disk, cache_mode="writethrough")
        injector = fs.enable_crash_injection(CrashMode.EXCEPTION)
        injector.enable(crash_point)
        
        crashed = False
        try:
            fs.write("/test.txt", b"recovered_data")
        except Exception:
            crashed = True
        
        assert crashed, f"Expected crash at {crash_point.name} but no crash occurred"
        fs.disable_crash_injection()
        try:
            fs.unmount()
        except Exception:
            pass
        
        # Step 3: Re-mount and verify
        fs2 = open_filesystem(temp_disk, cache_mode="writethrough")
        data = fs2.read("/test.txt")
        
        if is_early:
            # Before commit: transaction should be discarded, data should be empty
            assert data == b'', f"Early crash {crash_point.name}: expected empty data, got {data!r}"
        elif is_mid or is_late:
            # After commit: transaction should be replayed, data should be present
            assert data == b"recovered_data", f"Crash {crash_point.name}: expected recovered_data, got {data!r}"
        
        fs2.unmount()
    
    @pytest.mark.parametrize("crash_point", EARLY_CRASH_POINTS)
    def test_early_crash_point(self, temp_disk, crash_point):
        """Test early crash points (before transaction commit)."""
        self._run_crash_test(temp_disk, crash_point, is_early=True, is_mid=False, is_late=False)
    
    @pytest.mark.parametrize("crash_point", MID_CRASH_POINTS)
    def test_mid_crash_point(self, temp_disk, crash_point):
        """Test mid crash points (after commit, before apply/checkpoint)."""
        self._run_crash_test(temp_disk, crash_point, is_early=False, is_mid=True, is_late=False)
    
    @pytest.mark.parametrize("crash_point", LATE_CRASH_POINTS)
    def test_late_crash_point(self, temp_disk, crash_point):
        """Test late crash points (after apply, before/after checkpoint)."""
        self._run_crash_test(temp_disk, crash_point, is_early=False, is_mid=False, is_late=True)
    
    def test_all_crash_points_crash(self, temp_disk):
        """Verify that each crash point actually triggers a crash."""
        # Recovery-phase crash points only fire when recovery runs
        recovery_points = {
            CrashPoint.DURING_RECOVERY_SCAN,
            CrashPoint.DURING_RECOVERY_REPLAY,
            CrashPoint.AFTER_RECOVERY_REPLAY,
        }
        # Disk-level crash points don't trigger during normal operations
        disk_only_points = {
            CrashPoint.BEFORE_READ, CrashPoint.AFTER_READ,
            CrashPoint.BEFORE_WRITE, CrashPoint.AFTER_WRITE,
            CrashPoint.BEFORE_FLUSH, CrashPoint.AFTER_FLUSH,
        }
        skip_points = recovery_points | disk_only_points
        
        for cp in CrashPoint:
            if cp == CrashPoint.NONE or cp in skip_points:
                continue
            fs = create_filesystem(temp_disk, disk_size=4*1024*1024)
            fs.create("/test.txt")
            fs.unmount()
            
            fs2 = open_filesystem(temp_disk)
            injector = fs2.enable_crash_injection(CrashMode.EXCEPTION)
            injector.enable(cp)
            
            crashed = False
            try:
                fs2.write("/test.txt", b"data")
            except Exception:
                crashed = True
            
            assert crashed, f"Crash point {cp.name} did not trigger a crash"
            fs2.disable_crash_injection()
            try:
                fs2.unmount()
            except Exception:
                pass
            
            # Clean up for next iteration
            import os
            os.remove(temp_disk)


# Consistency Checker Tests
class TestConsistencyChecker:
    def test_check_clean_fs(self, mounted_fs):
        checker = ConsistencyChecker(
            mounted_fs.disk, mounted_fs.superblock,
            mounted_fs.inode_table, mounted_fs.block_allocator,
            mounted_fs.journal
        )
        errors = checker.check_all()
        assert len(errors) == 0
    
    def test_check_detects_corruption(self, mounted_fs):
        # Create a directory and corrupt its data block
        mounted_fs.mkdir("/testdir")
        
        # Get the directory inode
        stat = mounted_fs.stat("/testdir")
        data_blocks = stat['block_pointers']
        assert len(data_blocks) > 0
        corrupted_block = data_blocks[0]
        
        # Corrupt the directory block
        mounted_fs.disk.write_block(corrupted_block, b"CORRUPTED" + b"\x00" * 4087)
        
        checker = ConsistencyChecker(
            mounted_fs.disk, mounted_fs.superblock,
            mounted_fs.inode_table, mounted_fs.block_allocator,
            mounted_fs.journal
        )
        errors = checker.check_all()
        # Should detect directory structure corruption
        assert len(errors) > 0


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])