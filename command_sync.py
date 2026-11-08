"""Verify one global command set and remove obsolete guild duplicates."""

import asyncio
import hashlib
import json


def command_shape(command):
    """Compare public command fields without IDs or API-only default values."""
    data = command if isinstance(command, dict) else command.to_dict()

    def option_shape(option):
        return {
            "name": option["name"], "type": option["type"],
            "description": option.get("description", ""),
            "required": option.get("required", False),
            "autocomplete": option.get("autocomplete", False),
            "choices": [{"name": c["name"], "value": c["value"]} for c in option.get("choices", [])],
            "options": [option_shape(o) for o in option.get("options", [])],
            **{key: option.get(key) for key in ("min_value", "max_value", "min_length", "max_length")},
            "channel_types": option.get("channel_types", []),
        }

    return {"name": data["name"], "type": data.get("type", 1),
            "description": data.get("description", ""),
            "options": [option_shape(o) for o in data.get("options", [])]}


class CommandSync:
    def __init__(self, tree, store, application_id):
        self.tree, self.store, self.application_id = tree, store, application_id
        self.lock = asyncio.Lock()

    async def ensure(self):
        async with self.lock:
            local = self.tree.get_commands()
            payload = [command.to_dict(self.tree) for command in local]
            schema = {"application_id": self.application_id, "commands": payload}
            signature = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
            saved = self.store.meta.get("command_signature")

            remote = await self.tree.fetch_commands()

            def shapes(commands):
                return sorted((command_shape(c) for c in commands), key=lambda c: (c["type"], c["name"]))

            changed = saved != signature or shapes(payload) != shapes(remote)
            if changed:
                await self.tree.sync()
                remote = await self.tree.fetch_commands()
                if shapes(payload) != shapes(remote):
                    raise RuntimeError("Discord's command registrations did not match after syncing.")
                self.store.meta["command_signature"] = signature
                self.store.mark_dirty()
            print(f"{'Synced and verified' if changed else 'Verified'} {len(remote)} global commands.", flush=True)
            return len(remote), changed

    async def remove_guild_copies(self, guild):
        """Keep global commands; preserve guild commands with distinct names."""
        async with self.lock:
            def key(command):
                data = command if isinstance(command, dict) else command.to_dict()
                return data["name"], data.get("type", 1)

            expected = {key(c.to_dict(self.tree)) for c in self.tree.get_commands()}
            global_commands = await self.tree.fetch_commands()
            global_keys = {key(c) for c in global_commands}
            if not expected <= global_keys:
                raise RuntimeError("Global commands must be verified before removing guild copies.")
            remote = await self.tree.fetch_commands(guild=guild)
            duplicates = [c for c in remote if key(c) in expected]
            if duplicates:
                local_only = [c for c in self.tree.get_commands(guild=guild) if key(c.to_dict(self.tree)) not in expected]
                if len(duplicates) == len(remote) and not local_only:
                    # The previous sync registered a full duplicate set. One bulk
                    # update removes it without dozens of rate-limited deletes.
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                else:
                    for command in duplicates:
                        await command.delete()
                        self.tree.remove_command(command.name, guild=guild, type=command.type)
                remaining = await self.tree.fetch_commands(guild=guild)
                if any(key(c) in expected for c in remaining):
                    raise RuntimeError("Duplicate guild commands remained after cleanup.")
                print(f"Removed {len(duplicates)} duplicate commands from server {guild.name}; global commands retained.", flush=True)
            signatures = self.store.meta.get("guild_command_signatures", {})
            if str(guild.id) in signatures:
                del signatures[str(guild.id)]
                self.store.mark_dirty()
            return len(duplicates)
