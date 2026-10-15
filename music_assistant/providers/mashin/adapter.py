"""Version-local MA PCM, output-plan and source ownership adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import AudioError, LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio import overlay_active
from music_assistant.helpers.ffmpeg import FFMpeg

from .codec import PCMChunkReader, PCMPacing
from .models import AudioProfileId, OutputFormat
from .wire import ProtocolViolation

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player


@dataclass(frozen=True, slots=True)
class PlaybackContext:
    """Captured MA authority and time origin, never reread as a mutable seek basis."""

    media: PlayerMedia
    queue_id: str
    queue_item_id: str
    session_id: str
    seek_seconds: int
    item: QueueItem
    source_identity: tuple[str, str, str, int, int, int, str, str, int | None]


class MAAdapter:
    """Keep MA API dependencies and processing ownership in one place."""

    def __init__(self, mass: MusicAssistant, player: Player) -> None:
        """Bind the adapter to the one stable player."""
        self.mass = mass
        self.player = player

    def capture(self, media: PlayerMedia) -> PlaybackContext:
        """Validate real queue media and capture its seek context before any await."""
        if not media.source_id or not media.queue_item_id or not media.queue_session_id:
            raise ProtocolViolation(
                "media_unsupported", "Only authorized MA queue tracks are supported"
            )
        data = self.mass.player_queues.queue_data_or_none(media.source_id)
        item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        if (
            data is None
            or data.session_id != media.queue_session_id
            or data.queue.flow_mode
            or item is None
            or item.streamdetails is None
            or item.media_type != MediaType.TRACK
            or not item.duration
            or not item.available
            or self.player.state.group_members
            or media.source_id != self.player.player_id
            or self.mass.streams.get_crossfade_mode(data.queue) != CrossfadeMode.DISABLED
            or overlay_active(data.queue)
            or item.extra_attributes.get("playback_speed", 1.0) != 1.0
        ):
            raise ProtocolViolation("media_unsupported", "Unsupported playback or stale MA context")
        seek = item.streamdetails.seek_position
        if int(seek) != seek or seek < 0:
            raise ProtocolViolation("media_unsupported", "V1 requires a whole-second source seek")
        return PlaybackContext(
            media,
            media.source_id,
            media.queue_item_id,
            media.queue_session_id,
            int(seek),
            item,
            self.source_identity(item),
        )

    def valid(self, context: PlaybackContext, *, successor_of: str | None = None) -> bool:
        """Revalidate the captured session and optional actual MA successor."""
        data = self.mass.player_queues.queue_data_or_none(context.queue_id)
        if (
            data is None
            or data.session_id != context.session_id
            or not context.item.available
            or self.mass.player_queues.get_item(context.queue_id, context.queue_item_id)
            is not context.item
            or self.player.state.group_members
        ):
            return False
        if successor_of is not None:
            next_item = self.mass.player_queues.get_next_item(context.queue_id, successor_of)
            return next_item is not None and next_item.queue_item_id == context.queue_item_id
        return True

    def loaded(self, context: PlaybackContext) -> None:
        """Invoke MA's loaded/served path once after first usable audio."""
        self.mass.player_queues.track_loaded_in_buffer(context.queue_id, context.queue_item_id)

    async def release(self, owned_buffer: AudioBuffer | None) -> None:
        """Release an owned incomplete MA buffer through its public lifecycle."""
        if owned_buffer is not None:
            await owned_buffer.clear()

    @staticmethod
    def source_identity(
        item: QueueItem,
    ) -> tuple[str, str, str, int, int, int, str, str, int | None]:
        """Pin the recording/provider and exact original source format."""
        details = item.streamdetails
        if details is None:
            raise ProtocolViolation("source_failed", "Missing MA source details")
        audio = details.audio_format
        return (
            details.provider,
            details.item_id,
            str(audio.content_type),
            audio.sample_rate,
            audio.bit_depth,
            audio.channels,
            (item.media_item.uri or "") if item.media_item else "",
            item.media_item.version if item.media_item else "",
            item.duration,
        )


