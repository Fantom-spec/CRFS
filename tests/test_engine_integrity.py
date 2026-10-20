"""
In-process engine tests: transaction semantics, recovery edge cases,
resource exhaustion, corruption handling and fsck behaviour.
"""

import os
import shutil
import tempfile
import unittest

from src import (CrashInjectedError, CrashMode, CrashPoint, DiskFullError, FileSystem,
                 FileSystemError, FileTooLargeError, MountError, create_filesystem, open_filesystem)
from src.inode import InodeType
from src.journal import Transaction
from src.transaction import TransactionContext, TransactionError


class FsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crfs_e_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.img = os.path.join(self.tmp, "t.img")

    def new(self, **kw):
        kw.setdefault("disk_size", 4 * 1024 * 1024)
        fs = create_filesystem(self.img, **kw)
        self.addCleanup(lambda: fs.unmount())
        return fs

    def crash_write(self, fs, point, op, count=1):
        inj = fs.enable_crash_injection(CrashMode.EXCEPTION)
        inj.enable(point, count=count)
        with self.assertRaises(CrashInjectedError):
            op()
        fs.unmount()   # must be a no-op that writes nothing

    def assert_clean(self, fs):
        self.assertEqual(fs.check(), [])


class TestTransactionSemantics(FsCase):
    def test_metadata_writes_outside_transaction_are_rejected(self):
        fs = self.new()
        with self.assertRaises(TransactionError):
            fs.inode_table.allocate_inode(InodeType.FILE)
        with self.assertRaises(TransactionError):
            fs.io.write_block(fs.superblock.inode_table_start_block, b"\0" * 4096)

    def test_uncommitted_changes_do_not_reach_home_blocks(self):
        fs = self.new()
        fs.create("/f")
        before = fs.disk.read_block(fs.superblock.inode_table_start_block)
        tm = fs.tx_manager
        tm.begin()
        fs.root_dir.add_entry("ghost", 5, 1)
        self.assertEqual(fs.disk.read_block(fs.superblock.inode_table_start_block), before)
        self.assertIn("ghost", [e.name for e in fs.root_dir.list_entries()])  # staged read-your-writes
        tm.abort()
        self.assertNotIn("ghost", [e.name for e in fs.root_dir.list_entries()])
        self.assert_clean(fs)

    def test_abort_restores_superblock_counters_and_bitmap(self):
        fs = self.new()
        df = fs.df()
        alloc = fs.block_allocator.get_bitmap_summary()
        with self.assertRaises(RuntimeError):
            with TransactionContext(fs.tx_manager):
                fs.inode_table.allocate_inode(InodeType.FILE)
                fs.block_allocator.allocate_any(5)
                raise RuntimeError("boom")
        self.assertEqual(fs.df(), df)
        self.assertEqual(fs.block_allocator.get_bitmap_summary(), alloc)
        self.assert_clean(fs)

    def test_failed_operation_leaves_no_trace_and_txid_is_reused(self):
        fs = self.new()
        fs.create("/a")
        next_before = fs.journal._next_txid
        with self.assertRaises(FileSystemError):
            fs.create("/a")            # already exists
        with self.assertRaises(FileSystemError):
            fs.mkdir("/nope/sub")      # parent missing
        self.assertEqual(fs.journal._next_txid, next_before)
        self.assert_clean(fs)

    def test_lifecycle_events_follow_protocol_order(self):
        fs = self.new()
        fs.events.clear()
        fs.create("/f")
        kinds = [e["kind"] for e in fs.events.snapshot()]
        want = ["tx_begin", "tx_prepare", "journal_write", "journal_flush", "commit_write",
                "commit_flush", "home_apply", "home_flush", "checkpoint", "fs_op"]
        self.assertEqual(kinds, want)

    def test_commit_orders_flushes_correctly(self):
        """journal flush < commit write < commit flush < home write < home flush < checkpoint"""
        fs = self.new()
        order = []
        real_flush, real_write = fs.disk.flush, fs.disk.write_block
        jlo = fs.superblock.journal_start_block
        jhi = jlo + fs.superblock.journal_block_count

        def flush(phase=None):
            order.append(("flush", phase))
            return real_flush(phase)
        fs.disk.flush = flush
        fs.create("/f")
        phases = [p for k, p in order if k == "flush"]
        self.assertEqual(phases, ["journal", "commit", "home", "checkpoint"])

    def test_data_blocks_never_overwrite_live_blocks(self):
        fs = self.new()
        fs.create("/f")
        fs.write("/f", b"A" * 9000)
        old = fs.stat("/f")["block_pointers"]
        fs.write("/f", b"B" * 9000)
        new = fs.stat("/f")["block_pointers"]
        self.assertFalse(set(old) & set(new), "copy-on-write must use fresh blocks")
        self.assertEqual(fs.read("/f"), b"B" * 9000)
        self.assert_clean(fs)


