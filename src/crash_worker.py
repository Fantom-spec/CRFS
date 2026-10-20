"""
Crash worker: runs ONE phase of a crash scenario in its own OS process.

    python -m src.crash_worker '<json args>'

Phases
  setup    format a fresh image, create the baseline files, unmount cleanly
  crash    mount, arm the injector in SUBPROCESS mode, run the operation.
           If the crash point is reached the process is SIGKILLed (exit -9).
  recrash  mount with the injector armed BEFORE recovery runs (for the
           'crash during recovery' scenarios) -> SIGKILLed during recovery
  verify   mount (runs recovery), probe the state, run the checker, run
           recovery a second time and probe again

Every phase prints one line  CRFS_RESULT:<json>  on stdout (except a phase
that is killed).  Engine events are appended to args['events_file'] as they
happen, so even a killed process leaves its trail.
"""

import json
import sys

from .crash_injector import CrashInjector, CrashMode
from .events import EventLog
from .filesystem import FileSystem
from .virtual_disk import CrashPoint

LAB_DIR = "/lab"


def probe(fs: FileSystem) -> dict:
    """Observable state of the lab directory (decoded from the real image)."""
    entries = fs.ls(LAB_DIR)
    files, listing = {}, {}
    for e in entries:
        listing[e.name] = "dir" if e.file_type == 2 else "file"
        if e.file_type == 1:
            files[e.name] = fs.read(f"{LAB_DIR}/{e.name}").decode("utf-8", "replace")
    return {"listing": dict(sorted(listing.items())), "files": dict(sorted(files.items()))}


# operation name -> (setup(fs), run(fs), apply_to_probe(probe) -> probe after commit)
def _op_create(fs):
    fs.create(f"{LAB_DIR}/new.txt")


def _op_write(fs):
    fs.write(f"{LAB_DIR}/data.txt", b"NEW DATA")


def _op_mkdir(fs):
    fs.mkdir(f"{LAB_DIR}/newdir")


def _op_delete(fs):
    fs.delete(f"{LAB_DIR}/victim.txt")


def _op_rename(fs):
    fs.rename(f"{LAB_DIR}/old.txt", f"{LAB_DIR}/renamed.txt")


def _op_bigwrite(fs):
    fs.write(f"{LAB_DIR}/big.txt", b"B" * 10000)


def _after(p, fn):
    import copy
    q = copy.deepcopy(p)
    fn(q)
    q["listing"] = dict(sorted(q["listing"].items()))
    q["files"] = dict(sorted(q["files"].items()))
    return q


def _rename_probe(q):
    q["listing"]["renamed.txt"] = q["listing"].pop("old.txt")
    q["files"]["renamed.txt"] = q["files"].pop("old.txt")


OPERATIONS = {
    "create_file": {
        "label": "create file /lab/new.txt", "setup": [], "run": _op_create,
        "committed": lambda p: _after(p, lambda q: q["listing"].__setitem__("new.txt", "file") or
                                      q["files"].__setitem__("new.txt", ""))},
    "write_file": {
        "label": "overwrite /lab/data.txt ('OLD DATA' -> 'NEW DATA')",
        "setup": [("data.txt", b"OLD DATA")], "run": _op_write,
        "committed": lambda p: _after(p, lambda q: q["files"].__setitem__("data.txt", "NEW DATA"))},
    "mkdir": {
        "label": "create directory /lab/newdir", "setup": [], "run": _op_mkdir,
        "committed": lambda p: _after(p, lambda q: q["listing"].__setitem__("newdir", "dir"))},
    "delete_file": {
        "label": "delete /lab/victim.txt", "setup": [("victim.txt", b"VICTIM")], "run": _op_delete,
        "committed": lambda p: _after(p, lambda q: (q["listing"].pop("victim.txt"),
                                                    q["files"].pop("victim.txt")))},
    "rename_file": {
        "label": "rename /lab/old.txt -> /lab/renamed.txt", "setup": [("old.txt", b"RENAME ME")],
        "run": _op_rename, "committed": lambda p: _after(p, _rename_probe)},
    "multi_block_write": {
        "label": "write 10000 bytes (3 blocks) to /lab/big.txt", "setup": [("big.txt", b"A" * 5000)],
        "run": _op_bigwrite,
        "committed": lambda p: _after(p, lambda q: q["files"].__setitem__("big.txt", "B" * 10000))},
}


