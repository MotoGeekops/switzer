"""Small profile images; generated off the Discord event loop."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=8)
def font(size):
    for candidate in (Path("C:/Windows/Fonts/segoeui.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default(size=size)


@lru_cache(maxsize=64)
def render_profile(name, balance, background="default", badge="", title="", equipment=""):
    themes = {
        "default": ((25, 29, 48), (68, 78, 122)),
        "midnight": ((9, 13, 39), (70, 55, 125)),
        "sunset": ((80, 30, 73), (235, 133, 74)),
        "neon": ((6, 30, 49), (17, 145, 127)),
    }
    first, last = themes.get(background, themes["default"])
    image = Image.new("RGB", (900, 260))
    draw = ImageDraw.Draw(image)
    for x in range(900):
        amount = x / 899
        color = tuple(int(a + (b - a) * amount) for a, b in zip(first, last))
        draw.line((x, 0, x, 260), fill=color)
    for index in range(22):
        x, y = (index * 137 + 43) % 900, (index * 79 + 11) % 260
        draw.ellipse((x, y, x + 2, y + 2), fill=(177, 193, 215))
    draw.rounded_rectangle((25, 24, 875, 236), radius=18, fill=(18, 23, 39), outline=(116, 136, 174), width=1)
    draw.text((48, 40), str(name)[:28], font=font(33), fill="white")
    draw.text((48, 88), (title or "Switzer community member")[:46], font=font(20), fill=(180, 194, 226))
    draw.text((48, 130), f"{balance:,} coins", font=font(27), fill=(247, 210, 126))
    draw.text((48, 182), (badge or "Earn badges in the coin shop")[:48], font=font(18), fill=(135, 226, 190))
    draw.text((480, 183), equipment[:29], font=font(17), fill=(181, 202, 232))
    output = BytesIO()
    image.save(output, "PNG", optimize=False)
    return output.getvalue()
