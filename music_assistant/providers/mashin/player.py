"""Mashin Player adapter using validated actual client state."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.models.player import Player

from .wire import ProtocolViolation, integer

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from .adapter import PlaybackContext
    from .models import PlayerState
    from .provider import MashinDevice, MashinProvider


class MashinPlayer(Player):
    """One stable player belonging to its registered device, never to a socket."""

    def __init__(
        self, provider: MashinProvider, device: MashinDevice, player_id: str, name: str
    ) -> None:
        """Initialize a non-groupable, event-driven player."""
        super().__init__(provider, player_id)
        self.mashin = device
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            model="Mashin client",
            manufacturer="Mashin",
            identifiers={IdentifierType.UNKNOWN: device.device_id},
        )
        self._attr_available = False
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_can_group_with: set[str] = set()
        self._attr_active_source: str | None = None
        self._attr_elapsed_time: float = 0.0
        self.buffering = False

    @property
    def corrected_elapsed_time(self) -> float | None:
        """Freeze projected progress during device buffering or Control loss."""
        if self.buffering or not self.available:
            return self.elapsed_time
        return super().corrected_elapsed_time

    @property
    def needs_poll(self) -> bool:
        """Return False because actual device state arrives through Control."""
        return False

    async def play_media(self, media: PlayerMedia) -> None:
        """Load authorized MA queue media with its captured source seek origin."""
        try:
            context = self.mashin.session.adapter.capture(media)
            await self.mashin.session.load(context)
        except ProtocolViolation as err:
            raise PlayerCommandFailed(str(err)) from err

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Register the actual MA successor without opening its source."""
        session = self.mashin.session
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Next-track requested player=%s queue=%s item=%s current_item=%s next_item=%s",
            self.player_id,
            media.source_id,
            media.queue_item_id,
            session.current.context.queue_item_id if session.current is not None else None,
            session.next.context.queue_item_id if session.next is not None else None,
        )
        stage = "capture"
        try:
            context = session.adapter.capture(media)
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Next-track captured player=%s item=%s seek_seconds=%s duration=%s",
                self.player_id,
                context.queue_item_id,
                context.seek_seconds,
                context.item.duration,
            )
            stage = "prepare"
            await session.prepare(context)
        except ProtocolViolation as err:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Next-track rejected player=%s item=%s stage=%s reason=%s",
                self.player_id,
                media.queue_item_id,
                stage,
                str(err),
            )
            raise PlayerCommandFailed(str(err)) from err
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Next-track prepare completed player=%s item=%s",
            self.player_id,
            context.queue_item_id,
        )

    async def play(self) -> None:
        """Resume the current authorized stream without replacing it."""
        await self._control("play")

    async def pause(self) -> None:
        """Pause actual client output independently of receipt and production."""
        await self._control("pause")

    async def stop(self) -> None:
        """Invalidate playback and finish the regular MA stop path."""
        session = self.mashin.session
        session.generation = integer(session.generation + 1, 1)
        generation = session.generation
        session.buffer.load_accepted = False
        session.buffer.capacity_confirmed = False
        for run in (session.current, session.next):
            if run is not None:
                run.accepted = False
                run.source_attempt_id += 1
                session.buffer.streams[run.descriptor.stream_id].terminal = True
        try:
            if not session.suppress_stop_command and session.send_control is not None:
                await self._control("stop")
        finally:
            if session.generation == generation:
                await session.reset()
                self._attr_playback_state = PlaybackState.IDLE
                self._attr_current_media = None
                self._attr_active_source = None
                self._attr_elapsed_time = 0
                self._attr_elapsed_time_last_updated = time.time()
                self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send volume and wait for the client's independently reported actual value."""
        await self._control("set_volume", volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Send mute without overwriting configured volume or stream state."""
        await self._control("set_mute", muted)

    def report(self, state: PlayerState, context: PlaybackContext | None) -> None:
        """Project validated local output into MA without echoing device commands."""
        self._attr_volume_level = state.volume_percent
        self._attr_volume_muted = state.is_muted
        self.buffering = state.output_state == "buffering"
        self._attr_playback_state = {
            "playing": PlaybackState.PLAYING,
            "paused": PlaybackState.PAUSED,
            "buffering": PlaybackState.PLAYING,
            "ended": PlaybackState.IDLE,
            "idle": PlaybackState.IDLE,
            "error": PlaybackState.IDLE,
        }[state.output_state]
        if context is not None:
            self._attr_current_media = context.media
            self._attr_active_source = context.queue_id
            rate = (
                self.mashin.session.current.descriptor.output_format.sample_rate_hz
                if self.mashin.session.current
                else 48000
            )
            self._attr_elapsed_time = state.position_frame / rate
        else:
            self._attr_current_media = None
            self._attr_active_source = None
            self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def connected(self, available: bool) -> None:
        """Publish connection availability without inventing output progress."""
        self._attr_available = available
        self.update_state()

    async def _control(self, kind: str, value: int | bool | None = None) -> None:
        try:
            await self.mashin.session.control(kind, value)
        except ProtocolViolation as err:
            raise PlayerCommandFailed(str(err)) from err