class PCMSource:
    """One MA-normalized, player-processed and paced Float32 PCM reader."""

    def __init__(
        self,
        adapter: MAAdapter,
        context: PlaybackContext,
        output: OutputFormat,
        *,
        pacing: PCMPacing | None = None,
    ) -> None:
        """Capture format and ownership without starting a source."""
        self.adapter = adapter
        self.context = context
        self.output = output
        self.pacing = pacing
        self.process: FFMpeg | None = None
        self.reader: PCMChunkReader | None = None
        self.owned_buffer: AudioBuffer | None = None

    async def open(
        self,
        *,
        seek_seconds: int | None = None,
        repair: bool = False,
        start_timeout: float | None = 10,
    ) -> None:
        """Open only after the caller has reserved a chunk and passed all barriers."""
        context = self.context
        if not self.adapter.valid(context):
            raise ProtocolViolation("stale_playback", "MA playback is no longer authorized")
        audio = self.adapter.mass.streams.audio
        assert context.item.streamdetails is not None
        seek = context.seek_seconds if seek_seconds is None else seek_seconds
        prepared = await audio.get_audio_buffer(
            context.item,
            seek_position_ms=seek * 1000,
            reason="mashin_recovery" if repair else "streaming",
            allow_provider_match=not repair,
        )
        self.owned_buffer = prepared
        identity = self.adapter.source_identity(context.item)
        if not self.adapter.valid(context) or (repair and identity != context.source_identity):
            raise ProtocolViolation("source_failed", "The MA source identity changed")
        if not repair:
            context = self.context = replace(context, source_identity=identity)
        details = context.item.streamdetails
        if details is None:
            raise ProtocolViolation("source_failed", "Missing prepared MA source details")
        pcm = await audio.select_pcm_format(
            player=self.adapter.player,
            streamdetails=details,
            crossfade_enabled=False,
            overlay_active=False,
        )
        target = AudioFormat(
            content_type=ContentType.PCM_F32LE,
            sample_rate=self.output.sample_rate_hz,
            bit_depth=32,
            channels=self.output.channels,
        )
        final = AudioFormat(
            content_type=ContentType.FLAC
            if self.output.profile_id == AudioProfileId.FLAC
            else ContentType.OPUS,
            sample_rate=self.output.sample_rate_hz,
            bit_depth=self.output.bit_depth or 32,
            channels=self.output.channels,
        )
        plan = audio.get_player_output_plan(
            self.adapter.player.player_id,
            pcm,
            final,
            handoff_format=target,
            queue_id=context.queue_id,
            session_id=context.session_id,
            queue_item_id=context.queue_item_id,
        )
        if (
            not self.adapter.valid(context)
            or self.adapter.source_identity(context.item) != context.source_identity
        ):
            raise ProtocolViolation("source_failed", "The MA source identity changed")
        source = audio.get_queue_item_stream(
            context.item,
            pcm,
            seek_position=seek,
            playback_speed=1.0,
            raise_on_error=True,
            session_id=context.session_id,
            prepared_buffer=prepared,
        )
        budget = self.pacing if self.pacing is not None else PCMPacing()
        budget.configure(details.is_realtime)
        pacing = ["-readrate", str(budget.rate)]
        if not repair:
            pacing += ["-readrate_initial_burst", f"{budget.burst:g}"]
        if repair:
            source_format = details.audio_format
            resampling = len({source_format.sample_rate, pcm.sample_rate, target.sample_rate}) > 1
            channel_conversion = len({source_format.channels, pcm.channels, target.channels}) > 1
            if (
                plan.filter_params
                or details.volume_normalization_mode != VolumeNormalizationMode.DISABLED
                or details.fade_in
                or resampling
                or channel_conversion
            ):
                self.adapter.player.logger.info(
                    "PCM repair processing snapshot for player %s: active PCM-altering processing; output_filters=%s normalization=%s fade_in=%s resampling=%s channel_conversion=%s; best-effort matching may fail",
                    self.adapter.player.player_id,
                    len(plan.filter_params),
                    details.volume_normalization_mode,
                    bool(details.fade_in),
                    resampling,
                    channel_conversion,
                )
        self.process = FFMpeg(
            audio_input=source,
            input_format=pcm,
            output_format=target,
            filter_params=plan.filter_params,
            extra_input_args=pacing,
        )
        async with asyncio.timeout(start_timeout):
            await self.process.start()
        self.reader = PCMChunkReader(self.read, target.sample_rate, target.channels)

    async def read(self, size: int) -> bytes:
        """Read a requested bounded amount and reject decoder/source failures as EOF."""
        if self.process is None:
            raise ProtocolViolation("source_failed", "PCM source is not open")
        process = self.process
        if self.owned_buffer is not None and self.owned_buffer.cancelled:
            raise AudioError("MA PCM buffer was released")
        pacing = self.pacing
        bytes_per_second = self.output.sample_rate_hz * self.output.channels * 4 if pacing else 1
        if pacing is not None:
            await pacing.wait(size / bytes_per_second)
        if self.process is not process:
            raise AudioError("MA PCM processing was released")
        result: bytes = await process.read(size)
        if pacing is not None:
            pacing.commit(len(result) / bytes_per_second)
        if self.owned_buffer is not None and self.owned_buffer.cancelled:
            raise AudioError("MA PCM buffer was released during read")
        if not result:
            await process.wait_with_timeout(5)
            if isinstance(process.stdin_feeder_exception, LoginFailed):
                raise ProtocolViolation("forbidden", "MA source authorization failed")
            if isinstance(process.stdin_feeder_exception, MediaNotFoundError):
                raise ProtocolViolation("media_unsupported", "MA source is no longer available")
            if process.returncode != 0 or process.stdin_feeder_exception is not None:
                raise AudioError("Mashin PCM source failed")
        return result

    async def close(self) -> None:
        """Close the processing chain without changing playback authority."""
        process, self.process = self.process, None
        self.reader = None
        if process is not None:
            await process.close()
