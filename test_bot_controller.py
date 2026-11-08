"""Offline lifecycle tests; these never connect the real bot to Discord."""

import asyncio
from contextlib import suppress
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from bot_controller import BotController, BotProcess
from bot_runtime import InstanceLock, serve


FAKE_BOT = """
import asyncio
from bot_runtime import serve
class Client:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): print('CLEANLY CLOSED', flush=True)
    def is_ready(self): return True
    async def start(self, token): await asyncio.Event().wait()
asyncio.run(serve(Client(), 'fake-token', managed=True))
"""


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = BotController(self.root)
        self.app.bot = BotProcess([sys.executable, "-u", "-c", FAKE_BOT])

    def tearDown(self):
        if self.app.bot.running:
            self.app.bot.process.kill()
            self.app.bot.process.wait(timeout=5)
        if self.app.bot.process is not None:
            self.app.bot.process.stdin.close()
            self.app.bot.reader.join(timeout=2)
        with suppress(tk.TclError):
            self.root.destroy()

    def wait_for(self, condition):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for controller state")

    def online(self):
        return self.app.status.cget("text") == "●  Online"

    def test_start_restart_stop_and_duplicate_click(self):
        self.app.start_button.invoke()
        self.wait_for(self.online)
        first_pid = self.app.bot.process.pid
        self.app.start()
        self.assertEqual(first_pid, self.app.bot.process.pid)
        self.app.restart_button.invoke()
        self.wait_for(lambda: self.online() and self.app.bot.process.pid != first_pid)
        self.app.stop_button.invoke()
        self.wait_for(lambda: self.app.bot.process is None)
        self.assertIn("CLEANLY CLOSED", self.app.log.get("1.0", "end"))
        self.assertEqual(self.app.status.cget("text"), "●  Stopped")

    def test_unexpected_exit_is_visible_and_start_can_retry(self):
        self.app.bot.command = [sys.executable, "-c", "print('FAKE FAILURE'); raise SystemExit(7)"]
        self.app.start()
        self.wait_for(lambda: self.app.bot.process is None)
        self.assertIn("unexpectedly", self.app.status.cget("text"))
        self.assertIn("FAKE FAILURE", self.app.log.get("1.0", "end"))
        self.assertEqual(self.app.start_button.cget("state"), "normal")
        self.app.bot.command = [sys.executable, "-u", "-c", FAKE_BOT]
        self.app.start()
        self.wait_for(self.online)

    def test_shutdown_timeout_kills_only_owned_process(self):
        self.app.bot.command = [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(60)"]
        self.app.start()
        self.app.stop()
        self.app.bot.deadline = time.monotonic() - 1
        self.wait_for(lambda: self.app.bot.process is None)
        self.assertIn("Shutdown took too long", self.app.log.get("1.0", "end"))

    def test_closing_app_stops_bot(self):
        self.app.start()
        self.wait_for(self.online)
        process = self.app.bot.process
        self.app.close()
        self.wait_for(lambda: process.poll() is not None and self.app.bot.process is None)
        self.assertEqual(process.returncode, 0)


class RuntimeTests(unittest.TestCase):
    def test_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.lock"
            lock = InstanceLock(path)
            self.assertTrue(lock.acquire())
            code = "from bot_runtime import InstanceLock; import sys; lock=InstanceLock(sys.argv[1]); print(lock.acquire())"
            result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "False")
            lock.release()
            result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "True")

    def test_stopping_during_startup_or_pipe_loss_closes_client(self):
        class Client:
            closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

            def is_ready(self):
                return False

            async def start(self, token):
                await asyncio.sleep(60)

        for controls in ("stop\n", ""):
            client = Client()
            with patch("sys.stdin", io.StringIO(controls)):
                asyncio.run(asyncio.wait_for(serve(client, "fake", managed=True), timeout=3))
            self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
