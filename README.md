<div align="center">

# Switzer

### A helpful mind. A playful side. Your server's companion.

AI answers, reminders, community games, and a little personality — right inside Discord.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Discord](https://img.shields.io/badge/Discord-discord.py-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Tests](https://github.com/MotoGeekops/switzer/actions/workflows/tests.yml/badge.svg)](https://github.com/MotoGeekops/switzer/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-22C55E?style=flat-square)](LICENSE)

[Features](#features) · [Get started](#get-started) · [Commands](#commands) · [Development](#development)

</div>

---

Switzer is a self-hosted Discord bot that helps your community find answers, catch up on conversations, and have fun together. Mention it in a channel, ask about an attached screenshot, or give it a task to follow up on later. Optional games bring shared pets, trivia, adventures, and a virtual coin economy to your server.

## Features

| | What you get |
| :--- | :--- |
| **AI helper** | Conversational answers, image analysis, web search, and a message context-menu fact-check tool. |
| **Server knowledge** | Rules and FAQs, channel summaries, and automatic answers in selected channels. |
| **Reminders & tasks** | One-time reminders, recurring AI tasks, and follow-ups that persist across restarts. |
| **Creative tools** | Image generation, jokes, poems, compliments, and conversation starters. |
| **Community games** | Shared pets, quests, mysteries, party adventures, duels, leaderboards, and quote voting. |
| **Virtual economy** | Daily coins, cosmetics, profiles, slots, blackjack, baccarat, roulette, and coin flips. |
| **Desktop controls** | Start, Stop, Restart, live logs, automatic crash recovery, and a Windows keep-awake option. |

> **Games are opt-in.** The current configuration focuses on the AI helper. Set `SWITZER_GAMES_ENABLED=1` in `.env` and restart to enable the paused game, economy, and social commands.

## Get started

### 1. Install

Use **Python 3.12 or newer**. The desktop controller needs Tkinter, included with the standard Windows Python installer.

```powershell
git clone https://github.com/MotoGeekops/switzer.git
cd switzer
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

On macOS or Linux, create the environment with `python3`, run it with `.venv/bin/python`, and copy the configuration with `cp .env.example .env`.

### 2. Configure Discord

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications) and open **Bot** settings.
2. Copy the bot token into `DISCORD_TOKEN` in your local `.env`.
3. Enable **Message Content Intent** for mentions, channel context, and summaries.
4. Under **OAuth2 → URL Generator**, select `bot` and `applications.commands`. Invite the bot with **View Channels**, **Send Messages**, **Read Message History**, **Embed Links**, and **Attach Files** permissions.

Keep tokens and API keys private. `.env`, saved server data, and runtime files are excluded from Git.

### 3. Connect an AI provider

Add a [Google AI Studio](https://aistudio.google.com/) key as `GEMINI_API_KEY`, an [OpenRouter](https://openrouter.ai/) key as `OPENROUTER_API_KEY`, or both. Chat tries Gemini first when configured and can fall back to OpenRouter. Model choices live in [chat_client.py](chat_client.py).

Image generation uses Pollinations by default; the OpenRouter image option requires an OpenRouter key. Provider availability, model support, rate limits, and charges can change.

### 4. Run

Open the desktop controller, click **Start Bot**, and wait for **Online**:

```powershell
.\.venv\Scripts\python.exe bot_controller.py
```

For terminal use or a headless host:

```powershell
.\.venv\Scripts\python.exe bot.py
```

Try `/list` in your server. Switzer runs while its host and internet connection stay available. Continuous hosting requires leaving the process running or configuring a service on your host.

## Commands

| Try this | What happens |
| :--- | :--- |
| `@Switzer explain this` | Answers with channel context; reply to a message to ask about it. |
| `/ask` | Ask a question with optional web search or an image attachment. |
| Message menu → **Apps → Ask Switzer / Fact-Check** | Ask about a specific message or image. |
| `/summarize` | Get a private summary of recent messages. |
| `/task` | Add, list, run, pause, resume, or remove scheduled work. |
| `/autowatch` | Configure automatic help in a channel. |
| `/kb` | Manage rules, FAQs, and server reference facts. |
| `/image` | Generate an image from a prompt. |
| `/reset` | Clear your AI conversation history in the current server. |
| `/list` | Browse the command guide. |

Try a natural-language follow-up:

> @Switzer write a short essay about the American Civil War then in 5 minutes review your essay

Successful scheduling includes a task ID. Relative delays such as `in 10 minutes` and `every 2 hours` are supported. Clock-time requests such as `tomorrow at 9` are currently unsupported.

Members can manage their own one-time tasks. Recurring tasks, auto-watch configuration, and knowledge-base changes require **Manage Server**.

### Bring on the games

Set this in `.env` and restart:

```dotenv
SWITZER_GAMES_ENABLED=1
```

Explore `/pet`, `/quests`, `/shop`, `/mystery`, `/adventure`, `/duel`, `/hall`, `/leaderboard`, `/trivia`, and `/rps`. The economy includes `/balance`, `/daily`, `/profile`, and casino-style games using virtual coins.

## Reliability & data

- AI replies use Discord embed cards, with long answers split into pages.
- Tasks persist locally and retry failed generation or delivery with backoff. After five failures, a task pauses until resumed.
- The scheduler checks every five seconds. AI generation, networking, and Discord rate limits can add delays. A crash after Discord accepts a message but before its receipt is saved can duplicate the last chunk.
- `switzer_state.json` and `user_stats.json` store local state. Back them up before moving hosts; they are excluded from this repository.
- Relevant prompts, conversation context, and supported attachments may be sent to configured AI providers. Search and image generation use external services too. Review provider policies before using sensitive content.
- AI answers and search results can be incomplete or inaccurate. Verify consequential claims against their sources.

## Development

Run the offline suite without connecting a bot to Discord:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Optional live provider checks use your configured API keys and may consume quota. Their Discord sends are captured locally:

```powershell
.\.venv\Scripts\python.exe -u smoke_ai_helper.py
```

| File | Purpose |
| :--- | :--- |
| `bot.py` | Startup, chat, creative commands, and casino games. |
| `ai_helper.py` | Scheduling, knowledge base, auto-watch, and summaries. |
| `chat_client.py` | AI providers, fallback handling, vision, and search. |
| `community.py` / `community_game.py` | Community commands and game rules. |
| `switzer_ui.py` / `profile_card.py` | Reply cards and profile images. |
| `storage.py` | Persistent state and backups. |
| `bot_controller.py` / `bot_runtime.py` | Desktop controls and process lifecycle. |
| `command_sync.py` | Slash-command registration and duplicate cleanup. |

## Contributing

Bug reports and improvements are welcome through [issues](https://github.com/MotoGeekops/switzer/issues) and pull requests. Include reproduction steps, keep credentials and server data out of examples, and run offline tests before submitting code changes.

## License

Copyright © 2026 MotoGeekops. Released under the [MIT License](LICENSE).
