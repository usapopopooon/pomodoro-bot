"""Circular pomodoro timer image.

Generated once per phase transition — **not** on every tick. The file is
small (<10KB) and renders in a few ms with Pillow, so attaching a fresh
PNG on each phase change is cheaper and nicer-looking than editing an
embed every 10 seconds.

Design mirrors LionBot's pomodoro card: dark navy background, a phosphor
yellow progress ring with a leading dot, large centered time, phase label
underneath, and a row of battery-style segments along the top showing how
many focus rounds have been completed in this cycle.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.core.phase import Phase

# ---------------------------------------------------------------------------
# Geometry & palette
# ---------------------------------------------------------------------------

SIZE = 480
CENTER = SIZE // 2
RING_RADIUS = 180
RING_WIDTH = 14

BG = (17, 23, 37)
RING_BG = (34, 42, 61)
FG_WORK = (255, 204, 0)
FG_SHORT_BREAK = (46, 204, 113)
FG_LONG_BREAK = (52, 152, 219)
TEXT_PRIMARY = (238, 240, 246)
TEXT_SECONDARY = (140, 150, 170)

SEGMENT_Y = 58
SEGMENT_HEIGHT = 18
SEGMENT_GAP = 10

# Font candidates in priority order. The first existing file wins. PIL's
# default bitmap font is a last resort — ugly at large sizes but never
# crashes.
_FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),  # Debian slim
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Helvetica.ttc"),  # macOS dev
    Path("/Library/Fonts/Arial.ttf"),
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _phase_color(phase: Phase) -> tuple[int, int, int]:
    match phase:
        case Phase.WORK:
            return FG_WORK
        case Phase.SHORT_BREAK:
            return FG_SHORT_BREAK
        case Phase.LONG_BREAK:
            return FG_LONG_BREAK


def _phase_label(phase: Phase) -> str:
    match phase:
        case Phase.WORK:
            return "FOCUS"
        case Phase.SHORT_BREAK:
            return "BREAK"
        case Phase.LONG_BREAK:
            return "LONG BREAK"


def _text_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left), int(bottom - top)


def render_timer_png(
    *,
    phase: Phase,
    minutes_remaining: int,
    session_number: int,
    sessions_per_cycle: int,
) -> BytesIO:
    """Render one circular timer card as a PNG.

    ``session_number`` is 1-based. The top row draws ``sessions_per_cycle``
    segments: the first ``session_number - 1`` are filled (completed
    focus rounds), and if the current phase is ``WORK`` the in-progress
    segment gets a dimmed highlight.
    """
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    color = _phase_color(phase)

    # --- Top segment row ------------------------------------------------
    sessions_per_cycle = max(1, sessions_per_cycle)
    total_seg_width = SIZE * 2 // 3
    seg_each = (
        total_seg_width - SEGMENT_GAP * (sessions_per_cycle - 1)
    ) // sessions_per_cycle
    start_x = (SIZE - total_seg_width) // 2
    for i in range(sessions_per_cycle):
        x0 = start_x + i * (seg_each + SEGMENT_GAP)
        x1 = x0 + seg_each
        completed = i < max(0, session_number - 1)
        is_current = i == (session_number - 1) and phase is Phase.WORK
        fill: tuple[int, int, int]
        if completed:
            fill = color
        elif is_current:
            fill = (int(color[0] * 0.45), int(color[1] * 0.45), int(color[2] * 0.45))
        else:
            fill = RING_BG
        draw.rounded_rectangle(
            (x0, SEGMENT_Y, x1, SEGMENT_Y + SEGMENT_HEIGHT),
            radius=4,
            fill=fill,
        )

    # --- Background ring ------------------------------------------------
    ring_bbox = (
        CENTER - RING_RADIUS,
        CENTER - RING_RADIUS,
        CENTER + RING_RADIUS,
        CENTER + RING_RADIUS,
    )
    draw.ellipse(ring_bbox, outline=RING_BG, width=RING_WIDTH)

    # --- Leading dot at 12 o'clock --------------------------------------
    dot_r = 11
    draw.ellipse(
        (
            CENTER - dot_r,
            CENTER - RING_RADIUS - dot_r,
            CENTER + dot_r,
            CENTER - RING_RADIUS + dot_r,
        ),
        fill=color,
    )

    # --- Center time ----------------------------------------------------
    time_font = _load_font(84)
    time_str = f"{minutes_remaining:02d}:00"
    tw, th = _text_size(draw, time_str, time_font)
    time_y = CENTER - th // 2 - 20
    draw.text((CENTER - tw // 2, time_y), time_str, fill=TEXT_PRIMARY, font=time_font)

    # --- Phase label ----------------------------------------------------
    label_font = _load_font(28)
    label = _phase_label(phase)
    lw, lh = _text_size(draw, label, label_font)
    draw.text(
        (CENTER - lw // 2, time_y + th + 8),
        label,
        fill=color,
        font=label_font,
    )

    # --- Footer hint ----------------------------------------------------
    # Intentionally kept in ASCII: DejaVu Sans covers Latin reliably
    # without pulling in a CJK font pack. Short enough to stay below
    # the ring without crowding.
    footer_font = _load_font(16)
    footer = "Tap Present to join"
    fw, fh = _text_size(draw, footer, footer_font)
    draw.text(
        (CENTER - fw // 2, SIZE - fh - 30),
        footer,
        fill=TEXT_SECONDARY,
        font=footer_font,
    )

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
