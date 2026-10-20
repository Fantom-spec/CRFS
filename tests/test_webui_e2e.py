"""Browser end-to-end tests (Playwright + Chromium).  Skipped when Playwright is not installed."""
import os
import shutil
import tempfile
import unittest

from src.filesystem import FileSystem
from tests.test_webui import Server

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:  # pragma: no cover
    HAVE_PW = False


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class TestBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crfs_e2e_")
        self.srv = Server(self.dir)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(self.srv.base + "/")

    def tearDown(self):
        self.page.close()
        self.srv.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.assertEqual(self.errors, [])

    def nav(self, label):
        self.page.click(f"#nav button:has-text('{label}')")

    def new_image(self):
        self.page.click("#btn-new")
        self.page.fill(".modal input[type=text]", "e2e.img")
        self.page.click(".modal button:has-text('Create')")
        self.page.wait_for_selector("#mountpill .pill.ok")

    def test_create_file_in_ui_persists_to_image(self):
        self.new_image()
        self.nav("File browser")
        self.page.click("button:has-text('New file')")
        self.page.fill(".modal input[type=text]", "hello.txt")
        self.page.fill(".modal textarea", "typed in the browser")
        self.page.click(".modal button:has-text('Create')")
        self.page.wait_for_selector("td:has-text('hello.txt')")
        self.page.click("td:has-text('hello.txt')")
        self.page.wait_for_selector("pre.hex:has-text('typed in the browser')")
        self.srv.ws.close()  # unmount cleanly, then inspect with an independent engine
        fs = FileSystem(os.path.join(self.dir, "e2e.img"))
        fs.mount()
        try:
            self.assertEqual(fs.read("/hello.txt"), b"typed in the browser")
        finally:
            fs.unmount()

    def test_delete_requires_typed_confirmation(self):
        self.new_image()
        self.srv.post("/api/fs/create", {"path": "/gone.txt"})
        self.nav("File browser")
        self.page.click("td:has-text('gone.txt')")
        self.page.click("button.danger:has-text('Delete')")
        confirm = self.page.locator(".modal button:has-text('Confirm')")
        self.assertTrue(confirm.is_disabled())
        self.page.fill(".modal input", "nope")
        self.assertTrue(confirm.is_disabled())
        self.page.fill(".modal input", "DELETE")
        self.assertFalse(confirm.is_disabled())
        self.page.keyboard.press("Escape")
        self.assertEqual([e["name"] for e in self.srv.get("/api/ls?path=/")[1]["entries"]], ["gone.txt"])

    def test_error_is_shown_to_user(self):
        self.new_image()
        self.nav("File browser")
        self.page.click("button:has-text('New folder')")
        self.page.fill(".modal input[type=text]", "bad/name/x")
        self.page.click(".modal button:has-text('Create')")
        self.page.wait_for_selector(".toast.err")

    def test_crash_lab_runs_and_shows_verdict(self):
        self.nav("Crash lab")
        self.page.click("tr:has-text('AFTER_COMMIT_FLUSH')")
        self.page.click("button:has-text('Run scenario')")
        self.page.click(".modal button:has-text('Run')")
        self.page.wait_for_selector(".verdict", timeout=30000)
        self.assertEqual(self.page.inner_text(".verdict"), "PASS")
        self.assertIn("SIGKILL", self.page.inner_text("#main"))

    def test_lab_shows_old_data_before_recovery_and_new_data_after(self):
        self.nav("Crash lab")
        self.page.select_option("select[aria-label=operation]", "write_file")
        self.page.click("tr:has-text('AFTER_COMMIT_FLUSH')")
        self.page.click("button:has-text('Run scenario')")
        self.page.click(".modal button:has-text('Run')")
        self.page.wait_for_selector(".verdict", timeout=30000)
        row = self.page.locator("tr:has(td:text-is('data.txt'))").first
        cells = [c.strip() for c in row.locator("td").all_inner_texts()]
        self.assertEqual(cells, ["data.txt", '"OLD DATA"', '"OLD DATA"', '"NEW DATA"'])

    def test_crash_demo_hides_update_then_recovery_restores_it(self):
        self.new_image()
        self.srv.post("/api/fs/create", {"path": "/a.txt"})
        self.srv.post("/api/fs/write", {"path": "/a.txt", "text": "ORIGINAL TEXT"})
        self.nav("File browser")
        self.page.click("td:has-text('a.txt')")
        self.page.wait_for_selector("pre.hex:has-text('ORIGINAL TEXT')")
        self.page.click("button:has-text('Crash demo')")
        self.page.fill(".modal textarea", "UPDATED TEXT")
        self.page.click(".modal button:has-text('Continue')")
        self.page.fill(".modal input", "CRASH THIS IMAGE")
        self.page.click(".modal button:has-text('Confirm')")
        self.page.wait_for_selector(".crashbar")
        self.assertIn("recovery has NOT run", self.page.inner_text(".crashbar"))
        self.page.wait_for_selector("pre.hex:has-text('ORIGINAL TEXT')")        # crashed disk: old data
        self.assertNotIn("UPDATED TEXT", self.page.inner_text("pre.hex"))
        self.page.click(".crashbar button:has-text('Run recovery')")
        self.page.wait_for_selector("pre.hex:has-text('UPDATED TEXT')")         # recovered: new data
        self.assertEqual(self.page.locator(".crashbar").count(), 0)

    def test_checker_and_event_log_screens(self):
        self.new_image()
        self.srv.post("/api/fs/mkdir", {"path": "/d"})
        self.nav("Consistency")
        self.page.wait_for_selector(".banner.ok")
        self.nav("Event log")
        self.page.wait_for_selector("td:has-text('mkdir /d')")
        self.page.click(".chip:has-text('error')")
        self.page.wait_for_selector("text=No events match")


if __name__ == "__main__":
    unittest.main()
