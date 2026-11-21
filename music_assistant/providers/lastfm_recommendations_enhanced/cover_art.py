"""ListenBrainz-style playlist covers; rendering is IO-free and runs off the event loop."""

from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

CANVAS_SIZE = 1200
TOP_INSET_HEIGHT = 190
PLACEHOLDER_COLORS = ("#2C2C2C", "#1F1F1F", "#151515", "#080808")
FONT_CANDIDATES = (
    "tahomabd.ttf",
    "Tahoma Bold.ttf",
    "arialbd.ttf",
    "Arial Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
)


def build_split_cover_png(text_left: str, cover_images: list[bytes], text_right: str) -> bytes:
    """Render four artwork tiles and a curved title inset, filling missing tiles neutrally."""
    unique_images = list(dict.fromkeys(image for image in cover_images if image))[:4]
    canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "black")
    draw = ImageDraw.Draw(canvas)
    tile_width = CANVAS_SIZE // 2
    tile_height = (CANVAS_SIZE - TOP_INSET_HEIGHT) // 2
    for index in range(4):
        left = (index % 2) * tile_width
        top = TOP_INSET_HEIGHT + (index // 2) * tile_height
        draw.rectangle(
            (left, top, left + tile_width - 1, top + tile_height - 1),
            fill=PLACEHOLDER_COLORS[index],
        )
        if index < len(unique_images):
            with Image.open(BytesIO(unique_images[index])) as image:
                fitted = ImageOps.fit(
                    image.convert("RGB"),
                    (tile_width, tile_height),
                    method=Image.Resampling.LANCZOS,
                )
            canvas.paste(fitted, (left, top))
            fitted.close()
    points: list[tuple[float, float]] = [(0, 0), (CANVAS_SIZE, 0)]
    for step in range(65):
        progress = step / 64
        points.append(
            (
                CANVAS_SIZE * (1 - progress),
                TOP_INSET_HEIGHT + 132 * progress * (1 - progress),
            )
        )
    points.append((0, 0))
    draw.polygon(points, fill="black")
    left_font = _load_font(110)
    right_font = _load_font(64)
    left_label = fit_cover_text(text_left, left_font, 520)
    right_label = fit_cover_text(text_right, right_font, 480)
    left_position = (70, 50)
    left_bounds = _text_ink_bounds(left_label, left_font)
    right_bounds = _text_ink_bounds(right_label, right_font)
    draw.text(left_position, left_label, fill="white", font=left_font)
    draw.text(
        (
            CANVAS_SIZE - left_position[0] - left_bounds[0] - right_bounds[2],
            left_position[1] + left_bounds[1] - right_bounds[1],
        ),
        right_label,
        fill="white",
        font=right_font,
    )
    with BytesIO() as buffer:
        canvas.save(buffer, format="PNG", optimize=True)
        result = buffer.getvalue()
    canvas.close()
    return result


def fit_cover_text(
    value: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int
) -> str:
    """Keep a title on one line within its pixel budget, truncating only when necessary."""
    label = " ".join(value.split())
    if font.getlength(label) <= max_width:
        return label
    while label and font.getlength(label.rstrip() + "...") > max_width:
        label = label[:-1]
    return label.rstrip() + "..."


def _text_ink_bounds(
    label: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont
) -> tuple[int, int, int, int]:
    """Return visible glyph bounds, excluding padding in the font's layout box."""
    ink_bounds = font.getmask(label, mode="L").getbbox()
    if ink_bounds is None:
        return (0, 0, 0, 0)
    font_bounds = font.getbbox(label)
    return (
        font_bounds[0] + ink_bounds[0],
        font_bounds[1] + ink_bounds[1],
        font_bounds[0] + ink_bounds[2],
        font_bounds[1] + ink_bounds[3],
    )


@lru_cache(maxsize=8)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the same bold font candidates as the ListenBrainz cover renderer."""
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)