def main(argv=None) -> int:
    args = json.loads((argv or sys.argv)[1])
    phase = args["phase"]
    image = args["image"]
    cache_mode = args.get("cache_mode", "volatile")
    events = EventLog(sink_path=args.get("events_file"))
    fs = FileSystem(image, cache_mode=cache_mode, checkpoint=args.get("checkpoint", "eager"),
                    events=events)
    out = {"phase": phase}

    if phase == "setup":
        op = OPERATIONS[args["operation"]]
        fs.format(disk_size=args.get("disk_size", 2 * 1024 * 1024),
                  journal_blocks=args.get("journal_blocks", 32), inode_blocks=8)
        fs.mkdir(LAB_DIR)
        fs.create(f"{LAB_DIR}/base.txt")
        fs.write(f"{LAB_DIR}/base.txt", b"BASELINE")
        for name, content in op["setup"]:
            fs.create(f"{LAB_DIR}/{name}")
            fs.write(f"{LAB_DIR}/{name}", content)
        for _ in range(args.get("fill", 0)):      # advance the journal head (wraparound tests)
            fs.write(f"{LAB_DIR}/base.txt", b"BASELINE")
        out["probe"] = probe(fs)
        fs.unmount()

    elif phase == "crash":
        op = OPERATIONS[args["operation"]]
        fs.mount()
        for _ in range(args.get("prefill", 0)):   # committed txs that stay in the journal (lazy checkpoint)
            fs.write(f"{LAB_DIR}/base.txt", b"BASELINE")
        inj = fs.enable_crash_injection(CrashMode.SUBPROCESS)
        inj.enable(CrashPoint[args["point"]], count=args.get("count", 1),
                   torn_bytes=args.get("torn_bytes", 0))
        events.emit("lab", f"running: {op['label']} with crash armed at {args['point']}")
        op["run"](fs)
        # only reached if the crash point never fired
        events.emit("lab", "operation finished WITHOUT hitting the crash point", severity="warning")
        out["crash_point_reached"] = False
        fs.unmount()

    elif phase == "userop":
        # crash demo on a REAL image: run one user operation with a crash armed, SIGKILL at the point
        fs.mount()
        mode, path, text = args["mode"], args["path"], args.get("text", "")
        inj = fs.enable_crash_injection(CrashMode.SUBPROCESS)
        if mode == "create":          # two transactions: create (tx 1, completes) then write (tx 2, crashes)
            inj.enable(CrashPoint.AFTER_COMMIT_FLUSH, count=2)
            events.emit("lab", f"demo: create {path} then write it; crash armed after the 2nd commit is durable")
            fs.create(path)
            fs.write(path, text.encode("utf-8"))
        elif mode == "overwrite":
            inj.enable(CrashPoint.AFTER_COMMIT_FLUSH, count=1)
            events.emit("lab", f"demo: overwrite {path}; crash armed after its commit is durable")
            fs.write(path, text.encode("utf-8"))
        else:
            raise SystemExit(f"unknown mode {mode}")
        out["crash_point_reached"] = False
        fs.unmount()

    elif phase == "recrash":
        def arm(f):
            inj = f.enable_crash_injection(CrashMode.SUBPROCESS)
            inj.enable(CrashPoint[args["point"]], count=args.get("count", 1))
        events.emit("lab", f"mounting; crash armed at {args['point']} during recovery")
        fs.mount(pre_recovery_hook=arm)
        out["crash_point_reached"] = False
        fs.unmount()

    elif phase == "inspect":
        # look at the crashed image BEFORE recovery (read-only mount, nothing is modified)
        fs.mount(run_recovery=False)
        out["probe"] = probe(fs)
        out["journal"] = fs.journal_view()
        scan = fs.journal.scan()
        out["pending_committed"] = [t.txid for t in scan.committed]
        out["incomplete"] = scan.incomplete
        out["check"] = fs.check_report().to_dict()
        fs.unmount()

    elif phase == "verify":
        fs.mount(run_recovery=bool(args.get("run_recovery", True)))
        rec = fs.last_recovery
        out["recovery"] = rec.to_dict() if rec else None
        out["probe"] = probe(fs)
        rep = fs.check_report()
        out["check"] = rep.to_dict()
        rec2 = fs.recover()
        out["recovery_again"] = rec2.to_dict()
        out["probe_again"] = probe(fs)
        out["check_again"] = fs.check_report().to_dict()
        out["df"] = fs.df()
        fs.unmount()
    else:
        raise SystemExit(f"unknown phase {phase}")

    events.close()
    print("CRFS_RESULT:" + json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
