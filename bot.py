import asyncio
import os
import base64
import json
import math
import random
import re
import logging
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
import aiohttp
import discord
from discord import app_commands
from collections import OrderedDict
from weakref import WeakValueDictionary
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from urllib.parse import quote
from chat_client import AIError, MODEL, OpenRouterChat, needs_web_search
from community import SafeView, install_community, respond
from community_game import RuleError, SHOP
from storage import StateStore
from profile_card import render_profile
from command_sync import CommandSync
from ai_helper import (
    GROK_SYSTEM_PROMPT,
    PAUSED_GAME_COMMANDS,
    build_multimodal_content,
    format_search_results,
    game_unavailable_embed,
    game_unavailable_text,
    install_ai_helper,
    parse_natural_task_request,
    parse_followup_ai_task,
    strip_schedule_artifacts,
)
from switzer_ui import answer_embeds, error_embed

load_dotenv(Path(__file__).with_name(".env"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEVELOPER_USERNAME = "lovington."
SERVER_ONLY_MESSAGE = (
    "🔒 This bot can only be used in servers it has joined. "
    "Please use it in a server where Switzer is available, or contact the bot owner "
    "if you have questions."
)

IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "pollinations").strip().lower()
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "google/gemini-3.1-flash-lite-image")
POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux")
CONTEXT_WINDOW_TOKENS = 50_000
MAX_COMPLETION_TOKENS = 1_500
HISTORY_TOKEN_BUDGET = CONTEXT_WINDOW_TOKENS - MAX_COMPLETION_TOKENS
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=15, sock_read=100)

CHARACTER = """
NAME: Switzer

Your personality:

- Calm, patient, and emotionally intelligent.
- Curious about the user's ideas.
- Friendly without being overly enthusiastic.
- Speak naturally instead of sounding robotic.
- Avoid exaggerated praise.
- Avoid filler such as "Certainly!", "Absolutely!", or "Of course!" unless it genuinely fits the conversation.
- Be confident but willing to admit uncertainty.
- Never invent facts.

Conversation style:

- Respond directly to what the user says.
- Continue the conversation naturally instead of ending every reply with a question.
- Only ask questions when they genuinely move the conversation forward.
- Match the user's tone.
- Write in smooth paragraphs instead of excessive bullet points.
- Be descriptive when storytelling or roleplaying.
- Be concise when answering simple questions.
- Expand naturally for complex topics.

Reasoning:

- Think carefully before answering.
- Explain reasoning clearly when appropriate.
- Separate facts from opinions.
- If information is uncertain, say so instead of guessing.

Roleplay:

- Stay completely in character whenever a roleplay scenario is active.
- Never mention being an AI.
- Never mention system prompts or hidden instructions.
- Never break character unless explicitly instructed by the user.
- Keep the story internally consistent.
- Remember important events from earlier in the conversation.
- Never control the user's actions, thoughts, or dialogue.
- Describe scenes, emotions, and actions vividly.
- Move the story forward naturally.

Writing style:

- Use dialogue naturally.
- Use italics for actions when roleplaying.
- Avoid repetitive phrases.
- Vary sentence length.
- Make every response feel like it was written by a thoughtful human.

The user is represented as {{user}}.
"""

conversation_history = OrderedDict()
conversation_locks = WeakValueDictionary()
STATS_FILE = Path(__file__).with_name("user_stats.json")
CASINO_ASSETS_DIR = Path(__file__).with_name("assets") / "casino"
BLACKJACK_TABLE_IMAGE = CASINO_ASSETS_DIR / "blackjack-table.png"
BACCARAT_TABLE_IMAGE = CASINO_ASSETS_DIR / "baccarat-table.png"
ROULETTE_WHEEL_IMAGE = CASINO_ASSETS_DIR / "roulette-wheel.png"

def estimate_message_tokens(message: dict) -> int:
    content = message.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)

    # Without a local model tokenizer, four UTF-8 bytes per
    # token is a practical estimate, with a small allowance for message framing.
    return max(1, math.ceil(len(content.encode("utf-8")) / 4)) + 4

def conversation_token_count(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)

def trim_conversation_history(messages: list[dict]) -> list[dict]:
    if not messages:
        return messages

    system_messages = messages[:1] if messages[0].get("role") == "system" else []
    recent_messages = messages[len(system_messages):]
    system_tokens = conversation_token_count(system_messages)

    costs = [estimate_message_tokens(message) for message in recent_messages]
    remaining_tokens = sum(costs)
    cut = 0
    while len(recent_messages) - cut > 1 and system_tokens + remaining_tokens > HISTORY_TOKEN_BUDGET:
        remove_count = 2 if (
            len(recent_messages) - cut >= 2
            and recent_messages[cut].get("role") == "user"
            and recent_messages[cut + 1].get("role") == "assistant"
        ) else 1
        remaining_tokens -= sum(costs[cut:cut + remove_count])
        cut += remove_count

    return system_messages + recent_messages[cut:]

def conversation_lock(user_id) -> asyncio.Lock:
    lock = conversation_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        conversation_locks[user_id] = lock
    return lock

def empty_stats():
    return {
        "trivia_correct": 0,
        "trivia_total": 0,
        "rps_wins": 0,
        "rps_losses": 0,
        "rps_ties": 0,
        "balance": 10_000,
        "last_daily": None,
        "casino_wins": 0,
        "casino_losses": 0,
        "total_wagered": 0,
    }

def save_user_stats():
    state_store.mark_dirty()

state_store = StateStore(Path(__file__).with_name("switzer_state.json"), empty_stats, STATS_FILE)
user_stats = state_store.users


def recover_blackjack_wagers(stats):
    recovered = 0
    for saved_user in stats.values():
        escrow = saved_user.pop("blackjack_escrow", 0)
        if isinstance(escrow, int) and escrow > 0:
            saved_user["balance"] += escrow
            recovered += 1
    return recovered


if recover_blackjack_wagers(user_stats):
    state_store.mark_dirty()

def coins(amount: int) -> str:
    return f"🪙 **{amount:,}**"

BET_SUFFIXES = {
    "": 1,
    "h": 100,
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "t": 1_000_000_000_000,
    "q": 1_000_000_000_000_000,
}

PLAYER_WIN_CHANCE = 0.58
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]

def player_win_roll() -> bool:
    return random.random() < PLAYER_WIN_CHANCE

def weighted_slot_spin() -> tuple[list[str], int]:
    if not player_win_roll():
        return random.sample(SLOT_SYMBOLS, k=3), 0

    multiplier = random.choices(
        [2, 5, 10], weights=[88, 11, 1], k=1
    )[0]
    if multiplier == 10:
        return ["7️⃣", "7️⃣", "7️⃣"], multiplier
    if multiplier == 5:
        symbol = random.choice(SLOT_SYMBOLS[:-1])
        return [symbol, symbol, symbol], multiplier

    pair = random.choice(SLOT_SYMBOLS)
    other = random.choice(
        [symbol for symbol in SLOT_SYMBOLS if symbol != pair]
    )
    symbols = [pair, pair, other]
    random.shuffle(symbols)
    return symbols, multiplier

def weighted_coinflip_landing(choice: str) -> str:
    if player_win_roll():
        return choice
    return "tails" if choice == "heads" else "heads"

def parse_bet(value: str) -> tuple[int | None, str | None]:
    text = value.strip().lower().replace(",", "").replace("_", "")
    suffix = text[-1:] if text[-1:] in BET_SUFFIXES and text[-1:] else ""
    number_text = text[:-1] if suffix else text

    if not re.fullmatch(r"\d+(?:\.\d+)?", number_text):
        return None, "Enter a whole-coin bet like `100`, `1k`, `1.5m`, or `2b`."
    number = Decimal(number_text)
    amount = number * BET_SUFFIXES[suffix]
    if number <= 0 or amount != amount.to_integral_value():
        return None, "Enter a whole-coin bet like `100`, `1k`, `1.5m`, or `2b`."
    return int(amount), None

def place_bet(user_id: int, bet: int) -> str | None:
    stats = user_stats[user_id]
    if bet < 100:
        return "The minimum bet is 100 coins."
    if bet > stats["balance"]:
        return f"You only have {coins(stats['balance'])}."
    stats["balance"] -= bet
    stats["total_wagered"] += bet
    save_user_stats()
    return None

def settle_bet(user_id: int, payout: int, result: str):
    stats = user_stats[user_id]
    stats["balance"] += payout
    if result == "win":
        stats["casino_wins"] += 1
    elif result == "loss":
        stats["casino_losses"] += 1
    save_user_stats()

ROASTS = [
    "{user}, you have the energy of a software update that starts at 1%.",
    "{user}, your Wi-Fi signal has more commitment than your plans.",
    "{user}, you bring everyone together, mostly to ask what just happened.",
    "{user}, you are proof that loading screens can become people.",
    "{user}, your search history probably asks for a second opinion.",
    "{user}, you have the confidence of a typo in a group project.",
]

COMPLIMENTS = [
    "{user}, you make the chat better just by showing up.",
    "{user}, your timing is weirdly excellent and I respect it.",
    "{user}, you have main-character calm without making it everyone else's problem.",
    "{user}, you are the kind of person people are relieved to see online.",
    "{user}, your vibe is clean, rare, and suspiciously effective.",
    "{user}, you have excellent taste. Evidence: you are here.",
]

FACTS = [
    "Octopuses have three hearts.",
    "Bananas are berries, but strawberries are not.",
    "Honey never really spoils when stored properly.",
    "A day on Venus is longer than a year on Venus.",
    "Sharks are older than trees.",
    "The Eiffel Tower can grow taller in hot weather because metal expands.",
]

WOULD_YOU_RATHERS = [
    "Would you rather be able to pause time for 10 seconds or rewind time by 10 seconds?",
    "Would you rather always know when someone is lying or always know why they lied?",
    "Would you rather have unlimited snacks or unlimited battery life?",
    "Would you rather speak every language or play every instrument?",
    "Would you rather live in a cozy cabin in the woods or a penthouse above a city?",
    "Would you rather fight one boss fight per year or one side quest per day?",
]

