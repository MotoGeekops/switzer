"""One atomic snapshot for wallets and community progress, with async flushing."""

import asyncio
from collections import defaultdict
from contextlib import suppress
from copy import deepcopy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path, defaults, legacy=None):
        self.path = Path(path)
        self.defaults = defaults
        self.users = defaultdict(defaults)
        self.guilds = {}
        self.revision = self.saved_revision = 0
        self.task = None
        self.flush_lock = asyncio.Lock()
        data = None
        for candidate in (self.path, self.path.with_suffix(".bak")):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("users"), dict) or not isinstance(data.get("guilds"), dict):
                    raise ValueError("Invalid state format")
                for uid, values in data["users"].items():
                    int(uid)
                    if not isinstance(values, dict) or not isinstance(values.get("balance", 0), int):
                        raise ValueError("Invalid wallet")
                if candidate != self.path:
                    log.warning("Recovered Switzer state from the backup file.")
                    self.mark_dirty()
                break
            except (OSError, ValueError, TypeError, AttributeError):
                data = None
                log.exception("Could not read %s", candidate.name)
        if data is None and (self.path.exists() or self.path.with_suffix(".bak").exists()):
            raise RuntimeError("Switzer's saved data is unreadable. Restore a backup before starting; files were left intact.")
        if data is None:
            previous = json.loads(Path(legacy).read_text(encoding="utf-8")) if legacy and Path(legacy).exists() else {}
            data = {"users": previous, "guilds": {}}
            self.mark_dirty()
        for uid, values in data["users"].items():
            self.users[int(uid)].update(values)
        self.guilds = data["guilds"]
        self.meta = data.get("meta", {})

    def mark_dirty(self):
        self.revision += 1

    def snapshot(self):
        return deepcopy({"version": 1, "users": dict(self.users), "guilds": self.guilds, "meta": self.meta})

    def _write(self, snapshot):
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        # Each copy is replaced atomically; a damaged primary can use the backup.
        for target in (self.path, self.path.with_suffix(".bak")):
            temporary = target.with_name(target.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)

    async def flush(self):
        async with self.flush_lock:
            if self.revision == self.saved_revision:
                return
            revision, snapshot = self.revision, self.snapshot()
            # Shield the writer so shutdown cannot race a second filesystem write.
            writer = asyncio.create_task(asyncio.to_thread(self._write, snapshot))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await writer
                self.saved_revision = revision
                raise
            self.saved_revision = revision

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="save-switzer-state")

    async def _run(self):
        while True:
            await asyncio.sleep(0.4)
            try:
                await self.flush()
            except OSError:
                log.exception("Saving failed; will retry. Check disk space and folder permissions.")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await self.flush()
