"""Clean Discord embed rendering for Switzer's AI answers, reminders, and scheduled tasks."""

import re

import discord

COLOR_AI = 0x5865F2        # Discord blurple: normal AI answers
COLOR_TASK = 0xF5A623      # amber: scheduled AI task output
COLOR_REMINDER = 0x57F287  # green: reminders and confirmations
COLOR_MUTED = 0x99AAB5     # grey: cancellations / nothing to do
COLOR_ERROR = 0xED4245     # red: problems

# Embed descriptions allow 4096 characters; leave room for re-opened code fences.
PAGE_LIMIT = 4000

_RULE_LINE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")      # ***, ---, ___ (Discord can't render these)
_STRAY_MARKER = re.compile(r"^\s*[*_]{1,2}\s*$")       # a lone * or ** left on its own line
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")


def clean_markdown(text: str) -> str:
    """Turn model-flavoured markdown into markdown that renders cleanly inside Discord embeds."""
    lines = []
    fence = None
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append(line)
            continue
        if fence is not None:
            lines.append(line)
            continue
        if _RULE_LINE.match(line) or _STRAY_MARKER.match(line):
            lines.append("")
            continue
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1).strip().strip("*").strip()
            lines.append(f"**{title}**" if title else "")
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def split_pages(text: str, limit: int = PAGE_LIMIT) -> list[str]:
    """Split text at paragraph, line, sentence, then word boundaries; never mid-word unless unavoidable."""
    if limit < 128:
        raise ValueError("Page limit must be at least 128 characters.")
    remaining = (text or "").strip()
    pages = []
    while len(remaining) > limit:
        # Reserve room to close even an unusually long fence delimiter.
        window = remaining[:limit - 80]
        cut = 0
        for separator in ("\n\n", "\n", ". ", "! ", "? ", " "):
            index = window.rfind(separator)
            if index >= limit // 2:
                cut = index + len(separator)
                break
        if cut <= 0:
            cut = len(window)
        page = remaining[:cut].rstrip()
        rest = remaining[cut:].lstrip("\n")
        fence = _open_fence(page)
        if fence:
            marker, language = fence
            page += "\n" + marker
            rest = marker + language + "\n" + rest
        pages.append(page)
        remaining = rest
    if remaining.strip() or not pages:
        last = remaining.strip()
        fence = _open_fence(last)
        if fence:
            # Repair an unclosed block from the model without exceeding the page cap.
            suffix = "\n" + fence[0]
            if len(last) + len(suffix) > limit:
                pages.extend(split_pages(last, limit=limit - len(suffix)))
                return pages
            last += suffix
        pages.append(last)
    return pages


def _open_fence(text: str):
    opened = None
    for line in text.splitlines():
        match = re.match(r"^[ \t]*(`{3,}|~{3,})([^\n]*)$", line)
        if not match:
            continue
        marker, language = match.groups()
        if len(marker) > 64:
            continue
        if opened is None:
            opened = (marker, language[:64])
        elif marker[0] == opened[0][0] and len(marker) >= len(opened[0]):
            opened = None
    return opened


def _footer(base: str, index: int, total: int) -> str:
    text = base if total == 1 else f"{base} · Page {index + 1}/{total}"
    return text[:2048]


def page_embeds(text: str, *, title: str | None, footer: str, color: int) -> list[discord.Embed]:
    """Render long text as one embed per page. Only the first page carries the title."""
    pages = split_pages(clean_markdown(text))
    embeds = []
    for index, page in enumerate(pages):
        embed = discord.Embed(description=page or "\u200b", color=color)
        if index == 0 and title:
            embed.title = title[:256]
        embed.set_footer(text=_footer(footer, index, len(pages))[:6000 - len(embed)])
        embeds.append(embed)
    return embeds


def answer_embeds(text: str, *, footer: str = "Switzer AI") -> list[discord.Embed]:
    return page_embeds(text, title=None, footer=footer, color=COLOR_AI)


def reminder_body(direct_message: str) -> str:
    """Stored reminders read 'Hey <@id>! Time to X!'; the ping goes in the message content instead."""
    return re.sub(r"^\s*Hey\s+<@!?\d+>!?\s*", "", direct_message or "").strip() or direct_message


def task_embeds(task: dict, text: str, kind: str) -> list[discord.Embed]:
    if kind == "reminder":
        return page_embeds(reminder_body(text), title="⏰ Reminder",
                           footer=f"Reminder #{task['id']}", color=COLOR_REMINDER)
    prompt = re.sub(r"\s+", " ", task.get("prompt", "")).strip()
    if len(prompt) > 90:
        prompt = prompt[:89].rstrip() + "…"
    footer = f"Task #{task['id']}" + (f" · {prompt}" if prompt else "")
    return page_embeds(text, title="⚡ Scheduled Task", footer=footer, color=COLOR_TASK)


def status_embed(title: str, description: str = "", *, color: int = COLOR_REMINDER,
                 fields: list[tuple[str, str]] | None = None, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title[:256], description=(description or "")[:4096], color=color)
    for name, value in (fields or [])[:25]:
        available = 6000 - len(embed)
        name = (name or "\u200b")[:min(256, max(0, available - 1))]
        value = (value or "\u200b")[:min(1024, max(0, available - len(name)))]
        if not name or not value:
            break
        embed.add_field(name=name, value=value, inline=True)
    if footer:
        available = 6000 - len(embed)
        if available:
            embed.set_footer(text=footer[:min(2048, available)])
    return embed


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}"[:4096], color=COLOR_ERROR)


def plain_text(content=None, *, embed=None, embeds=None, **_ignored) -> str:
    """Flatten a send()/reply() call into readable text (used by tests and logs)."""
    parts = [str(content)] if content else []
    for item in ([embed] if embed else []) + list(embeds or []):
        parts.extend(filter(None, (item.title, item.description)))
        for field in item.fields:
            parts.extend((field.name, field.value))
        if item.footer and item.footer.text:
            parts.append(item.footer.text)
    return "\n".join(parts)


def message_text(message) -> str:
    """Read both old text messages and the bot's new embed cards as context."""
    return plain_text(getattr(message, "content", ""), embeds=getattr(message, "embeds", []) or [])
