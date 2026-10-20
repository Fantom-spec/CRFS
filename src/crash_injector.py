"""
Crash Injector Module

Deterministic crash injection.

The engine calls ``disk.fire(point, info)`` at every important boundary of the
commit/recovery protocol.  A ``CrashInjector`` attached to the disk decides
whether this is *the* crash:

* ``CrashMode.EXCEPTION``  - the injector first makes the disk behave as if
  power was lost (all unflushed writes are discarded, the file handle is
  closed) and then raises ``CrashInjectedError``.  Fast; used by unit tests.
* ``CrashMode.SUBPROCESS`` - the injector SIGKILLs the current process.  The
  process gets no chance to flush, close or clean up.  Used by the
  subprocess crash tests and by the UI crash lab.
* ``CrashMode.NONE``       - only records that the point was reached.

``enable(point, count=k)`` crashes on the k-th time the point is reached.
For the mid-flush points (DURING_*_WRITE) ``count`` selects *which physical
block write* of the flush is the last to NOT happen, so count=1 means "none of
the pending blocks reached the disk", count=3 means "two did".  ``torn_bytes``
additionally persists only a prefix of the block that was about to be written
(a torn sector write).
"""

import os
import signal
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .virtual_disk import CrashInjectedError, CrashPoint, VirtualDisk


class CrashMode(Enum):
    EXCEPTION = "exception"
    SUBPROCESS = "subprocess"
    NONE = "none"


@dataclass
class CrashScenario:
    name: str
    description: str
    crash_point: CrashPoint
    operation: str
    expected_outcome: str
    # 'rolled_back' -> operation must NOT survive recovery
    # 'committed'   -> operation MUST survive recovery
    # (under the volatile-cache durability model; see docs/journaling-and-recovery.md)
    expected_state: str = "rolled_back"
    count: int = 1
    torn_bytes: int = 0
    phase: str = "transaction"  # or 'recovery'


