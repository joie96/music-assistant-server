"""Mashin Stream V3 multi-client player provider with independent devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

CONTROL_WEBSOCKET_PATH = "/mashin-stream/v3/control"
AUDIO_WEBSOCKET_PATH = "/mashin-stream/v3/audio"

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create the shared Mashin Stream provider without registering clients or starting audio."""
    from .provider import MashinStreamProvider  # noqa: PLC0415

    return MashinStreamProvider(mass, manifest, config, set())
