"""
Crash Injector Module

Provides deterministic crash injection for testing crash recovery.
Supports both in-process (exception-based) and subprocess (SIGKILL) modes.
"""

import os
import sys
import signal
import subprocess
import time
from enum import Enum
from dataclasses import dataclass
from typing import Callable, Optional, List, Dict, Any
from .virtual_disk import VirtualDisk, CrashPoint


class CrashMode(Enum):
    """Crash injection mode."""
    EXCEPTION = "exception"      # Raise exception in-process
    SUBPROCESS = "subprocess"    # Kill subprocess
    NONE = "none"                # No crash injection


@dataclass
class CrashScenario:
    """Defines a crash test scenario."""
    name: str
    description: str
    crash_point: CrashPoint
    operation: str  # Description of operation being tested
    expected_outcome: str  # Expected result after recovery


class CrashInjector:
    """
    Manages crash injection for testing.
    
    Supports:
    - In-process exception-based crashes (for unit tests)
    - Subprocess termination (for realistic crash testing)
    - Configurable crash points
    - Scenario orchestration
    """
    
    # Predefined crash scenarios
    SCENARIOS = [
        CrashScenario(
            name="crash_before_journal_write",
            description="Crash before writing journal records",
            crash_point=CrashPoint.BEFORE_JOURNAL_WRITE,
            operation="file_create",
            expected_outcome="File not created, no journal records"
        ),
        CrashScenario(
            name="crash_during_journal_write",
            description="Crash during journal record writing",
            crash_point=CrashPoint.BEFORE_JOURNAL_WRITE,  # In practice, would be mid-write
            operation="file_create",
            expected_outcome="Partial journal records, transaction discarded"
        ),
        CrashScenario(
            name="crash_after_journal_write_before_flush",
            description="Crash after journal write but before flush",
            crash_point=CrashPoint.AFTER_JOURNAL_WRITE,
            operation="file_create",
            expected_outcome="Journal records in OS buffer, not on disk - discarded"
        ),
        CrashScenario(
            name="crash_after_journal_flush_before_commit",
            description="Crash after journal flush but before commit record",
            crash_point=CrashPoint.AFTER_JOURNAL_FLUSH,
            operation="file_create",
            expected_outcome="Journal records on disk, no commit - discarded"
        ),
        CrashScenario(
            name="crash_during_commit_write",
            description="Crash during commit record write",
            crash_point=CrashPoint.BEFORE_COMMIT_WRITE,
            operation="file_create",
            expected_outcome="Torn commit record - discarded"
        ),
        CrashScenario(
            name="crash_after_commit_write_before_flush",
            description="Crash after commit write but before commit flush",
            crash_point=CrashPoint.AFTER_COMMIT_WRITE,
            operation="file_create",
            expected_outcome="Commit record in OS buffer - may or may not be durable"
        ),
        CrashScenario(
            name="crash_after_commit_flush_before_apply",
            description="Crash after commit flush but before home block apply",
            crash_point=CrashPoint.AFTER_COMMIT_FLUSH,
            operation="file_create",
            expected_outcome="Transaction committed, replay applies changes"
        ),
        CrashScenario(
            name="crash_during_home_apply",
            description="Crash during home block application",
            crash_point=CrashPoint.BEFORE_HOME_WRITE,
            operation="file_create",
            expected_outcome="Transaction committed, replay completes apply"
        ),
        CrashScenario(
            name="crash_after_home_apply_before_flush",
            description="Crash after home apply but before home flush",
            crash_point=CrashPoint.AFTER_HOME_WRITE,
            operation="file_create",
            expected_outcome="Home blocks in OS buffer - replay re-applies"
        ),
        CrashScenario(
            name="crash_after_home_flush_before_checkpoint",
            description="Crash after home flush but before checkpoint",
            crash_point=CrashPoint.AFTER_HOME_FLUSH,
            operation="file_create",
            expected_outcome="Transaction fully applied, checkpoint advances on next mount"
        ),
        CrashScenario(
            name="crash_during_checkpoint",
            description="Crash during checkpoint write",
            crash_point=CrashPoint.BEFORE_CHECKPOINT,
            operation="file_create",
            expected_outcome="Checkpoint incomplete, next recovery re-scans"
        ),
        CrashScenario(
            name="crash_during_recovery_scan",
            description="Crash during recovery journal scan",
            crash_point=CrashPoint.DURING_RECOVERY_SCAN,
            operation="recovery",
            expected_outcome="Recovery restarts, same result"
        ),
        CrashScenario(
            name="crash_during_recovery_replay",
            description="Crash during recovery replay",
            crash_point=CrashPoint.DURING_RECOVERY_REPLAY,
            operation="recovery",
            expected_outcome="Recovery restarts, idempotent replay completes"
        ),
    ]
    
    def __init__(self, mode: CrashMode = CrashMode.EXCEPTION):
        self.mode = mode
        self._crash_point: Optional[CrashPoint] = None
        self._crash_callback: Optional[Callable[[CrashPoint], None]] = None
        self._enabled = False
        self._crash_count = 0
        self._target_crash_count = 1
    
    def enable(self, crash_point: CrashPoint, count: int = 1) -> None:
        """Enable crash injection at specific point."""
        self._crash_point = crash_point
        self._target_crash_count = count
        self._crash_count = 0
        self._enabled = True
    
    def disable(self) -> None:
        """Disable crash injection."""
        self._enabled = False
        self._crash_point = None
    
    def set_callback(self, callback: Callable[[CrashPoint], None]) -> None:
        """Set callback to invoke at crash point."""
        self._crash_callback = callback
    
    def maybe_crash(self, point: CrashPoint) -> None:
        """Check if crash should be injected at this point."""
        if not self._enabled or self._crash_point is None:
            return
        
        if point == self._crash_point:
            self._crash_count += 1
            if self._crash_count >= self._target_crash_count:
                self._trigger_crash(point)
    
    def _trigger_crash(self, point: CrashPoint) -> None:
        """Trigger the crash."""
        if self._crash_callback:
            self._crash_callback(point)
        
        if self.mode == CrashMode.EXCEPTION:
            raise CrashInjectedError(f"Crash injected at {point.name}")
        elif self.mode == CrashMode.SUBPROCESS:
            # In subprocess mode, the parent process manages the kill
            os.kill(os.getpid(), signal.SIGKILL)
        else:
            pass  # NONE mode - just record
    
    def get_scenarios(self) -> List[CrashScenario]:
        """Get all predefined crash scenarios."""
        return self.SCENARIOS
    
    def get_scenario(self, name: str) -> Optional[CrashScenario]:
        """Get scenario by name."""
        for s in self.SCENARIOS:
            if s.name == name:
                return s
        return None


