"""Constants for YT Music provider."""

from enum import StrEnum


class YTMRecommendationIcons(StrEnum):
    """Icons for YTM recommendation types."""

    NEW_RELEASES = "mdi-new-box"
    RECOMMENDED = "mdi-star-circle-outline"
    DEFAULT = "mdi-music-note-outline"