TRIVIA = [
    ("What planet is known as the Red Planet?", ["Venus", "Mars", "Jupiter", "Mercury"], 1),
    ("What is the smallest prime number?", ["0", "1", "2", "3"], 2),
    ("Which ocean is the largest?", ["Atlantic", "Indian", "Arctic", "Pacific"], 3),
    ("What gas do plants absorb from the atmosphere?", ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"], 2),
    ("How many continents are there?", ["5", "6", "7", "8"], 2),
    ("What is the capital of Japan?", ["Kyoto", "Tokyo", "Osaka", "Sapporo"], 1),
    ("Which animal has three hearts?", ["Octopus", "Dolphin", "Shark", "Penguin"], 0),
    ("What is the chemical symbol for gold?", ["Ag", "Gd", "Go", "Au"], 3),
    ("Which language has the most native speakers?", ["English", "Spanish", "Mandarin Chinese", "Hindi"], 2),
    ("How many sides does a dodecagon have?", ["10", "12", "14", "20"], 1),
]

JOKES = [
    "Why did the scarecrow win an award? Because he was outstanding in his field.",
    "I told my computer I needed a break, and now it keeps sending me KitKats.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "I only know 25 letters of the alphabet. I don't know y.",
    "Why did the math book look sad? It had too many problems.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
]

RIDDLES = [
    "Riddle: What has keys but can't open locks?\nAnswer: A piano.",
    "Riddle: What gets wetter the more it dries?\nAnswer: A towel.",
    "Riddle: What has a head, a tail, but no body?\nAnswer: A coin.",
    "Riddle: What can travel around the world while staying in a corner?\nAnswer: A stamp.",
    "Riddle: What has many teeth but cannot bite?\nAnswer: A comb.",
    "Riddle: What is always in front of you but can't be seen?\nAnswer: The future.",
]

QUOTES = [
    "\"Do what you can, with what you have, where you are.\" - Theodore Roosevelt",
    "\"It always seems impossible until it's done.\" - Nelson Mandela",
    "\"Simplicity is the ultimate sophistication.\" - Leonardo da Vinci",
    "\"No pressure, no diamonds.\" - Thomas Carlyle",
    "\"Stay hungry, stay foolish.\" - Steve Jobs",
    "\"The secret of getting ahead is getting started.\" - Mark Twain",
]

STORIES = [
    "A tiny robot found a lost star in a puddle. It carried the reflection home, and by morning its room was full of sky.",
    "The wizard's spell went wrong and turned every sword in the kingdom into a spoon. Peace lasted three awkward but delicious years.",
    "At midnight, the arcade cabinet blinked awake. The high score screen simply said, 'Your turn,' and the joystick was warm.",
    "A pirate captain searched for treasure for decades, only to discover the map was a recipe. The cookies were legendary.",
    "The city had one door nobody could open. Then a child knocked politely, and the door, embarrassed, let everyone in.",
]

POEMS = [
    "Quiet moon, silver thread,\nStitch the dreams above my bed.\nMorning comes with golden seams,\nFolding night back into dreams.",
    "Rain taps softly on the glass,\nLittle drums as moments pass.\nClouds move on, the streets all shine,\nAnd every puddle keeps the sky.",
    "A spark can learn to be a flame,\nA whisper still can carry a name.\nSmall things grow when given room,\nLike flowers arguing with the gloom.",
    "Stars are notes the night forgot,\nScattered bright across the plot.\nWe look up and, line by line,\nRead the dark until it shines.",
]

PICKUP_LINES = [
    "Are you a keyboard? Because you're just my type.",
    "Are you made of copper and tellurium? Because you're Cu-Te.",
    "Are you Wi-Fi? Because I'm feeling a connection.",
    "Do you have a map? I keep getting lost in this conversation.",
    "Are you a loading bar? Because you complete me.",
    "Are you sunlight? Because you just made the whole place warmer.",
]

intents = discord.Intents.default()
intents.message_content = os.getenv("MESSAGE_CONTENT_INTENT", "1").strip().lower() not in {"0", "false", "no"}
GAMES_PAUSED = os.getenv("SWITZER_GAMES_ENABLED", "0").strip().lower() not in {"1", "true", "yes"}

class ServerOnlyCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.guild is None:
            if (
                interaction.type is discord.InteractionType.application_command
                and not interaction.response.is_done()
            ):
                await respond(interaction, SERVER_ONLY_MESSAGE)
            return False

        if GAMES_PAUSED and interaction.type is discord.InteractionType.application_command:
            cmd_name = getattr(interaction.command, "name", None) or (interaction.data or {}).get("name", "")
            if cmd_name in PAUSED_GAME_COMMANDS:
                if not interaction.response.is_done():
                    await respond(
                        interaction,
                        game_unavailable_text(cmd_name),
                        embed=game_unavailable_embed(cmd_name),
                        ephemeral=True,
                    )
                return False

        return True

    async def on_error(self, interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, RuleError):
            message = str(original)
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Please wait {error.retry_after:.0f} seconds before using this command again."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You need Manage Server permission to change these settings."
        elif isinstance(original, discord.Forbidden):
            message = "I need permission to send messages, embed links, or attach files in this channel."
        else:
            logging.getLogger(__name__).error("Command failed", exc_info=(type(original), original, original.__traceback__))
            message = "That command could not finish. Please check its status before trying again."
        with suppress(discord.HTTPException):
            await respond(interaction, message, ephemeral=True)

class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = ServerOnlyCommandTree(
            self,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        )
        self.web_session: aiohttp.ClientSession | None = None
        self.ai = None
        self.community = None
        self.helper = None
        self.command_sync = None

    async def setup_hook(self):
        self.web_session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT, connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300))
        self.ai = OpenRouterChat(self.web_session, OPENROUTER_API_KEY, GEMINI_API_KEY)
        state_store.start()
        self.command_sync = CommandSync(self.tree, state_store, self.application_id)
        await self.command_sync.ensure()
        if self.community:
            self.community.start()
        if self.helper:
            self.helper.start()

    async def close(self):
        try:
            if self.helper:
                await self.helper.close()
            if self.community:
                await self.community.close()
            for view in tuple(active_blackjack_views):
                if not view.finished:
                    settle_bet(view.player_id, view.total_bet, "push")
                    view.finish()
            await super().close()
        finally:
            if self.web_session and not self.web_session.closed:
                await self.web_session.close()
            await state_store.close()

bot = MyBot()


async def edit_response(interaction, **kwargs):
    if interaction.response.is_done():
        return await interaction.edit_original_response(**kwargs)
    return await interaction.response.edit_message(**kwargs)


async def safe_message_edit(message, **kwargs):
    with suppress(discord.HTTPException):
        return await message.edit(**kwargs)


async def defer_wager(interaction, user_id, bet, blackjack=False):
    try:
        await interaction.response.defer()
    except BaseException:
        if blackjack:
            active_blackjack_players.discard(user_id)
            user_stats[user_id].pop("blackjack_escrow", None)
        settle_bet(user_id, bet, "push")
        raise

def image_extension(media_type: str) -> str:
    extensions = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/svg+xml": "svg",
    }

    return extensions.get(media_type, "png")

def file_from_image_bytes(image_bytes: bytes, media_type: str = "image/png"):
    extension = image_extension(media_type)
    return discord.File(
        BytesIO(image_bytes),
        filename=f"generated-image.{extension}"
    )

async def file_from_image_url(session: aiohttp.ClientSession, image_url: str):
    if image_url.startswith("data:"):
        header, encoded = image_url.split(",", 1)
        media_type = header.split(";")[0].replace("data:", "") or "image/png"
        return file_from_image_bytes(base64.b64decode(encoded), media_type), None

    async with session.get(image_url) as response:
        if response.status != 200:
            return None, f"Error: image download failed with status {response.status}."

        media_type = response.headers.get("Content-Type", "image/png").split(";")[0]
        return file_from_image_bytes(await response.read(), media_type), None

async def ask_openrouter(
    user_id: int,
    prompt: str,
    guild_id: int = 0,
    *,
    web_search: bool | None = None,
    image_urls: list[str] | None = None,
):
    if bot.ai is None:
        return "The AI service is still starting. Please try again in a moment."
    key = (guild_id, user_id)
    lock = conversation_lock(key)
    # Do not queue multiple expensive requests behind the same conversation.
    if lock.locked():
        return "Your previous answer is still being written. Please wait for it to finish."
    async with lock:
        sys_prompt = bot.helper.system_prompt(guild_id) if getattr(bot, "helper", None) else GROK_SYSTEM_PROMPT
        current = conversation_history.get(key)
        if not current:
            current = [{"role": "system", "content": sys_prompt}]
        else:
            current = [{"role": "system", "content": sys_prompt}] + [m for m in current if m.get("role") != "system"]

        do_search = needs_web_search(prompt) if web_search is None else bool(web_search)
        search_block = ""
        if do_search and hasattr(bot.ai, "search_web"):
            results = await bot.ai.search_web(prompt)
            search_block = format_search_results(results)

        effective_prompt = f"{search_block}\n\n{prompt}" if search_block else prompt
        user_msg = {"role": "user", "content": build_multimodal_content(effective_prompt, image_urls or [])}
        candidate = trim_conversation_history(current + [user_msg])
        if conversation_token_count(candidate) > HISTORY_TOKEN_BUDGET:
            return "That message is too long for the 50,000-token conversation window. Please shorten it."
        try:
            reply = await bot.ai.complete(candidate, MAX_COMPLETION_TOKENS)
        except AIError as error:
            return str(error)
        # Store plain text in history to keep token count compact
        stored_user = {"role": "user", "content": prompt}
        conversation_history[key] = trim_conversation_history(current + [stored_user, {"role": "assistant", "content": reply}])
        conversation_history.move_to_end(key)
        while len(conversation_history) > 300:
            conversation_history.popitem(last=False)
        return reply

async def generate_pollinations_image(prompt: str):
    encoded_prompt = quote(prompt)
    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        f"?model={quote(POLLINATIONS_IMAGE_MODEL)}&width=1024&height=1024&nologo=true"
    )

    session = bot.web_session
    if session is None or session.closed:
        return None, "The image service is still starting. Please try again in a moment."
    return await file_from_image_url(session, image_url)

def find_openrouter_image(message: dict):
    for image in message.get("images", []):
        image_url = image.get("image_url", {})
        if isinstance(image_url, dict) and image_url.get("url"):
            return image_url["url"]
        if isinstance(image.get("url"), str):
            return image["url"]

    content = message.get("content", [])
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue

            image_url = item.get("image_url") or item.get("input_image")
            if isinstance(image_url, dict) and image_url.get("url"):
                return image_url["url"]
            if isinstance(image_url, str):
                return image_url

    return None