class CrashInjector:
    SCENARIOS = [
        CrashScenario("crash_before_journal_write", "Crash before any journal record is written",
                      CrashPoint.BEFORE_JOURNAL_WRITE, "file_create",
                      "Nothing reached the disk; operation absent", "rolled_back"),
        CrashScenario("crash_during_journal_write",
                      "Crash in the middle of the journal flush (only some blocks persisted)",
                      CrashPoint.DURING_JOURNAL_WRITE, "file_create",
                      "Torn journal, no commit record; transaction discarded", "rolled_back"),
        CrashScenario("crash_after_journal_write_before_flush",
                      "Crash after journal records were written but before they were flushed",
                      CrashPoint.AFTER_JOURNAL_WRITE, "file_create",
                      "Journal records only in the volatile cache; lost", "rolled_back"),
        CrashScenario("crash_after_journal_flush_before_commit",
                      "Journal flushed but commit record not yet written",
                      CrashPoint.AFTER_JOURNAL_FLUSH, "file_create",
                      "Complete journal records but no commit; discarded", "rolled_back"),
        CrashScenario("crash_during_commit_write",
                      "Commit record torn (only a prefix of its block persisted)",
                      CrashPoint.DURING_COMMIT_WRITE, "file_create",
                      "Torn commit record fails its checksum; discarded", "rolled_back", torn_bytes=12),
        CrashScenario("crash_after_commit_write_before_flush",
                      "Commit record written but not flushed",
                      CrashPoint.AFTER_COMMIT_WRITE, "file_create",
                      "Commit not durable (volatile cache); discarded", "rolled_back"),
        CrashScenario("crash_after_commit_flush_before_apply",
                      "Commit durable, home locations not yet updated",
                      CrashPoint.AFTER_COMMIT_FLUSH, "file_create",
                      "Committed: recovery replays the journal", "committed"),
        CrashScenario("crash_during_home_apply",
                      "Crash while home metadata blocks are being persisted",
                      CrashPoint.DURING_HOME_WRITE, "file_create",
                      "Committed; some home blocks updated, recovery completes the rest", "committed",
                      count=2),
        CrashScenario("crash_after_home_apply_before_flush",
                      "Home blocks written but not flushed",
                      CrashPoint.AFTER_HOME_WRITE, "file_create",
                      "Committed; home writes lost, recovery re-applies", "committed"),
        CrashScenario("crash_after_home_flush_before_checkpoint",
                      "Home blocks durable, checkpoint not yet persisted",
                      CrashPoint.AFTER_HOME_FLUSH, "file_create",
                      "Committed; replay is idempotent", "committed"),
        CrashScenario("crash_during_checkpoint",
                      "Crash while the checkpoint (superblock) is being persisted",
                      CrashPoint.DURING_CHECKPOINT_WRITE, "file_create",
                      "Committed; old checkpoint stays, recovery re-replays idempotently", "committed"),
        CrashScenario("crash_after_checkpoint",
                      "Crash right after the checkpoint became durable",
                      CrashPoint.AFTER_CHECKPOINT, "file_create",
                      "Committed and checkpointed; nothing to replay", "committed"),
        CrashScenario("crash_during_recovery_scan",
                      "Crash while recovery is scanning the journal",
                      CrashPoint.DURING_RECOVERY_SCAN, "recovery",
                      "Recovery restarts and reaches the same result", "committed", phase="recovery"),
        CrashScenario("crash_during_recovery_replay",
                      "Crash while recovery is replaying committed transactions",
                      CrashPoint.DURING_RECOVERY_REPLAY, "recovery",
                      "Recovery restarts; idempotent replay completes", "committed", phase="recovery"),
    ]

    def __init__(self, mode: CrashMode = CrashMode.EXCEPTION):
        self.mode = mode
        self._crash_point: Optional[CrashPoint] = None
        self._enabled = False
        self._count = 0
        self._target = 1
        self._torn_bytes = 0
        self._disk: Optional[VirtualDisk] = None
        self._crash_callback: Optional[Callable[[CrashPoint], None]] = None
        self.events = None
        self.reached: List[str] = []
        self.crashed_at: Optional[CrashPoint] = None

    # ------------------------------------------------------------ control
    def attach_disk(self, disk: VirtualDisk) -> None:
        self._disk = disk

    def enable(self, crash_point: CrashPoint, count: int = 1, torn_bytes: int = 0) -> None:
        self._crash_point = crash_point
        self._target = max(1, count)
        self._count = 0
        self._torn_bytes = torn_bytes
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False
        self._crash_point = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_callback(self, callback: Callable[[CrashPoint], None]) -> None:
        self._crash_callback = callback

    # --------------------------------------------------------------- hook
    def maybe_crash(self, point: CrashPoint, info: Optional[dict] = None) -> None:
        if not self._enabled or self._crash_point is None:
            return
        if point == self._crash_point:
            self._count += 1
            if self._count >= self._target:
                self._trigger_crash(point, info)

    __call__ = maybe_crash

    def _trigger_crash(self, point: CrashPoint, info: Optional[dict]) -> None:
        self.crashed_at = point
        if self.events is not None:
            self.events.emit("crash_injected", f"CRASH injected at {point.name}"
                             + (f" (block {info['block']}, {info['index']}/{info['total']})"
                                if info and 'block' in info else ""),
                             severity="error", point=point.name, mode=self.mode.value,
                             info=info or {})
        if self._crash_callback:
            self._crash_callback(point)
        if self.mode == CrashMode.NONE:
            return
        # torn sector write of the block that was about to be written
        if self._torn_bytes and info and "block" in info and self._disk is not None:
            self._disk.torn_write(info["block"], self._torn_bytes)
        if self.mode == CrashMode.SUBPROCESS:
            os.kill(os.getpid(), signal.SIGKILL)
        if self._disk is not None:
            self._disk.crash()          # power loss: unflushed writes vanish
        raise CrashInjectedError(f"Crash injected at {point.name}", point)

    # ---------------------------------------------------------- scenarios
    def get_scenarios(self) -> List[CrashScenario]:
        return self.SCENARIOS

    def get_scenario(self, name: str) -> Optional[CrashScenario]:
        for s in self.SCENARIOS:
            if s.name == name:
                return s
        return None


# Global injector (kept for API compatibility with the original CLI)
_global_injector: Optional[CrashInjector] = None


def get_global_injector() -> CrashInjector:
    global _global_injector
    if _global_injector is None:
        _global_injector = CrashInjector()
    return _global_injector


def set_global_injector(injector: CrashInjector) -> None:
    global _global_injector
    _global_injector = injector


def maybe_crash(point: CrashPoint) -> None:
    get_global_injector().maybe_crash(point)
