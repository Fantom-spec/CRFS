"""
CRFS web UI backend (standard library only).

Design rules
  * The UI never touches a disk image itself: every action calls the real
    engine (src.FileSystem) and reports what the engine returned or raised.
  * Images live in ONE configured directory; names are validated; no
    arbitrary paths, no shell.
  * The server binds to loopback only and every state-changing request needs a
    per-launch token (defends against other web pages / DNS rebinding).
  * Destructive actions (format, delete image, crash-lab run) need an explicit
    typed confirmation in the request body.
  * The Crash lab ONLY runs on temporary lab images in separate processes.
    The one exception is "Crash demo" (/api/demo/crash): it crashes the
    user's CURRENTLY MOUNTED image, in a separate process, and only after the
    typed confirmation "CRASH THIS IMAGE". The image is left consistent
    (recovery fixes it) but is mounted read-only until recovery is run.
"""

import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from src.crash_injector import CrashInjector
from src.crash_lab import expected_state_for, run_scenario
from src.crash_worker import OPERATIONS
from src.events import EventLog
from src.filesystem import FileSystem, FileSystemError
from src.virtual_disk import CrashInjectedError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.img$")
MAX_BODY = 256 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, etype: str = "Error"):
        super().__init__(message)
        self.status, self.etype = status, etype