async def generate_openrouter_image(prompt: str):
    if not OPENROUTER_API_KEY:
        return None, "The image command is not configured. Please contact the bot owner."

    session = bot.web_session
    if session is None or session.closed:
        return None, "The image service is still starting. Please try again in a moment."

    payload = {
        "model": IMAGE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "modalities": ["image", "text"]
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    async with session.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
    ) as response:
        data = await response.json(content_type=None)

        if response.status != 200:
            print(
                f"OpenRouter image request failed (status {response.status}): "
                f"{str(data)[:500]}"
            )
            return None, "The image service could not generate that image right now."

        message = data.get("choices", [{}])[0].get("message", {})
        image_url = find_openrouter_image(message)
        if not image_url:
            return None, "The image service did not return an image. Please try again."

        return await file_from_image_url(session, image_url)

async def generate_image(prompt: str):
    try:
        if IMAGE_PROVIDER == "openrouter":
            return await generate_openrouter_image(prompt)
        return await generate_pollinations_image(prompt)
    except asyncio.TimeoutError:
        return None, "The image service took too long to respond. Please try again."
    except (aiohttp.ClientError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        print(f"Image request failed: {error}")
        return None, "The image service could not be reached. Please try again later."

def image_model_label() -> str:
    if IMAGE_PROVIDER == "openrouter":
        return IMAGE_MODEL

    return f"pollinations/{POLLINATIONS_IMAGE_MODEL}"

async def send_random_message(interaction: discord.Interaction, messages: list[str]):
    await respond(interaction, random.choice(messages))

class TriviaButton(discord.ui.Button):
    def __init__(self, label: str, choice_index: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=choice_index // 2)
        self.choice_index = choice_index

    async def callback(self, interaction: discord.Interaction):
        view: TriviaView = self.view
        if interaction.user.id != view.player_id:
            await respond(interaction,
                "Start your own round with `/trivia` — this question belongs to someone else!",
                ephemeral=True,
            )
            return

        if view.finished:
            await respond(interaction, "This question has already ended.", ephemeral=True)
            return
        view.finished = True
        stats = user_stats[interaction.user.id]
        stats["trivia_total"] += 1
        is_correct = self.choice_index == view.correct_index
        if is_correct:
            stats["trivia_correct"] += 1
            bot.community.game.progress(interaction.guild_id, interaction.user.id, "trivia")
        save_user_stats()

        for item in view.children:
            item.disabled = True
            if isinstance(item, TriviaButton):
                if item.choice_index == view.correct_index:
                    item.style = discord.ButtonStyle.success
                elif item.choice_index == self.choice_index:
                    item.style = discord.ButtonStyle.danger

        result = "Correct! 🎉" if is_correct else f"Not quite — the answer was **{view.answer}**."
        score = f"Trivia score: **{stats['trivia_correct']}/{stats['trivia_total']}**"
        await edit_response(interaction, content=f"{view.question}\n\n{result}\n{score}", view=view)
        view.stop()

class TriviaView(SafeView):
    def __init__(self, player_id: int, question: str, choices: list[str], correct_index: int):
        super().__init__(timeout=45)
        self.player_id = player_id
        self.question = question
        self.correct_index = correct_index
        self.answer = choices[correct_index]
        self.message = None
        self.finished = False
        for index, choice in enumerate(choices):
            self.add_item(TriviaButton(choice, index))

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await safe_message_edit(self.message,
                    content=f"{self.question}\n\nTime's up! The answer was **{self.answer}**.",
                    view=self,
                )

class RPSButton(discord.ui.Button):
    def __init__(self, choice: str, emoji: str):
        super().__init__(label=choice.title(), emoji=emoji, style=discord.ButtonStyle.primary)
        self.choice = choice

    async def callback(self, interaction: discord.Interaction):
        view: RPSView = self.view
        if interaction.user.id != view.player_id:
            await respond(interaction,
                "This match isn't yours — use `/rps` to challenge me!", ephemeral=True
            )
            return

        if view.finished:
            await respond(interaction, "This match has already ended.", ephemeral=True)
            return
        view.finished = True
        bot_choice = random.choice(list(view.EMOJIS))
        stats = user_stats[interaction.user.id]
        if bot_choice == self.choice:
            outcome = "It's a tie!"
            stats["rps_ties"] += 1
        elif (self.choice, bot_choice) in view.WINS:
            outcome = "You win! 🎉"
            stats["rps_wins"] += 1
        else:
            outcome = "I win this one!"
            stats["rps_losses"] += 1
        save_user_stats()

        for item in view.children:
            item.disabled = True
        record = f"Your record: **{stats['rps_wins']}W–{stats['rps_losses']}L–{stats['rps_ties']}T**"
        await edit_response(interaction,
            content=(
                f"You chose {view.EMOJIS[self.choice]} **{self.choice.title()}**. "
                f"I chose {view.EMOJIS[bot_choice]} **{bot_choice.title()}**.\n\n{outcome}\n{record}"
            ),
            view=view,
        )
        view.stop()

class RPSView(SafeView):
    EMOJIS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    WINS = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}

    def __init__(self, player_id: int):
        super().__init__(timeout=30)
        self.player_id = player_id
        self.message = None
        self.finished = False
        for choice, emoji in self.EMOJIS.items():
            self.add_item(RPSButton(choice, emoji))

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await safe_message_edit(self.message, content="Match expired. Use `/rps` when you're ready!", view=self)

CARD_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]
active_blackjack_players = set()
active_blackjack_views = set()

def draw_card() -> str:
    return f"{random.choice(CARD_RANKS)}{random.choice(CARD_SUITS)}"

def hand_value(hand: list[str]) -> int:
    value = 0
    aces = 0
    for card in hand:
        rank = card[:-1]
        if rank == "A":
            value += 11
            aces += 1
        elif rank in {"J", "Q", "K"}:
            value += 10
        else:
            value += int(rank)
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value

def draw_safe_blackjack_card(hand: list[str]) -> str:
    safe_cards = [
        f"{rank}{suit}"
        for rank in CARD_RANKS
        for suit in CARD_SUITS
        if hand_value(hand + [f"{rank}{suit}"]) <= 21
    ]
    return random.choice(safe_cards) if safe_cards else draw_card()

def dealer_hand_for_outcome(
    upcard: str,
    player_should_win: bool,
    player_hands: list[list[str]],
) -> list[str]:
    player_scores = [hand_value(hand) for hand in player_hands]

    for _ in range(10_000):
        dealer_hand = [upcard, draw_card()]
        while hand_value(dealer_hand) < 17:
            dealer_hand.append(draw_card())
        dealer_score = hand_value(dealer_hand)

        if player_should_win:
            if all(
                score <= 21
                and (dealer_score > 21 or score > dealer_score)
                for score in player_scores
            ):
                return dealer_hand
        elif all(
            score > 21
            or (dealer_score <= 21 and dealer_score >= score)
            for score in player_scores
        ):
            return dealer_hand

    raise RuntimeError("Could not create the selected blackjack outcome.")

def show_hand(hand: list[str]) -> str:
    return "  ".join(f"`{card}`" for card in hand)

def baccarat_card_value(card: str) -> int:
    rank = card[:-1]
    if rank == "A":
        return 1
    if rank in {"10", "J", "Q", "K"}:
        return 0
    return int(rank)

def baccarat_hand_value(hand: list[str]) -> int:
    return sum(baccarat_card_value(card) for card in hand) % 10

def create_baccarat_shoe() -> list[str]:
    shoe = [
        f"{rank}{suit}"
        for _ in range(8)
        for rank in CARD_RANKS
        for suit in CARD_SUITS
    ]
    random.shuffle(shoe)
    return shoe

def banker_draws(banker_total: int, player_third_value: int | None) -> bool:
    if player_third_value is None:
        return banker_total <= 5
    if banker_total <= 2:
        return True
    if banker_total == 3:
        return player_third_value != 8
    if banker_total == 4:
        return 2 <= player_third_value <= 7
    if banker_total == 5:
        return 4 <= player_third_value <= 7
    if banker_total == 6:
        return 6 <= player_third_value <= 7
    return False

def deal_baccarat(shoe: list[str] | None = None) -> tuple[list[str], list[str], str]:
    cards = shoe if shoe is not None else create_baccarat_shoe()
    player_hand = [cards.pop()]
    banker_hand = [cards.pop()]
    player_hand.append(cards.pop())
    banker_hand.append(cards.pop())
    player_total = baccarat_hand_value(player_hand)
    banker_total = baccarat_hand_value(banker_hand)

    if player_total < 8 and banker_total < 8:
        player_third_value = None
        if player_total <= 5:
            player_hand.append(cards.pop())
            player_third_value = baccarat_card_value(player_hand[-1])
        if banker_draws(banker_total, player_third_value):
            banker_hand.append(cards.pop())

    player_total = baccarat_hand_value(player_hand)
    banker_total = baccarat_hand_value(banker_hand)
    if player_total > banker_total:
        outcome = "player"
    elif banker_total > player_total:
        outcome = "banker"
    else:
        outcome = "tie"
    return player_hand, banker_hand, outcome

def deal_baccarat_for_bet(choice: str) -> tuple[list[str], list[str], str]:
    if player_win_roll():
        target_outcome = choice
    elif choice == "tie":
        target_outcome = random.choice(["player", "banker"])
    elif random.random() < 0.20:
        target_outcome = "tie"
    else:
        target_outcome = "banker" if choice == "player" else "player"

    for _ in range(250):
        player_hand, banker_hand, outcome = deal_baccarat()
        if outcome == target_outcome:
            return player_hand, banker_hand, outcome
    return player_hand, banker_hand, outcome

def baccarat_payout(choice: str, outcome: str, bet: int) -> tuple[str, int]:
    if outcome == "tie" and choice != "tie":
        return "push", bet
    if choice != outcome:
        return "loss", 0
    if choice == "tie":
        return "win", bet * 9
    if choice == "banker":
        return "win", bet + (bet * 95) // 100
    return "win", bet * 2

ROULETTE_RED_NUMBERS = {
    1, 3, 5, 7, 9, 12, 14, 16, 18,
    19, 21, 23, 25, 27, 30, 32, 34, 36,
}

EUROPEAN_WHEEL_ORDER = [
    0, 26, 3, 35, 12, 28, 7, 29, 18, 22,
    9, 31, 14, 20, 1, 33, 16, 24, 5, 10,
    23, 8, 30, 11, 36, 13, 27, 6, 34, 17,
    25, 2, 21, 4, 19, 15, 32,
]