class CrashInjectedError(Exception):
    """Raised when crash is injected in exception mode."""
    pass


class SubprocessCrashRunner:
    """
    Runs crash tests in subprocesses for realistic crash simulation.
    
    This is the preferred mode for integration testing because it
    actually terminates the process, simulating power loss.
    """
    
    def __init__(self, test_script: str, disk_image: str):
        self.test_script = test_script
        self.disk_image = disk_image
        self.results: List[Dict[str, Any]] = []
    
    def run_scenario(self, scenario: CrashScenario, setup_commands: List[str]) -> Dict[str, Any]:
        """
        Run a crash scenario in a subprocess.
        
        Args:
            scenario: Crash scenario to test
            setup_commands: Commands to run before crash point
            
        Returns:
            Result dictionary with outcome
        """
        # Create test script that:
        # 1. Runs setup commands
        # 2. Enables crash injection at scenario.crash_point
        # 3. Performs the operation
        # 4. If crash injected, process dies
        # 5. Parent process restarts, runs recovery, checks result
        
        test_code = self._generate_test_code(scenario, setup_commands)
        
        # Write test script
        script_path = f"/tmp/crash_test_{scenario.name}.py"
        with open(script_path, 'w') as f:
            f.write(test_code)
        
        # Run subprocess
        start_time = time.time()
        try:
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            duration = time.time() - start_time
            
            crashed = result.returncode != 0 and result.returncode != -signal.SIGKILL
            killed = result.returncode == -signal.SIGKILL
            
            return {
                'scenario': scenario.name,
                'crashed': crashed,
                'killed': killed,
                'returncode': result.returncode,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'duration': duration,
            }
        except subprocess.TimeoutExpired:
            return {
                'scenario': scenario.name,
                'crashed': False,
                'killed': False,
                'error': 'Timeout',
                'duration': time.time() - start_time,
            }
    
    def _generate_test_code(self, scenario: CrashScenario, setup_commands: List[str]) -> str:
        """Generate test script for scenario."""
        # This is a template - actual implementation would be more complex
        return f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.abspath(__file__))}")

from src.filesystem import FileSystem
from src.crash_injector import CrashInjector, CrashMode, CrashPoint

# Setup
fs = FileSystem("{self.disk_image}")
fs.mount()

# Run setup commands
{chr(10).join(setup_commands)}

# Enable crash injection
injector = CrashInjector(CrashMode.SUBPROCESS)
injector.enable(CrashPoint.{scenario.crash_point.name})

# Perform operation (this should crash)
try:
    # Operation would go here
    pass
except Exception as e:
    print(f"Crashed: {{e}}")
    sys.exit(1)

print("Completed without crash")
sys.exit(0)
'''
    
    def run_all_scenarios(self, setup_commands: List[str]) -> List[Dict[str, Any]]:
        """Run all crash scenarios."""
        results = []
        for scenario in CrashInjector.SCENARIOS:
            print(f"Running scenario: {scenario.name}")
            result = self.run_scenario(scenario, setup_commands)
            results.append(result)
            print(f"  Result: {'CRASHED' if result.get('crashed') or result.get('killed') else 'NO CRASH'}")
        return results


# Global crash injector instance for easy access
_global_injector: Optional[CrashInjector] = None


def get_global_injector() -> CrashInjector:
    """Get or create global crash injector."""
    global _global_injector
    if _global_injector is None:
        _global_injector = CrashInjector()
    return _global_injector


def set_global_injector(injector: CrashInjector) -> None:
    """Set global crash injector."""
    global _global_injector
    _global_injector = injector


def maybe_crash(point: CrashPoint) -> None:
    """Global crash point function."""
    injector = get_global_injector()
    injector.maybe_crash(point)