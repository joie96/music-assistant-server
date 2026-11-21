"""Generated category identities stay stable and distinct."""

import pytest

from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    DECADES,
    EXTRA_FOLDERS,
    MOODS,
    category_for_key,
    genre_for_tag,
)


def test_categories_have_unique_stable_keys() -> None:
    """Resolve only published categories, not arbitrary input used as a Last.fm tag."""
    categories = (*MOODS, *DECADES)
    assert len({category.key for category in categories}) == len(categories)
    assert all(category_for_key(category.key) == category for category in categories)
    assert category_for_key("unpublished") is None
    assert len({folder_id for folder_id, _name in EXTRA_FOLDERS}) == 8


def test_moods_preserve_existing_categories_and_append_six_flow_inspired_categories() -> None:
    """Keep old playlist identities while adding six independently addressable mood tags."""
    assert [(category.key, category.name, category.tag) for category in MOODS] == [
        ("relax", "Relaxation", "chillout"),
        ("focus", "Concentration", "ambient"),
        ("energy", "Energy", "energetic"),
        ("melancholy", "Melancholy", "melancholic"),
        ("love", "Love", "love"),
        ("motivation", "Motivation", "workout"),
        ("chill", "Chill", "chill"),
        ("sad", "Sad", "sad"),
        ("focused", "Focus", "focus"),
        ("party", "Party", "party"),
    ]
    assert len({category.tag for category in MOODS}) == 10
    assert all(genre_for_tag(category.tag) is None for category in MOODS[4:])


@pytest.mark.parametrize("tag", ["Shoegaze", "Dream Pop", "Post-Rock", "Ambient", "Mystic Folk"])
def test_genres_accept_dynamic_subgenres(tag: str) -> None:
    """Keep real genre candidates outside the former eight-genre selection."""
    category = genre_for_tag(tag)
    assert category is not None
    assert category.tag == tag.casefold()


@pytest.mark.parametrize(
    "tag", ["seen live", "favorites", "sad", "energetic", "1990s", "my music", ""]
)
def test_genres_reject_known_non_genre_tags(tag: str) -> None:
    """Community annotations, mood-only tags and periods must not become genre folders."""
    assert genre_for_tag(tag) is None


@pytest.mark.parametrize(
    ("first", "second"),
    [("Hip Hop", "hip-hop"), ("R&B", "rhythm and blues"), ("Post-Rock", "post rock")],
)
def test_genre_identity_normalizes_case_punctuation_and_aliases(first: str, second: str) -> None:
    """Genre IDs do not depend on response capitalization or punctuation."""
    before = genre_for_tag(first)
    after = genre_for_tag(second)
    assert before is not None
    assert after is not None
    assert before.key == after.key