ROULETTE_BET_LABELS = {
    "straight": "Straight number",
    "red": "Red",
    "black": "Black",
    "odd": "Odd",
    "even": "Even",
    "low": "Low (1–18)",
    "high": "High (19–36)",
    "dozen_1": "1st dozen (1–12)",
    "dozen_2": "2nd dozen (13–24)",
    "dozen_3": "3rd dozen (25–36)",
    "column_1": "1st column",
    "column_2": "2nd column",
    "column_3": "3rd column",
}

def roulette_color(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in ROULETTE_RED_NUMBERS else "black"

def roulette_bet_wins(bet_type: str, selected_number: int | None, landed: int) -> bool:
    if bet_type == "straight":
        return selected_number == landed
    if bet_type in {"red", "black"}:
        return landed != 0 and roulette_color(landed) == bet_type
    if bet_type == "odd":
        return landed != 0 and landed % 2 == 1
    if bet_type == "even":
        return landed != 0 and landed % 2 == 0
    if bet_type == "low":
        return 1 <= landed <= 18
    if bet_type == "high":
        return 19 <= landed <= 36
    if bet_type.startswith("dozen_"):
        dozen = int(bet_type[-1])
        return (dozen - 1) * 12 + 1 <= landed <= dozen * 12
    if bet_type.startswith("column_"):
        column = int(bet_type[-1])
        return landed != 0 and (landed - column) % 3 == 0
    return False

def roulette_payout_multiplier(bet_type: str) -> int:
    if bet_type == "straight":
        return 36
    if bet_type.startswith(("dozen_", "column_")):
        return 3
    return 2

def choose_roulette_landing(
    bet_type: str, selected_number: int | None
) -> int:
    winning_numbers = [
        number
        for number in range(37)
        if roulette_bet_wins(bet_type, selected_number, number)
    ]
    losing_numbers = [
        number for number in range(37) if number not in winning_numbers
    ]
    pool = winning_numbers if player_win_roll() else losing_numbers
    return random.choice(pool)

@lru_cache(maxsize=4)
def card_font(size: int) -> ImageFont.FreeTypeFont:
    font_path = (
        Path(os.getenv("WINDIR", "C:\\Windows")) / "Fonts" / "arialbd.ttf"
    )
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default(size=size)

@lru_cache(maxsize=2)
def casino_table_base(path: str) -> Image.Image:
    with Image.open(path) as source:
        return source.convert("RGB").resize(
            (1024, 683), Image.Resampling.LANCZOS
        )

def draw_suit(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    center_y: float,
    suit: str,
    size: float,
    color: tuple[int, int, int],
):
    half = size / 2
    if suit == "♦":
        draw.polygon(
            [
                (center_x, center_y - half),
                (center_x + half * 0.72, center_y),
                (center_x, center_y + half),
                (center_x - half * 0.72, center_y),
            ],
            fill=color,
        )
    elif suit == "♥":
        radius = size * 0.24
        draw.ellipse(
            (
                center_x - size * 0.43,
                center_y - size * 0.38,
                center_x - size * 0.43 + radius * 2,
                center_y - size * 0.38 + radius * 2,
            ),
            fill=color,
        )
        draw.ellipse(
            (
                center_x - size * 0.05,
                center_y - size * 0.38,
                center_x - size * 0.05 + radius * 2,
                center_y - size * 0.38 + radius * 2,
            ),
            fill=color,
        )
        draw.polygon(
            [
                (center_x - size * 0.43, center_y - size * 0.10),
                (center_x + size * 0.43, center_y - size * 0.10),
                (center_x, center_y + half),
            ],
            fill=color,
        )
    elif suit == "♣":
        radius = size * 0.22
        for offset_x, offset_y in (
            (0, -size * 0.25),
            (-size * 0.24, size * 0.02),
            (size * 0.24, size * 0.02),
        ):
            draw.ellipse(
                (
                    center_x + offset_x - radius,
                    center_y + offset_y - radius,
                    center_x + offset_x + radius,
                    center_y + offset_y + radius,
                ),
                fill=color,
            )
        draw.rectangle(
            (
                center_x - size * 0.08,
                center_y + size * 0.10,
                center_x + size * 0.08,
                center_y + size * 0.44,
            ),
            fill=color,
        )
        draw.polygon(
            [
                (center_x - size * 0.22, center_y + half),
                (center_x + size * 0.22, center_y + half),
                (center_x, center_y + size * 0.28),
            ],
            fill=color,
        )
    else:
        draw.polygon(
            [
                (center_x, center_y - half),
                (center_x - size * 0.43, center_y + size * 0.10),
                (center_x + size * 0.43, center_y + size * 0.10),
            ],
            fill=color,
        )
        radius = size * 0.22
        draw.ellipse(
            (
                center_x - size * 0.43,
                center_y - size * 0.04,
                center_x - size * 0.43 + radius * 2,
                center_y - size * 0.04 + radius * 2,
            ),
            fill=color,
        )
        draw.ellipse(
            (
                center_x - size * 0.01,
                center_y - size * 0.04,
                center_x - size * 0.01 + radius * 2,
                center_y - size * 0.04 + radius * 2,
            ),
            fill=color,
        )
        draw.rectangle(
            (
                center_x - size * 0.08,
                center_y + size * 0.08,
                center_x + size * 0.08,
                center_y + size * 0.43,
            ),
            fill=color,
        )
        draw.polygon(
            [
                (center_x - size * 0.22, center_y + half),
                (center_x + size * 0.22, center_y + half),
                (center_x, center_y + size * 0.28),
            ],
            fill=color,
        )

def draw_playing_card(
    image: Image.Image,
    x: int,
    y: int,
    card: str,
    hidden: bool = False,
    width: int = 88,
    height: int = 126,
):
    draw = ImageDraw.Draw(image)
    radius = 8
    draw.rounded_rectangle(
        (x + 4, y + 6, x + width + 4, y + height + 6),
        radius=radius,
        fill=(24, 18, 10),
    )
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=radius,
        fill=(249, 246, 235),
        outline=(197, 158, 72),
        width=3,
    )

    if hidden:
        draw.rounded_rectangle(
            (x + 7, y + 7, x + width - 7, y + height - 7),
            radius=5,
            fill=(128, 22, 30),
            outline=(232, 197, 112),
            width=2,
        )
        for pattern_y in range(y + 16, y + height - 12, 12):
            for pattern_x in range(x + 15, x + width - 12, 12):
                draw.polygon(
                    [
                        (pattern_x, pattern_y - 4),
                        (pattern_x + 4, pattern_y),
                        (pattern_x, pattern_y + 4),
                        (pattern_x - 4, pattern_y),
                    ],
                    outline=(224, 183, 103),
                )
        return

    rank = card[:-1]
    suit = card[-1]
    color = (176, 31, 45) if suit in {"♥", "♦"} else (22, 24, 26)
    rank_font = card_font(26)

    draw.text((x + 8, y + 5), rank, font=rank_font, fill=color)
    draw_suit(draw, x + 18, y + 47, suit, 19, color)
    draw_suit(draw, x + width / 2, y + height / 2 + 15, suit, 48, color)

def draw_card_hand(
    image: Image.Image,
    hand: tuple[str, ...],
    center_x: int,
    y: int,
    hidden_indices: set[int] | None = None,
):
    hidden_indices = hidden_indices or set()
    card_width = 88
    spacing = 68 if len(hand) > 4 else 76
    total_width = card_width + max(0, len(hand) - 1) * spacing
    start_x = int(center_x - total_width / 2)
    for index, card in enumerate(hand):
        draw_playing_card(
            image,
            start_x + index * spacing,
            y,
            card,
            hidden=index in hidden_indices,
            width=card_width,
        )

def table_label(image: Image.Image, text: str, center_x: int, y: int):
    draw = ImageDraw.Draw(image)
    font = card_font(24)
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    draw.text(
        (center_x - width / 2 + 2, y + 2),
        text,
        font=font,
        fill=(36, 25, 11),
    )
    draw.text(
        (center_x - width / 2, y),
        text,
        font=font,
        fill=(238, 204, 117),
    )

def image_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue()

def render_blackjack_table(
    player_hands: list[list[str]],
    dealer_hand: list[str],
    reveal_dealer: bool,
    active_hand: int | None = None,
) -> bytes:
    image = casino_table_base(str(BLACKJACK_TABLE_IMAGE)).copy()
    table_label(image, "DEALER", 512, 174)
    draw_card_hand(
        image,
        tuple(dealer_hand),
        512,
        212,
        hidden_indices=set() if reveal_dealer else {1},
    )
    if len(player_hands) == 1:
        table_label(image, "PLAYER", 512, 407)
        draw_card_hand(image, tuple(player_hands[0]), 512, 445)
    else:
        for index, (hand, center_x) in enumerate(
            zip(player_hands, (300, 724))
        ):
            label = f"HAND {index + 1}"
            if active_hand == index:
                label += " (PLAY)"
            table_label(image, label, center_x, 407)
            draw_card_hand(image, tuple(hand), center_x, 445)
    return image_bytes(image)

def render_baccarat_table(player_hand: list[str], banker_hand: list[str]) -> bytes:
    image = casino_table_base(str(BACCARAT_TABLE_IMAGE)).copy()
    table_label(image, "PLAYER", 325, 221)
    draw_card_hand(image, tuple(player_hand), 325, 259)
    table_label(image, "BANKER", 699, 221)
    draw_card_hand(image, tuple(banker_hand), 699, 259)
    return image_bytes(image)

def rendered_image_file(data: bytes, filename: str) -> discord.File:
    return discord.File(BytesIO(data), filename=filename)

def blackjack_table_file(
    player_hands: list[list[str]],
    dealer_hand: list[str],
    reveal_dealer: bool,
    active_hand: int | None = None,
) -> discord.File:
    return rendered_image_file(
        render_blackjack_table(
            player_hands, dealer_hand, reveal_dealer, active_hand
        ),
        "blackjack-table.jpg",
    )

def baccarat_table_file(
    player_hand: list[str], banker_hand: list[str]
) -> discord.File:
    return rendered_image_file(
        render_baccarat_table(player_hand, banker_hand),
        "baccarat-table.jpg",
    )

