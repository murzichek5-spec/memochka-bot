"""Рендер классического демотиватора через Pillow.

AI придумывает только текст. Рамка, размеры, переносы и JPEG делаются локально —
так быстрее, дешевле и предсказуемее, чем просить модель рисовать всю картинку.
"""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def _font(size: int, serif: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        ["DejaVuSerif.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
        if serif
        else ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )

    # На разных серверах шрифты лежат в разных местах. Пробуем известные варианты,
    # а если всё по пизде — хотя бы используем встроенный шрифт Pillow.
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            pass

    return ImageFont.load_default()


def _fit_image(image: Image.Image, max_w: int, max_h: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    return copy


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    # Не даём подписи превратиться в дипломную работу: максимум четыре строки.
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
    return lines[:4]


def make_demotivator(image_bytes: bytes, title: str, subtitle: str = "") -> bytes:
    # Возвращаем байты JPEG, чтобы main.py мог сразу завернуть их в BufferedInputFile.
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    canvas_w = 1000
    image = _fit_image(image, 850, 720)

    border = 4
    photo_x = (canvas_w - image.width) // 2
    photo_y = 65

    title_font = _font(54, serif=True)
    subtitle_font = _font(26, serif=True)

    measure = Image.new("RGB", (canvas_w, 200), "black")
    measure_draw = ImageDraw.Draw(measure)

    title = " ".join(title.split())[:220]
    subtitle = " ".join(subtitle.split())[:300]

    title_lines = _wrap_text(measure_draw, title, title_font, 900)
    subtitle_lines = _wrap_text(measure_draw, subtitle, subtitle_font, 850) if subtitle else []

    title_line_h = 66
    subtitle_line_h = 34
    text_block_h = len(title_lines) * title_line_h
    if subtitle_lines:
        text_block_h += 14 + len(subtitle_lines) * subtitle_line_h

    canvas_h = photo_y + image.height + 75 + text_block_h + 65
    canvas = Image.new("RGB", (canvas_w, canvas_h), "black")
    draw = ImageDraw.Draw(canvas)

    draw.rectangle(
        (
            photo_x - 10,
            photo_y - 10,
            photo_x + image.width + 10,
            photo_y + image.height + 10,
        ),
        outline="white",
        width=border,
    )
    canvas.paste(image, (photo_x, photo_y))

    y = photo_y + image.height + 55

    for line in title_lines:
        box = draw.textbbox((0, 0), line, font=title_font)
        w = box[2] - box[0]
        draw.text(((canvas_w - w) / 2, y), line, fill="white", font=title_font)
        y += title_line_h

    if subtitle_lines:
        y += 8
        for line in subtitle_lines:
            box = draw.textbbox((0, 0), line, font=subtitle_font)
            w = box[2] - box[0]
            draw.text(
                ((canvas_w - w) / 2, y),
                line,
                fill=(215, 215, 215),
                font=subtitle_font,
            )
            y += subtitle_line_h

    output = BytesIO()
    canvas.save(output, format="JPEG", quality=94, optimize=True)
    return output.getvalue()
