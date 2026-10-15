"""Mashin setup and provider configuration fields."""

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType


def entries() -> tuple[ConfigEntry, ...]:
    """Return shared admission settings for token-free LAN device registration."""
    return (
        ConfigEntry(
            key="max_registered_devices",
            type=ConfigEntryType.INTEGER,
            default_value=32,
            range=(1, 4096),
            category="advanced",
        ),
        ConfigEntry(
            key="max_websocket_connections",
            type=ConfigEntryType.INTEGER,
            default_value=128,
            range=(2, 16384),
            category="advanced",
        ),
    )
