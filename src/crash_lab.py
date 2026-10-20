"""
Crash lab: runs a crash scenario end to end across REAL processes.

    1. setup   process formats an image, creates the baseline, exits cleanly
    2. crash   a second process mounts it, arms the injector, starts the
               operation and is SIGKILLed at the chosen crash point
    3. verify  a third, fresh process mounts the same image (recovery runs),
               inspects the filesystem, runs the consistency checker, runs
               recovery again and re-inspects

The verdict compares the OBSERVED state with the state the protocol requires
(`rolled_back` or `committed`).  "Recovery returned without an exception" is
never enough.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

from .crash_injector import CrashInjector, CrashScenario
from .crash_worker import OPERATIONS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_worker(args: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    t0 = time.time()
    cp = subprocess.run([sys.executable, "-m", "src.crash_worker", json.dumps(args)],
                        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    result = None
    for line in cp.stdout.splitlines():
        if line.startswith("CRFS_RESULT:"):
            result = json.loads(line[len("CRFS_RESULT:"):])
    return {"returncode": cp.returncode, "killed": cp.returncode == -signal.SIGKILL,
            "result": result, "stderr": cp.stderr[-2000:], "seconds": round(time.time() - t0, 3)}


def read_events(path: str) -> List[Dict[str, Any]]:
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def expected_state_for(scenario: CrashScenario, cache_mode: str) -> str:
    """'rolled_back' or 'committed' for this scenario under the given durability model."""
    if cache_mode == "writethrough" and scenario.name in (
            "crash_after_commit_write_before_flush",):
        return "committed"      # a written commit record is already durable when writes are write-through
    return scenario.expected_state


def run_scenario(scenario_name: str, operation: str = "create_file", workdir: Optional[str] = None,
                 cache_mode: str = "volatile", count: Optional[int] = None,
                 torn_bytes: Optional[int] = None, journal_blocks: int = 32,
                 checkpoint: str = "eager", fill: int = 0, prefill: int = 0,
                 expected_override: Optional[str] = None,
                 point_override: Optional[str] = None, progress=None) -> Dict[str, Any]:
    scenario = CrashInjector().get_scenario(scenario_name)
    if scenario is None:
        raise ValueError(f"unknown scenario {scenario_name!r}")
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}")
    own = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="crfs_lab_")
    os.makedirs(workdir, exist_ok=True)
    image = os.path.join(workdir, "lab.img")
    events_file = os.path.join(workdir, "events.jsonl")
    for f in (image, events_file):
        if os.path.exists(f):
            os.remove(f)
    common = {"image": image, "events_file": events_file, "cache_mode": cache_mode,
              "operation": operation, "checkpoint": checkpoint, "journal_blocks": journal_blocks,
              "fill": fill, "prefill": prefill}
    steps: List[Dict[str, Any]] = []

    def note(step):
        steps.append(step)
        if progress:
            progress(step)
    point = point_override or scenario.crash_point.name
    expected = expected_override or expected_state_for(scenario, cache_mode)

    s1 = _run_worker({**common, "phase": "setup"})
    note({"step": "setup", **{k: s1[k] for k in ("returncode", "seconds")}})
    if s1["returncode"] != 0 or not s1["result"]:
        return {"scenario": scenario_name, "verdict": "ERROR", "error": "setup failed: " + s1["stderr"],
                "steps": steps, "events": read_events(events_file), "workdir": workdir}
    before = s1["result"]["probe"]

    if scenario.phase == "recovery":
        # leave a committed-but-unreplayed transaction, then crash *during recovery*
        c = _run_worker({**common, "phase": "crash", "point": "AFTER_COMMIT_FLUSH", "count": 1})
        note({"step": "crash (leave committed tx in journal)", "killed": c["killed"],
                      "returncode": c["returncode"]})
        r = _run_worker({**common, "phase": "recrash", "point": point,
                         "count": count if count is not None else scenario.count})
        note({"step": f"crash during recovery at {point}", "killed": r["killed"],
                      "returncode": r["returncode"]})
        crashed = c["killed"] and r["killed"]
    else:
        c = _run_worker({**common, "phase": "crash", "point": point,
                         "count": count if count is not None else scenario.count,
                         "torn_bytes": torn_bytes if torn_bytes is not None else scenario.torn_bytes})
        note({"step": f"run '{OPERATIONS[operation]['label']}' with crash at {point}",
                      "killed": c["killed"], "returncode": c["returncode"], "stderr": c["stderr"]})
        crashed = c["killed"]

    ins = _run_worker({**common, "phase": "inspect"})
    note({"step": "inspect the crashed image BEFORE recovery (read-only mount)",
          "returncode": ins["returncode"], "seconds": ins["seconds"]})
    v = _run_worker({**common, "phase": "verify"})
    note({"step": "fresh process: mount + recovery + inspect + fsck", "returncode": v["returncode"],
          "seconds": v["seconds"]})
    out: Dict[str, Any] = {
        "scenario": scenario_name, "description": scenario.description, "operation": operation,
        "crash_point": point, "cache_mode": cache_mode, "expected_state": expected,
        "crash_reached": crashed, "steps": steps, "workdir": workdir, "image": image,
        "events": read_events(events_file), "before": before,
        "pre_recovery": ins["result"],
    }
    if v["returncode"] != 0 or not v["result"]:
        out.update({"verdict": "ERROR", "error": "verify process failed: " + v["stderr"]})
        return out
    vr = v["result"]
    committed_state = OPERATIONS[operation]["committed"](before)
    want = committed_state if expected == "committed" else before
    actual = vr["probe"]
    checks = {
        "process_was_killed_at_crash_point": crashed,
        "state_matches_expectation": actual == want,
        "baseline_file_intact": actual["files"].get("base.txt") == "BASELINE",
        "fsck_clean_after_recovery": not vr["check"]["errors"],
        "recovery_succeeded": bool(vr["recovery"] and vr["recovery"]["success"]),
        "second_recovery_idempotent": (vr["probe_again"] == actual and not vr["check_again"]["errors"]
                                       and vr["recovery_again"]["transactions_replayed"] == 0),
    }
    out.update({
        "expected_probe": want, "actual_probe": actual, "recovery": vr["recovery"],
        "check": vr["check"], "checks": checks,
        "survived": actual == committed_state,
        "verdict": "PASS" if all(checks.values()) else "FAIL",
    })
    return out
