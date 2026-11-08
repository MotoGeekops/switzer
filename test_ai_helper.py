"""Offline tests for Grok-style AI helper, 24/7 tasks, KB, auto-watch, and paused game notices."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from switzer_ui import plain_text

import bot as bot_module
from ai_helper import PAUSED_GAME_COMMANDS, install_ai_helper
from chat_client import needs_web_search, parse_ddg_html
from community_game import RuleError
from storage import StateStore
from test_community import defaults, interaction


class AIHelperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.json", defaults)
        self.now = 1_760_000_000.0
        self.client = bot_module.MyBot()
        self.client.ai = SimpleNamespace(
            complete=AsyncMock(return_value="Grok-style response!"),
            search_web=AsyncMock(return_value=[{"title": "Example News", "url": "https://example.com", "snippet": "Latest update."}]),
        )
        self.helper = install_ai_helper(self.client, self.store)
        self.helper.engine.clock = lambda: self.now
        self.client.helper = self.helper

    async def asyncTearDown(self):
        await self.helper.close()
        self.temp.cleanup()

    async def test_paused_game_commands_highlight_unavailable_and_future_return(self):
        for cmd_name in ("blackjack", "pet", "trivia", "slots", "duel"):
            self.assertIn(cmd_name, PAUSED_GAME_COMMANDS)
            inter = interaction()
            inter.type = discord.InteractionType.application_command
            inter.command = SimpleNamespace(name=cmd_name)
            allowed = await self.client.tree.interaction_check(inter)
            self.assertFalse(allowed)
            self.assertEqual(len(inter.response.calls), 1)
            sent_text = inter.response.calls[0][0][0]
            sent_kwargs = inter.response.calls[0][1]
            self.assertIn("unavailable right now", sent_text.lower())
            self.assertIn("may come back in the future", sent_text.lower())
            self.assertTrue(sent_kwargs.get("ephemeral"))
            embed_desc = sent_kwargs["embed"].description.lower()
            self.assertIn("unavailable right now", embed_desc)
            self.assertIn("may come back in the future", embed_desc)

    async def test_ai_helper_commands_are_allowed_by_interaction_check(self):
        for cmd_name in ("ask", "task", "autowatch", "kb", "summarize", "image", "list", "reset"):
            inter = interaction()
            inter.type = discord.InteractionType.application_command
            inter.command = SimpleNamespace(name=cmd_name)
            allowed = await self.client.tree.interaction_check(inter)
            self.assertTrue(allowed)

    async def test_24_7_task_lifecycle_and_scheduler_tick(self):
        admin = interaction()
        admin.user.guild_permissions.manage_guild = True
        task_cmd = self.client.tree.get_command("task")

        await task_cmd.callback(admin, action="add", prompt="Summarize top AI news", interval="1h", web_search=True)
        tasks = self.helper.engine.list_tasks(1)
        self.assertEqual(len(tasks), 1)
        task_id = tasks[0]["id"]

        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(self.client, "get_channel", return_value=channel):
            # Not due yet
            await self.helper.tick(1)
            channel.send.assert_not_awaited()

            # Advance clock 1 hour -> task executes automatically
            self.now += 3601
            await self.helper.tick(1)
            channel.send.assert_awaited_once()
            sent_msg = plain_text(*channel.send.call_args.args, **channel.send.call_args.kwargs)
            self.assertIn(f"#{task_id}", sent_msg)
            self.assertIn("Grok-style response!", sent_msg)

        # Pause and resume
        await task_cmd.callback(interaction(), action="list")
        await task_cmd.callback(admin, action="pause", task_id=task_id)
        self.assertEqual(self.helper.engine.get_task(1, task_id)["status"], "paused")
        await task_cmd.callback(admin, action="resume", task_id=task_id)
        self.assertEqual(self.helper.engine.get_task(1, task_id)["status"], "active")
        await task_cmd.callback(admin, action="remove", task_id=task_id)
        self.assertEqual(self.helper.engine.list_tasks(1), [])

    async def test_kb_and_mention_reply_with_context(self):
        admin = interaction()
        admin.user.guild_permissions.manage_guild = True
        kb_cmd = self.client.tree.get_command("kb")
        await kb_cmd.callback(admin, action="add", topic="Verification", content="React in #verify to get the Member role.")
        self.assertIn("React in #verify", self.helper.system_prompt(1))

        # Simulate @Switzer mention on a message
        self.client._connection.user = SimpleNamespace(id=999, bot=True)
        fake_msg = SimpleNamespace(
            author=SimpleNamespace(id=10, bot=False, display_name="Alice"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100, send=AsyncMock()),
            content="<@999> how do I get verified today?",
            mentions=[SimpleNamespace(id=999)],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        handled = await self.helper.handle_message(fake_msg)
        self.assertTrue(handled)
        fake_msg.reply.assert_awaited_once()
        # Web search triggered because of 'today'
        self.client.ai.search_web.assert_awaited()

    async def test_autowatch_answers_questions_in_watched_channel(self):
        admin = interaction()
        admin.user.guild_permissions.manage_guild = True
        autowatch_cmd = self.client.tree.get_command("autowatch")
        target_chan = SimpleNamespace(id=555)
        await autowatch_cmd.callback(admin, mode="questions", channel=target_chan, instructions="Be helpful.")

        self.client._connection.user = SimpleNamespace(id=999, bot=True)
        msg = SimpleNamespace(
            author=SimpleNamespace(id=20, bot=False, display_name="Bob"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=555, send=AsyncMock()),
            content="How do I join the event?",
            mentions=[],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        self.assertTrue(await self.helper.handle_message(msg))
        msg.reply.assert_awaited_once()

    async def test_natural_language_reminder_in_mention(self):
        self.client._connection.user = SimpleNamespace(id=999, bot=True)
        fake_msg = SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False, display_name="Lovington"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100, send=AsyncMock()),
            content="<@999> remind to feed my dogs in like 30 minutes, can you do that",
            mentions=[SimpleNamespace(id=999)],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        handled = await self.helper.handle_message(fake_msg)
        self.assertTrue(handled)
        fake_msg.reply.assert_awaited_once()
        reply_text = plain_text(*fake_msg.reply.call_args.args, **fake_msg.reply.call_args.kwargs)
        self.assertIn("Reminder locked in", reply_text)
        self.assertIn("30 minutes", reply_text)
        self.assertIn("feed your dogs", reply_text)

        tasks = self.helper.engine.list_tasks(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["interval"], "once")
        self.assertEqual(tasks[0]["next_run"], self.now + 1800)

        # Advance 30 minutes -> reminder fires and pings <@42>
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(self.client, "get_channel", return_value=channel):
            self.now += 1801
            await self.helper.tick(1)
            channel.send.assert_awaited_once()
            fired_text = plain_text(*channel.send.call_args.args, **channel.send.call_args.kwargs)
            self.assertIn("<@42>", fired_text)
            self.assertIn("feed your dogs", fired_text)
        self.assertEqual(self.helper.engine.list_tasks(1), [])

        # Also verify 'remind me in 10 seconds i need to feed my dog' strips 'i need to' cleanly
        msg2 = SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False, display_name="Lovington"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100, send=AsyncMock()),
            content="<@999> remind me in 10 seconds i need to feed my dog",
            mentions=[SimpleNamespace(id=999)],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        await self.helper.handle_message(msg2)
        reply2 = plain_text(*msg2.reply.call_args.args, **msg2.reply.call_args.kwargs)
        self.assertIn("to **feed your dog**.", reply2)
        self.assertNotIn("i need to", reply2.lower())
        self.helper.engine.remove_task(1, self.helper.engine.list_tasks(1)[0]["id"])

        # Verify normal chat questions or complaints NEVER create ghost scheduled tasks (even with 'every day')
        self.client.ai.complete = AsyncMock(return_value="[SCHEDULE_TASK: delay=10s | repeat=once | task=Ghost] Yes, it is fresh.")
        for prompt_text in (
            "<@999> is this recent?",
            "<@999> why did you just do a scheduled task for no reason i didnt ask for it",
            "<@999> I work out every 2 days, is that enough?",
            "<@999> why does my PC crash every day?",
        ):
            msg_normal = SimpleNamespace(
                author=SimpleNamespace(id=42, bot=False, display_name="Lovington"),
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=100, send=AsyncMock()),
                content=prompt_text,
                mentions=[SimpleNamespace(id=999)],
                attachments=[],
                reference=None,
                reply=AsyncMock(),
            )
            await self.helper.handle_message(msg_normal)
            self.assertEqual(self.helper.engine.list_tasks(1), [])
            sent_reply = plain_text(*msg_normal.reply.call_args.args, **msg_normal.reply.call_args.kwargs)
            self.assertNotIn("SCHEDULE_TASK", sent_reply)
            self.assertNotIn("Scheduled task", sent_reply)

        # Verify 'remind me in an hour' and natural-language 'cancel my reminders'
        msg_hr = SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False, display_name="Lovington"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100, send=AsyncMock()),
            content="<@999> remind me in an hour to check the oven",
            mentions=[SimpleNamespace(id=999)],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        await self.helper.handle_message(msg_hr)
        self.assertEqual(len(self.helper.engine.list_tasks(1)), 1)
        self.assertEqual(self.helper.engine.list_tasks(1)[0]["next_run"], self.now + 3600)

        msg_cancel = SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False, display_name="Lovington"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100, send=AsyncMock()),
            content="<@999> cancel my reminders",
            mentions=[SimpleNamespace(id=999)],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        await self.helper.handle_message(msg_cancel)
        self.assertEqual(self.helper.engine.list_tasks(1), [])
        self.assertIn("Cancelled **1**", plain_text(*msg_cancel.reply.call_args.args, **msg_cancel.reply.call_args.kwargs))

        # Verify multi-step 'do X now, then in 5 minutes review Y' (user's exact prompt)
        self.client.ai.complete = AsyncMock(side_effect=[
            "The American Civil War remains the most transformative period in U.S. history...",
            "Here is the human-polished review and revision of the essay...",
        ])
        msg_essay = SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False, display_name="Lovington"),
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=100, send=AsyncMock()),
            content="<@999> create a essay for me about the american civil war than about 5 minutes review your essay and make sure its human like written. draft the essay now.",
            mentions=[SimpleNamespace(id=999)],
            attachments=[],
            reference=None,
            reply=AsyncMock(),
        )
        await self.helper.handle_message(msg_essay)
        immediate_reply = plain_text(*msg_essay.reply.call_args.args, **msg_essay.reply.call_args.kwargs)
        self.assertIn("The American Civil War remains", immediate_reply)
        self.assertIn("Scheduled Follow-Up Task", plain_text(*msg_essay.channel.send.call_args.args, **msg_essay.channel.send.call_args.kwargs))
        self.assertIn("review your essay and make sure its human like written", plain_text(*msg_essay.channel.send.call_args.args, **msg_essay.channel.send.call_args.kwargs))

        tasks = self.helper.engine.list_tasks(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["next_run"], self.now + 300)
        self.assertIsNone(tasks[0]["direct_message"])

        # Advance 5 minutes -> AI executes the follow-up review task and pings <@42>
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(self.client, "get_channel", return_value=channel):
            self.now += 301
            await self.helper.tick(1)
            channel.send.assert_awaited_once()
            followup_post = plain_text(*channel.send.call_args.args, **channel.send.call_args.kwargs)
            self.assertIn("<@42>", followup_post)
            self.assertIn("human-polished review", followup_post)
        self.assertEqual(self.helper.engine.list_tasks(1), [])

    def test_web_search_detection_and_ddg_parser(self):
        self.assertTrue(needs_web_search("What is the latest news today?"))
        self.assertFalse(needs_web_search("is this recent?"))
        self.assertFalse(needs_web_search("Tell me a bedtime story about dragons"))
        sample_html = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle">Test Title</a>'
            '<a class="result__snippet">Sample snippet text.</a>'
        )
        parsed = parse_ddg_html(sample_html)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["url"], "https://example.com/article")
        self.assertEqual(parsed[0]["title"], "Test Title")


if __name__ == "__main__":
    unittest.main()