class TestRecoveryEdgeCases(FsCase):
    def test_uncommitted_transaction_is_never_replayed(self):
        fs = self.new()
        fs.create("/f"); fs.write("/f", b"old")
        self.crash_write(fs, CrashPoint.AFTER_JOURNAL_FLUSH, lambda: fs.write("/f", b"new"))
        fs2 = open_filesystem(self.img)
        self.assertEqual(fs2.read("/f"), b"old")
        self.assertEqual(fs2.last_recovery.transactions_replayed, 0)
        self.assertEqual(fs2.last_recovery.transactions_discarded, 1)
        self.assert_clean(fs2); fs2.unmount()

    def test_committed_transaction_is_replayed_exactly_once_and_repeat_recovery_is_noop(self):
        fs = self.new()
        fs.create("/f")
        self.crash_write(fs, CrashPoint.AFTER_COMMIT_FLUSH, lambda: fs.write("/f", b"new"))
        for i in range(4):
            fs2 = open_filesystem(self.img)
            self.assertEqual(fs2.read("/f"), b"new")
            self.assertEqual(fs2.last_recovery.transactions_replayed, 1 if i == 0 else 0)
            self.assert_clean(fs2)
            fs2.unmount()

    def test_crash_during_recovery_then_recover_again(self):
        fs = self.new()
        fs.create("/f")
        self.crash_write(fs, CrashPoint.AFTER_COMMIT_FLUSH, lambda: fs.write("/f", b"new"))
        for point in (CrashPoint.DURING_RECOVERY_SCAN, CrashPoint.DURING_RECOVERY_REPLAY,
                      CrashPoint.AFTER_RECOVERY_REPLAY, CrashPoint.BEFORE_CHECKPOINT,
                      CrashPoint.DURING_CHECKPOINT_WRITE):
            f = FileSystem(self.img)
            def arm(x, point=point):
                x.enable_crash_injection(CrashMode.EXCEPTION).enable(point)
            with self.assertRaises(CrashInjectedError):
                f.mount(pre_recovery_hook=arm)
        fs3 = open_filesystem(self.img)
        self.assertEqual(fs3.read("/f"), b"new")
        self.assert_clean(fs3); fs3.unmount()

    def test_read_only_mount_without_recovery_then_explicit_recover(self):
        fs = self.new()
        fs.create("/f"); fs.write("/f", b"old")
        self.crash_write(fs, CrashPoint.AFTER_COMMIT_FLUSH, lambda: fs.write("/f", b"new"))
        ro = open_filesystem(self.img, run_recovery=False)
        self.assertEqual(ro.read("/f"), b"old")            # pre-recovery view
        self.assertTrue(any("not yet checkpointed" in w for w in ro.check_report().warnings))
        with self.assertRaises(FileSystemError):
            ro.write("/f", b"x")
        res = ro.recover()
        self.assertEqual(res.transactions_replayed, 1)
        self.assertEqual(ro.read("/f"), b"new")
        ro.write("/f", b"again")                          # writable after recovery
        self.assert_clean(ro); ro.unmount()

    def _crash_with_committed_tx(self):
        fs = self.new()
        fs.create("/f"); fs.write("/f", b"old")
        self.crash_write(fs, CrashPoint.AFTER_COMMIT_FLUSH, lambda: fs.write("/f", b"new"))
        ro = open_filesystem(self.img, run_recovery=False)
        tx = ro.journal.scan().committed[0]
        sb = ro.superblock
        ro.unmount()
        return sb, tx

    def _flip(self, abs_block, offset):
        with open(self.img, "r+b") as f:
            f.seek(abs_block * 4096 + offset)
            b = f.read(1)
            f.seek(abs_block * 4096 + offset)
            f.write(bytes([b[0] ^ 0xFF]))

    def test_corrupt_data_record_discards_transaction(self):
        sb, tx = self._crash_with_committed_tx()
        self._flip(sb.journal_start_block + tx.start_block, 60)
        fs = open_filesystem(self.img)
        self.assertEqual(fs.read("/f"), b"old")
        self.assertEqual(fs.last_recovery.transactions_replayed, 0)
        self.assertTrue(any("corrupt" in w for w in fs.last_recovery.warnings), fs.last_recovery.warnings)
        self.assert_clean(fs); fs.unmount()

    def test_corrupt_commit_record_discards_transaction(self):
        sb, tx = self._crash_with_committed_tx()
        self._flip(sb.journal_start_block + tx.commit_block, 26)
        fs = open_filesystem(self.img)
        self.assertEqual(fs.read("/f"), b"old")
        self.assert_clean(fs); fs.unmount()

    def test_missing_data_records_detected_by_commit_record_count(self):
        sb, tx = self._crash_with_committed_tx()
        # wipe the block that holds the data records but leave the commit record intact
        with open(self.img, "r+b") as f:
            f.seek((sb.journal_start_block + tx.start_block) * 4096)
            f.write(b"\0" * 4096)
        fs = open_filesystem(self.img)
        self.assertEqual(fs.read("/f"), b"old")
        self.assert_clean(fs); fs.unmount()

    def _craft_bad_tx(self, target):
        fs = self.new()
        fs.create("/f")
        fs.unmount()
        ro = open_filesystem(self.img, run_recovery=False)
        j = ro.journal
        tx = Transaction(txid=j.checkpoint_txid + 1)
        tx.add_data_record(target, 0, b"EVIL")
        j.write_transaction(tx); j.flush_journal(); j.write_commit(tx); j.flush_commit()
        ro.unmount()

    def test_stale_pointer_beyond_disk_is_rejected(self):
        self._craft_bad_tx(10 ** 9)
        with self.assertRaises(FileSystemError) as cm:
            open_filesystem(self.img)
        self.assertIn("beyond end of disk", str(cm.exception))

    def test_record_targeting_journal_region_is_rejected(self):
        self._craft_bad_tx(5)
        with self.assertRaises(FileSystemError) as cm:
            open_filesystem(self.img)
        self.assertIn("journal region", str(cm.exception))

    def test_garbage_image_gives_clear_mount_error(self):
        with open(self.img, "wb") as f:
            f.write(os.urandom(4096 * 8))
        with self.assertRaises(MountError):
            open_filesystem(self.img)
        with self.assertRaises(MountError):
            open_filesystem(os.path.join(self.tmp, "missing.img"))

    def test_stale_journal_records_from_earlier_laps_are_ignored(self):
        fs = self.new(journal_blocks=8)
        fs.create("/f")
        for i in range(40):                      # many laps around an 8-block ring
            fs.write("/f", f"v{i}".encode())
        self.crash_write(fs, CrashPoint.AFTER_JOURNAL_FLUSH, lambda: fs.write("/f", b"never"))
        fs2 = open_filesystem(self.img)
        self.assertEqual(fs2.read("/f"), b"v39")
        self.assertEqual(fs2.last_recovery.transactions_replayed, 0)
        self.assert_clean(fs2); fs2.unmount()


