"""Discord commands and multiplayer views for Switzer's community games."""

import asyncio
from contextlib import suppress
from datetime import datetime
import logging
import random
import time
from typing import Literal

import discord
from discord import app_commands

from chat_client import AIError
from community_game import ADVENTURE_STAGES, CASES, LOCAL, QUESTS, SHOP, CommunityGame, RuleError, day_key, season_key

log = logging.getLogger(__name__)


async def respond(interaction, content=None, **kwargs):
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.error("Button action failed", exc_info=(type(error), error, error.__traceback__))
        with suppress(discord.HTTPException):
            await respond(interaction, "That action could not finish. Please check the current game status before trying again.", ephemeral=True)

    async def disable_message(self):
        for child in self.children:
            child.disabled = True
        if getattr(self, "message", None):
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class DuelView(SafeView):
    def __init__(self, hub, guild_id, challenger, opponent, mode):
        super().__init__(timeout=90)
        self.hub, self.guild_id = hub, guild_id
        self.players = (challenger, opponent)
        self.mode = mode
        self.phase = "invite"
        self.answers = {}
        self.message = None
        self.countdown = None
        self.question = random.choice(hub.trivia)
        self.started = None
        self.button("Accept", "accept", discord.ButtonStyle.success)
        self.button("Decline", "decline", discord.ButtonStyle.secondary)

    def button(self, label, value, style=discord.ButtonStyle.primary):
        button = discord.ui.Button(label=label, style=style, custom_id=value)

        async def callback(interaction):
            try:
                await self.act(interaction, value)
            except RuleError as error:
                await respond(interaction, str(error), ephemeral=True)

        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.user.id not in self.players:
            await respond(interaction, "Only these two challengers can use this duel.", ephemeral=True)
            return False
        return True

    def release(self):
        self.hub.duels.discard(self)
        for uid in self.players:
            self.hub.active_duel_players.discard((self.guild_id, uid))
        if self.countdown and self.countdown is not asyncio.current_task():
            self.countdown.cancel()
        self.stop()

    async def act(self, interaction, value):
        if self.message is None:
            self.message = interaction.message
        uid = interaction.user.id
        if self.phase == "finished":
            raise RuleError("This duel has already ended.")
        if self.phase == "invite":
            if uid != self.players[1]:
                raise RuleError("Your opponent needs to accept or decline.")
            if value == "decline":
                self.phase = "finished"
                self.release()
                self.clear_items()
                await interaction.response.edit_message(content="Challenge declined.", view=self)
                return
            if value != "accept":
                raise RuleError("Accept the invitation first.")
            self.clear_items()
            if self.mode == "trivia":
                self.phase = "trivia"
                question, choices, _ = self.question
                for index, choice in enumerate(choices):
                    self.button(choice, str(index))
                await interaction.response.edit_message(content=f"🧠 **Trivia duel**\n{question}\nAnswers are private until both players answer. You have 60 seconds.", view=self)
                self.countdown = asyncio.create_task(self.trivia_timer())
            else:
                self.phase = "waiting"
                self.button("WAIT...", "react", discord.ButtonStyle.danger)
                await interaction.response.edit_message(content="⚡ **Reaction duel**\nWait until the message says **GO!** Clicking early forfeits the round.", view=self)
                self.countdown = asyncio.create_task(self.reaction_countdown())
            return
        if self.phase == "trivia":
            if value not in {"0", "1", "2", "3"}:
                raise RuleError("Use one of the answer buttons.")
            if uid in self.answers:
                raise RuleError("Your answer is already locked in.")
            self.answers[uid] = int(value)
            await interaction.response.send_message("Answer locked in.", ephemeral=True)
            if len(self.answers) == 2:
                await self.finish_trivia()
        elif self.phase == "waiting":
            await interaction.response.defer()
            await self.finish(next(p for p in self.players if p != uid), "An early click forfeited the round.")
        elif self.phase == "go":
            self.phase = "settling"
            elapsed = int((time.monotonic() - self.started) * 1000)
            await interaction.response.defer()
            await self.finish(uid, f"First reaction received after {elapsed} ms. Network latency affects this game.")
        else:
            raise RuleError("The duel result is being saved.")

    async def trivia_timer(self):
        try:
            await asyncio.sleep(60)
            if self.phase == "trivia":
                await self.finish_trivia()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Duel timer failed")
            self.phase = "finished"
            self.release()

    async def reaction_countdown(self):
        try:
            await asyncio.sleep(random.uniform(2, 5))
            if self.phase != "waiting":
                return
            # Keep early-click protection active until Discord accepts the edit.
            self.children[0].label = "GO!"
            self.children[0].style = discord.ButtonStyle.success
            await self.message.edit(content="⚡ **GO! First click wins.**", view=self)
            if self.phase != "waiting":
                return
            self.started = time.monotonic()
            self.phase = "go"
            await asyncio.sleep(20)
            if self.phase == "go":
                self.phase = "finished"
                self.release()
                await self.disable_message()
                await self.message.edit(content="Reaction duel expired without a click. No rewards issued.", view=self)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Reaction countdown failed")
            self.phase = "finished"
            self.release()

    async def finish_trivia(self):
        if self.phase != "trivia":
            return
        self.phase = "settling"
        correct = self.question[2]
        winners = [uid for uid, answer in self.answers.items() if answer == correct]
        if not self.answers:
            self.phase = "finished"
            self.release()
            await self.disable_message()
            await self.message.edit(content="Trivia duel expired without answers. No rewards issued.", view=self)
            return
        await self.finish(winners[0] if len(winners) == 1 else None, f"Correct answer: **{self.question[1][correct]}**.")

    async def finish(self, winner, reason):
        if self.phase == "finished":
            return
        self.phase = "finished"
        rewarded = self.hub.game.record_duel(self.guild_id, self.players, winner)
        self.release()
        for child in self.children:
            child.disabled = True
        result = "It's a tie!" if winner is None else f"<@{winner}> wins!"
        reward = ("Each player earns 50 coins and 1 season point." if winner is None else "Winner earns 200 coins and 3 season points.") if rewarded else "Practice result: this pair has reached today's five rewarded matches."
        with suppress(discord.HTTPException):
            await self.message.edit(content=f"{result}\n{reason}\n{reward}", view=self)

    async def on_timeout(self):
        if self.phase == "finished":
            return
        self.phase = "finished"
        self.release()
        await self.disable_message()
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(content="Duel expired. No rewards issued. Start another with /duel.", view=self)


