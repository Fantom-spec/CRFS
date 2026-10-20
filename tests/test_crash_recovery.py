"""
Crash-recovery tests that use REAL process death.

The crash lab (src/crash_lab.py) runs each scenario in three separate OS
processes: setup -> crash (SIGKILL at the chosen protocol boundary) -> fresh
verifier that mounts the same image, lets recovery run, inspects the state and
runs fsck.  A scenario only passes if the observed state is exactly what the
protocol requires; "recovery did not raise" is never sufficient.

Run:  python -m unittest tests.test_crash_recovery -v      (or: pytest tests/)
"""

import shutil
import tempfile
import unittest

from src.crash_injector import CrashInjector
from src.crash_lab import run_scenario
from src.crash_worker import OPERATIONS

SCENARIOS = CrashInjector.SCENARIOS
MID_FLUSH = {"crash_during_journal_write", "crash_during_commit_write",
             "crash_during_home_apply", "crash_during_checkpoint"}


def assert_pass(tc, r, label=""):
    tc.assertEqual(r["verdict"], "PASS", f"{label}: {r.get('error')} checks={r.get('checks')} "
                   f"actual={r.get('actual_probe')} expected={r.get('expected_probe')}")
    tc.assertTrue(r["crash_reached"], f"{label}: the crash point was never reached (process not killed)")
    tc.assertEqual(r["survived"], r["expected_state"] == "committed", label)


class _LabCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crfs_t_")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestCrashMatrixVolatile(_LabCase):
    """Every scenario x every operation, strict volatile-cache disk, SIGKILL."""

    def test_full_matrix(self):
        for op in OPERATIONS:
            for sc in SCENARIOS:
                with self.subTest(op=op, scenario=sc.name):
                    r = run_scenario(sc.name, op, workdir=f"{self.tmp}/{op}-{sc.name}")
                    assert_pass(self, r, f"{op}/{sc.name}")


class TestCrashMatrixWriteThrough(_LabCase):
    """
    Write-through disk model (every write durable immediately).  Mid-flush
    crash points do not exist in this model, so only the boundary points run.
    """

    def test_boundaries(self):
        for sc in SCENARIOS:
            if sc.name in MID_FLUSH:
                continue
            with self.subTest(scenario=sc.name):
                r = run_scenario(sc.name, "write_file", workdir=f"{self.tmp}/{sc.name}",
                                 cache_mode="writethrough")
                assert_pass(self, r, sc.name)

    def test_commit_written_unflushed_is_durable_when_writethrough(self):
        r = run_scenario("crash_after_commit_write_before_flush", "write_file",
                         workdir=f"{self.tmp}/x", cache_mode="writethrough")
        self.assertEqual(r["expected_state"], "committed")
        self.assertTrue(r["survived"])


class TestMidFlushSweeps(_LabCase):
    """Crash after k of n physical block writes of a flush (torn multi-block writes)."""

    def _sweep(self, scenario, expected, max_k=12):
        reached_any = 0
        for k in range(1, max_k + 1):
            r = run_scenario(scenario, "multi_block_write", workdir=f"{self.tmp}/{scenario}-{k}",
                             count=k, expected_override=expected)
            if not r["crash_reached"]:
                break               # the flush has fewer than k blocks
            reached_any += 1
            assert_pass(self, r, f"{scenario} k={k}")
        self.assertGreaterEqual(reached_any, 3, "sweep should cover several block positions")

    def test_journal_flush_torn_at_every_block(self):
        self._sweep("crash_during_journal_write", "rolled_back")

    def test_home_apply_torn_at_every_block(self):
        self._sweep("crash_during_home_apply", "committed")

    def test_commit_record_torn_at_various_prefixes(self):
        # the commit record is 24 (header) + 12 (payload) = 36 bytes long: any shorter
        # prefix fails its checksum (note: a 35-byte prefix would also be 'complete', because the
        # last byte is 0x00 and the block previously held zeros) -> rolled back; a prefix covering all 36 bytes IS a
        # complete commit record -> the transaction is committed.
        for nbytes in (1, 8, 12, 24, 32, 36, 40, 100):
            whole = nbytes >= 36
            with self.subTest(torn_bytes=nbytes):
                r = run_scenario("crash_during_commit_write", "write_file",
                                 workdir=f"{self.tmp}/torn{nbytes}", torn_bytes=nbytes,
                                 expected_override="committed" if whole else "rolled_back")
                assert_pass(self, r, f"torn {nbytes}")
                self.assertEqual(r["survived"], whole)


class TestWraparound(_LabCase):
    """Small ring (8 blocks) so transactions straddle / wrap the end of the journal."""

    POINTS = ["crash_after_journal_flush_before_commit", "crash_during_commit_write",
              "crash_after_commit_flush_before_apply", "crash_during_home_apply",
              "crash_after_home_flush_before_checkpoint", "crash_during_checkpoint"]

    def test_crash_at_every_ring_position(self):
        for fill in range(0, 8):
            for sc in self.POINTS:
                with self.subTest(fill=fill, scenario=sc):
                    r = run_scenario(sc, "write_file", workdir=f"{self.tmp}/{fill}-{sc}",
                                     journal_blocks=8, fill=fill)
                    assert_pass(self, r, f"fill={fill} {sc}")


class TestLazyCheckpoint(_LabCase):
    """Several committed transactions sit in the journal when the crash happens."""

    def test_recovery_replays_many_transactions(self):
        for sc in SCENARIOS:
            with self.subTest(scenario=sc.name):
                r = run_scenario(sc.name, "mkdir", workdir=f"{self.tmp}/{sc.name}",
                                 checkpoint="lazy", prefill=3, journal_blocks=32)
                assert_pass(self, r, sc.name)

    def test_lazy_pending_transactions_are_actually_replayed(self):
        r = run_scenario("crash_after_commit_flush_before_apply", "create_file",
                         workdir=f"{self.tmp}/p", checkpoint="lazy", prefill=3)
        self.assertEqual(r["verdict"], "PASS")
        self.assertGreaterEqual(r["recovery"]["transactions_replayed"], 4)


if __name__ == "__main__":
    unittest.main()
