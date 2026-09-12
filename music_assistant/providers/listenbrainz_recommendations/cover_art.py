"""Reusable cover generation helpers for ListenBrainz playlists."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

_CANVAS_SIZE = 1200
_TEXT_COLOR = "#FFFFFF"
_TOP_INSET_COLOR = "#000000"

_FONT_CANDIDATES = (
    "tahomabd.ttf",
    "Tahoma Bold.ttf",
    "arialbd.ttf",
    "Arial Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
)

_TEXT_LEFT_SIZE = 110
_TEXT_LEFT_X = 70
_TEXT_LEFT_Y = 50

_TEXT_RIGHT_SIZE = 48
_TEXT_RIGHT_X = 1130
_TEXT_RIGHT_Y = 100

_TOP_INSET_HEIGHT = 190
_TOP_CURVE_DEPTH = 66

_PLACEHOLDER_COLORS = ("#2C2C2C", "#1F1F1F", "#151515", "#080808")
_CURVE_SEGMENTS = 64


@lru_cache(maxsize=8)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a bold font with cached size-based lookup."""
    for font_name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(font_name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _build_top_overlay_points() -> list[tuple[float, float]]:
    """Build polygon points for a top inset with a curved lower edge."""
    points: list[tuple[float, float]] = [(0.0, 0.0), (float(_CANVAS_SIZE), 0.0), (float(_CANVAS_SIZE), float(_TOP_INSET_HEIGHT))]

    x0 = float(_CANVAS_SIZE)
    y0 = float(_TOP_INSET_HEIGHT)
    x1 = float(_CANVAS_SIZE // 2)
    y1 = float(_TOP_INSET_HEIGHT + _TOP_CURVE_DEPTH)
    x2 = 0.0
    y2 = float(_TOP_INSET_HEIGHT)

    for step in range(_CURVE_SEGMENTS + 1):
        t = step / _CURVE_SEGMENTS
        x = ((1 - t) ** 2 * x0) + (2 * (1 - t) * t * x1) + ((t**2) * x2)
        y = ((1 - t) ** 2 * y0) + (2 * (1 - t) * t * y1) + ((t**2) * y2)
        points.append((x, y))

    points.append((0.0, 0.0))
    return points


def _truncate_text(value: str, max_length: int) -> str:
    """Trim long labels so text does not overflow the designed area."""
    clean = value.strip()
    if len(clean) <= max_length:
        return clean
    return f"{clean[: max_length - 1].rstrip()}..."


def build_split_cover_png(
    text_left: str,
    cover_images: list[bytes],
    text_right: str | None = None,
) -> bytes:
    """Build a 2x2 tiled cover PNG with text and top inset overlay. Returns raw PNG bytes."""
    safe_text_left = _truncate_text(text_left, max_length=32)
    safe_text_right = _truncate_text(text_right, max_length=18) if text_right else ""

    # Keep order stable and avoid duplicate images in the tiled background.
    unique_urls: list[bytes] = []
    seen: set[bytes] = set()
    for image in cover_images:
        if not image or image in seen:
            continue
        seen.add(image)
        unique_urls.append(image)
        if len(unique_urls) >= 4:
            break

    available_height = _CANVAS_SIZE - _TOP_INSET_HEIGHT
    canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), _TOP_INSET_COLOR)
    draw = ImageDraw.Draw(canvas)

    if len(unique_urls) == 1:
        tile_rects = [(0, _TOP_INSET_HEIGHT, _CANVAS_SIZE, available_height)]
    else:
        half_width = _CANVAS_SIZE // 2
        half_height = available_height // 2
        remaining_height = available_height - half_height
        tile_rects = [
            (0, _TOP_INSET_HEIGHT, half_width, half_height),
            (half_width, _TOP_INSET_HEIGHT, half_width, half_height),
            (0, _TOP_INSET_HEIGHT + half_height, half_width, remaining_height),
            (half_width, _TOP_INSET_HEIGHT + half_height, half_width, remaining_height),
        ]

    for idx, (x, y, width, height) in enumerate(tile_rects):
        draw.rectangle(
            (x, y, x + width, y + height),
            fill=_PLACEHOLDER_COLORS[idx % len(_PLACEHOLDER_COLORS)],
        )

        if idx < len(unique_urls):
            with Image.open(BytesIO(unique_urls[idx])) as image:
                fitted = ImageOps.fit(
                    image.convert("RGB"),
                    (max(1, width), max(1, height)),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
            canvas.paste(fitted, (x, y))

    draw.polygon(_build_top_overlay_points(), fill=_TOP_INSET_COLOR)

    font_left = _load_font(_TEXT_LEFT_SIZE)
    font_right = _load_font(_TEXT_RIGHT_SIZE)
    draw.text((_TEXT_LEFT_X, _TEXT_LEFT_Y), safe_text_left, fill=_TEXT_COLOR, font=font_left)

    if safe_text_right:
        right_bbox = draw.textbbox((0, 0), safe_text_right, font=font_right)
        right_text_width = right_bbox[2] - right_bbox[0]
        draw.text(
            (_TEXT_RIGHT_X - right_text_width, _TEXT_RIGHT_Y),
            safe_text_right,
            fill=_TEXT_COLOR,
            font=font_right,
        )

    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
