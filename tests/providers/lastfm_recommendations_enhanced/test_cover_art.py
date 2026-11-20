"""Pixel and label contracts for generated playlist covers."""

from io import BytesIO

import pytest
from PIL import Image, ImageFont

from music_assistant.providers.lastfm_recommendations_enhanced.cover_art import (
    CANVAS_SIZE,
    PLACEHOLDER_COLORS,
    build_split_cover_png,
    fit_cover_text,
)


def image_bytes(color: str) -> bytes:
    """Return a small real PNG for an artwork tile."""
    with Image.new("RGB", (30, 20), color) as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def test_cover_has_four_real_tiles_and_listenbrainz_title_inset() -> None:
    """Keep all four artwork tiles and the curved black inset in stable positions."""
    colors = ("red", "green", "blue", "yellow")
    payload = build_split_cover_png("Genre", [image_bytes(color) for color in colors], "Jazz")
    with Image.open(BytesIO(payload)) as cover:
        assert cover.size == (CANVAS_SIZE, CANVAS_SIZE)
        assert cover.format == "PNG"
        for point, color in zip(
            [(300, 450), (900, 450), (300, 950), (900, 950)], colors, strict=True
        ):
            assert cover.getpixel(point) == Image.new("RGB", (1, 1), color).getpixel((0, 0))
        assert cover.getpixel((600, 205)) == (0, 0, 0)
        assert cover.getpixel((10, 205)) == (255, 0, 0)
        assert max(cover.crop((70, 50, 590, 185)).convert("L").getextrema()) == 255
        assert max(cover.crop((650, 100, 1130, 185)).convert("L").getextrema()) == 255


@pytest.mark.parametrize("image_count", [0, 1, 2, 3])
def test_missing_images_use_placeholders_without_repeating_artwork(image_count: int) -> None:
    """Use a predictable four-tile layout even when fewer covers are available."""
    images = [image_bytes(color) for color in ("red", "green", "blue")[:image_count]]
    with Image.open(BytesIO(build_split_cover_png("Genre", images, "Jazz"))) as cover:
        points = [(300, 450), (900, 450), (300, 950), (900, 950)]
        for index in range(image_count, 4):
            expected = Image.new("RGB", (1, 1), PLACEHOLDER_COLORS[index]).getpixel((0, 0))
            assert cover.getpixel(points[index]) == expected


def test_duplicate_and_empty_images_do_not_fill_multiple_tiles() -> None:
    """Deduplicate by image content rather than by source URL alone."""
    red = image_bytes("red")
    images = [b"", red, red, image_bytes("blue")]
    assert build_split_cover_png("Genre", images, "Jazz") == build_split_cover_png(
        "Genre", [red, image_bytes("blue")], "Jazz"
    )


def test_long_titles_fit_their_pixel_budget_and_keep_one_line() -> None:
    """Limit both labels without trusting a character-count approximation."""
    font = ImageFont.load_default(size=48)
    fitted = fit_cover_text("Very long genre name " * 30, font, 480)
    assert fitted.endswith("...")
    assert font.getlength(fitted) <= 480
    assert fit_cover_text("  Jazz\nRock  ", font, 480) == "Jazz Rock"
