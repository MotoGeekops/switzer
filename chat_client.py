"""Shared, bounded OpenRouter requests for chat, vision, and live web search."""

import asyncio
import base64
import html
import logging
import re
from urllib.parse import quote_plus, unquote, urlparse
import aiohttp

GEMINI_MODEL = "gemini-3-flash-preview"
MODEL = "nvidia/nemotron-3.5-super-vl:free"
FALLBACK = "google/gemma-4-31b-it:free"
log = logging.getLogger(__name__)

SEARCH_KEYWORDS = re.compile(
    r"\b(news|latest|today|current|currently|right now|update|updates|price|weather|"
    r"score|scores|who won|is this true|fact check|fact-check|search|look up|lookup|"
    r"2025|2026|2027|recent|recently|happening)\b",
    re.IGNORECASE,
)


PRONOUN_ONLY_STOPWORDS = {
    "is", "are", "was", "were", "this", "that", "it", "these", "those", "recent",
    "recently", "true", "real", "new", "latest", "current", "currently", "now",
    "today", "right", "really", "actually", "still", "happening", "update", "updates",
}


def needs_web_search(text: str) -> bool:
    if not text:
        return False
    if "http://" in text or "https://" in text:
        return True
    if not SEARCH_KEYWORDS.search(text):
        return False
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", text)]
    topic_words = [w for w in words if w not in PRONOUN_ONLY_STOPWORDS and len(w) > 1]
    return len(topic_words) >= 1


def parse_ddg_html(raw_html: str, max_results: int = 4) -> list[dict]:
    results = []
    blocks = re.findall(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>)',
        raw_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for href, raw_title, snippet_a, snippet_div in blocks:
        if "uddg=" in href:
            match = re.search(r"uddg=([^&]+)", href)
            if match:
                href = unquote(match.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet_a or snippet_div)).strip()
        if title and href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break
    return results


class AIError(Exception):
    pass


class OpenRouterChat:
    def __init__(self, session, key, gemini_key=None):
        self.session, self.key = session, key
        self.gemini_key = gemini_key
        self.slots = asyncio.Semaphore(3)

    async def _image_part(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"} or parsed.port not in (None, 443):
            raise AIError("Please attach the image directly in Discord so I can read it safely.")
        async with self.session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if response.status != 200 or mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise AIError("I couldn't read that attachment. Please attach a PNG, JPEG, or WebP image.")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise AIError("That image is too large. Please use an image smaller than 8 MB.")
                chunks.append(chunk)
            if not size:
                raise AIError("The image attachment was empty. Please upload it again.")
            return {"inlineData": {"mimeType": mime, "data": base64.b64encode(b"".join(chunks)).decode("ascii")}}

    async def search_web(self, query: str, max_results: int = 4) -> list[dict]:
        """Fetch live web search snippets without requiring paid API keys."""
        if not self.session or getattr(self.session, "closed", False) or not hasattr(self.session, "get"):
            return []
        clean_query = re.sub(r"\s+", " ", query).strip()[:200]
        if not clean_query:
            return []
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(clean_query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        }
        try:
            async with self.session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10, connect=5, sock_read=8),
            ) as response:
                if response.status != 200:
                    return []
                body = await response.text(errors="replace")
                return parse_ddg_html(body, max_results=max_results)
        except Exception:
            log.debug("Web search skipped or timed out for query: %s", clean_query)
            return []

    async def _complete_gemini(self, messages, max_tokens=1500) -> str | None:
        if not self.gemini_key or not self.session or not hasattr(self.session, "post"):
            return None
        system_parts = []
        contents = []
        for msg in messages:
            role = msg.get("role", "user")
            raw_content = msg.get("content", "")
            parts = []
            if isinstance(raw_content, list):
                for item in raw_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        if item.get("text"):
                            parts.append({"text": str(item["text"])})
                    elif isinstance(item, dict) and item.get("type") == "image_url":
                        parts.append(await self._image_part(item["image_url"]["url"]))
            else:
                if raw_content:
                    parts.append({"text": str(raw_content)})
            if not parts:
                continue
            if role == "system":
                system_parts.extend(parts)
            else:
                gemini_role = "model" if role == "assistant" else "user"
                if contents and contents[-1]["role"] == gemini_role:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": gemini_role, "parts": parts})

        if not contents:
            return None

        payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        try:
            async with self.session.post(
                url,
                headers={"x-goog-api-key": self.gemini_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=25, connect=8, sock_read=20),
            ) as response:
                if response.status != 200:
                    log.warning("Google AI Studio returned HTTP %s; falling back to OpenRouter", response.status)
                    return None
                data = await response.json(content_type=None)
                candidates = data.get("candidates") or []
                parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")).strip()
                return text or None
        except Exception as err:
            log.warning("Google AI Studio request failed (%s); falling back to OpenRouter", type(err).__name__)
            return None

    async def complete(self, messages, max_tokens=1500):
        if not self.key and not self.gemini_key:
            raise AIError("AI chat is not configured. The owner needs to add GEMINI_API_KEY or OPENROUTER_API_KEY to .env.")
        if not self.session or getattr(self.session, "closed", False):
            raise AIError("The AI connection is restarting. Please try again in a moment.")
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=1)
        except asyncio.TimeoutError:
            raise AIError("Switzer is answering several people. Please try again in a few seconds.") from None
        try:
            if self.gemini_key:
                gemini_reply = await self._complete_gemini(messages, max_tokens=max_tokens)
                if gemini_reply:
                    return gemini_reply
                if not self.key:
                    raise AIError("Google AI Studio is temporarily busy. Please try again in a moment.")

            async with self.session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"models": [MODEL, FALLBACK], "messages": messages,
                      "max_tokens": max_tokens, "reasoning": {"enabled": False}},
                timeout=aiohttp.ClientTimeout(total=55, connect=10, sock_read=45),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, UnicodeDecodeError):
                    raise AIError("The AI service returned an unreadable response. Please try again.") from None
                if response.status != 200:
                    log.warning("OpenRouter returned HTTP %s", response.status)
                    if response.status == 429:
                        raise AIError("The free AI limit has been reached. Please wait before trying again.")
                    if response.status in (401, 402, 403):
                        raise AIError("OpenRouter rejected the request. The owner should check the API key, account limits, and provider settings.")
                    raise AIError("The free AI providers are temporarily unavailable. Please try again later.")
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    raise AIError("The AI service returned an incomplete answer. Please try again.") from None
                if not isinstance(content, str) or not content.strip():
                    raise AIError("The AI service returned an empty answer. Please try again.")
                return content.strip()
        except asyncio.TimeoutError:
            raise AIError("The AI service is taking too long. Please try again.") from None
        except aiohttp.ClientError:
            raise AIError("The AI service could not be reached. Please try again later.") from None
        finally:
            self.slots.release()

