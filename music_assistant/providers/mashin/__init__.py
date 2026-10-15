"""Mashin V1 multi-client player provider with independent devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create the shared Mashin provider without registering clients or starting audio."""
    from .provider import MashinProvider  # noqa: PLC0415

    return MashinProvider(mass, manifest, config, set())