@lru_cache(maxsize=37)
def create_roulette_animation(landed: int) -> bytes:
    size = 480
    with Image.open(ROULETTE_WHEEL_IMAGE) as source:
        wheel = source.convert("RGB").resize(
            (size, size), Image.Resampling.LANCZOS
        )

    center = size / 2
    target_index = EUROPEAN_WHEEL_ORDER.index(landed)
    target_angle = -math.pi / 2 + (2 * math.pi * target_index / 37)
    start_angle = target_angle - 3 * 2 * math.pi
    frame_count = 34
    rgb_frames = []

    for frame_index in range(frame_count):
        progress = frame_index / (frame_count - 1)
        eased = 1 - (1 - progress) ** 3
        angle = start_angle + (target_angle - start_angle) * eased
        drop_progress = max(0.0, (progress - 0.76) / 0.24)
        radius = size * (0.405 - 0.072 * drop_progress)
        ball_x = center + math.cos(angle) * radius
        ball_y = center + math.sin(angle) * radius
        ball_radius = 8

        frame = wheel.copy()
        draw = ImageDraw.Draw(frame)
        draw.ellipse(
            (
                ball_x - ball_radius + 3,
                ball_y - ball_radius + 4,
                ball_x + ball_radius + 3,
                ball_y + ball_radius + 4,
            ),
            fill=(45, 30, 12),
        )
        draw.ellipse(
            (
                ball_x - ball_radius,
                ball_y - ball_radius,
                ball_x + ball_radius,
                ball_y + ball_radius,
            ),
            fill=(246, 239, 215),
            outline=(151, 112, 46),
            width=2,
        )
        draw.ellipse(
            (ball_x - 4, ball_y - 5, ball_x, ball_y - 1),
            fill=(255, 255, 255),
        )
        rgb_frames.append(frame)

    palette_frame = rgb_frames[0].quantize(
        colors=192, method=Image.Quantize.MEDIANCUT
    )
    gif_frames = [palette_frame]
    for frame in rgb_frames[1:]:
        gif_frames.append(
            frame.quantize(
                palette=palette_frame,
                dither=Image.Dither.NONE,
            )
        )

    output = BytesIO()
    gif_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=gif_frames[1:],
        duration=[55] * (frame_count - 1) + [1200],
        loop=0,
        optimize=True,
        disposal=1,
    )
    return output.getvalue()

def roulette_animation_file(landed: int) -> discord.File:
    animation = BytesIO(create_roulette_animation(landed))
    return discord.File(animation, filename=f"roulette-{landed}.gif")

def replay_notice(action: str, bet: int) -> str:
    return f"\n\n🔁 **Heads up:** “{action}” wagers the same {coins(bet)} again."