class Workspace:
    """Server-side state: the image directory, the mounted filesystem and lab jobs."""

    def __init__(self, image_dir: str):
        self.image_dir = os.path.abspath(image_dir)
        os.makedirs(self.image_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.events = EventLog(capacity=20000)
        self.fs: Optional[FileSystem] = None
        self.image_name: Optional[str] = None
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.crashed: Optional[Dict[str, Any]] = None   # set after a crash demo until recovery runs
        self.token = secrets.token_urlsafe(24)

    # ---- images
    def image_path(self, name: str) -> str:
        if not isinstance(name, str) or not IMAGE_NAME_RE.match(name):
            raise ApiError(400, "image name must match [A-Za-z0-9._-]{1,64} and end in .img", "BadName")
        path = os.path.abspath(os.path.join(self.image_dir, name))
        if os.path.dirname(path) != self.image_dir:
            raise ApiError(400, "image must live in the image directory", "BadName")
        return path

    def list_images(self):
        out = []
        for n in sorted(os.listdir(self.image_dir)):
            p = os.path.join(self.image_dir, n)
            if IMAGE_NAME_RE.match(n) and os.path.isfile(p):
                st = os.stat(p)
                out.append({"name": n, "size": st.st_size, "mtime": st.st_mtime,
                            "mounted": n == self.image_name and self.fs is not None and self.fs.is_mounted})
        return out

    def require_fs(self) -> FileSystem:
        if self.fs is None or not self.fs.is_mounted:
            raise ApiError(409, "no image is mounted - open or create one first", "NotMounted")
        return self.fs

    def close(self):
        self.crashed = None
        if self.fs is not None:
            try:
                self.fs.unmount()
            except Exception:
                pass
        self.fs = None
        self.image_name = None


def need_confirm(body: dict, expected: str) -> None:
    if body.get("confirm") != expected:
        raise ApiError(400, f"confirmation required: send confirm = {expected!r}", "ConfirmationRequired")


# ----------------------------------------------------------------- routes
def build_routes(ws: Workspace) -> Dict[tuple, Callable]:
    R: Dict[tuple, Callable] = {}

    def route(method, path):
        def deco(fn):
            R[(method, path)] = fn
            return fn
        return deco

    def fsq(q, key="path", default="/"):
        return q.get(key, [default])[0]

    # ---- session / images
    @route("GET", "/api/session")
    def session(q, b):
        return {"token": ws.token, "image_dir": ws.image_dir, "mounted": ws.fs is not None and ws.fs.is_mounted,
                "image": ws.image_name}

    @route("GET", "/api/images")
    def images(q, b):
        return {"images": ws.list_images(), "current": ws.image_name}

    @route("POST", "/api/images/create")
    def create_image(q, b):
        name = b.get("name", "")
        path = ws.image_path(name)
        if os.path.exists(path):
            raise ApiError(409, f"{name} already exists (formatting an existing image needs the format action)", "Exists")
        size_mb = int(b.get("size_mb", 8))
        if not 1 <= size_mb <= 256:
            raise ApiError(400, "size_mb must be 1..256")
        with ws.lock:
            ws.close()
            fs = FileSystem(path, cache_mode=b.get("cache_mode", "volatile"),
                            checkpoint=b.get("checkpoint", "eager"), events=ws.events)
            try:
                fs.format(disk_size=size_mb * 1024 * 1024, journal_blocks=int(b.get("journal_blocks", 64)),
                          inode_blocks=int(b.get("inode_blocks", 16)), volume_label=str(b.get("label", ""))[:16])
            except ValueError as e:
                if os.path.exists(path):
                    os.remove(path)
                raise ApiError(400, str(e), "BadParameter")
            ws.fs, ws.image_name = fs, name
            return fs.overview()

    @route("POST", "/api/images/format")
    def format_image(q, b):
        name = b.get("name", "")
        path = ws.image_path(name)
        if not os.path.exists(path):
            raise ApiError(404, f"{name} does not exist")
        need_confirm(b, f"FORMAT {name}")
        with ws.lock:
            ws.close()
            fs = FileSystem(path, cache_mode=b.get("cache_mode", "volatile"),
                            checkpoint=b.get("checkpoint", "eager"), events=ws.events)
            size = os.path.getsize(path)
            fs.format(disk_size=size, journal_blocks=int(b.get("journal_blocks", 64)),
                      inode_blocks=int(b.get("inode_blocks", 16)), volume_label=str(b.get("label", ""))[:16])
            ws.fs, ws.image_name = fs, name
            return fs.overview()

    @route("POST", "/api/images/open")
    def open_image(q, b):
        name = b.get("name", "")
        path = ws.image_path(name)
        if not os.path.exists(path):
            raise ApiError(404, f"{name} does not exist")
        with ws.lock:
            ws.close()
            fs = FileSystem(path, cache_mode=b.get("cache_mode", "volatile"),
                            checkpoint=b.get("checkpoint", "eager"), events=ws.events)
            fs.mount(run_recovery=bool(b.get("run_recovery", True)))
            ws.fs, ws.image_name = fs, name
            return fs.overview()

    @route("POST", "/api/images/close")
    def close_image(q, b):
        with ws.lock:
            ws.close()
        return {"mounted": False}

    @route("POST", "/api/images/delete")
    def delete_image(q, b):
        name = b.get("name", "")
        path = ws.image_path(name)
        need_confirm(b, f"DELETE {name}")
        with ws.lock:
            if ws.image_name == name:
                ws.close()
            if not os.path.exists(path):
                raise ApiError(404, f"{name} does not exist")
            os.remove(path)
        return {"deleted": name}

    # ---- state / introspection
    @route("GET", "/api/state")
    def state(q, b):
        with ws.lock:
            if ws.fs is None or not ws.fs.is_mounted:
                return {"mounted": False, "image_dir": ws.image_dir}
            out = ws.fs.overview()
            out["crashed"] = ws.crashed
            return out

    @route("GET", "/api/layout")
    def layout(q, b):
        with ws.lock:
            fs = ws.require_fs()
            return {"regions": fs.layout(), "block_size": fs.superblock.block_size}

    @route("GET", "/api/block")
    def block(q, b):
        with ws.lock:
            fs = ws.require_fs()
            n = int(fsq(q, "n", "0"))
            info = fs.block_info(n)
            raw = fs.read_raw_block(n)
            info["hex"] = raw.hex()
            return info

    @route("GET", "/api/inode")
    def inode(q, b):
        with ws.lock:
            return ws.require_fs().inode_info(int(fsq(q, "n", "1")))

    @route("GET", "/api/inodes")
    def inodes(q, b):
        with ws.lock:
            fs = ws.require_fs()
            return {"inodes": [fs.inode_info(i.inode_num) for i in fs.inode_table.iter_inodes()]}

    @route("GET", "/api/bitmap")
    def bitmap(q, b):
        with ws.lock:
            fs = ws.require_fs()
            alloc = set(fs.block_allocator.iter_allocated())
            sb = fs.superblock
            return {"data_start": sb.data_start_block, "count": sb.data_block_count,
                    "allocated": sorted(alloc)}

    # ---- filesystem
    @route("GET", "/api/tree")
    def tree(q, b):
        with ws.lock:
            return ws.require_fs().tree(fsq(q), depth=int(fsq(q, "depth", "8")))

    @route("GET", "/api/ls")
    def ls(q, b):
        with ws.lock:
            fs = ws.require_fs()
            path = fsq(q)
            entries = []
            for e in fs.ls(path):
                p = path.rstrip("/") + "/" + e.name
                st = fs.stat(p)
                entries.append({"name": e.name, "inode": e.inode_num, "type": "dir" if e.file_type == 2 else "file",
                                "size": st["size"], "blocks": st["blocks"], "mtime": st["mtime"]})
            return {"path": path, "entries": entries}

    @route("GET", "/api/stat")
    def stat(q, b):
        with ws.lock:
            return ws.require_fs().stat(fsq(q))

    @route("GET", "/api/read")
    def read(q, b):
        with ws.lock:
            data = ws.require_fs().read(fsq(q))
            try:
                return {"size": len(data), "text": data.decode("utf-8"), "binary": False}
            except UnicodeDecodeError:
                return {"size": len(data), "text": data[:512].decode("latin-1"), "binary": True,
                        "base64": base64.b64encode(data).decode()}

    def fsop(fn):
        def handler(q, b):
            with ws.lock:
                fs = ws.require_fs()
                result = fn(fs, b)
                out = {"ok": True, "result": result, "txid": (fs.tx_manager.last_tx_info or {}).get("txid")}
                out["tx"] = fs.tx_manager.last_tx_info
                return out
        return handler

    R[("POST", "/api/fs/mkdir")] = fsop(lambda fs, b: fs.mkdir(b["path"]))
    R[("POST", "/api/fs/create")] = fsop(lambda fs, b: fs.create(b["path"]))
    R[("POST", "/api/fs/write")] = fsop(lambda fs, b: fs.write(b["path"], str(b.get("text", "")).encode("utf-8")))
    R[("POST", "/api/fs/append")] = fsop(lambda fs, b: fs.append(b["path"], str(b.get("text", "")).encode("utf-8")))
    R[("POST", "/api/fs/delete")] = fsop(lambda fs, b: fs.delete(b["path"]))
    R[("POST", "/api/fs/rmdir")] = fsop(lambda fs, b: fs.rmdir(b["path"]))
    R[("POST", "/api/fs/rename")] = fsop(lambda fs, b: fs.rename(b["old"], b["new"]))

    # ---- journal / events / check / recovery
    @route("GET", "/api/journal")
    def journal(q, b):
        with ws.lock:
            return ws.require_fs().journal_view()

    @route("GET", "/api/events")
    def events(q, b):
        since = int(fsq(q, "since", "0"))
        return {"events": ws.events.snapshot(since_seq=since, kind=q.get("kind", [None])[0],
                                             severity=q.get("severity", [None])[0],
                                             limit=int(fsq(q, "limit", "1000")))}

    @route("POST", "/api/events/clear")
    def events_clear(q, b):
        ws.events.clear()
        return {"cleared": True}

    @route("POST", "/api/check")
    def check(q, b):
        with ws.lock:
            return ws.require_fs().check_report().to_dict()

    @route("GET", "/api/check")
    def check_last(q, b):
        with ws.lock:
            return ws.require_fs().last_check or {"passed": None}

    @route("POST", "/api/recover")
    def recover(q, b):
        with ws.lock:
            res = ws.require_fs().recover()
            ws.crashed = None
            return res.to_dict()

    @route("POST", "/api/checkpoint")
    def checkpoint(q, b):
        with ws.lock:
            ws.require_fs().checkpoint()
            return ws.require_fs().overview()

    # ---- crash demo on the user's own mounted image
    @route("POST", "/api/demo/crash")
    def demo_crash(q, b):
        need_confirm(b, "CRASH THIS IMAGE")
        mode = b.get("mode", "overwrite")
        if mode not in ("overwrite", "create"):
            raise ApiError(400, "mode must be 'overwrite' or 'create'", "BadParameter")
        path, text = b.get("path", ""), str(b.get("text", ""))
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 200 or len(text) > 8192:
            raise ApiError(400, "path must be absolute (max 200 chars); text max 8192 chars", "BadParameter")
        with ws.lock:
            fs = ws.require_fs()
            name, img = ws.image_name, fs.disk_path
            old = None
            if mode == "overwrite":
                try:
                    old = fs.read(path).decode("utf-8", "replace")
                except Exception:
                    raise ApiError(400, f"{path} must be an existing file to overwrite it", "NotFound")
            else:
                try:
                    fs.stat(path)
                    raise ApiError(400, f"{path} already exists; pick a new name or use overwrite", "Exists")
                except FileSystemError:
                    pass
            if fs.tx_manager.read_only:
                raise ApiError(409, "image is mounted read-only (run recovery first)", "ReadOnly")
            cache, ckpt = fs.cache_mode, fs.tx_manager.checkpoint_policy
            ws.events.emit("crash_demo", f"CRASH DEMO: {mode} {path} on {name}; the process will be SIGKILLed",
                           severity="warning", path=path, mode=mode)
            ws.close()                                   # clean unmount, then crash in another process
            env = dict(os.environ)
            env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            args = {"phase": "userop", "image": img, "mode": mode, "path": path, "text": text,
                    "cache_mode": cache, "checkpoint": ckpt}
            cp = subprocess.run([sys.executable, "-m", "src.crash_worker", json.dumps(args)],
                                cwd=env["PYTHONPATH"], env=env, capture_output=True, text=True, timeout=60)
            killed = cp.returncode == -signal.SIGKILL
            # look at the crashed disk WITHOUT running recovery (read-only mount)
            nfs = FileSystem(img, cache_mode=cache, checkpoint=ckpt, events=ws.events)
            nfs.mount(run_recovery=False)
            ws.fs, ws.image_name = nfs, name
            pending = [t["txid"] for t in nfs.journal_view()["transactions"] if t["state"] == "committed-pending-replay"]
            ws.crashed = {"mode": mode, "path": path, "new_text": text, "old_text": old, "killed": killed,
                          "returncode": cp.returncode, "pending_tx": pending}
            ws.events.emit("crash_injected", f"process {'KILLED by SIGKILL' if killed else 'exited'} "
                           f"(returncode {cp.returncode}); image mounted read-only WITHOUT recovery; "
                           f"committed-but-unapplied tx: {pending}", severity="error", returncode=cp.returncode)
            if not killed:
                ws.crashed["stderr"] = cp.stderr[-500:]
            return {"ok": True, **ws.crashed}

    # ---- crash lab
    @route("GET", "/api/lab/scenarios")
    def lab_scenarios(q, b):
        return {
            "scenarios": [{"name": s.name, "description": s.description, "crash_point": s.crash_point.name,
                           "expected_state": s.expected_state, "phase": s.phase, "count": s.count,
                           "torn_bytes": s.torn_bytes, "outcome": s.expected_outcome}
                          for s in CrashInjector.SCENARIOS],
            "operations": [{"name": k, "label": v["label"]} for k, v in OPERATIONS.items()],
        }

    @route("POST", "/api/lab/run")
    def lab_run(q, b):
        scen = b.get("scenario", "")
        op = b.get("operation", "create_file")
        sc = CrashInjector().get_scenario(scen)
        if sc is None or op not in OPERATIONS:
            raise ApiError(400, "unknown scenario or operation", "BadParameter")
        need_confirm(b, "RUN CRASH LAB")
        if any(j["status"] == "running" for j in ws.jobs.values()):
            raise ApiError(409, "a lab run is already in progress", "Busy")
        job_id = uuid.uuid4().hex[:10]
        job = {"id": job_id, "status": "running", "steps": [], "result": None, "scenario": scen,
               "operation": op, "started": time.time()}
        ws.jobs[job_id] = job
        mode = b.get("cache_mode", "volatile")

        def work():
            workdir = tempfile.mkdtemp(prefix="crfs_lab_")
            try:
                res = run_scenario(scen, op, workdir=workdir, cache_mode=mode,
                                   checkpoint=b.get("checkpoint", "eager"),
                                   progress=lambda s: job["steps"].append(s))
                job["result"] = res
                job["status"] = "done"
            except Exception as e:  # pragma: no cover
                job["status"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
                job["finished"] = time.time()
                if job["result"]:
                    for ev in job["result"].get("events", []):
                        ws.events.emit("lab_" + ev["kind"], ev["message"], ev["severity"], ev.get("txid"),
                                       **{"job": job_id})
        threading.Thread(target=work, daemon=True).start()
        return {"job": job_id}

    @route("GET", "/api/lab/job")
    def lab_job(q, b):
        job = ws.jobs.get(fsq(q, "id", ""))
        if not job:
            raise ApiError(404, "no such job")
        return job

    return R


# ----------------------------------------------------------------- server
def make_handler(ws: Workspace, routes, port_getter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CRFS-UI/2"

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in LOOPBACK

        def _send(self, status: int, payload: Any, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser closed the connection (page reload / tab switch); nothing to report

        def _static(self, path: str):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.abspath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": path}})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        def _dispatch(self, method: str):
            if not self._host_ok():
                return self._send(403, {"ok": False, "error": {"type": "Forbidden", "message": "bad Host header"}})
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                if method != "GET":
                    return self._send(405, {"ok": False, "error": {"type": "MethodNotAllowed", "message": method}})
                return self._static(u.path)
            fn = routes.get((method, u.path))
            if fn is None:
                return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": u.path}})
            try:
                body = {}
                if method == "POST":
                    if self.headers.get("X-CRFS-Token") != ws.token:
                        raise ApiError(403, "missing or wrong session token", "Forbidden")
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        raise ApiError(413, "request too large", "TooLarge")
                    raw = self.rfile.read(n) if n else b"{}"
                    try:
                        body = json.loads(raw.decode() or "{}")
                    except ValueError:
                        raise ApiError(400, "body must be JSON", "BadJSON")
                    if not isinstance(body, dict):
                        raise ApiError(400, "body must be a JSON object", "BadJSON")
                result = fn(parse_qs(u.query), body)
                self._send(200, result)
            except ApiError as e:
                self._send(e.status, {"ok": False, "error": {"type": e.etype, "message": str(e)}})
            except KeyError as e:
                self._send(400, {"ok": False, "error": {"type": "MissingField", "message": f"missing field {e}"}})
            except (FileSystemError, ValueError) as e:
                self._send(400, {"ok": False, "error": {"type": type(e).__name__, "message": str(e)}})
            except CrashInjectedError as e:
                self._send(500, {"ok": False, "error": {"type": "Crash", "message": str(e)}})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"ok": False, "error": {"type": type(e).__name__, "message": str(e)}})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(image_dir: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False):
    if host not in LOOPBACK:
        raise SystemExit(f"refusing to bind to {host!r}: the UI exposes destructive operations and is "
                         f"restricted to loopback addresses")
    ws = Workspace(image_dir)
    routes = build_routes(ws)
    httpd = ThreadingHTTPServer((host, port), make_handler(ws, routes, None))
    httpd.daemon_threads = True
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"CRFS UI listening on {url}   (images in {ws.image_dir})")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        ws.close()
        httpd.server_close()
    return ws
