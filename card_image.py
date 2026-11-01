"""Карточка персонажа картинкой (PNG) на Pillow.

Шрифт DejaVu лежит в папке fonts, поэтому кириллица рисуется и на сервере без системных шрифтов.
"""
import io
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont

import game as G
from content import PACK_NAMES

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

WIDTH = 900
PAD = 48
BG = (24, 26, 30)
PANEL = (36, 39, 45)
LINE = (62, 66, 74)
TEXT = (232, 232, 228)
MUTED = (150, 154, 160)
ACCENT = (214, 168, 74)
SEVERITY = {0: (120, 180, 120), 1: (205, 180, 90), 2: (220, 130, 70), 3: (215, 80, 70)}

_fonts = {}


def _font(path, size):
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def _wrap(draw, text, font, max_width):
    """Перенос по словам с учётом реальной ширины текста"""
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            while draw.textlength(word, font=font) > max_width:   # очень длинное слово
                cut = max(1, int(len(word) * max_width / draw.textlength(word, font=font)) - 1)
                lines.append(word[:cut])
                word = word[cut:]
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_card(player, game_obj):
    """Возвращает PNG-байты карточки игрока"""
    title_f = _font(BOLD, 40)
    name_f = _font(BOLD, 30)
    small_f = _font(REGULAR, 22)
    label_f = _font(BOLD, 22)
    value_f = _font(REGULAR, 28)

    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    inner = WIDTH - 2 * PAD - 40

    rows = []
    for key in G.TRAIT_KEYS:
        trait = player.traits[key]
        rows.append((G.TRAIT_TITLES[key].upper(), _wrap(scratch, trait.text, value_f, inner),
                     SEVERITY.get(trait.severity) if key == "health" else None))
    card_title, card_desc, _, _ = G.CARDS[player.card]
    card_lines = _wrap(scratch, card_desc, small_f, inner)
    name_lines = _wrap(scratch, player.name, name_f, WIDTH - 2 * PAD)

    # высота
    h = PAD + 50 + 12 + len(name_lines) * 38 + 10 + 30 + 28
    for _, lines, _ in rows:
        h += 20 + 28 + len(lines) * 36 + 18
    h += 24 + 20 + 34 + len(card_lines) * 30 + 24 + PAD

    img = Image.new("RGB", (WIDTH, h), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    d.text((PAD, y), "СЕКРЕТНАЯ КАРТОЧКА", font=title_f, fill=ACCENT)
    y += 50 + 12
    for line in name_lines:
        d.text((PAD, y), line, font=name_f, fill=TEXT)
        y += 38
    y += 10
    pack = PACK_NAMES.get(getattr(game_obj, "pack", "realism"), "")
    catastrophe = game_obj.catastrophe["name"] if game_obj.catastrophe else ""
    d.text((PAD, y), f"{catastrophe}. Набор: {pack}", font=small_f, fill=MUTED)
    y += 30 + 28

    for label, lines, color in rows:
        top = y
        box_h = 20 + 28 + len(lines) * 36 + 8
        d.rounded_rectangle((PAD, top, WIDTH - PAD, top + box_h), radius=14, fill=PANEL)
        if color:
            d.rounded_rectangle((PAD, top, PAD + 8, top + box_h), radius=4, fill=color)
        y = top + 16
        d.text((PAD + 20, y), label, font=label_f, fill=MUTED)
        y += 32
        for line in lines:
            d.text((PAD + 20, y), line, font=value_f, fill=TEXT)
            y += 36
        y = top + box_h + 10

    y += 14
    d.line((PAD, y, WIDTH - PAD, y), fill=LINE, width=2)
    y += 20
    d.text((PAD, y), f"ОСОБАЯ КАРТА: {card_title.upper()}", font=label_f, fill=ACCENT)
    y += 34
    for line in card_lines:
        d.text((PAD, y), line, font=small_f, fill=TEXT)
        y += 30

    img = img.crop((0, 0, WIDTH, min(h, y + PAD)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def available():
    """Можно ли рисовать карточки на этом сервере"""
    try:
        _font(REGULAR, 20)
        _font(BOLD, 20)
        return True
    except Exception:
        return False