class TestResourceExhaustion(FsCase):
    def test_disk_full_is_clean_and_recoverable(self):
        fs = self.new(disk_size=512 * 1024, journal_blocks=8, inode_blocks=2)
        written = []
        with self.assertRaises(DiskFullError):
            for i in range(200):
                fs.create(f"/f{i}")
                fs.write(f"/f{i}", bytes([i % 256]) * 8192)
                written.append(i)
        self.assertGreater(len(written), 3)
        self.assert_clean(fs)
        for i in written:                                       # earlier files intact
            self.assertEqual(fs.read(f"/f{i}"), bytes([i % 256]) * 8192)
        fs.unmount()
        fs = open_filesystem(self.img)
        self.assert_clean(fs)
        fs.delete(f"/f{written[0]}")                            # space can be reclaimed
        fs.create("/again"); fs.write("/again", b"x" * 100)
        self.assert_clean(fs); fs.unmount()

    def test_failed_write_does_not_destroy_old_content(self):
        fs = self.new(disk_size=256 * 1024, journal_blocks=8, inode_blocks=1)
        fs.create("/f"); fs.write("/f", b"precious")
        fs.create("/hog")
        free = fs.df()["free_blocks"]
        with self.assertRaises(DiskFullError):
            fs.write("/hog", b"z" * 4096 * 12 * 100) if False else fs.write("/hog", b"z" * 49152)
            for i in range(100):
                fs.create(f"/h{i}")
                fs.write(f"/h{i}", b"z" * 49152)
        self.assertEqual(fs.read("/f"), b"precious")
        self.assert_clean(fs)

    def test_inode_exhaustion(self):
        fs = self.new(disk_size=1024 * 1024, journal_blocks=8, inode_blocks=1)   # 16 inodes
        n = 0
        with self.assertRaises(DiskFullError):
            for i in range(100):
                fs.create(f"/f{i}")
                n += 1
        self.assertEqual(n, 15)    # root uses inode 1
        self.assert_clean(fs)

    def test_file_too_large(self):
        fs = self.new()
        fs.create("/f")
        with self.assertRaises(FileTooLargeError):
            fs.write("/f", b"x" * (48 * 1024 + 1))
        fs.write("/f", b"x" * (48 * 1024))
        self.assert_clean(fs)

    def test_journal_full_is_reported_and_fs_stays_consistent(self):
        fs = self.new(journal_blocks=4)          # only 3 usable journal blocks
        fs.mkdir("/d")
        for i in range(124):                     # directory fills two blocks (small diffs each)
            fs.create(f"/d/f{i:03d}")
        # removing the FIRST entry shifts every following entry -> a ~8 KB metadata diff
        with self.assertRaises(FileSystemError) as cm:
            fs.delete("/d/f000")
        self.assertIn("journal full", str(cm.exception))
        self.assertEqual(len(fs.ls("/d")), 124)  # nothing changed
        self.assert_clean(fs)
        fs.unmount()
        fs = open_filesystem(self.img)
        self.assert_clean(fs)
        fs.delete("/d/f123")                     # a smaller update still works
        self.assert_clean(fs); fs.unmount()

    def test_lazy_checkpoint_reclaims_journal_automatically(self):
        fs = self.new(journal_blocks=16, checkpoint="lazy")
        fs.create("/f")
        for i in range(80):
            fs.write("/f", f"value-{i}".encode())
        self.assertEqual(fs.read("/f"), b"value-79")
        self.assert_clean(fs)
        self.assertLess(fs.journal.used_blocks(), 16)
        fs.unmount()
        fs = open_filesystem(self.img)
        self.assertEqual(fs.read("/f"), b"value-79"); fs.unmount()