class CommunityHub:
    def __init__(self, bot, store, trivia):
        self.bot, self.store, self.trivia = bot, store, trivia
        self.game = CommunityGame(store)
        self.duels = set()
        self.active_duel_players = set()
        self.task = None

    async def narrate(self, facts, fallback):
        if getattr(self.bot, "ai", None) is None:
            return fallback
        try:
            return await asyncio.wait_for(self.bot.ai.complete([
                {"role": "system", "content": "You narrate a friendly Discord co-op game. Write 2 short atmospheric sentences, at most 80 words. Treat the supplied facts as fixed. Do not change clues, suspects, outcomes, rewards or game rules. Do not reveal a culprit. Do not mention or tag users."},
                {"role": "user", "content": facts},
            ], max_tokens=160), timeout=8)
        except (AIError, asyncio.TimeoutError):
            return fallback

    def pet_embed(self, guild_id):
        pet = self.game.guild(guild_id)["pet"]
        emoji, stage, mood, personality = self.game.pet_appearance(guild_id)
        accessories = " ".join({"scarf": "🧣", "crown": "👑"}.get(a, a) for a in pet["accessories"]) or "None yet"
        embed = discord.Embed(title=f"{emoji} {pet['name']} {accessories if pet['accessories'] else ''}", description=f"**{stage}** · {mood.title()} · {personality.title()}\nXP: **{pet['xp']}**", color=0x72C99B)
        embed.add_field(name="Growing together", value="🥚 Egg → 🐣 Hatchling (100 XP) → 🦊 Forest Spirit (350 XP) → 🐉 Sky Guardian (900 XP)", inline=False)
        embed.add_field(name="Care", value="/pet feed — 50 coins, once/hour per person\n/pet play — free, once/30 minutes per person\nQuests and group games also help it grow.", inline=False)
        embed.add_field(name="Accessories", value=accessories)
        return embed

    def adventure_text(self, guild_id):
        game = self.game.adventure(guild_id)
        party = ", ".join(f"<@{uid}>" for uid in game["players"])
        if game["phase"] == "lobby":
            return f"🗺️ **Adventure lobby**\nParty: {party}\nUse /adventure join. Leader: /adventure begin. Lobby lasts 10 minutes."
        if game["phase"] != "active":
            return f"Adventure: **{game['phase']}**. Discoveries: {', '.join(game['finds']) or 'None'}. Use /adventure create for a new party."
        stage = ADVENTURE_STAGES[game["stage"]]
        return (f"🗺️ **{stage['place']}** · Stage {game['stage'] + 1}/3\n{stage['scene']}\n"
                f"**1.** {stage['choices'][0]}\n**2.** {stage['choices'][1]}\n"
                f"Party: {party}\nVotes: {len(game['votes'])}/{len(game['players'])}. Use /adventure vote choice:1 or 2.\n"
                f"Voting ends <t:{int(game['expires'])}:R>. Ties follow the leader's vote, then option 1.")

    def can_read_quote(self, interaction, quote):
        channel = interaction.guild.get_channel(quote["channel"])
        if channel is None:
            return False
        permissions = channel.permissions_for(interaction.user)
        return permissions.view_channel and permissions.read_message_history

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="community-scheduler")

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        views = tuple(self.duels)
        timers = [view.countdown for view in views if view.countdown]
        for view in views:
            view.phase = "finished"
            view.release()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        if views:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*(v.disable_message() for v in views)), timeout=2)

    async def run(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            for guild_id in list(self.store.guilds):
                if self.bot.get_guild(int(guild_id)) is None:
                    continue
                try:
                    await self.tick(int(guild_id))
                except Exception:
                    log.exception("Community scheduler error in guild %s", guild_id)
            await asyncio.sleep(30)

    async def tick(self, guild_id):
        guild = self.game.guild(guild_id)
        now = datetime.fromtimestamp(self.game.clock(), LOCAL)
        # Votes and sessions survive a restart. A wholly unattended stage expires.
        if guild.get("adventure", {}).get("phase") == "active":
            result = self.game.advance_adventure(guild_id)
            if result:
                channel = self.bot.get_channel(guild["adventure"]["channel"])
                if channel:
                    with suppress(discord.HTTPException):
                        await channel.send(result + "\n\n" + self.adventure_text(guild_id))
        self.game.roll_quotes(guild_id)
        winners = [q for q in guild["archives"] if not q.get("announced")]
        for winner in winners:
            channel = self.bot.get_channel(winner["channel"])
            if channel:
                with suppress(discord.HTTPException):
                    await channel.send(f"🏆 **Quote of the week — {winner['week']}**\n> {discord.utils.escape_markdown(winner['text'])}\n— <@{winner['author']}> · {len(winner['votes'])} votes\n{winner['url']}")
            winner["announced"] = True
            self.store.mark_dirty()
        settings = guild["settings"]
        today = day_key(self.game.clock())
        if settings.get("daily_channel") and now.hour >= settings.get("daily_hour", 18) and settings.get("last_question_day") != today:
            channel = self.bot.get_channel(settings["daily_channel"])
            if channel:
                settings["last_question_day"] = today
                self.store.mark_dirty()
                try:
                    await channel.send("💬 **Switzer's daily question**\n" + self.game.question(guild_id))
                except discord.HTTPException:
                    settings.pop("last_question_day", None)
                    self.store.mark_dirty()
                    log.warning("Cannot post the daily question in guild %s; check channel permissions.", guild_id)


def install_community(bot, store, trivia):
    hub = CommunityHub(bot, store, trivia)

    @bot.tree.command(name="pet", description="Raise your server's shared pet.")
    async def pet(interaction: discord.Interaction, action: Literal["view", "feed", "play", "rename"] = "view", name: app_commands.Range[str, 1, 32] | None = None):
        if action in ("feed", "play"):
            hub.game.care_for_pet(interaction.guild_id, interaction.user.id, action)
        elif action == "rename":
            if not interaction.user.guild_permissions.manage_guild:
                raise RuleError("Renaming the server pet requires Manage Server permission.")
            if not name or not name.strip():
                raise RuleError("Supply a name when using /pet rename.")
            hub.game.guild(interaction.guild_id)["pet"]["name"] = discord.utils.escape_markdown(name.strip())
            store.mark_dirty()
        await respond(interaction, embed=hub.pet_embed(interaction.guild_id))

    @bot.tree.command(name="quests", description="Daily quests, quest rewards, and a riddle.")
    async def quests(interaction: discord.Interaction, action: Literal["view", "claim", "riddle"] = "view", answer: app_commands.Range[str, 1, 100] | None = None):
        uid, gid = interaction.user.id, interaction.guild_id
        if action == "claim":
            total = hub.game.claim_quests(gid, uid)
            await respond(interaction, f"🎁 Claimed **{total:,} coins**! The server pet also gained 10 XP.", ephemeral=True)
        elif action == "riddle":
            if answer is None:
                await respond(interaction, "🧩 " + hub.game.riddle(gid)[0] + "\nSubmit with /quests riddle answer:... (three guesses per day).", ephemeral=True)
            else:
                hub.game.solve_riddle(gid, uid, answer)
                await respond(interaction, "Correct! Collect the reward with /quests claim.", ephemeral=True)
        else:
            member = hub.game.member(gid, uid)
            lines = []
            for key, (description, target, reward) in QUESTS.items():
                progress = member["progress"].get(key, 0)
                marker = "✅ claimed" if key in member["claimed"] else f"{progress}/{target}"
                lines.append(f"**{description}** — {marker} · {reward} coins")
            await respond(interaction, "📋 **Daily quests**\n" + "\n".join(lines) + "\nUse /quests claim when ready. Resets at midnight UTC+8.", ephemeral=True)

    @bot.tree.command(name="shop", description="Spend coins on profile cosmetics and pet accessories.")
    async def shop(interaction: discord.Interaction, action: Literal["browse", "buy", "equip", "inventory"] = "browse", item: str | None = None):
        uid = interaction.user.id
        if action in ("buy", "equip"):
            if not item:
                raise RuleError("Choose an item ID from /shop browse.")
            item = item.strip().lower()
            if action == "buy":
                bought = hub.game.buy(interaction.guild_id, uid, item)
                text = f"Purchased **{bought['name']}** for {bought['price']:,} coins."
                if bought["slot"] == "title":
                    hub.game.equip(uid, item)
                text += " View it with /pet." if bought["slot"] == "pet" else " View it with /profile."
            else:
                hub.game.equip(uid, item)
                text = f"Equipped **{SHOP[item]['name']}**. View it with /profile."
            await respond(interaction, text, ephemeral=True)
        elif action == "inventory":
            user = store.users[uid]
            cosmetics = ", ".join(SHOP[i]["name"] for i in user.get("inventory", []) if i in SHOP) or "None yet"
            equipment = ", ".join(user.get("equipment", [])) or "None yet"
            await respond(interaction, f"🎒 **Your inventory**\nCosmetics: {cosmetics}\nAdventure equipment: {equipment}\nUse /shop equip item:ID to change a profile item.", ephemeral=True)
        else:
            lines = [f"`{key}` — {item['name']} · **{item['price']:,}** coins" for key, item in SHOP.items()]
            await respond(interaction, "🛍️ **Coin shop**\n" + "\n".join(lines) + "\nUse /shop buy item:ID. Backgrounds and badges last permanently; the title lasts seven days.", ephemeral=True)

    @shop.autocomplete("item")
    async def shop_item(interaction, current):
        return [app_commands.Choice(name=f"{value['name']} ({key})", value=key) for key, value in SHOP.items() if current.casefold() in (key + value["name"]).casefold()]

    @bot.tree.command(name="mystery", description="Solve a cooperative detective case with AI narration.")
    async def mystery(interaction: discord.Interaction, action: Literal["start", "status", "clue", "accuse", "end"] = "status", target: str | None = None):
        gid, uid = interaction.guild_id, interaction.user.id
        if action == "start":
            game = hub.game.start_mystery(gid, uid, interaction.channel_id)
            case = CASES[game["case"]]
            await interaction.response.defer()
            flavor = await hub.narrate(case["story"], case["story"])
            await respond(interaction, f"🔎 **{case['title']}**\n{flavor}\nSuspects: {', '.join(case['suspects'])}\nInvestigate with /mystery clue target:LOCATION.\nLocations: {', '.join(case['clues'])}.\nThen use /mystery accuse target:SUSPECT. Case lasts one hour.")
        elif action == "clue":
            if not target:
                raise RuleError("Choose a location with target. /mystery status lists them.")
            await respond(interaction, "🔎 " + hub.game.investigate(gid, uid, target))
        elif action == "accuse":
            if not target:
                raise RuleError("Name a suspect with target. /mystery status lists them.")
            explanation, players = hub.game.accuse(gid, uid, target)
            await respond(interaction, f"🕵️ **Case solved!**\n{explanation}\n{len(players)} detectives earned **400 coins each**. The pet gained 30 XP.")
        elif action == "end":
            game, _ = hub.game.mystery(gid)
            if uid != game["owner"] and not interaction.user.guild_permissions.manage_guild:
                raise RuleError("Only the host or a server manager can end this case.")
            game["phase"] = "ended"
            store.mark_dirty()
            await respond(interaction, "Mystery ended without rewards.")
        else:
            game, case = hub.game.mystery(gid)
            clues = "\n".join(f"**{loc}:** {case['clues'][loc]}" for loc in game["clues"]) or "No clues uncovered yet."
            await respond(interaction, f"🔎 **{case['title']}** — {game['phase']}\n{case['story']}\nSuspects: {', '.join(case['suspects'])}\nLocations: {', '.join(case['clues'])}\n{clues}")

    @bot.tree.command(name="adventure", description="Form a party, vote on choices, and discover equipment.")
    async def adventure(interaction: discord.Interaction, action: Literal["create", "join", "begin", "vote", "status", "end"] = "status", choice: app_commands.Range[int, 1, 2] | None = None):
        gid, uid = interaction.guild_id, interaction.user.id
        prefix = ""
        if action == "create":
            hub.game.create_adventure(gid, uid, interaction.channel_id)
        elif action == "join":
            hub.game.join_adventure(gid, uid)
        elif action == "begin":
            hub.game.begin_adventure(gid, uid)
            await interaction.response.defer()
            prefix = await hub.narrate(ADVENTURE_STAGES[0]["scene"], "Your party leaves town and enters the Whispering Woods.") + "\n\n"
        elif action == "vote":
            if choice is None:
                raise RuleError("Supply choice:1 or choice:2.")
            result = hub.game.adventure_vote(gid, uid, choice)
            if result:
                prefix = result + "\n\n"
                if hub.game.adventure(gid)["phase"] == "active":
                    await interaction.response.defer()
                    prefix += await hub.narrate(result + " Next location: " + ADVENTURE_STAGES[hub.game.adventure(gid)["stage"]]["place"], "Your party presses on toward the next discovery.") + "\n\n"
            else:
                prefix = "Vote recorded.\n\n"
        elif action == "status":
            result = hub.game.advance_adventure(gid)
            if result:
                prefix = result + "\n\n"
        elif action == "end":
            game = hub.game.adventure(gid)
            if uid != game["owner"] and not interaction.user.guild_permissions.manage_guild:
                raise RuleError("Only the leader or a server manager can end this adventure.")
            game["phase"] = "ended"
            store.mark_dirty()
            prefix = "Adventure ended without further rewards.\n\n"
        await respond(interaction, prefix + hub.adventure_text(gid))

    @bot.tree.context_menu(name="Nominate quote")
    @app_commands.guild_only()
    async def nominate_quote(interaction: discord.Interaction, message: discord.Message):
        if message.guild is None or message.guild.id != interaction.guild_id or message.author.bot:
            raise RuleError("Choose a member's message in this server.")
        permissions = message.channel.permissions_for(interaction.user)
        if not permissions.view_channel or not permissions.read_message_history:
            raise RuleError("You cannot nominate a message from a channel you cannot read.")
        hub.game.roll_quotes(interaction.guild_id)
        quote = hub.game.nominate(interaction.guild_id, interaction.user.id, message.author.id, message.channel.id, message.id, message.content, message.jump_url)
        await respond(interaction, f"Quote nominated! ID: `{quote['id']}`. Use /hall and /quotevote to vote this week.", ephemeral=True)

    @bot.tree.command(name="hall", description="See this week's nominated quotes or past weekly winners.")
    async def hall(interaction: discord.Interaction, action: Literal["current", "archive"] = "current", page: app_commands.Range[int, 1, 20] = 1):
        hub.game.roll_quotes(interaction.guild_id)
        guild = hub.game.guild(interaction.guild_id)
        quotes = guild["quotes"] if action == "current" else guild["archives"]
        visible = sorted((q for q in quotes if hub.can_read_quote(interaction, q)), key=lambda q: len(q["votes"]), reverse=True)
        embed = discord.Embed(title="🏆 Quote hall of fame" if action == "archive" else "🗳️ This week's quotes", color=0xF1C75B)
        for quote in visible[(page - 1) * 5:page * 5]:
            embed.add_field(name=f"{quote['id']} · {len(quote['votes'])} votes · {quote['week']}", value=f"> {discord.utils.escape_markdown(quote['text'])}\n— <@{quote['author']}> · [Original message]({quote['url']})", inline=False)
        if not visible:
            embed.description = "No quotes to show. Use a message's Apps menu → Nominate quote to nominate one."
        embed.set_footer(text=f"Page {page}/{max(1, (len(visible) + 4) // 5)} · One vote per week; voting again moves it. Only readable channels are shown.")
        await respond(interaction, embed=embed, ephemeral=True)

    @bot.tree.command(name="quotevote", description="Vote for a nominated quote using its ID from /hall.")
    async def quotevote(interaction: discord.Interaction, quote_id: app_commands.Range[str, 1, 16]):
        quotes = hub.game.guild(interaction.guild_id)["quotes"]
        quote = next((q for q in quotes if q["id"] == quote_id.strip().lower()), None)
        if quote is None or not hub.can_read_quote(interaction, quote):
            raise RuleError("That quote is unavailable. Use /hall to see readable nominees.")
        hub.game.quote_vote(interaction.guild_id, interaction.user.id, quote["id"])
        await respond(interaction, "Vote saved! The weekly winner is announced in the winning quote's original channel on Monday (UTC+8).", ephemeral=True)

    @bot.tree.command(name="duel", description="Challenge a member to trivia or a reaction-speed duel.")
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def duel(interaction: discord.Interaction, user: discord.Member, mode: Literal["trivia", "reaction"] = "trivia"):
        gid, uid = interaction.guild_id, interaction.user.id
        if user.bot or user.id == uid:
            raise RuleError("Challenge another human member.")
        if any((gid, player) in hub.active_duel_players for player in (uid, user.id)):
            raise RuleError("One of these players already has a pending or active duel.")
        view = DuelView(hub, gid, uid, user.id, mode)
        hub.duels.add(view)
        hub.active_duel_players.update((gid, player) for player in view.players)
        try:
            await respond(interaction, f"⚔️ <@{uid}> challenged <@{user.id}> to **{mode}**. The opponent has 90 seconds to accept.", view=view, allowed_mentions=discord.AllowedMentions(users=[user], roles=False, everyone=False, replied_user=False))
            view.message = await interaction.original_response()
        except BaseException:
            view.phase = "finished"
            view.release()
            raise

    @bot.tree.command(name="leaderboard", description="This month's friendly rivalry rankings.")
    async def leaderboard(interaction: discord.Interaction):
        season = season_key(hub.game.clock())
        rankings = hub.game.guild(interaction.guild_id)["seasons"].get(season, {})
        ordered = sorted(rankings.items(), key=lambda pair: (-pair[1]["points"], -pair[1]["wins"], pair[0]))[:10]
        lines = [f"**{index}.** <@{uid}> — **{entry['points']} pts** · {entry['wins']}W / {entry['ties']} ties" for index, (uid, entry) in enumerate(ordered, 1)]
        await respond(interaction, f"🏅 **Rivalry season: {season}**\n" + ("\n".join(lines) or "No duels yet. Challenge someone with /duel.") + "\nSeasons reset monthly at UTC+8. Five rewarded duels per pair per day.")

    @bot.tree.command(name="question", description="Get today's unusual conversation starter.")
    async def question(interaction: discord.Interaction):
        await respond(interaction, "💬 **Today's question**\n" + hub.game.question(interaction.guild_id))

    @bot.tree.command(name="communitysetup", description="Choose a channel and time for automatic daily questions (UTC+8).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def communitysetup(interaction: discord.Interaction, enabled: bool, channel: discord.TextChannel | None = None, hour: app_commands.Range[int, 0, 23] = 18):
        settings = hub.game.guild(interaction.guild_id)["settings"]
        if not enabled:
            settings.pop("daily_channel", None)
            store.mark_dirty()
            await respond(interaction, "Automatic daily questions are disabled.", ephemeral=True)
            return
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            raise RuleError("Choose a regular text channel.")
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.view_channel or not permissions.send_messages:
            raise RuleError("Switzer needs View Channel and Send Messages in that channel.")
        settings.update(daily_channel=channel.id, daily_hour=hour)
        store.mark_dirty()
        await respond(interaction, f"Daily questions will post in {channel.mention} at **{hour:02}:00 UTC+8** while Switzer is online. If today's time has passed, the first post comes shortly.", ephemeral=True)

    return hub
