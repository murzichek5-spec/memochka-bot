"""Карточка /mcit поверх нового фирменного фиолетового билборда.

В шаблоне персонаж уже сидит отдельно справа и не залезает на щит.
При каждом рендере мы заново закрашиваем только внутреннюю область билборда
и рисуем свежую цитату, так что старая демо-надпись не мешает.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


TEMPLATE_PATH = Path(__file__).resolve().parent / "assets" / "quote_card_template.png"

PANEL_BOX = (150, 375, 930, 855)
TITLE_BOX = (235, 410, 845, 475)
TEXT_BOX = (230, 500, 840, 700)
AUTHOR_BOX = (285, 730, 795, 790)

BOARD_FILL = (22, 7, 52)
BOARD_LINE = (24, 10, 58)
TEXT_COLOR = (247, 242, 255)
AUTHOR_COLOR = (214, 164, 246)
TITLE_COLOR = (222, 173, 245)
TITLE_TEXT = "цитаты великих людей"


def _font(size: int, *, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_body(draw: ImageDraw.ImageDraw, text: str):
    text = " ".join((text or "").split()).strip()[:900]
    max_width = TEXT_BOX[2] - TEXT_BOX[0]
    max_height = TEXT_BOX[3] - TEXT_BOX[1]

    for size in range(56, 28, -2):
        font = _font(size)
        lines = _wrap(draw, text, font, max_width)
        line_h = int(size * 1.18)
        if len(lines) <= 4 and len(lines) * line_h <= max_height:
            return font, lines, line_h

    font = _font(28)
    lines = _wrap(draw, text, font, max_width)
    if len(lines) > 5:
        lines = lines[:5]
        last = lines[-1]
        while _text_width(draw, last + "…", font) > max_width and len(last) > 2:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return font, lines, 38


def _draw_smooth_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill) -> None:
    x, y = xy
    draw.text((x + 2, y + 2), text, font=font, fill=(48, 13, 73))
    draw.text((x, y), text, font=font, fill=fill)


def _paint_board(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle(PANEL_BOX, fill=BOARD_FILL)
    for y in range(PANEL_BOX[1], PANEL_BOX[3], 10):
        draw.line((PANEL_BOX[0], y, PANEL_BOX[2], y), fill=BOARD_LINE, width=1)


def make_quote_card(text: str, author: str | None = None, title: str = TITLE_TEXT) -> bytes:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Не найден шаблон карточки: {TEMPLATE_PATH}")

    with Image.open(TEMPLATE_PATH) as source:
        image = source.convert("RGB")

    draw = ImageDraw.Draw(image)
    _paint_board(draw)

    clean_title = " ".join((title or TITLE_TEXT).split()).strip() or TITLE_TEXT
    title_font = _font(28)
    title_w = _text_width(draw, clean_title, title_font)
    title_x = (TITLE_BOX[0] + TITLE_BOX[2] - title_w) / 2
    title_y = TITLE_BOX[1] + 9
    _draw_smooth_text(draw, (title_x, title_y), clean_title, title_font, TITLE_COLOR)

    body = " ".join((text or "").split()).strip() or "..."
    body_font, lines, line_h = _fit_body(draw, body)
    block_h = len(lines) * line_h
    y = TEXT_BOX[1] + max(0, (TEXT_BOX[3] - TEXT_BOX[1] - block_h) // 2)
    for line in lines:
        w = _text_width(draw, line, body_font)
        x = (TEXT_BOX[0] + TEXT_BOX[2] - w) / 2
        _draw_smooth_text(draw, (x, y), line, body_font, TEXT_COLOR)
        y += line_h

    author_text = " ".join((author or "неизвестный гений").split()).strip()[:90]
    author_font = _font(26)
    footer = f"— {author_text}"
    w = _text_width(draw, footer, author_font)
    x = (AUTHOR_BOX[0] + AUTHOR_BOX[2] - w) / 2
    y = AUTHOR_BOX[1] + 6
    _draw_smooth_text(draw, (x, y), footer, author_font, AUTHOR_COLOR)

    out = BytesIO()
    image.save(out, format="JPEG", quality=95, optimize=True)
    return out.getvalue()
