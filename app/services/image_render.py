"""
Render the final duty schedule as a PNG image (Pillow).

Data source is identical to the xlsx export (scheduler.build_schedule_data):
each row = {full_name, phone, telegram_tag, email, projects, no_duty}.

Cross-platform font loading: prefers a bundled DejaVuSans.ttf (drop it into
assets/ on Linux), then bundled Arial (copied from Windows), then Pillow's
built-in default as a last resort.
"""
import os
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# Font candidates in priority order: (regular, bold)
_FONT_CANDIDATES = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
]

# ── Layout constants (pixels) ────────────────────────────────────────────────
IMG_WIDTH       = 1400
MARGIN          = 30
TITLE_SIZE      = 40
HEADER_SIZE     = 26
CELL_SIZE       = 24
FOOTER_SIZE     = 20
ROW_PAD_Y       = 14          # vertical padding inside a row
LINE_GAP        = 6           # gap between wrapped lines in a cell
CELL_PAD_X      = 14          # horizontal padding inside a cell

# Columns: (title, weight) — weights distribute the content width.
_COLUMNS = [
    ("Имя и фамилия", 0.24),
    ("Телефон",       0.24),
    ("Telegram",      0.17),
    ("Проекты",       0.35),
]

# Colours
C_BG          = (255, 255, 255)
C_TITLE       = (20, 30, 50)
C_HEADER_BG   = (68, 114, 196)     # blue, matches xlsx header
C_HEADER_TXT  = (255, 255, 255)
C_GRID        = (180, 188, 200)
C_ROW_A       = (255, 255, 255)
C_ROW_B       = (238, 242, 248)    # zebra
C_TEXT        = (25, 30, 40)
C_NODUTY_BG   = (255, 199, 206)    # light red
C_NODUTY_TXT  = (156, 0, 6)        # dark red
C_FOOTER      = (120, 128, 140)


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    for regular, boldname in _FONT_CANDIDATES:
        name = boldname if bold else regular
        path = os.path.join(_ASSETS, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # Last resort — Pillow built-in (may lack good cyrillic metrics)
    return ImageFont.load_default()


def _meaningful(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s in ("", "-", "—", "–") else s


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def _wrap(draw, text: str, font, max_w: float) -> list[str]:
    """Word-wrap text to fit max_w; hard-break tokens longer than the column."""
    if not text:
        return [""]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        cur = ""
        for word in words:
            candidate = word if not cur else f"{cur} {word}"
            if _text_w(draw, candidate, font) <= max_w:
                cur = candidate
                continue
            if cur:
                lines.append(cur)
                cur = ""
            # word alone may still be too wide — hard-break by chars
            if _text_w(draw, word, font) <= max_w:
                cur = word
            else:
                piece = ""
                for ch in word:
                    if _text_w(draw, piece + ch, font) <= max_w:
                        piece += ch
                    else:
                        if piece:
                            lines.append(piece)
                        piece = ch
                cur = piece
        lines.append(cur)
    return lines or [""]


def _phone_cell(row: dict) -> str:
    """Phone over email (each meaningful, like the xlsx export)."""
    phone = _meaningful(row.get("phone"))
    email = _meaningful(row.get("email"))
    if phone and email:
        return f"{phone}\n{email}"
    return phone or email


def render_schedule_png(
    period: str,
    rows: list[dict],
    output_path: str,
) -> str:
    """
    Render the schedule to a PNG file at output_path. Returns output_path.
    `rows` — list of dicts from scheduler.build_schedule_data.
    """
    title_font  = _load_font(TITLE_SIZE, bold=True)
    header_font = _load_font(HEADER_SIZE, bold=True)
    cell_font   = _load_font(CELL_SIZE)
    cell_bold   = _load_font(CELL_SIZE, bold=True)
    footer_font = _load_font(FOOTER_SIZE)

    content_w = IMG_WIDTH - 2 * MARGIN
    col_w = [int(content_w * w) for _, w in _COLUMNS]
    # Fix rounding so columns sum exactly to content_w
    col_w[-1] = content_w - sum(col_w[:-1])
    col_x = [MARGIN]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    # Measure with a scratch image
    scratch = Image.new("RGB", (10, 10))
    md = ImageDraw.Draw(scratch)

    line_h = CELL_SIZE + LINE_GAP

    def _cell_lines(text: str, font, col_idx: int) -> list[str]:
        max_w = col_w[col_idx] - 2 * CELL_PAD_X
        return _wrap(md, text, font, max_w)

    # Pre-compute wrapped content + row heights
    header_lines = [_cell_lines(t, header_font, i) for i, (t, _) in enumerate(_COLUMNS)]
    header_h = max(len(c) for c in header_lines) * line_h + 2 * ROW_PAD_Y

    body: list[dict] = []
    for row in rows:
        name = row.get("full_name", "")
        phone = _phone_cell(row)
        tag = _meaningful(row.get("telegram_tag"))
        projects = ", ".join(row.get("projects", []))
        cells = [
            _cell_lines(name, cell_font, 0),
            _cell_lines(phone, cell_font, 1),
            _cell_lines(tag, cell_font, 2),
            _cell_lines(projects, cell_font, 3),
        ]
        h = max(len(c) for c in cells) * line_h + 2 * ROW_PAD_Y
        body.append({"cells": cells, "h": h, "no_duty": bool(row.get("no_duty"))})

    # Total canvas height
    title_h = TITLE_SIZE + 24
    footer_h = FOOTER_SIZE + 24
    total_h = (MARGIN + title_h + header_h + sum(r["h"] for r in body)
               + footer_h + MARGIN)

    img = Image.new("RGB", (IMG_WIDTH, total_h), C_BG)
    draw = ImageDraw.Draw(img)

    # Title
    y = MARGIN
    draw.text((MARGIN, y), f"График дежурств · {period}", font=title_font, fill=C_TITLE)
    y += title_h

    def _draw_row(cells, y0, h, *, bg, fg, font, bold_first=False):
        # background
        draw.rectangle([MARGIN, y0, MARGIN + content_w, y0 + h], fill=bg)
        # cell text
        for ci, lines in enumerate(cells):
            cx = col_x[ci] + CELL_PAD_X
            cy = y0 + ROW_PAD_Y
            f = cell_bold if (bold_first and ci == 0) else font
            for ln in lines:
                draw.text((cx, cy), ln, font=f, fill=fg)
                cy += line_h
        # vertical grid lines
        for x in col_x[1:]:
            draw.line([x, y0, x, y0 + h], fill=C_GRID, width=1)
        # outer left/right
        draw.line([MARGIN, y0, MARGIN, y0 + h], fill=C_GRID, width=1)
        draw.line([MARGIN + content_w, y0, MARGIN + content_w, y0 + h], fill=C_GRID, width=1)
        # bottom line
        draw.line([MARGIN, y0 + h, MARGIN + content_w, y0 + h], fill=C_GRID, width=1)

    # Header
    draw.line([MARGIN, y, MARGIN + content_w, y], fill=C_GRID, width=1)
    _draw_row(header_lines, y, header_h, bg=C_HEADER_BG, fg=C_HEADER_TXT, font=header_font)
    y += header_h

    # Body rows (zebra; no_duty rows get red treatment)
    for i, r in enumerate(body):
        if r["no_duty"]:
            bg, fg = C_NODUTY_BG, C_NODUTY_TXT
        else:
            bg = C_ROW_A if i % 2 == 0 else C_ROW_B
            fg = C_TEXT
        _draw_row(r["cells"], y, r["h"], bg=bg, fg=fg, font=cell_font, bold_first=True)
        y += r["h"]

    # Footer
    y += 12
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((MARGIN, y), f"Сформировано: {stamp}", font=footer_font, fill=C_FOOTER)

    img.save(output_path, "PNG")
    return output_path
