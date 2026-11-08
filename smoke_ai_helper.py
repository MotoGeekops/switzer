"""Opt-in live AI checks. Captures Discord output locally; never posts to a server."""

import asyncio
import base64
from io import BytesIO
from pathlib import Path
import tempfile
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from dotenv import dotenv_values
from PIL import Image

from ai_helper import AIHelperHub, build_multimodal_content
from chat_client import OpenRouterChat
from storage import StateStore
from switzer_ui import plain_text


async def main():
    config = dotenv_values(Path(__file__).with_name(".env"))
    async with aiohttp.ClientSession() as session:
        ai = OpenRouterChat(session, config.get("OPENROUTER_API_KEY"), config.get("GEMINI_API_KEY"))
        channel = SimpleNamespace(id=100, guild=SimpleNamespace(id=1), send=AsyncMock())
        bot = SimpleNamespace(ai=ai, user=SimpleNamespace(id=999), get_channel=lambda cid: channel)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            helper = AIHelperHub(bot, StateStore(path, dict))
            message = SimpleNamespace(
                id=123, author=SimpleNamespace(id=42, bot=False), guild=SimpleNamespace(id=1),
                channel=channel, mentions=[bot.user], attachments=[], reference=None, reply=AsyncMock(),
                content="<@999> Write a 100-word essay about the American Civil War then in 5 seconds review your essay for historical accuracy and revise it naturally in 100 words",
            )
            await helper.handle_message(message)
            tasks = helper.engine.list_tasks(1)
            assert len(tasks) == 1, "Follow-up was not created"
            task = tasks[0]
            assert "American Civil War" in task["context_block"]
            assert "Scheduled Follow-Up Task" in plain_text(*channel.send.call_args.args, **channel.send.call_args.kwargs)
            print("PASS: live AI draft delivered and real 5-second follow-up persisted")
            await asyncio.sleep(max(0, task["next_run"] - time.time()) + 0.1)
            # Recreate the helper from disk to exercise restart recovery.
            helper = AIHelperHub(bot, StateStore(path, dict))
            await helper.tick(1)
            assert not helper.engine.list_tasks(1), "Follow-up failed; retained for retry"
            text = plain_text(*channel.send.call_args.args, **channel.send.call_args.kwargs)
            assert len(text) > 100 and "<@42>" in text
            print("PASS: reloaded task produced a real AI review and delivered it locally")
            print("Review excerpt:", channel.send.call_args.kwargs['embed'].description[:450])

        if ai.gemini_key:
            png = BytesIO()
            Image.new("RGB", (32, 32), "red").save(png, format="PNG")
            encoded = base64.b64encode(png.getvalue()).decode("ascii")
            image = {"inlineData": {"mimeType": "image/png", "data": encoded}}
            with patch.object(ai, "_image_part", new=AsyncMock(return_value=image)):
                answer = await ai.complete([{"role": "user", "content": build_multimodal_content(
                    "What single color is this image? Answer in one word.", ["data:image/png;base64," + encoded])}], max_tokens=60)
            assert "red" in answer.lower(), "Vision did not identify the test image"
            print("PASS: live vision identified the generated red image")
        results = await ai.search_web("American Civil War National Park Service")
        assert results, "Search returned no usable sources"
        print(f"PASS: live search returned {len(results)} usable sources")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
