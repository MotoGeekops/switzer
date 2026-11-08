"""Offline tests for currency safety, persistent games, and Discord interactions."""

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import discord

from chat_client import AIError, OpenRouterChat, MODEL, FALLBACK
from community import DuelView, install_community
from community_game import ADVENTURE_STAGES, CASES, LOCAL, QUESTS, SHOP, CommunityGame, RuleError, day_key, season_key, week_key
from profile_card import render_profile
from storage import StateStore


def defaults():
    return {"balance": 10000, "trivia_correct": 0, "trivia_total": 0, "rps_wins": 0,
            "rps_losses": 0, "rps_ties": 0, "casino_wins": 0, "casino_losses": 0,
            "total_wagered": 0, "last_daily": None}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = StateStore(self.path, defaults)
        self.now = datetime(2026, 11, 6, 12, tzinfo=LOCAL).timestamp()
        self.game = CommunityGame(self.store, lambda: self.now)


class GameTests(Fixture):
    def test_pet_cooldowns_growth_and_shared_accessories(self):
        self.game.care_for_pet(1, 10, "feed")
        self.assertEqual(self.store.users[10]["balance"], 9950)
        with self.assertRaises(RuleError):
            self.game.care_for_pet(1, 10, "feed")
        self.game.care_for_pet(1, 10, "play")
        self.game.care_for_pet(1, 11, "feed")
        self.assertEqual(self.game.guild(1)["pet"]["xp"], 40)
        self.game.guild(1)["pet"]["xp"] = 350
        self.assertEqual(self.game.pet_appearance(1)[1], "Forest Spirit")
        self.game.buy(1, 10, "scarf")
        before = self.store.users[11]["balance"]
        with self.assertRaises(RuleError):
            self.game.buy(1, 11, "scarf")
        self.assertEqual(self.store.users[11]["balance"], before)
        self.assertNotIn("scarf", self.game.guild(2)["pet"]["accessories"])

    def test_quest_rewards_exactly_once_and_next_day_reset(self):
        for event, (_, target, reward) in QUESTS.items():
            for _ in range(target + 2):
                self.game.progress(1, 10, event)
        reward = self.game.claim_quests(1, 10)
        self.assertEqual(reward, sum(q[2] for q in QUESTS.values()))
        with self.assertRaises(RuleError):
            self.game.claim_quests(1, 10)
        self.assertEqual(self.store.users[10]["balance"], 10000 + reward)
        self.now += 86400
        self.assertEqual(self.game.member(1, 10)["claimed"], [])
        self.assertEqual(self.game.member(2, 10)["progress"], {})

    def test_daily_riddle_attempt_limit_and_success(self):
        answer = self.game.riddle(1)[1][0]
        self.game.solve_riddle(1, 10, answer.upper() + "!")
        with self.assertRaises(RuleError):
            self.game.solve_riddle(1, 10, answer)
        for _ in range(3):
            with self.assertRaises(RuleError):
                self.game.solve_riddle(1, 11, "definitely wrong")
        with self.assertRaises(RuleError):
            self.game.solve_riddle(1, 11, answer)

    def test_shop_insufficient_money_duplicate_and_expiring_title(self):
        self.store.users[10]["balance"] = 100
        with self.assertRaises(RuleError):
            self.game.buy(1, 10, "midnight")
        self.assertEqual(self.store.users[10]["balance"], 100)
        self.store.users[10]["balance"] = 10000
        self.game.buy(1, 10, "midnight")
        self.assertEqual(self.store.users[10]["equipped"]["background"], "midnight")
        with self.assertRaises(RuleError):
            self.game.buy(1, 10, "midnight")
        self.game.buy(1, 10, "legend")
        self.game.equip(10, "legend")
        with self.assertRaises(RuleError):
            self.game.buy(1, 10, "legend")
        self.now += 8 * 86400
        with self.assertRaises(RuleError):
            self.game.equip(10, "legend")
        self.game.buy(1, 10, "legend")

    def test_mystery_clues_required_rewards_exactly_once(self):
        game = self.game.start_mystery(1, 10, 100)
        case = CASES[game["case"]]
        with self.assertRaises(RuleError):
            self.game.accuse(1, 10, case["culprit"])
        for location in case["clues"]:
            self.game.investigate(1, 11, location)
        explanation, players = self.game.accuse(1, 10, case["culprit"])
        self.assertEqual(set(players), {10, 11})
        self.assertEqual(self.store.users[10]["balance"], 10400)
        with self.assertRaises(RuleError):
            self.game.accuse(1, 10, case["culprit"])
        self.assertEqual(self.store.users[10]["balance"], 10400)

    def test_mystery_wrong_guess_expiry_and_daily_limit(self):
        game = self.game.start_mystery(1, 10, 100)
        case = CASES[game["case"]]
        for location in case["clues"]:
            self.game.investigate(1, 10, location)
        wrong = next(s for s in case["suspects"] if s != case["culprit"])
        with self.assertRaises(RuleError):
            self.game.accuse(1, 10, wrong)
        with self.assertRaises(RuleError):
            self.game.accuse(1, 10, case["culprit"])
        self.now += 3601
        self.assertEqual(self.game.mystery(1)[0]["phase"], "expired")
        for _ in range(2):
            self.game.start_mystery(1, 10, 100)["phase"] = "ended"
        with self.assertRaises(RuleError):
            self.game.start_mystery(1, 10, 100)

    def test_adventure_party_votes_loot_rewards_and_daily_limit(self):
        game = self.game.create_adventure(1, 10, 100)
        self.game.join_adventure(1, 11)
        with self.assertRaises(RuleError):
            self.game.begin_adventure(1, 11)
        self.game.begin_adventure(1, 10)
        for stage in range(3):
            self.assertIsNone(self.game.adventure_vote(1, 10, 2))
            with self.assertRaises(RuleError):
                self.game.adventure_vote(1, 10, 1)
            result = self.game.adventure_vote(1, 11, 1)
            self.assertIn(ADVENTURE_STAGES[stage]["finds"][1], result)
        self.assertEqual(game["phase"], "complete")
        self.assertEqual(self.store.users[10]["balance"], 10600)
        self.assertEqual(len(self.store.users[10]["equipment"]), 3)
        self.assertIsNone(self.game.advance_adventure(1))
        with self.assertRaises(RuleError):
            self.game.adventure_vote(1, 10, 1)
        self.game.member(1, 10)["adventures"] = 3
        with self.assertRaises(RuleError):
            self.game.create_adventure(1, 10, 100)

    def test_adventure_timeout_uses_cast_votes_or_expires_without_reward(self):
        game = self.game.create_adventure(1, 10, 100)
        self.game.join_adventure(1, 11)
        self.game.begin_adventure(1, 10)
        self.game.adventure_vote(1, 11, 2)
        self.now += 91
        self.assertIn("Firefly Lantern", self.game.advance_adventure(1))
        self.now += 91
        self.assertIn("expired", self.game.advance_adventure(1))
        self.assertEqual(game["phase"], "expired")
        self.assertEqual(self.store.users[11]["balance"], 10000)

    def test_quotes_one_vote_weekly_archive_and_nomination_cap(self):
        first = self.game.nominate(1, 10, 11, 100, 1, "funny", "https://discord.com/channels/1/100/1")
        second = self.game.nominate(1, 10, 12, 100, 2, "funnier", "https://discord.com/channels/1/100/2")
        self.game.quote_vote(1, 15, first["id"])
        self.game.quote_vote(1, 15, second["id"])
        self.assertEqual(first["votes"], [])
        self.assertEqual(second["votes"], [15])
        with self.assertRaises(RuleError):
            self.game.quote_vote(1, 12, second["id"])
        self.game.nominate(1, 10, 13, 100, 3, "third", "https://discord.com/channels/1/100/3")
        with self.assertRaises(RuleError):
            self.game.nominate(1, 10, 13, 100, 4, "fourth", "https://discord.com/channels/1/100/4")
        self.now += 7 * 86400
        self.assertEqual(self.game.roll_quotes(1), [second])
        self.assertEqual(self.game.roll_quotes(1), [])
        self.assertEqual(len(self.game.guild(1)["archives"]), 1)

    def test_season_points_pair_cap_and_next_month(self):
        for _ in range(5):
            self.assertTrue(self.game.record_duel(1, (10, 11), 10))
        self.assertFalse(self.game.record_duel(1, (11, 10), 10))
        entry = self.game.guild(1)["seasons"][season_key(self.now)]["10"]
        self.assertEqual(entry["points"], 15)
        self.assertEqual(self.store.users[10]["balance"], 11000)
        self.now += 31 * 86400
        self.game.record_duel(1, (10, 11), None)
        self.assertEqual(self.game.guild(1)["seasons"][season_key(self.now)]["10"]["points"], 1)

    def test_daily_question_is_stable_and_changes_next_day(self):
        first = self.game.question(1)
        self.assertEqual(first, self.game.question(1))
        self.now += 86400
        self.assertNotEqual(first, self.game.question(1))


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_migration_roundtrip_and_backup_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            legacy = Path(directory) / "user_stats.json"
            original = json.dumps({"10": {"balance": 12345, "trivia_correct": 7}})
            legacy.write_text(original)
            store = StateStore(path, defaults, legacy)
            self.assertEqual(store.users[10]["balance"], 12345)
            game = CommunityGame(store)
            game.create_adventure(1, 10, 100)
            await store.flush()
            again = StateStore(path, defaults, legacy)
            self.assertEqual(again.users[10]["trivia_correct"], 7)
            self.assertEqual(CommunityGame(again).adventure(1)["phase"], "lobby")
            self.assertEqual(legacy.read_text(), original)
            path.write_text("broken json")
            with self.assertLogs("storage", level="WARNING"):
                recovered = StateStore(path, defaults, legacy)
            self.assertEqual(recovered.users[10]["balance"], 12345)

    async def test_unreadable_primary_and_backup_do_not_reset_money(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("broken")
            with self.assertLogs("storage", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    StateStore(path, defaults)
            self.assertEqual(path.read_text(), "broken")

    async def test_coalesces_writes_and_flushes_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json", defaults)
            with patch.object(store, "_write", wraps=store._write) as writer:
                for _ in range(20):
                    store.users[10]["balance"] += 1
                    store.mark_dirty()
                await store.flush()
                await store.flush()
                self.assertEqual(writer.call_count, 1)
            store.start()
            store.users[10]["balance"] += 5
            store.mark_dirty()
            await store.close()
            self.assertEqual(StateStore(store.path, defaults).users[10]["balance"], 10025)

    async def test_shutdown_waits_for_inflight_writer_and_saves_latest_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json", defaults)
            started = threading.Event()
            write = store._write
            def slow_write(snapshot):
                started.set()
                time.sleep(0.05)
                write(snapshot)
            with patch.object(store, "_write", side_effect=slow_write):
                store.start()
                saving = asyncio.create_task(store.flush())
                await asyncio.to_thread(started.wait, 2)
                store.users[10]["balance"] += 77
                store.mark_dirty()
                await store.close()
                await saving
            self.assertEqual(StateStore(store.path, defaults).users[10]["balance"], 10077)


class FakeResponse:
    def __init__(self):
        self.done = False
        self.calls = []

    def is_done(self):
        return self.done

    async def send_message(self, *args, **kwargs):
        self.done = True
        self.calls.append((args, kwargs))

    async def defer(self, **kwargs):
        self.done = True

    async def edit_message(self, **kwargs):
        self.done = True
        self.calls.append(((), kwargs))


def interaction(uid=10, gid=1):
    message = SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, bot=False, name=f"player{uid}", display_name=f"Player {uid}",
                             guild_permissions=SimpleNamespace(manage_guild=False),
                             display_avatar=SimpleNamespace(url="https://example.com/avatar.png")),
        guild_id=gid, channel_id=100, guild=SimpleNamespace(id=gid),
        response=FakeResponse(), followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(return_value=message), message=message,
        edit_original_response=AsyncMock(),
    )


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.module = bot
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.json", defaults)
        self.client = bot.MyBot()
        self.hub = install_community(self.client, self.store, bot.TRIVIA)
        self.client.community = self.hub
        self.patches = [patch.object(bot, "bot", self.client), patch.object(bot, "state_store", self.store), patch.object(bot, "user_stats", self.store.users)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for view in tuple(self.module.active_blackjack_views):
            view.finish()
        await self.hub.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def command(self, name, inter=None, **kwargs):
        inter = inter or interaction()
        await self.client.tree.get_command(name).callback(inter, **kwargs)
        return inter

    async def test_every_command_schema_and_permissions(self):
        commands = self.client.tree.get_commands()
        self.assertEqual(len(commands), 12)
        json.dumps([c.to_dict(self.client.tree) for c in commands])
        setup = self.client.tree.get_command("communitysetup")
        self.assertTrue(setup.default_permissions.manage_guild)
        for name in ("pet", "quests", "shop", "question", "leaderboard", "hall"):
            await self.command(name)
        ask_schema = self.module.ask.to_dict(self.client.tree)
        self.assertLessEqual(ask_schema["options"][0]["max_length"], 6000)

    async def test_adventure_and_mystery_command_flows(self):
        await self.command("adventure", action="create")
        await self.command("adventure", interaction(11), action="join")
        await self.command("adventure", action="begin")
        for _ in range(3):
            await self.command("adventure", action="vote", choice=1)
            await self.command("adventure", interaction(11), action="vote", choice=2)
        self.assertEqual(self.store.users[10]["balance"], 10600)
        await self.command("mystery", action="start")
        game, case = self.hub.game.mystery(1)
        for place in case["clues"]:
            await self.command("mystery", action="clue", target=place)
        await self.command("mystery", action="accuse", target=case["culprit"])
        self.assertEqual(self.store.users[10]["balance"], 11000)

    async def test_duplicate_trivia_and_rps_callbacks_count_once(self):
        view = self.module.TriviaView(10, "Q?", ["A", "B", "C", "D"], 0)
        await view.children[0].callback(interaction())
        await view.children[0].callback(interaction())
        self.assertEqual(self.store.users[10]["trivia_total"], 1)
        self.assertEqual(self.hub.game.member(1, 10)["progress"]["trivia"], 1)
        rps = self.module.RPSView(10)
        await rps.children[0].callback(interaction())
        await rps.children[0].callback(interaction())
        stats = self.store.users[10]
        self.assertEqual(sum(stats[k] for k in ("rps_wins", "rps_losses", "rps_ties")), 1)

    async def test_blackjack_acknowledges_before_off_thread_render_and_refunds_once(self):
        main_thread = threading.get_ident()
        inter = interaction()

        def renderer(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertTrue(inter.response.done)
            return discord.File(BytesIO(b"fake image"), filename="table.jpg")

        with patch.object(self.module, "draw_card", side_effect=["8H", "5H", "9H", "7H"]), patch.object(self.module, "blackjack_table_file", side_effect=renderer):
            await self.module.play_blackjack(inter, 10, 100)
        view = next(v for v in self.module.active_blackjack_views if v.player_id == 10)
        self.assertEqual(self.store.users[10]["balance"], 9900)
        self.assertEqual(self.store.users[10]["blackjack_escrow"], 100)
        view.message = None
        await view.on_timeout()
        await view.on_timeout()
        self.assertEqual(self.store.users[10]["balance"], 10000)
        self.assertNotIn("blackjack_escrow", self.store.users[10])

    async def test_failed_acknowledgement_refunds_each_casino_wager(self):
        for play, args in ((self.module.play_blackjack, (10, 100)),
                           (self.module.play_baccarat, (10, "player", 100)),
                           (self.module.play_roulette, (10, "red", 100))):
            inter = interaction()
            inter.response.defer = AsyncMock(side_effect=RuntimeError("fake network failure"))
            with self.assertRaises(RuntimeError):
                await play(inter, *args)
            self.assertEqual(self.store.users[10]["balance"], 10000)
            self.assertNotIn(10, self.module.active_blackjack_players)

    async def test_restart_refunds_persisted_blackjack_escrow_once(self):
        self.store.users[10].update(balance=9900, blackjack_escrow=100)
        self.store.mark_dirty()
        await self.store.flush()
        again = StateStore(self.store.path, defaults)
        self.assertEqual(self.module.recover_blackjack_wagers(again.users), 1)
        self.assertEqual(again.users[10]["balance"], 10000)
        self.assertEqual(self.module.recover_blackjack_wagers(again.users), 0)

    async def test_chat_histories_are_separate_for_each_server(self):
        self.client.ai = SimpleNamespace(complete=AsyncMock(return_value="Hello!"))
        with patch.object(self.module, "conversation_history", OrderedDict()):
            await self.module.ask_openrouter(10, "server one", 1)
            await self.module.ask_openrouter(10, "server two", 2)
            self.assertEqual(len(self.module.conversation_history), 2)
            second_messages = self.client.ai.complete.call_args_list[1].args[0]
            self.assertNotIn("server one", str(second_messages))
            await self.module.reset.callback(interaction(10, 1))
            self.assertNotIn((1, 10), self.module.conversation_history)
            self.assertIn((2, 10), self.module.conversation_history)

    async def test_profile_command_includes_purchased_background_card(self):
        self.hub.game.buy(1, 10, "midnight")
        inter = interaction()
        await self.module.profile.callback(inter)
        kwargs = inter.followup.send.call_args.kwargs
        self.assertEqual(kwargs["file"].filename, "profile.png")
        self.assertEqual(kwargs["embed"].image.url, "attachment://profile.png")
        self.assertLess(len(kwargs["embed"]), 6000)

    async def test_commands_have_one_central_error_handler(self):
        for command in self.client.tree.get_commands():
            self.assertIsNone(command.on_error)
        inter = interaction()
        await self.client.tree.on_error(inter, discord.app_commands.CommandInvokeError(self.client.tree.get_command("pet"), RuleError("Test rejection")))
        self.assertEqual(len(inter.response.calls), 1)
        self.assertEqual(inter.response.calls[0][0][0], "Test rejection")

    async def test_trivia_duel_hidden_answers_and_single_reward(self):
        view = DuelView(self.hub, 1, 10, 11, "trivia")
        view.message = SimpleNamespace(edit=AsyncMock())
        view.question = ("Q", ["A", "B", "C", "D"], 0)
        await view.act(interaction(11), "accept")
        answer = interaction(10)
        await view.act(answer, "0")
        self.assertTrue(answer.response.calls[0][1]["ephemeral"])
        await view.act(interaction(11), "1")
        self.assertEqual(view.phase, "finished")
        self.assertEqual(self.store.users[10]["balance"], 10200)
        await view.finish(10, "duplicate")
        self.assertEqual(self.store.users[10]["balance"], 10200)

    async def test_reaction_duel_early_click_forfeits(self):
        view = DuelView(self.hub, 1, 10, 11, "reaction")
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.act(interaction(11), "accept")
        await view.act(interaction(10), "react")
        self.assertEqual(view.phase, "finished")
        self.assertEqual(self.store.users[11]["balance"], 10200)

    async def test_reaction_duel_first_click_settles_once(self):
        view = DuelView(self.hub, 1, 10, 11, "reaction")
        view.message = SimpleNamespace(edit=AsyncMock())
        view.phase = "go"
        view.started = time.monotonic()
        await view.act(interaction(10), "react")
        with self.assertRaises(RuleError):
            await view.act(interaction(11), "react")
        self.assertEqual(self.store.users[10]["balance"], 10200)
        self.assertEqual(self.store.users[11]["balance"], 10000)

    async def test_private_quotes_are_hidden_and_hall_is_ephemeral(self):
        quote = self.hub.game.nominate(1, 10, 11, 222, 333, "private joke", "https://discord.com/channels/1/222/333")
        inter = interaction()
        hidden = SimpleNamespace(permissions_for=lambda _: SimpleNamespace(view_channel=False, read_message_history=False))
        inter.guild.get_channel = lambda channel_id: hidden
        self.assertFalse(self.hub.can_read_quote(inter, quote))
        await self.command("hall", inter)
        args, kwargs = inter.response.calls[0]
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(len(kwargs["embed"].fields), 0)

    async def test_scheduler_daily_question_once_and_disabled_by_default(self):
        sender = SimpleNamespace(send=AsyncMock())
        with patch.object(self.client, "get_channel", return_value=sender):
            await self.hub.tick(1)
            sender.send.assert_not_awaited()
            settings = self.hub.game.guild(1)["settings"]
            settings.update(daily_channel=100, daily_hour=0)
            await self.hub.tick(1)
            await self.hub.tick(1)
            sender.send.assert_awaited_once()

    async def test_weekly_announcement_is_not_lost_if_hall_archives_first(self):
        quote = self.hub.game.nominate(1, 10, 11, 100, 333, "winner", "https://discord.com/channels/1/100/333")
        quote["week"] = "2026-W01"
        quote["votes"] = [12]
        self.hub.game.roll_quotes(1)
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(self.client, "get_channel", return_value=channel):
            await self.hub.tick(1)
            await self.hub.tick(1)
        channel.send.assert_awaited_once()

    async def test_profile_backgrounds_render_differently(self):
        first = await asyncio.to_thread(render_profile, "Test", 10000, "midnight")
        second = await asyncio.to_thread(render_profile, "Test", 10000, "sunset")
        self.assertNotEqual(first, second)
        from PIL import Image
        self.assertEqual(Image.open(BytesIO(first)).size, (900, 260))


class ChatTests(unittest.IsolatedAsyncioTestCase):
    def session(self, status, data):
        response = SimpleNamespace(status=status, json=AsyncMock(return_value=data))
        class Request:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): return False
        session = SimpleNamespace(post=lambda *args, **kwargs: Request())
        return session

    async def test_free_fallbacks_and_payload(self):
        captured = {}
        session = self.session(200, {"choices": [{"message": {"content": "hello"}}]})
        original = session.post
        def post(*args, **kwargs):
            captured.update(kwargs)
            return original(*args, **kwargs)
        session.post = post
        reply = await OpenRouterChat(session, "fake-key").complete([{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "hello")
        self.assertEqual(captured["json"]["models"], [MODEL, FALLBACK])
        self.assertTrue(all(m.endswith(":free") for m in captured["json"]["models"]))
        self.assertFalse(captured["json"]["reasoning"]["enabled"])

    async def test_rate_limit_and_empty_response_are_friendly(self):
        for status, data, expected in [(429, {}, "free AI limit"), (200, {"choices": []}, "incomplete"), (200, {"choices": [{"message": {"content": ""}}]}, "empty")]:
            with self.assertRaisesRegex(AIError, expected):
                await OpenRouterChat(self.session(status, data), "fake-key").complete([])

    async def test_busy_queue_does_not_wait_indefinitely(self):
        client = OpenRouterChat(self.session(200, {}), "fake-key")
        for _ in range(3):
            await client.slots.acquire()
        with self.assertRaisesRegex(AIError, "several people"):
            await client.complete([])


if __name__ == "__main__":
    unittest.main()
