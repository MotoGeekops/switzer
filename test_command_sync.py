import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest

from command_sync import CommandSync


class Command:
    def __init__(self, data):
        self.data = data

    def to_dict(self, tree=None):
        return deepcopy(self.data)


class Tree:
    def __init__(self):
        self.local = [Command({"name": "quests", "type": 1, "description": "Daily quests", "options": [{"name": "answer", "type": 3, "description": "Answer", "required": False, "max_length": 100}]}), Command({"name": "pet", "type": 1, "description": "Shared pet"})]
        self.remote = {}
        self.uploads = []
        self.copies = []
        self.bad_upload = False
        self.guild_local = {}
        self.cleared = []

    def copy_global_to(self, guild):
        self.copies.append(guild.id)

    def get_commands(self, guild=None):
        return self.local if guild is None else self.guild_local.get(guild.id, [])

    def clear_commands(self, guild):
        self.cleared.append(guild.id)
        self.guild_local[guild.id] = []

    def remove_command(self, name, guild, type):
        self.guild_local[guild.id] = [c for c in self.guild_local.get(guild.id, []) if c.data["name"] != name]

    async def fetch_commands(self, guild=None):
        scope = guild.id if guild else None
        result = []
        for data in deepcopy(self.remote.get(scope, [])):
            command = Command(data)
            command.name = data["name"]
            command.type = data.get("type", 1)
            async def delete(name=command.name):
                self.remote[scope] = [c for c in self.remote[scope] if c["name"] != name]
            command.delete = delete
            result.append(command)
        return result

    async def sync(self, guild=None):
        key = guild.id if guild else None
        self.uploads.append(key)
        self.remote[key] = [] if self.bad_upload else [c.to_dict() for c in self.get_commands(guild)]


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tree = Tree()
        self.dirty = 0
        self.store = SimpleNamespace(meta={}, mark_dirty=self.mark_dirty)
        self.sync = CommandSync(self.tree, self.store, 123)
        self.guild = SimpleNamespace(id=456, name="Test server")

    def mark_dirty(self):
        self.dirty += 1

    async def test_global_registration_and_no_redundant_upload(self):
        self.assertEqual(await self.sync.ensure(), (2, True))
        self.assertEqual(await self.sync.ensure(), (2, False))
        self.assertEqual(self.tree.uploads, [None])

    async def test_cached_signature_cannot_hide_missing_remote_quest(self):
        await self.sync.ensure()
        self.tree.remote[None] = [self.tree.local[1].to_dict()]
        self.assertEqual(await self.sync.ensure(), (2, True))
        self.assertEqual(self.tree.uploads, [None, None])

    async def test_duplicate_guild_set_removed_global_commands_retained(self):
        await self.sync.ensure()
        self.tree.remote[456] = [c.to_dict() for c in self.tree.local]
        self.store.meta["guild_command_signatures"] = {"456": "old"}
        self.assertEqual(await self.sync.remove_guild_copies(self.guild), 2)
        self.assertEqual(self.tree.remote[456], [])
        self.assertEqual(len(self.tree.remote[None]), 2)
        self.assertEqual(self.tree.copies, [])
        self.assertNotIn("456", self.store.meta["guild_command_signatures"])
        self.assertEqual(await self.sync.remove_guild_copies(self.guild), 0)
        self.assertEqual(self.tree.uploads, [None, 456])

    async def test_remote_option_changes_are_repaired(self):
        await self.sync.ensure()
        self.tree.remote[None][0]["options"][0]["max_length"] = 200
        self.assertEqual(await self.sync.ensure(), (2, True))

    async def test_failed_verification_does_not_save_success_signature(self):
        self.tree.bad_upload = True
        with self.assertRaisesRegex(RuntimeError, "did not match"):
            await self.sync.ensure()
        self.assertEqual(self.store.meta, {})

    async def test_simultaneous_ready_events_do_not_upload_twice(self):
        await asyncio.gather(self.sync.ensure(), self.sync.ensure())
        self.assertEqual(self.tree.uploads, [None])

    async def test_distinct_guild_command_is_preserved(self):
        await self.sync.ensure()
        special = {"name": "server-only", "type": 1, "description": "Unique command"}
        self.tree.remote[456] = [self.tree.local[0].to_dict(), special]
        self.assertEqual(await self.sync.remove_guild_copies(self.guild), 1)
        self.assertEqual(self.tree.remote[456], [special])
        self.assertEqual(self.tree.cleared, [])

    async def test_copies_not_deleted_if_global_registration_missing(self):
        self.tree.remote[456] = [c.to_dict() for c in self.tree.local]
        with self.assertRaisesRegex(RuntimeError, "Global commands must be verified"):
            await self.sync.remove_guild_copies(self.guild)
        self.assertEqual(len(self.tree.remote[456]), 2)
        self.assertEqual(self.tree.uploads, [])


if __name__ == "__main__":
    unittest.main()
