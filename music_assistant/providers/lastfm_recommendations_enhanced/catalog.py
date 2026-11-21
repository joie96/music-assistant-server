"""Stable discovery categories and generated, read-only playlist identities."""

import re
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class Category:
    """A stable key and the corresponding public Last.fm tag."""

    key: str
    name: str
    tag: str


@dataclass(frozen=True)
class GenreProfile:
    """Separate personal and global Explore categories for one generation."""

    genres: tuple[Category, ...]
    explore_genres: tuple[Category, ...] = ()


GENRE_FOLDER_FAMILIES = (
    ("global_genre_albums", "explore albums"),
    ("global_genre_artists", "explore artists"),
    ("user_genre_artists", "artists from your favorite genre"),
)
NON_GENRE_TAGS = frozenset(
    {
        "seenlive",
        "favorites",
        "favourites",
        "favorite",
        "favourite",
        "awesome",
        "beautiful",
        "love",
        "loved",
        "malevocalists",
        "femalevocalists",
        "all",
        "american",
        "british",
        "german",
        "french",
        "english",
        "canadian",
        "swedish",
        "relax",
        "relaxing",
        "focus",
        "concentration",
        "energetic",
        "melancholic",
        "melancholy",
        "happy",
        "sad",
        "workout",
        "sleep",
        "party",
        "chill",
    }
)
GENRE_ALIASES = {
    "hiphop": "hip-hop",
    "dreampop": "dream pop",
    "synthpop": "synthpop",
    "rb": "r&b",
    "rnb": "r&b",
    "rhythmandblues": "r&b",
}
MOODS = (
    Category("relax", "Relaxation", "chillout"),
    Category("focus", "Concentration", "ambient"),
    Category("energy", "Energy", "energetic"),
    Category("melancholy", "Melancholy", "melancholic"),
    Category("love", "Love", "love"),
    Category("motivation", "Motivation", "workout"),
    Category("chill", "Chill", "chill"),
    Category("sad", "Sad", "sad"),
    Category("focused", "Focus", "focus"),
    Category("party", "Party", "party"),
)
DECADES = tuple(Category(str(year), f"{year}s", f"{year}s") for year in range(1980, 2030, 10))
EXTRA_FOLDERS = (
    ("user_genre_playlists_monthly", "Your genre playlists"),
    ("user_artist_playlists_monthly", "Your artist playlists"),
    ("global_popular_artist_albums", "Albums by worldwide popular artists"),
    ("de_popular_artist_albums", "Albums by popular artists in Germany"),
    ("global_genre_playlists", "Explore genre playlists"),
    ("global_artist_radio_playlists", "Artist radios"),
    ("global_mood_playlists", "Mood playlists"),
    ("global_decade_playlists", "Decade playlists"),
)


@dataclass(frozen=True)
class PlaylistSpec:
    """A recipe rebuilt in the requesting user's context, never a stored track list."""

    item_id: str
    name: str
    tag: str = ""
    artist: str = ""
    personal: bool = False


def category_for_key(key: str) -> Category | None:
    """Resolve an advertised category key without allowing arbitrary API parameters."""
    return next((category for category in (*MOODS, *DECADES) if category.key == key), None)


def genre_for_tag(value: str) -> Category | None:
    """Normalize community genre candidates, excluding known noise, periods and mood-only tags."""
    tag = " ".join(value.casefold().split())
    normalized = re.sub(r"[\W_]+", "", tag)
    if (
        not normalized
        or normalized in NON_GENRE_TAGS
        or tag.startswith(("my ", "my-", "my_"))
        or re.fullmatch(r"\d{2,4}s?", normalized)
        or len(tag) > 80
    ):
        return None
    tag = GENRE_ALIASES.get(normalized, tag)
    normalized = re.sub(r"[\W_]+", "", tag)
    name = "Hip-Hop" if tag == "hip-hop" else "R&B" if tag == "r&b" else tag.title()
    return Category(sha256(normalized.encode()).hexdigest()[:16], name, tag)
