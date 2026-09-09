"""Text/image rendering for the MXW01 (384 px wide)."""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from .protocol import PRINTER_WIDTH_PIXELS
except ImportError:  # loaded standalone (CLI use), not as the HA package
    from protocol import PRINTER_WIDTH_PIXELS

_COMPONENT_DIR = os.path.dirname(__file__)
_FONT_CANDIDATES = [
    os.path.join(_COMPONENT_DIR, "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


MIN_FONT_SIZE = 14


def _wrap(line: str, font, max_w: int, probe) -> list[str]:
    """Greedy word-wrap for a line that is too wide even at the smallest font."""
    words, out, cur = line.split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if probe.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out or [""]


def render_text(text: str, font_size: int = 56, padding: int = 12, align: str = "center") -> Image.Image:
    """Render text to a 384-px-wide label. **Never clips**: the font shrinks to
    fit the widest line (down to MIN_FONT_SIZE), and anything still too wide is
    word-wrapped. Found by the preview card 2026-09-09 — "Luna's snacks" at 56 px
    printed as "Luna's snack" and nobody could see that before paper.
    """
    lines = text.split("\n")
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    max_w = PRINTER_WIDTH_PIXELS - padding * 2
    size = font_size
    while True:
        font = _load_font(size)
        widest = max((probe.textlength(line, font=font) for line in lines), default=0)
        if widest <= max_w or size <= MIN_FONT_SIZE:
            break
        size -= 2
    if size <= MIN_FONT_SIZE:
        lines = [seg for line in lines for seg in _wrap(line, font, max_w, probe)]
    font_size = size
    boxes = [probe.textbbox((0, 0), line, font=font) for line in lines]
    heights = [b[3] - b[1] for b in boxes]
    gap = max(4, font_size // 6)
    height = sum(heights) + gap * (len(lines) - 1) + padding * 2
    img = Image.new("L", (PRINTER_WIDTH_PIXELS, height), 255)
    draw = ImageDraw.Draw(img)
    y = padding
    for line, box, lh in zip(lines, boxes, heights):
        w = box[2] - box[0]
        if align == "left":
            x = padding
        elif align == "right":
            x = PRINTER_WIDTH_PIXELS - padding - w
        else:
            x = (PRINTER_WIDTH_PIXELS - w) // 2
        draw.text((max(0, x) - box[0], y - box[1]), line, font=font, fill=0)
        y += lh + gap
    return img


def load_image(path: str) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("L", "1"):
        # Composite transparency onto white before grayscale conversion.
        if "A" in img.getbands():
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img.convert("RGBA"))
        img = img.convert("L")
    if img.width != PRINTER_WIDTH_PIXELS:
        new_h = max(1, round(img.height * PRINTER_WIDTH_PIXELS / img.width))
        img = img.resize((PRINTER_WIDTH_PIXELS, new_h), Image.LANCZOS)
    return img
