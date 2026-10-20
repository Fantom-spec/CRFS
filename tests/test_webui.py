"""UI <-> backend integration tests.

Each test talks to a real HTTP server (loopback, ephemeral port) that wraps the
real engine.  The 'persisted' checks re-open the image with a brand-new FileSystem
object (the same code path as the CLI), so they prove the UI really changed the
image and not just some in-memory state.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src.filesystem import FileSystem
from webui.app import LOOPBACK, Workspace, build_routes, make_handler, serve


class Server:
    def __init__(self, image_dir):
        self.ws = Workspace(image_dir)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.ws, build_routes(self.ws), None))
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.get("/api/session")[1]["token"]

    def req(self, method, path, body=None, token=True, headers=None):
        h = dict(headers or {})
        data = None
        if body is not None or method == "POST":
            data = json.dumps(body or {}).encode()
            h["Content-Type"] = "application/json"
        if token and method == "POST":
            h["X-CRFS-Token"] = self.token
        r = urllib.request.Request(self.base + path, data, h, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body or {}, **kw)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.ws.close()


class UITestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crfs_ui_test_")
        self.srv = Server(self.dir)
        s, _ = self.srv.post("/api/images/create", {"name": "t.img", "size_mb": 4})
        self.assertEqual(s, 200)
        self.img = os.path.join(self.dir, "t.img")

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def fresh_mount(self):
        """Independent engine instance on the same image (== what the CLI does)."""
        self.srv.ws.fs.disk.flush("test")  # no-op normally; makes the intent explicit
        fs = FileSystem(self.img)
        fs.mount()
        return fs


class TestPersistence(UITestBase):
    def test_create_via_ui_is_in_the_real_image(self):
        self.assertEqual(self.srv.post("/api/fs/mkdir", {"path": "/docs"})[0], 200)
        self.assertEqual(self.srv.post("/api/fs/create", {"path": "/docs/a.txt"})[0], 200)
        self.assertEqual(self.srv.post("/api/fs/write", {"path": "/docs/a.txt", "text": "hello ui"})[0], 200)
        fs = self.fresh_mount()
        try:
            self.assertEqual(fs.read("/docs/a.txt"), b"hello ui")
            self.assertEqual([e.name for e in fs.ls("/docs")], ["a.txt"])
            self.assertEqual(fs.check_report().passed, True)
        finally:
            fs.unmount()

    def test_read_and_metadata_agree_with_engine(self):
        self.srv.post("/api/fs/create", {"path": "/f"})
        self.srv.post("/api/fs/write", {"path": "/f", "text": "x" * 5000})
        _, rd = self.srv.get("/api/read?path=/f")
        _, st = self.srv.get("/api/stat?path=/f")
        self.assertEqual(rd["size"], 5000)
        self.assertEqual(st["size"], 5000)
        self.assertEqual(st["blocks"], 2)
        fs = self.fresh_mount()
        try:
            self.assertEqual(fs.stat("/f")["size"], st["size"])
            self.assertEqual(fs.stat("/f")["inode"], st["inode"])
        finally:
            fs.unmount()

    def test_state_survives_server_restart(self):
        self.srv.post("/api/fs/create", {"path": "/keep"})
        self.srv.post("/api/fs/write", {"path": "/keep", "text": "persist me"})
        self.srv.stop()
        self.srv = Server(self.dir)
        self.assertEqual(self.srv.get("/api/state")[1]["mounted"], False)
        self.assertEqual(self.srv.post("/api/images/open", {"name": "t.img"})[0], 200)
        self.assertEqual(self.srv.get("/api/read?path=/keep")[1]["text"], "persist me")

    def test_cli_and_ui_share_an_image(self):
        self.srv.post("/api/fs/mkdir", {"path": "/from_ui"})
        self.srv.post("/api/images/close")
        fs = FileSystem(self.img)
        fs.mount()
        fs.create("/from_cli")
        fs.unmount()
        self.srv.post("/api/images/open", {"name": "t.img"})
        names = sorted(e["name"] for e in self.srv.get("/api/ls?path=/")[1]["entries"])
        self.assertEqual(names, ["from_cli", "from_ui"])


class TestJournalEvents(UITestBase):
    def test_events_match_real_transaction(self):
        self.srv.post("/api/events/clear")
        _, r = self.srv.post("/api/fs/create", {"path": "/e"})
        txid = r["txid"]
        self.assertIsNotNone(txid)
        _, ev = self.srv.get("/api/events?limit=500")
        kinds = [e["kind"] for e in ev["events"] if e["txid"] == txid]
        order = ["tx_begin", "tx_prepare", "journal_write", "commit_flush", "home_apply", "checkpoint"]
        idx = [kinds.index(k) for k in order]
        self.assertEqual(idx, sorted(idx), kinds)          # real lifecycle order
        _, jv = self.srv.get("/api/journal")
        self.assertIn(txid, [t["txid"] for t in jv["transactions"]])  # and it is really in the journal

    def test_aborted_op_emits_no_commit(self):
        self.srv.post("/api/events/clear")
        s, r = self.srv.post("/api/fs/create", {"path": "/missing_dir/x"})
        self.assertEqual(s, 400)
        kinds = [e["kind"] for e in self.srv.get("/api/events")[1]["events"]]
        self.assertNotIn("commit_flush", kinds)


class TestCrashLab(UITestBase):
    def _run(self, scenario, op="create_file"):
        s, r = self.srv.post("/api/lab/run", {"scenario": scenario, "operation": op, "confirm": "RUN CRASH LAB"})
        self.assertEqual(s, 200, r)
        for _ in range(200):
            j = self.srv.get("/api/lab/job?id=" + r["job"])[1]
            if j["status"] != "running":
                return j
            time.sleep(0.1)
        self.fail("lab job did not finish")

    def test_lab_kills_a_real_process_and_validates_state(self):
        j = self._run("crash_after_commit_flush_before_apply")
        self.assertEqual(j["status"], "done", j)
        res = j["result"]
        killed = [s for s in j["steps"] if s.get("killed")]
        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0]["returncode"], -9)        # SIGKILL, not an exception
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(res["expected_probe"], res["actual_probe"])
        self.assertIn("new.txt", res["actual_probe"]["listing"])
        self.assertEqual(res["recovery"]["transactions_replayed"], 1)

    def test_lab_rollback_scenario_expected_differs_from_committed(self):
        j = self._run("crash_after_journal_flush_before_commit")
        res = j["result"]
        self.assertEqual(res["verdict"], "PASS")
        self.assertNotIn("new.txt", res["actual_probe"]["listing"])

    def test_lab_does_not_touch_mounted_image(self):
        def snap():
            with open(self.img, "rb") as f:
                return f.read()
        before = snap()
        self._run("crash_before_journal_write")
        self.assertEqual(snap(), before)

    def test_lab_needs_confirmation(self):
        s, r = self.srv.post("/api/lab/run", {"scenario": "crash_before_journal_write", "operation": "create_file"})
        self.assertEqual(s, 400)
        self.assertEqual(r["error"]["type"], "ConfirmationRequired")

    def test_lab_unknown_scenario(self):
        s, r = self.srv.post("/api/lab/run", {"scenario": "nope", "confirm": "RUN CRASH LAB"})
        self.assertEqual(s, 400)


class TestCheckerScreen(UITestBase):
    def test_clean_image_passes(self):
        self.assertTrue(self.srv.post("/api/check")[1]["passed"])

    def test_real_corruption_is_reported(self):
        self.srv.post("/api/fs/create", {"path": "/victim"})
        fs = self.srv.ws.fs
        # corrupt for real: clear the inode's bitmap-visible block allocation behind the engine's back
        ino = fs.stat("/victim")["inode"]
        fs.disk.write_block(fs.superblock.bitmap_start_block, b"\x00" * fs.superblock.block_size)
        fs.disk.flush("test")
        rep = self.srv.post("/api/check")[1]
        self.assertFalse(rep["passed"])
        self.assertTrue(rep["errors"])
        self.assertTrue(all("code" in f and "why" in f for f in rep["findings"]))


class TestErrorsAndSafety(UITestBase):
    def test_useful_errors(self):
        s, r = self.srv.post("/api/fs/create", {"path": "/a/b"})
        self.assertEqual(s, 400)
        self.assertTrue(r["error"]["message"])
        s, r = self.srv.get("/api/read?path=/nope")
        self.assertEqual(s, 400)
        self.assertIn("/nope", r["error"]["message"])  # names the offending path
        s, r = self.srv.post("/api/fs/mkdir", {})
        self.assertEqual(r["error"]["type"], "MissingField")

    def test_no_image_mounted(self):
        self.srv.post("/api/images/close")
        s, r = self.srv.get("/api/ls?path=/")
        self.assertEqual(s, 409)
        self.assertEqual(r["error"]["type"], "NotMounted")

    def test_destructive_actions_need_typed_confirmation(self):
        s, r = self.srv.post("/api/images/delete", {"name": "t.img"})
        self.assertEqual((s, r["error"]["type"]), (400, "ConfirmationRequired"))
        self.assertTrue(os.path.exists(self.img))
        s, r = self.srv.post("/api/images/format", {"name": "t.img", "confirm": "yes"})
        self.assertEqual(s, 400)
        self.assertTrue(os.path.exists(self.img))
        self.srv.post("/api/fs/create", {"path": "/survivor"})
        s, _ = self.srv.post("/api/images/format", {"name": "t.img", "confirm": "FORMAT t.img"})
        self.assertEqual(s, 200)
        self.assertEqual(self.srv.get("/api/ls?path=/")[1]["entries"], [])
        s, _ = self.srv.post("/api/images/delete", {"name": "t.img", "confirm": "DELETE t.img"})
        self.assertEqual(s, 200)
        self.assertFalse(os.path.exists(self.img))

    def test_post_requires_session_token(self):
        s, r = self.srv.post("/api/fs/mkdir", {"path": "/x"}, token=False)
        self.assertEqual(s, 403)
        s, r = self.srv.post("/api/fs/mkdir", {"path": "/x"}, token=False, headers={"X-CRFS-Token": "wrong"})
        self.assertEqual(s, 403)
        self.assertEqual(self.srv.get("/api/ls?path=/")[1]["entries"], [])

    def test_bad_host_header_rejected(self):
        s, _ = self.srv.get("/api/state", headers={"Host": "evil.example.com"})
        self.assertEqual(s, 403)

    def test_no_arbitrary_paths(self):
        for bad in ["../x.img", "/etc/passwd", "a/b.img", "x.txt", ".img", "a" * 80 + ".img"]:
            s, r = self.srv.post("/api/images/create", {"name": bad})
            self.assertEqual(s, 400, bad)
            self.assertEqual(r["error"]["type"], "BadName")
        s, _ = self.srv.get("/../../etc/passwd")
        self.assertEqual(s, 404)
        s, _ = self.srv.get("/..%2f..%2fetc/passwd")
        self.assertEqual(s, 404)

    def test_oversized_and_bad_json(self):
        r = urllib.request.Request(self.srv.base + "/api/fs/write", b"not json",
                                   {"X-CRFS-Token": self.srv.token, "Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r)
        self.assertEqual(cm.exception.code, 400)

    def test_refuses_non_loopback_bind(self):
        with self.assertRaises(SystemExit):
            serve(self.dir, host="0.0.0.0", port=0)
        self.assertIn("127.0.0.1", LOOPBACK)

    def test_static_ui_served(self):
        s, raw = self.srv.get("/")
        self.assertEqual(s, 200)
        self.assertIn(b"CRFS", raw)


class TestCrashDemoOnRealImage(UITestBase):
    """The File-browser crash demo: a real SIGKILL on the user's own image."""

    def _crash(self, **body):
        return self.srv.post("/api/demo/crash", {"confirm": "CRASH THIS IMAGE", **body})

    def test_overwrite_crash_shows_old_data_until_recovery(self):
        self.srv.post("/api/fs/create", {"path": "/a.txt"})
        self.srv.post("/api/fs/write", {"path": "/a.txt", "text": "ORIGINAL"})
        s, r = self._crash(mode="overwrite", path="/a.txt", text="UPDATED")
        self.assertEqual(s, 200, r)
        self.assertTrue(r["killed"])
        self.assertEqual(r["returncode"], -9)                       # SIGKILL
        self.assertEqual(self.srv.get("/api/read?path=/a.txt")[1]["text"], "ORIGINAL")
        st = self.srv.get("/api/state")[1]
        self.assertTrue(st["read_only"])
        self.assertEqual(st["crashed"]["pending_tx"], r["pending_tx"])
        states = [t["state"] for t in self.srv.get("/api/journal")[1]["transactions"]]
        self.assertEqual(states[-1], "committed-pending-replay")
        self.assertEqual(self.srv.post("/api/fs/write", {"path": "/a.txt", "text": "x"})[0], 400)
        self.assertEqual(self.srv.post("/api/recover")[1]["transactions_replayed"], 1)
        self.assertEqual(self.srv.get("/api/read?path=/a.txt")[1]["text"], "UPDATED")
        self.assertIsNone(self.srv.get("/api/state")[1]["crashed"])
        self.assertTrue(self.srv.post("/api/check")[1]["passed"])

    def test_create_crash_leaves_empty_file_until_recovery(self):
        s, r = self._crash(mode="create", path="/new.txt", text="HELLO")
        self.assertTrue(r["killed"])
        self.assertEqual(self.srv.get("/api/read?path=/new.txt")[1]["text"], "")
        self.srv.post("/api/recover")
        self.assertEqual(self.srv.get("/api/read?path=/new.txt")[1]["text"], "HELLO")

    def test_independent_mount_sees_same_crashed_state(self):
        self.srv.post("/api/fs/create", {"path": "/a.txt"})
        self.srv.post("/api/fs/write", {"path": "/a.txt", "text": "ORIGINAL"})
        self._crash(mode="overwrite", path="/a.txt", text="UPDATED")
        fs = FileSystem(self.img)
        fs.mount(run_recovery=False)
        try:
            self.assertEqual(fs.read("/a.txt"), b"ORIGINAL")
        finally:
            fs.unmount()

    def test_needs_confirmation_and_valid_target(self):
        self.srv.post("/api/fs/create", {"path": "/a.txt"})
        s, r = self.srv.post("/api/demo/crash", {"mode": "overwrite", "path": "/a.txt", "text": "x"})
        self.assertEqual(r["error"]["type"], "ConfirmationRequired")
        self.assertEqual(self._crash(mode="overwrite", path="/missing", text="x")[0], 400)
        self.assertEqual(self._crash(mode="create", path="/a.txt", text="x")[0], 400)
        self.assertEqual(self._crash(mode="overwrite", path="relative", text="x")[0], 400)
        self.assertEqual(self._crash(mode="bogus", path="/a.txt", text="x")[0], 400)
        self.assertIsNone(self.srv.get("/api/state")[1]["crashed"])   # nothing was crashed


if __name__ == "__main__":
    unittest.main()