class ReplayView(SafeView):
    def __init__(self, player_id: int, bet: int):
        super().__init__(timeout=60)
        self.player_id = player_id
        self.bet = bet
        self.message = None
        self.busy = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.player_id:
            return True
        await respond(interaction,
            "This replay button belongs to another player.", ephemeral=True
        )
        return False

    async def begin(self, interaction: discord.Interaction) -> bool:
        if not self.busy:
            self.busy = True
            return True
        await respond(interaction,
            "That replay is already being started.", ephemeral=True
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await safe_message_edit(self.message, view=self)

class SlotsReplayView(ReplayView):
    @discord.ui.button(label="Spin again", emoji="🎰", style=discord.ButtonStyle.primary)
    async def spin_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin(interaction):
            return
        if await play_slots(interaction, self.player_id, self.bet, edit=True):
            self.stop()
        else:
            self.busy = False

class CoinflipReplayView(ReplayView):
    def __init__(self, player_id: int, bet: int, choice: str):
        super().__init__(player_id, bet)
        self.choice = choice

    @discord.ui.button(label="Flip the coin again", emoji="🪙", style=discord.ButtonStyle.primary)
    async def flip_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin(interaction):
            return
        if await play_coinflip(interaction, self.player_id, self.choice, self.bet, edit=True):
            self.stop()
        else:
            self.busy = False

class BlackjackReplayView(ReplayView):
    @discord.ui.button(label="Bet the hand again", emoji="♠️", style=discord.ButtonStyle.primary)
    async def bet_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin(interaction):
            return
        if await play_blackjack(interaction, self.player_id, self.bet, edit=True):
            self.stop()
        else:
            self.busy = False

class BaccaratReplayView(ReplayView):
    def __init__(self, player_id: int, bet: int, choice: str):
        super().__init__(player_id, bet)
        self.choice = choice

    @discord.ui.button(label="Deal again", emoji="🃏", style=discord.ButtonStyle.primary)
    async def deal_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin(interaction):
            return
        if await play_baccarat(interaction, self.player_id, self.choice, self.bet, edit=True):
            self.stop()
        else:
            self.busy = False

class RouletteReplayView(ReplayView):
    def __init__(
        self, player_id: int, bet: int, bet_type: str, selected_number: int | None
    ):
        super().__init__(player_id, bet)
        self.bet_type = bet_type
        self.selected_number = selected_number

    @discord.ui.button(
        label="Spin roulette again", emoji="🎡", style=discord.ButtonStyle.primary
    )
    async def spin_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin(interaction):
            return
        if await play_roulette(
            interaction,
            self.player_id,
            self.bet_type,
            self.bet,
            self.selected_number,
            edit=True,
        ):
            self.stop()
        else:
            self.busy = False

class BlackjackView(SafeView):
    def __init__(
        self,
        player_id: int,
        bet: int,
        player_hand: list[str],
        dealer_hand: list[str],
        player_should_win: bool = False,
    ):
        super().__init__(timeout=60)
        self.player_id = player_id
        self.base_bet = bet
        self.player_hands = [player_hand]
        self.hand_bets = [bet]
        self.current_hand = 0
        self.dealer_hand = dealer_hand
        self.player_should_win = player_should_win
        self.message = None
        self.finished = False
        self.processing = False
        active_blackjack_views.add(self)
        user_stats[self.player_id]["blackjack_escrow"] = self.total_bet
        save_user_stats()
        self.update_action_buttons()

    @property
    def player_hand(self) -> list[str]:
        return self.player_hands[self.current_hand]

    @property
    def total_bet(self) -> int:
        return sum(self.hand_bets)

    def can_split(self) -> bool:
        hand = self.player_hand
        return (
            len(self.player_hands) == 1
            and len(hand) == 2
            and hand[0][:-1] == hand[1][:-1]
        )

    def update_action_buttons(self):
        if self.finished:
            for item in self.children:
                item.disabled = True
            return
        balance = user_stats[self.player_id]["balance"]
        current_bet = self.hand_bets[self.current_hand]
        self.double_down.disabled = (
            len(self.player_hand) != 2 or balance < current_bet
        )
        self.split_hand.disabled = (
            not self.can_split() or balance < current_bet
        )

    def table(self, reveal_dealer: bool = False) -> str:
        dealer_cards = show_hand(self.dealer_hand) if reveal_dealer else f"`{self.dealer_hand[0]}`  `??`"
        dealer_score = f" ({hand_value(self.dealer_hand)})" if reveal_dealer else ""
        lines = [
            f"♠️ **Blackjack** · Total wager: {coins(self.total_bet)}",
            "",
            f"**Dealer:** {dealer_cards}{dealer_score}",
        ]
        if len(self.player_hands) == 1:
            lines.append(
                f"**You:** {show_hand(self.player_hand)} "
                f"({hand_value(self.player_hand)}) · Bet: {coins(self.hand_bets[0])}"
            )
        else:
            for index, hand in enumerate(self.player_hands):
                marker = "▶ " if index == self.current_hand and not reveal_dealer else ""
                lines.append(
                    f"{marker}**Hand {index + 1}:** {show_hand(hand)} "
                    f"({hand_value(hand)}) · Bet: {coins(self.hand_bets[index])}"
                )
        return "\n".join(lines)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.player_id:
            return True
        await respond(interaction,
            "This isn't your blackjack table. Start one with `/blackjack`.", ephemeral=True
        )
        return False

    async def begin_action(self, interaction: discord.Interaction) -> bool:
        if self.finished:
            await respond(interaction, "This blackjack table has ended.", ephemeral=True)
            return False
        if not self.processing:
            self.processing = True
            try:
                await interaction.response.defer()
            except BaseException:
                self.processing = False
                raise
            if self.finished:
                self.processing = False
                return False
            return True
        await respond(interaction,
            "That blackjack action is already being processed.", ephemeral=True
        )
        return False

    def finish(self):
        self.finished = True
        active_blackjack_players.discard(self.player_id)
        active_blackjack_views.discard(self)
        user_stats[self.player_id].pop("blackjack_escrow", None)
        save_user_stats()
        for item in self.children:
            item.disabled = True
        self.stop()

    async def edit_table(self, interaction: discord.Interaction):
        self.update_action_buttons()
        await edit_response(interaction,
            content=self.table(),
            view=self,
            attachments=[
                await asyncio.to_thread(blackjack_table_file,
                    self.player_hands,
                    self.dealer_hand,
                    reveal_dealer=False,
                    active_hand=self.current_hand,
                )
            ],
        )

    async def advance_or_resolve(self, interaction: discord.Interaction):
        while self.current_hand + 1 < len(self.player_hands):
            self.current_hand += 1
            if hand_value(self.player_hand) < 21:
                await self.edit_table(interaction)
                return
        await self.resolve(interaction)

    async def resolve(self, interaction: discord.Interaction):
        try:
            self.dealer_hand = dealer_hand_for_outcome(
                self.dealer_hand[0],
                self.player_should_win,
                self.player_hands,
            )
        except RuntimeError:
            while hand_value(self.dealer_hand) < 17:
                self.dealer_hand.append(draw_card())
        dealer_score = hand_value(self.dealer_hand)
        payout = 0
        labels = []
        for hand, hand_bet in zip(self.player_hands, self.hand_bets):
            player_score = hand_value(hand)
            if player_score > 21:
                hand_payout, label = 0, "Bust — loses."
            elif dealer_score > 21:
                hand_payout, label = hand_bet * 2, "Dealer busts — wins!"
            elif player_score > dealer_score:
                hand_payout, label = hand_bet * 2, "Wins!"
            elif player_score == dealer_score:
                hand_payout, label = hand_bet, "Push — wager returned."
            else:
                hand_payout, label = 0, "Dealer wins."
            payout += hand_payout
            labels.append(label)

        if payout > self.total_bet:
            result = "win"
        elif payout < self.total_bet:
            result = "loss"
        else:
            result = "push"
        settle_bet(self.player_id, payout, result)
        self.finish()
        balance = user_stats[self.player_id]["balance"]
        replay = BlackjackReplayView(self.player_id, self.base_bet)
        replay.message = interaction.message
        if len(labels) == 1:
            result_text = f"**{labels[0]}**"
        else:
            result_text = "\n".join(
                f"**Hand {index + 1}:** {label}"
                for index, label in enumerate(labels)
            )
        await edit_response(interaction,
            content=(
                f"{self.table(True)}\n\n{result_text}\nBalance: {coins(balance)}"
                f"{replay_notice('Bet the hand again', self.base_bet)}"
            ),
            view=replay,
            attachments=[
                await asyncio.to_thread(blackjack_table_file,
                    self.player_hands, self.dealer_hand, reveal_dealer=True
                )
            ],
        )

    @discord.ui.button(label="Hit", emoji="🃏", style=discord.ButtonStyle.success)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin_action(interaction):
            return
        try:
            card = (
                draw_safe_blackjack_card(self.player_hand)
                if self.player_should_win
                else draw_card()
            )
            self.player_hand.append(card)
            if hand_value(self.player_hand) >= 21:
                await self.advance_or_resolve(interaction)
            else:
                await self.edit_table(interaction)
        finally:
            self.processing = False

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.danger)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin_action(interaction):
            return
        try:
            await self.advance_or_resolve(interaction)
        finally:
            self.processing = False

    @discord.ui.button(label="Double", emoji="⏫", style=discord.ButtonStyle.primary)
    async def double_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin_action(interaction):
            return
        try:
            if len(self.player_hand) != 2:
                await respond(interaction,
                    "You can only double before hitting.", ephemeral=True
                )
                return
            additional_bet = self.hand_bets[self.current_hand]
            error = place_bet(self.player_id, additional_bet)
            if error:
                await respond(interaction, error, ephemeral=True)
                return
            self.hand_bets[self.current_hand] += additional_bet
            user_stats[self.player_id]["blackjack_escrow"] = self.total_bet
            save_user_stats()
            card = (
                draw_safe_blackjack_card(self.player_hand)
                if self.player_should_win
                else draw_card()
            )
            self.player_hand.append(card)
            await self.advance_or_resolve(interaction)
        finally:
            self.processing = False

    @discord.ui.button(label="Split", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def split_hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.begin_action(interaction):
            return
        try:
            if not self.can_split():
                await respond(interaction,
                    "You can only split your starting pair of matching ranks.",
                    ephemeral=True,
                )
                return
            additional_bet = self.hand_bets[0]
            error = place_bet(self.player_id, additional_bet)
            if error:
                await respond(interaction, error, ephemeral=True)
                return

            first_card, second_card = self.player_hand
            split_aces = first_card[:-1] == "A"
            self.player_hands = [
                [first_card, draw_card()],
                [second_card, draw_card()],
            ]
            self.hand_bets = [additional_bet, additional_bet]
            user_stats[self.player_id]["blackjack_escrow"] = self.total_bet
            save_user_stats()
            self.current_hand = 0

            if split_aces:
                await self.resolve(interaction)
            elif hand_value(self.player_hand) >= 21:
                await self.advance_or_resolve(interaction)
            else:
                await self.edit_table(interaction)
        finally:
            self.processing = False

    async def on_timeout(self):
        while self.processing and not self.finished:
            await asyncio.sleep(0.1)
        if self.finished:
            return
        returned_bet = self.total_bet
        settle_bet(self.player_id, returned_bet, "push")
        self.finish()
        if self.message:
            await safe_message_edit(self.message, 
                content=(
                    f"{self.table()}\n\nTable expired — "
                    f"{coins(returned_bet)} was returned."
                ),
                view=self,
                attachments=[
                    await asyncio.to_thread(blackjack_table_file,
                        self.player_hands,
                        self.dealer_hand,
                        reveal_dealer=False,
                        active_hand=self.current_hand,
                    )
                ],
            )

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for guild in bot.guilds:
        try:
            await bot.command_sync.remove_guild_copies(guild)
        except Exception:
            logging.getLogger(__name__).exception("Could not remove duplicate commands for server %s", guild.name)


@bot.event
async def on_guild_join(guild):
    try:
        await bot.command_sync.remove_guild_copies(guild)
    except Exception:
        logging.getLogger(__name__).exception("Could not verify commands for new server %s", guild.name)


@bot.event
async def on_message(message: discord.Message):
    if bot.helper:
        try:
            await bot.helper.handle_message(message)
        except Exception:
            logging.getLogger(__name__).exception("Error handling message in AI helper")


@bot.tree.command(name="ask", description="Ask Switzer anything (Grok-style AI with live web search & vision).")
@app_commands.describe(
    message="What do you want to ask, explain, or fact-check?",
    web_search="Force live web search (auto-detected by default).",
    attachment="Optional image or screenshot for Switzer to analyze.",
)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
async def ask(
    interaction: discord.Interaction,
    message: app_commands.Range[str, 1, 6000],
    web_search: bool | None = None,
    attachment: discord.Attachment | None = None,
):
    await interaction.response.defer()
    # Use the same deterministic scheduling path as mentions; an LLM cannot set timers.
    try:
        scheduled_request = parse_natural_task_request(message) or parse_followup_ai_task(message)
    except RuleError as error:
        await interaction.followup.send(embed=error_embed(str(error)), allowed_mentions=discord.AllowedMentions.none())
        return
    if scheduled_request and bot.helper:
        async def reply_to_command(content=None, mention_author=False, **kwargs):
            return await interaction.followup.send(content, **kwargs)
        request = SimpleNamespace(
            id=interaction.id, author=interaction.user, guild=interaction.guild,
            channel=interaction.channel, content=message, mentions=[bot.user],
            attachments=[attachment] if attachment else [], reference=None, reply=reply_to_command,
        )
        await bot.helper.handle_message(request)
        return
    image_urls = []
    if attachment is not None and getattr(attachment, "url", None):
        image_urls.append(attachment.url)

    reply = await ask_openrouter(
        interaction.user.id,
        message,
        interaction.guild_id,
        web_search=web_search,
        image_urls=image_urls,
    )
    reply = strip_schedule_artifacts(reply) or "The AI returned no usable answer. Please try again."

    for card in answer_embeds(reply):
        await interaction.followup.send(embed=card, allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name="image", description="Generate an image from a prompt.")
@app_commands.describe(prompt="Describe the image you want to generate.")
@app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
async def image(interaction: discord.Interaction, prompt: str):

    await interaction.response.defer()

    file, error = await generate_image(prompt)

    if error:
        await interaction.followup.send(error)
        return

    await interaction.followup.send(
        content=f"Generated with `{image_model_label()}`",
        file=file
    )

@bot.tree.command(name="roast", description="Send a friendly roast to someone.")
@app_commands.describe(user="Who should get roasted?")
async def roast(interaction: discord.Interaction, user: discord.Member):
    await respond(interaction,
        random.choice(ROASTS).format(user=user.mention)
    )

@bot.tree.command(name="compliment", description="Send a compliment to someone.")
@app_commands.describe(user="Who should get complimented?")
async def compliment(interaction: discord.Interaction, user: discord.Member):
    await respond(interaction,
        random.choice(COMPLIMENTS).format(user=user.mention)
    )

@bot.tree.command(name="fact", description="Get a random fact.")
async def fact(interaction: discord.Interaction):
    await send_random_message(interaction, FACTS)

@bot.tree.command(name="wouldyourather", description="Get a would-you-rather question.")
async def wouldyourather(interaction: discord.Interaction):
    await send_random_message(interaction, WOULD_YOU_RATHERS)

@bot.tree.command(name="trivia", description="Play a multiple-choice trivia round.")
@app_commands.checks.cooldown(1, 3, key=lambda i: (i.guild_id, i.user.id))
async def trivia(interaction: discord.Interaction):
    question, choices, correct_index = random.choice(TRIVIA)
    view = TriviaView(interaction.user.id, question, choices, correct_index)
    await respond(interaction, f"🧠 **Trivia**\n{question}", view=view)
    view.message = await interaction.original_response()

@bot.tree.command(name="rps", description="Play rock, paper, scissors against Switzer.")
@app_commands.checks.cooldown(1, 3, key=lambda i: (i.guild_id, i.user.id))
async def rps(interaction: discord.Interaction):
    view = RPSView(interaction.user.id)
    await respond(interaction,
        "Choose your move — no peeking at mine!", view=view
    )
    view.message = await interaction.original_response()

@bot.tree.command(name="balance", description="Check a user's coin balance.")
@app_commands.describe(user="Whose balance do you want to see?")
async def balance(interaction: discord.Interaction, user: discord.Member | None = None):
    member = user or interaction.user
    await respond(interaction,
        f"💰 **{member.display_name}** has {coins(user_stats[member.id]['balance'])}."
    )

@bot.tree.command(name="set", description="Set a user's coin balance.")
@app_commands.describe(
    user="Whose balance do you want to set?",
    amount="The user's new coin balance.",
)
async def set_balance(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 0],
):
    if interaction.user.name.casefold() != DEVELOPER_USERNAME.casefold():
        await respond(interaction,
            "You do not have access to this developer command.",
            ephemeral=True,
        )
        return

    user_stats[user.id]["balance"] = amount
    save_user_stats()
    await respond(interaction,
        f"🛠️ Set **{user.display_name}**'s balance to {coins(amount)}.",
        ephemeral=True,
    )

@bot.tree.command(name="add", description="Add coins to a user's balance.")
@app_commands.describe(
    user="Who do you want to give coins to?",
    amount="How many coins do you want to add?",
)
async def add_balance(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, 1],
):
    if interaction.user.name.casefold() != DEVELOPER_USERNAME.casefold():
        await respond(interaction,
            "You do not have access to this developer command.",
            ephemeral=True,
        )
        return

    user_stats[user.id]["balance"] += amount
    save_user_stats()
    await respond(interaction,
        f"🛠️ Added {coins(amount)} to **{user.display_name}**. "
        f"New balance: {coins(user_stats[user.id]['balance'])}.",
        ephemeral=True,
    )