class TestFsckDetectsDamage(FsCase):
    def _fs(self):
        fs = self.new()
        fs.mkdir("/d"); fs.create("/d/f"); fs.write("/d/f", b"hello")
        return fs

    def _raw_patch(self, fs, block, offset, data):
        b = bytearray(fs.disk.read_block(block)); b[offset:offset + len(data)] = data
        fs.disk.write_block(block, bytes(b))

    def codes(self, fs):
        return {f["code"] for f in fs.check_report().findings if f["severity"] == "error"}

    def test_clean_fs_has_no_findings(self):
        self.assertEqual(self._fs().check_report().findings, [])

    def test_bitmap_leak_and_dangling(self):
        fs = self._fs()
        blk = fs.stat("/d/f")["block_pointers"][0]
        idx = blk - fs.superblock.data_start_block
        sb = fs.superblock
        bm = sb.bitmap_start_block
        self._raw_patch(fs, bm, idx // 8, bytes([fs.disk.read_block(bm)[idx // 8] & ~(1 << (idx % 8)) & 0xFF]))
        self.assertIn("REF_BUT_FREE", self.codes(fs))

    def test_duplicate_block_ownership(self):
        fs = self._fs()
        fs.create("/g"); fs.write("/g", b"other")
        a = fs.stat("/d/f"); g = fs.stat("/g")
        ino = fs.inode_table.read_inode(g["inode"])
        ino.direct[0] = a["block_pointers"][0]
        fs.disk.write_block(*self._inode_loc(fs, ino))
        self.assertIn("DUP_BLOCK", self.codes(fs))

    def _inode_loc(self, fs, ino):
        blk, off = fs.inode_table._get_inode_location(ino.inode_num)
        b = bytearray(fs.disk.read_block(blk)); b[off:off + 256] = ino.to_bytes()
        return blk, bytes(b)

    def test_orphan_and_bad_link_count(self):
        fs = self._fs()
        ino = fs.inode_table.read_inode(fs.stat("/d/f")["inode"])
        ino.link_count = 3
        fs.disk.write_block(*self._inode_loc(fs, ino))
        self.assertIn("LINK_COUNT", self.codes(fs))
        # unlink the directory entry behind the filesystem's back -> orphan
        d = fs.root_dir
        fs.tx_manager.begin(); fs._dir(fs.stat("/d")["inode"]).remove_entry("f"); fs.tx_manager.commit()
        self.assertIn("ORPHAN_INODE", self.codes(fs))

    def test_corrupt_directory_block_is_reported(self):
        fs = self._fs()
        blk = fs.stat("/d")["block_pointers"][0]
        fs.disk.write_block(blk, b"CORRUPTED" + b"\0" * 4087)
        self.assertTrue(self.codes(fs) & {"DIR_CORRUPT", "DOT_BAD"})

    def test_dangling_dirent_and_free_count_mismatch(self):
        fs = self._fs()
        ino = fs.inode_table.read_inode(fs.stat("/d/f")["inode"])
        from src.inode import Inode
        fs.disk.write_block(*self._inode_loc(fs, Inode(inode_num=ino.inode_num)))   # free it behind the fs's back
        c = self.codes(fs)
        self.assertIn("DIRENT_FREE_INODE", c)
        self.assertIn("INODE_COUNT", c)


class TestNamespace(FsCase):
    def test_rename_directory_across_parents_updates_dotdot_and_link_counts(self):
        fs = self.new()
        fs.mkdir("/a"); fs.mkdir("/b"); fs.mkdir("/a/sub"); fs.create("/a/sub/f")
        fs.rename("/a/sub", "/b/sub")
        self.assert_clean(fs)
        self.assertEqual([e.name for e in fs.ls("/b/sub")], ["f"])
        with self.assertRaises(FileSystemError):
            fs.rename("/b", "/b/sub/inside")      # into itself
        self.assert_clean(fs)

    def test_many_files_grow_directory_beyond_one_block(self):
        fs = self.new(journal_blocks=64)
        fs.mkdir("/d")
        for i in range(70):
            fs.create(f"/d/f{i}")
        self.assertEqual(len(fs.ls("/d")), 70)
        self.assert_clean(fs)
        fs.unmount()
        fs = open_filesystem(self.img)
        self.assertEqual(len(fs.ls("/d")), 70)
        for i in range(70):
            fs.delete(f"/d/f{i}")
        self.assert_clean(fs); fs.unmount()

    def test_invalid_names_and_paths(self):
        fs = self.new()
        for bad in ("/a/../b", "/./x", "/" + "n" * 60):
            with self.assertRaises(FileSystemError):
                fs.create(bad)
        with self.assertRaises(FileSystemError):
            fs.mkdir("/")
        self.assert_clean(fs)

    def test_repeated_remounts_stay_consistent(self):
        fs = self.new()
        fs.create("/log")
        fs.unmount()
        for i in range(30):
            fs = open_filesystem(self.img)
            fs.append("/log", f"{i};".encode())
            self.assert_clean(fs)
            fs.unmount()
        fs = open_filesystem(self.img)
        self.assertEqual(fs.read("/log").decode().count(";"), 30); fs.unmount()


if __name__ == "__main__":
    unittest.main()
