"""Shared single-instance and graceful shutdown support for Switzer."""

import asyncio
from contextlib import suppress
from pathlib import Path
import sys
import threading

STATUS_PREFIX = "[SWITZER STATUS] "
ROOT = Path(__file__).resolve().parent


class InstanceLock:
    """An OS lock is released even if the process crashes."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.file = handle
        return True

    def release(self):
        if self.file is not None:
            self.file.close()
            self.file = None


async def serve(client, token, managed=False):
    """Cancel startup or a running connection, then let Discord clean up."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    monitor = None

    def read_controls():
        if sys.stdin is not None:
            for line in sys.stdin:
                if line.strip().lower() == "stop":
                    break
        # Losing the controller pipe also shuts down the bot.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(task.cancel)

    async def report_status():
        previous = None
        while True:
            status = "Online" if client.is_ready() else "Connecting"
            if status != previous:
                print(STATUS_PREFIX + status, flush=True)
                previous = status
            await asyncio.sleep(0.4)

    try:
        async with client:
            if managed:
                monitor = asyncio.create_task(report_status())
                threading.Thread(target=read_controls, daemon=True).start()
            try:
                await client.start(token)
            except asyncio.CancelledError:
                print("Shutting down the bot...", flush=True)
    finally:
        if monitor is not None:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor


def run_bot(client, token, managed=False):
    lock = InstanceLock(ROOT / ".bot.lock")
    if not lock.acquire():
        print("Switzer is already running. Stop the other instance first.", flush=True)
        raise SystemExit(2)
    try:
        if not token:
            print("DISCORD_TOKEN is missing. Add it to the .env file first.", flush=True)
            raise SystemExit(1)
        from discord.utils import setup_logging
        setup_logging()
        try:
            asyncio.run(serve(client, token, managed))
        except KeyboardInterrupt:
            pass
    finally:
        lock.release()