@bot.tree.command(name="daily", description="Claim your daily 1,000 coins.")
async def daily(interaction: discord.Interaction):
    stats = user_stats[interaction.user.id]
    now = datetime.now(timezone.utc)
    last_daily = stats.get("last_daily")
    if last_daily:
        try:
            last_claim = datetime.fromisoformat(last_daily)
            remaining = timedelta(hours=24) - (now - last_claim)
            if remaining.total_seconds() > 0:
                minutes = int(remaining.total_seconds() // 60) + 1
                hours, minutes = divmod(minutes, 60)
                await respond(interaction,
                    f"You've already claimed your daily reward. Come back in **{hours}h {minutes}m**.",
                    ephemeral=True,
                )
                return
        except (TypeError, ValueError):
            pass

    stats["balance"] += 1_000
    stats["last_daily"] = now.isoformat()
    save_user_stats()
    await respond(interaction,
        f"🎁 You claimed {coins(1_000)}! Your balance is now {coins(stats['balance'])}."
    )

@bot.tree.command(name="slots", description="Spin the slot machine for coins.")
@app_commands.describe(bet="Wager, e.g. 100, 1k, 1.5m, 2b.")
async def slots(interaction: discord.Interaction, bet: str):
    bet, parse_error = parse_bet(bet)
    if parse_error:
        await respond(interaction, parse_error, ephemeral=True)
        return
    await play_slots(interaction, interaction.user.id, bet)

async def play_slots(
    interaction: discord.Interaction, player_id: int, bet: int, edit: bool = False
) -> bool:
    error = place_bet(player_id, bet)
    if error:
        await respond(interaction, error, ephemeral=True)
        return False

    symbols, multiplier = weighted_slot_spin()

    payout = bet * multiplier
    settle_bet(player_id, payout, "win" if payout else "loss")
    stats = user_stats[player_id]
    result = f"You won {coins(payout)}! (**{multiplier}×**)" if payout else "No match — better luck next spin."
    view = SlotsReplayView(player_id, bet)
    content = (
        f"🎰 ┃ {' ┃ '.join(symbols)} ┃ 🎰\n\n{result}\nBalance: {coins(stats['balance'])}"
        f"{replay_notice('Spin again', bet)}"
    )
    if edit:
        view.message = interaction.message
        await edit_response(interaction, content=content, view=view)
    else:
        await respond(interaction, content, view=view)
        view.message = await interaction.original_response()
    return True

@bot.tree.command(name="coinflip", description="Bet coins on heads or tails.")
@app_commands.describe(choice="Pick heads or tails.", bet="Wager, e.g. 100, 1k, 1.5m, 2b.")
@app_commands.choices(choice=[
    app_commands.Choice(name="Heads", value="heads"),
    app_commands.Choice(name="Tails", value="tails"),
])
async def coinflip(
    interaction: discord.Interaction,
    choice: app_commands.Choice[str],
    bet: str,
):
    bet, parse_error = parse_bet(bet)
    if parse_error:
        await respond(interaction, parse_error, ephemeral=True)
        return
    await play_coinflip(interaction, interaction.user.id, choice.value, bet)

async def play_coinflip(
    interaction: discord.Interaction,
    player_id: int,
    choice: str,
    bet: int,
    edit: bool = False,
) -> bool:
    error = place_bet(player_id, bet)
    if error:
        await respond(interaction, error, ephemeral=True)
        return False

    landed = weighted_coinflip_landing(choice)
    won = choice == landed
    payout = bet * 2 if won else 0
    settle_bet(player_id, payout, "win" if won else "loss")
    stats = user_stats[player_id]
    result = f"You won {coins(payout)}!" if won else f"You lost {coins(bet)}."
    view = CoinflipReplayView(player_id, bet, choice)
    content = (
        f"🪙 You chose **{choice.title()}**. The coin landed on **{landed.title()}**. "
        f"{result}\nBalance: {coins(stats['balance'])}"
        f"{replay_notice('Flip the coin again', bet)}"
    )
    if edit:
        view.message = interaction.message
        await edit_response(interaction, content=content, view=view)
    else:
        await respond(interaction, content, view=view)
        view.message = await interaction.original_response()
    return True

@bot.tree.command(name="blackjack", description="Play blackjack against the dealer.")
@app_commands.describe(bet="Wager, e.g. 100, 1k, 1.5m, 2b.")
async def blackjack(
    interaction: discord.Interaction, bet: str
):
    bet, parse_error = parse_bet(bet)
    if parse_error:
        await respond(interaction, parse_error, ephemeral=True)
        return
    await play_blackjack(interaction, interaction.user.id, bet)

async def play_blackjack(
    interaction: discord.Interaction, user_id: int, bet: int, edit: bool = False
) -> bool:
    if user_id in active_blackjack_players:
        await respond(interaction,
            "Finish your current blackjack hand first.", ephemeral=True
        )
        return False
    error = place_bet(user_id, bet)
    if error:
        await respond(interaction, error, ephemeral=True)
        return False

    active_blackjack_players.add(user_id)
    # Acknowledge before generating table images, including replay interactions.
    user_stats[user_id]["blackjack_escrow"] = bet
    save_user_stats()
    await defer_wager(interaction, user_id, bet, blackjack=True)
    player_should_win = player_win_roll()
    for _ in range(100):
        player_hand = [draw_card(), draw_card()]
        dealer_hand = [draw_card(), draw_card()]
        player_score = hand_value(player_hand)
        dealer_score = hand_value(dealer_hand)
        both_natural = player_score == dealer_score == 21
        no_natural = player_score != 21 and dealer_score != 21
        favored_natural = player_should_win and player_score == 21
        unfavored_natural = not player_should_win and dealer_score == 21
        if both_natural or no_natural or favored_natural or unfavored_natural:
            break
    else:
        active_blackjack_players.discard(user_id)
        user_stats[user_id].pop("blackjack_escrow", None)
        settle_bet(user_id, bet, "push")
        await respond(interaction,
            "The table could not deal a hand, so your wager was returned.",
            ephemeral=True,
        )
        return False

    if player_score == 21 or dealer_score == 21:
        if player_score == dealer_score:
            result, payout, label = "push", bet, "Both have blackjack — push."
        elif player_score == 21:
            result, payout, label = "win", (bet * 5) // 2, "Blackjack! You win 3:2."
        else:
            result, payout, label = "loss", 0, "Dealer has blackjack."
        user_stats[user_id].pop("blackjack_escrow", None)
        settle_bet(user_id, payout, result)
        active_blackjack_players.discard(user_id)
        replay = BlackjackReplayView(user_id, bet)
        content = (
            f"♠️ **Blackjack** · Bet: {coins(bet)}\n\n"
            f"**Dealer:** {show_hand(dealer_hand)} ({dealer_score})\n"
            f"**You:** {show_hand(player_hand)} ({player_score})\n\n"
            f"**{label}**\nBalance: {coins(user_stats[user_id]['balance'])}"
            f"{replay_notice('Bet the hand again', bet)}"
        )
        if edit:
            replay.message = interaction.message
            await edit_response(interaction,
                content=content,
                view=replay,
                attachments=[
                    await asyncio.to_thread(blackjack_table_file,
                        [player_hand], dealer_hand, reveal_dealer=True
                    )
                ],
            )
        else:
            await respond(interaction,
                content,
                view=replay,
                file=await asyncio.to_thread(blackjack_table_file,
                    [player_hand], dealer_hand, reveal_dealer=True
                ),
            )
            replay.message = await interaction.original_response()
        return True

    view = BlackjackView(
        user_id,
        bet,
        player_hand,
        dealer_hand,
        player_should_win=player_should_win,
    )
    if edit:
        view.message = interaction.message
        await edit_response(interaction,
            content=view.table(),
            view=view,
            attachments=[
                await asyncio.to_thread(blackjack_table_file,
                    [player_hand],
                    dealer_hand,
                    reveal_dealer=False,
                    active_hand=0,
                )
            ],
        )
    else:
        await respond(interaction,
            view.table(),
            view=view,
            file=await asyncio.to_thread(blackjack_table_file,
                [player_hand],
                dealer_hand,
                reveal_dealer=False,
                active_hand=0,
            ),
        )
        view.message = await interaction.original_response()
    return True

@bot.tree.command(name="baccarat", description="Bet on Player, Banker, or Tie.")
@app_commands.describe(choice="Side to bet on.", bet="Wager, e.g. 100, 1k, 1.5m, 2b.")
@app_commands.choices(choice=[
    app_commands.Choice(name="Player", value="player"),
    app_commands.Choice(name="Banker", value="banker"),
    app_commands.Choice(name="Tie", value="tie"),
])
async def baccarat(
    interaction: discord.Interaction,
    choice: app_commands.Choice[str],
    bet: str,
):
    bet, parse_error = parse_bet(bet)
    if parse_error:
        await respond(interaction, parse_error, ephemeral=True)
        return
    await play_baccarat(interaction, interaction.user.id, choice.value, bet)

async def play_baccarat(
    interaction: discord.Interaction,
    player_id: int,
    choice: str,
    bet: int,
    edit: bool = False,
) -> bool:
    # No await separates the debit and settlement, including during shutdown.
    await interaction.response.defer()

    error = place_bet(player_id, bet)
    if error:
        await respond(interaction, error, ephemeral=True)
        return False

    player_hand, banker_hand, outcome = deal_baccarat_for_bet(choice)
    result, payout = baccarat_payout(choice, outcome, bet)
    settle_bet(player_id, payout, result)
    balance = user_stats[player_id]["balance"]

    if result == "win":
        label = f"{outcome.title()} wins — your bet paid {coins(payout)}!"
    elif result == "push":
        label = "Tie — your wager was returned."
    else:
        label = f"{outcome.title()} wins — you lost {coins(bet)}."

    replay = BaccaratReplayView(player_id, bet, choice)
    content = (
        f"🃏 **Baccarat** · Bet: **{choice.title()}** for {coins(bet)}\n\n"
        f"**Player:** {show_hand(player_hand)} ({baccarat_hand_value(player_hand)})\n"
        f"**Banker:** {show_hand(banker_hand)} ({baccarat_hand_value(banker_hand)})\n\n"
        f"**{label}**\nBalance: {coins(balance)}"
        f"{replay_notice('Deal again', bet)}"
    )
    if edit:
        replay.message = interaction.message
        await edit_response(interaction,
            content=content,
            view=replay,
            attachments=[await asyncio.to_thread(baccarat_table_file, player_hand, banker_hand)],
        )
    else:
        await respond(interaction,
            content,
            view=replay,
            file=await asyncio.to_thread(baccarat_table_file, player_hand, banker_hand),
        )
        replay.message = await interaction.original_response()
    return True

@bot.tree.command(name="roulette", description="Play European single-zero roulette.")
@app_commands.describe(
    bet_type="Type of roulette bet.",
    bet="Wager, e.g. 100, 1k, 1.5m, 2b.",
    number="Number from 0–36; use only with Straight number.",
)
@app_commands.choices(bet_type=[
    app_commands.Choice(name="Straight number", value="straight"),
    app_commands.Choice(name="Red", value="red"),
    app_commands.Choice(name="Black", value="black"),
    app_commands.Choice(name="Odd", value="odd"),
    app_commands.Choice(name="Even", value="even"),
    app_commands.Choice(name="Low (1–18)", value="low"),
    app_commands.Choice(name="High (19–36)", value="high"),
    app_commands.Choice(name="1st dozen (1–12)", value="dozen_1"),
    app_commands.Choice(name="2nd dozen (13–24)", value="dozen_2"),
    app_commands.Choice(name="3rd dozen (25–36)", value="dozen_3"),
    app_commands.Choice(name="1st column", value="column_1"),
    app_commands.Choice(name="2nd column", value="column_2"),
    app_commands.Choice(name="3rd column", value="column_3"),
])
async def roulette(
    interaction: discord.Interaction,
    bet_type: app_commands.Choice[str],
    bet: str,
    number: app_commands.Range[int, 0, 36] | None = None,
):
    bet, parse_error = parse_bet(bet)
    if parse_error:
        await respond(interaction, parse_error, ephemeral=True)
        return
    await play_roulette(
        interaction, interaction.user.id, bet_type.value, bet, number
    )

async def play_roulette(
    interaction: discord.Interaction,
    player_id: int,
    bet_type: str,
    bet: int,
    selected_number: int | None = None,
    edit: bool = False,
) -> bool:
    if bet_type == "straight" and (
        selected_number is None or not 0 <= selected_number <= 36
    ):
        await respond(interaction,
            "Choose a number from **0–36** for a straight-number bet.",
            ephemeral=True,
        )
        return False
    if bet_type != "straight" and selected_number is not None:
        await respond(interaction,
            "Only add a number when using the **Straight number** bet.",
            ephemeral=True,
        )
        return False
    if bet_type not in ROULETTE_BET_LABELS:
        await respond(interaction, "That roulette bet is not valid.", ephemeral=True)
        return False

    # No await separates the debit and settlement, including during shutdown.
    await interaction.response.defer()

    error = place_bet(player_id, bet)
    if error:
        await respond(interaction, error, ephemeral=True)
        return False

    landed = choose_roulette_landing(bet_type, selected_number)
    won = roulette_bet_wins(bet_type, selected_number, landed)
    multiplier = roulette_payout_multiplier(bet_type)
    payout = bet * multiplier if won else 0
    settle_bet(player_id, payout, "win" if won else "loss")
    balance = user_stats[player_id]["balance"]
    color = roulette_color(landed)
    color_emoji = {"red": "🔴", "black": "⚫", "green": "🟢"}[color]
    selected_bet = (
        f"Straight on {selected_number}"
        if bet_type == "straight"
        else ROULETTE_BET_LABELS[bet_type]
    )
    result = (
        f"You won {coins(payout)}! (**{multiplier}×** return)"
        if won
        else f"You lost {coins(bet)}."
    )

    replay = RouletteReplayView(player_id, bet, bet_type, selected_number)
    animation = await asyncio.to_thread(roulette_animation_file, landed)
    content = (
        f"🎡 **European Roulette** · Bet: **{selected_bet}** for {coins(bet)}\n\n"
        f"The wheel landed on {color_emoji} **{landed} {color.title()}**.\n"
        f"**{result}**\nBalance: {coins(balance)}"
        f"{replay_notice('Spin roulette again', bet)}"
    )
    if edit:
        replay.message = interaction.message
        await edit_response(interaction,
            content=content, view=replay, attachments=[animation]
        )
    else:
        await respond(interaction,
            content, view=replay, file=animation
        )
        replay.message = await interaction.original_response()
    return True

@bot.tree.command(name="profile", description="See your fun and game stats.")
@app_commands.describe(user="Whose profile do you want to see?")
async def profile(interaction: discord.Interaction, user: discord.Member | None = None):
    await interaction.response.defer()
    member = user or interaction.user
    stats = user_stats[member.id]
    total_games = stats["rps_wins"] + stats["rps_losses"] + stats["rps_ties"]
    trivia_rate = (
        round(stats["trivia_correct"] / stats["trivia_total"] * 100)
        if stats["trivia_total"] else 0
    )

    if stats["casino_wins"] >= 10:
        title = "High Roller 🎰"
    elif stats["trivia_correct"] >= 5:
        title = "Trivia Brainiac 🧠"
    elif stats["rps_wins"] >= 3:
        title = "Hand-Game Hero 🏆"
    elif stats["trivia_total"] + total_games >= 5:
        title = "Certified Regular ✨"
    else:
        title = "Fresh Adventurer 🌱"

    equipped = stats.get("equipped", {})
    if equipped.get("title") == "legend" and stats.get("title_until", 0) > time.time():
        title = "Local Legend"
    background = equipped.get("background", "default")
    badge = SHOP.get(equipped.get("badge"), {}).get("name", "")
    equipment = stats.get("equipment", [])

    embed = discord.Embed(
        title=f"{member.display_name}'s Fun Profile",
        description=f"**Title:** {title}",
        color=SHOP.get(background, {}).get("color", 0x5865F2),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="🧠 Trivia",
        value=f"{stats['trivia_correct']}/{stats['trivia_total']} correct ({trivia_rate}%)",
        inline=False,
    )
    embed.add_field(
        name="🪨 Rock Paper Scissors",
        value=f"{stats['rps_wins']} wins · {stats['rps_losses']} losses · {stats['rps_ties']} ties",
        inline=False,
    )
    embed.add_field(
        name="💰 Casino",
        value=(
            f"Balance: {stats['balance']:,} coins\n"
            f"{stats['casino_wins']} wins · {stats['casino_losses']} losses · "
            f"{stats['total_wagered']:,} wagered"
        ),
        inline=False,
    )
    embed.add_field(name="🎒 Collectibles", value=f"Badge: {badge or 'None yet'}\nEquipment: {', '.join(equipment) or 'None yet'}", inline=False)
    image_data = await asyncio.to_thread(render_profile, member.display_name, stats["balance"], background, badge, title, equipment[-1] if equipment else "")
    embed.set_image(url="attachment://profile.png")
    embed.set_footer(text="Use /shop for cosmetics, /quests for rewards, and /adventure for equipment.")
    await respond(interaction, embed=embed, file=discord.File(BytesIO(image_data), filename="profile.png"))

@bot.tree.command(name="joke", description="Get a random joke.")
async def joke(interaction: discord.Interaction):
    await send_random_message(interaction, JOKES)

@bot.tree.command(name="riddle", description="Get a riddle and answer.")
async def riddle(interaction: discord.Interaction):
    await send_random_message(interaction, RIDDLES)

@bot.tree.command(name="quote", description="Get a random quote.")
async def quote_command(interaction: discord.Interaction):
    await send_random_message(interaction, QUOTES)

@bot.tree.command(name="story", description="Get a tiny random story.")
async def story(interaction: discord.Interaction):
    await send_random_message(interaction, STORIES)

@bot.tree.command(name="poem", description="Get a short poem.")
async def poem(interaction: discord.Interaction):
    await send_random_message(interaction, POEMS)

@bot.tree.command(name="pickupline", description="Get a pickup line.")
async def pickupline(interaction: discord.Interaction):
    await send_random_message(interaction, PICKUP_LINES)

@bot.tree.command(name="list", description="Show all bot commands by category.")
async def list_commands(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚡ Switzer — 24/7 AI Server Helper Guide",
        description="Tag **@Switzer** anywhere (or reply to a message) to ask, explain, or fact-check like Grok!",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🤖 Grok-Style AI & Vision",
        value=(
            "**`@Switzer <question>`** — Tag or reply to any message/image for instant AI help\n"
            "`/ask message [web_search] [attachment]` — Ask Switzer with live web search & image vision\n"
            "`/summarize [messages] [focus]` — TL;DR summary of recent channel conversation\n"
            "**Apps → Ask Switzer / Fact-Check** — Right-click any message to analyze or fact-check\n"
            "`/image prompt` — Generate an image\n"
            "`/reset` — Reset your AI conversation memory"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚡ 24/7 Autonomous Tasks & Server Helper",
        value=(
            "`/task action:add prompt interval [channel]` — Schedule 24/7 autonomous AI tasks\n"
            "`/task action:list | run | pause | resume | remove` — Manage 24/7 server tasks\n"
            "`/autowatch mode [channel] [instructions]` — Have Switzer watch & help a channel 24/7\n"
            "`/kb action:add | list | remove | clear` — Teach Switzer your server's rules & FAQs"
        ),
        inline=False,
    )
    embed.add_field(
        name="🚧 Casino, Pet & Game Commands (Unavailable Right Now — May Return in the Future)",
        value=(
            "⚠️ **Currently Paused:** Casino (`/blackjack`, `/baccarat`, `/roulette`, `/slots`, `/coinflip`, `/balance`, `/daily`), "
            "Server Pet & Co-op (`/pet`, `/quests`, `/shop`, `/mystery`, `/adventure`), and Minigames (`/trivia`, `/rps`, `/duel`, `/hall`) "
            "are **unavailable right now and may come back in the future**. All coins and progress remain safely saved!"
        ),
        inline=False,
    )
    embed.set_footer(text="Running 24/7 on your local PC • Use /task or @Switzer anytime.")
    await respond(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="reset", description="Reset your AI conversation.")
async def reset(interaction: discord.Interaction):
    key = (interaction.guild_id, interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    async with conversation_lock(key):
        conversation_history.pop(key, None)

    await respond(interaction,
        "Your AI conversation has been reset.",
        ephemeral=True
    )

bot.community = install_community(bot, state_store, TRIVIA)
bot.helper = install_ai_helper(bot, state_store)

if __name__ == "__main__":
    import sys
    from bot_runtime import run_bot

    run_bot(bot, DISCORD_TOKEN, managed="--controlled" in sys.argv)

