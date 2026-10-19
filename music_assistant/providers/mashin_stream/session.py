"""Mashin player lifecycle, reconciliation and coordinated audio production."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .buffer import DeliveryBuffer
from .codec import ChunkEncoder, PCMChunkReader, PCMPacing
from .models import (
    AudioBinding,
    AudioHello,
    AudioProfileChanged,
    AudioProfileId,
    AudioReconnectRequest,
    BufferStatus,
    ChunkAck,
    ClientResumeState,
    ClientStreamCursor,
    CommandAck,
    ErrorMessage,
    Hello,
    MuteCommand,
    OutputFormat,
    PlaybackIdentity,
    PlayerState,
    PreparedActivated,
    PreparedActivation,
    ProtocolError,
    ProtocolLimits,
    SetAudioProfile,
    SetMedia,
    StateRequest,
    StreamCommand,
    StreamDescriptor,
    StreamSnapshot,
    TrackEnded,
    TrackStarted,
    TransportStatus,
    VolumeCommand,
    Welcome,
    parse_model,
)
from .recovery import RecoveryIncident, RecoveryRateLimit, match_pcm
from .wire import MAX_INTEGER, AudioChunk, ProtocolViolation, integer

if TYPE_CHECKING:
    from logging import Logger

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer

    from .adapter import MAAdapter, PCMSource, PlaybackContext

type SendControl = Callable[[str, object], Awaitable[None]]
type SendAudio = Callable[[bytes], Awaitable[None]]
type CommandPayload = SetMedia | VolumeCommand | MuteCommand | StateRequest | StreamCommand


@dataclass(slots=True)
class StreamRun:
    """Immutable playback context with bounded attempt-local processing state."""

    descriptor: StreamDescriptor
    context: PlaybackContext
    encoder: ChunkEncoder
    pacing: PCMPacing = field(default_factory=PCMPacing)
    source: PCMSource | None = None
    owned_buffer: AudioBuffer | None = None
    accepted: bool = False
    source_attempt_id: int = 0
    tail: bytes = b""
    error: ProtocolError | None = None
    loaded: bool = False
    ended: bool = False
    output_revision: int = 0
    position_frame: int = 0
    position_validated: bool = False
    incident: RecoveryIncident | None = None


@dataclass(slots=True)
class PendingCommand:
    """Exactly one unresolved command with immutable epoch, ID and payload."""

    epoch: str
    kind: str
    payload: CommandPayload
    command_id: int
    result: asyncio.Future[CommandAck]


@dataclass(slots=True)
class TransportLogWindow:
    """Bounded per-device diagnostic counters and independent rate-limit timestamps."""

    summary_at: float | None = None
    chunks_sent: int = 0
    bytes_sent: int = 0
    acks: int = 0
    reports: int = 0
    max_send_millis: float = 0.0
    warning_at: float | None = None
    slow_sends: int = 0


class MashinStreamSession:
    """Coordinate one bound player's commands, receipt and actual output reports."""

    def __init__(  # noqa: PLR0915
        self,
        adapter: MAAdapter,
        device_id: str,
        profile: AudioProfileId,
        publish: Callable[[PlayerState, PlaybackContext | None], None],
        local_stop: Callable[[], Awaitable[None]],
        fallback: Callable[[int, bool], Awaitable[None]],
        spawn: Callable[[Awaitable[None]], asyncio.Task[None]],
    ) -> None:
        """Capture the MA boundary callbacks without opening audio sources."""
        self.adapter = adapter
        self.device_id = device_id
        self.profile = profile
        self.publish = publish
        self.local_stop = local_stop
        self.fallback = fallback
        self.spawn = spawn
        self.buffer = DeliveryBuffer()
        self.current: StreamRun | None = None
        self.next: StreamRun | None = None
        self.media_revision = 0
        self.activation_target: PlaybackContext | None = None
        self.activation: PreparedActivation | None = None
        self.generation = 0
        self.epoch: str | None = None
        self.sequence = 0
        self.capacity_after_order = 0
        self.command_counter = 0
        self.audio_counter = 0
        self.pending: PendingCommand | None = None
        self.revoking = False
        self.send_control: SendControl | None = None
        self.send_audio: SendAudio | None = None
        self.close_control: Callable[[], Awaitable[None]] | None = None
        self.close_audio: Callable[[], Awaitable[None]] | None = None
        self.wake = asyncio.Event()
        self.sender_wake = asyncio.Event()
        self.producer: asyncio.Task[None] | None = None
        self.sender: asyncio.Task[None] | None = None
        self.setup_timer: asyncio.Task[None] | None = None
        self.capacity_timer: asyncio.Task[None] | None = None
        self.ack_timer: asyncio.Task[None] | None = None
        self.binding = ""
        self.binding_issued = 0.0
        self.binding_consumed = True
        self.reconnect_request_id: str | None = None
        self.output_state = "idle"
        self.volume_percent = 100
        self.is_muted = False
        self.tombstones: deque[str] = deque(maxlen=32)
        self.rate = RecoveryRateLimit()
        self.closed = False
        self.suppress_stop_command = False
        self.recovery_fallback: RecoveryIncident | None = None
        self.recovery_pacing: PCMPacing | None = None
        self.recovery_pause = False
        self.reissue_revocation = False
        self.reissue_media: SetMedia | None = None
        self.profile_restart_run: StreamRun | None = None
        self.profile_restart_paused = False
        self.last_profile_change: tuple[SetAudioProfile, AudioProfileChanged] | None = None
        self.transport_log = TransportLogWindow()

    @property
    def logger(self) -> Logger:
        """Use the player's current standard MA provider logger."""
        return self.adapter.player.logger

    def start_workers(self) -> None:
        """Start exactly one sequential producer and one stop-and-wait sender."""
        self.producer = self.spawn(self._produce())
        self.sender = self.spawn(self._deliver())

    async def connect(self, hello: Hello, send: SendControl, server_revision: str) -> Welcome:
        """Reconcile local state before establishing a new control epoch."""
        hello.capabilities.validate()
        if hello.device_id != self.device_id:
            raise ProtocolViolation("forbidden", "Device is not bound to this provider")
        await self._reconcile(hello.resume_state)
        self.profile = hello.audio_profile
        self.last_profile_change = None
        self.logger.debug(
            "Client connected to player %s with audio profile %s",
            self.adapter.player.player_id,
            self.profile.value,
        )
        self.epoch = str(uuid4())
        self.sequence = self.command_counter = self.audio_counter = 0
        self.capacity_after_order = 0
        self.send_control = send
        self.send_audio = None
        self.buffer.disconnect()
        self.reconnect_request_id = None
        self.binding = self._issue_binding()
        return Welcome(
            self.adapter.player.player_id,
            "0.1.0",
            server_revision,
            ProtocolLimits(),
            list(AudioProfileId),
            self.profile,
            self.binding,
            self.snapshot(self.current),
            "idle"
            if self.current is None
            else ("paused" if self.output_state == "paused" else "playing"),
            self.media_revision - int(self.reissue_revocation or self.reissue_media is not None),
            self.snapshot(self.next),
        )

    async def after_welcome(self) -> None:
        """Reissue a still-authorized complete state using fresh command authority."""
        if self.reissue_media is not None:
            payload, self.reissue_media = self.reissue_media, None
            await self.command("set_media", replace(payload, command_id=self._command_id()))
            return
        if self.activation_target is not None:
            await self._send_activation()
            return
        if self.reissue_revocation and self.current is not None and self.next is not None:
            self.reissue_revocation = False
            await self._announce(revoke_next=True, advance=False)

    def bind_audio(self, hello: AudioHello, send: SendAudio) -> None:
        """Consume the binding and monotonically claim an audio connection atomically."""
        if (
            self.epoch is None
            or hello.device_id != self.device_id
            or self.binding_consumed
            or not secrets.compare_digest(hello.audio_binding_id, self.binding)
            or time.monotonic() - self.binding_issued >= 30
            or hello.audio_connection_id <= self.audio_counter
        ):
            raise ProtocolViolation("forbidden", "Invalid or expired audio binding")
        self.audio_counter = hello.audio_connection_id
        self.binding_consumed = True
        self._cursors(hello.received_streams)
        self.buffer.bind(hello.audio_connection_id)
        self.send_audio = send
        self._cancel_timer(self.ack_timer)

    def audio_ready(self) -> None:
        """Start the capacity response deadline only after audio_ready was sent."""
        self.buffer.ready = True
        self._capacity_deadline()

    async def load(self, context: PlaybackContext, *, autoplay: bool = True) -> None:
        """Replace A/B with a fresh authorized MA generation and PCM origin."""
        successor = self.next
        if (
            autoplay
            and self.current is not None
            and successor is not None
            and successor.accepted
            and successor.error is None
            and not self.revoking
            and successor.context.queue_id == context.queue_id
            and successor.context.queue_item_id == context.queue_item_id
            and successor.context.seek_seconds == context.seek_seconds
            and successor.context.source_identity == context.source_identity
            and successor.context.item is context.item
            and getattr(successor.context, "processing_identity", None)
            == getattr(context, "processing_identity", None)
            and successor.descriptor.output_format.profile_id == self.profile
            and self.adapter.valid(context)
        ):
            self.activation_target = context
            self.buffer.load_accepted = False
            await self._close_run(successor, release=True)
            if (
                self.next is not successor
                or self.activation_target is not context
                or not self.adapter.valid(context)
            ):
                raise ProtocolViolation("stale_playback", "A newer operation replaced activation")
            successor.context = context
            successor.loaded = False
            if self.buffer.streams[successor.descriptor.stream_id].final_index is None:
                successor.incident = successor.incident or RecoveryIncident()
            await self._send_activation()
            return
        if self.recovery_fallback is not None and self.recovery_pause:
            autoplay = False
        if (
            self.profile_restart_run is not None
            and self.profile_restart_paused
            and context.queue_item_id == self.profile_restart_run.context.queue_item_id
            and context.queue_id == self.profile_restart_run.context.queue_id
        ):
            autoplay = False
        retained = None
        if successor is not None and self.current is not None:
            retained = self.adapter.reauthorize_successor(successor.context, context)
            if retained is not None and (
                successor.context.queue_item_id == context.queue_item_id
                or successor.descriptor.output_format.profile_id != self.profile
                or successor.error is not None
                or not successor.accepted
            ):
                retained = None
        saved_stream = None
        if retained is not None and successor is not None:
            await self._close_run(successor, release=True)
            if not self.adapter.valid(context):
                raise ProtocolViolation("stale_playback", "MA load changed during reauthorization")
            successor.context = retained
            successor.loaded = False
            saved_stream = self.buffer.streams.pop(successor.descriptor.stream_id)
            self.next = None
            if saved_stream.final_index is None:
                successor.incident = successor.incident or RecoveryIncident()
        self.generation = integer(self.generation + 1, 1)
        generation = self.generation
        await self.reset()
        if self.generation != generation:
            raise ProtocolViolation("stale_playback", "A newer MA load replaced this request")
        self.current = self._run(context)
        self.logger.debug(
            "Registering stream %s for player %s with audio profile %s",
            self.current.descriptor.stream_id,
            self.adapter.player.player_id,
            self.profile.value,
        )
        if self.recovery_fallback is not None:
            self.current.incident = self.recovery_fallback
            if self.recovery_pacing is not None:
                self.current.pacing = self.recovery_pacing
        self.buffer.register(
            self.current.descriptor.stream_id, self.current.descriptor.output_format.sample_rate_hz
        )
        if saved_stream is not None:
            assert successor is not None
            self.next = successor
            self.buffer.streams[successor.descriptor.stream_id] = saved_stream
        await self._announce(autoplay=autoplay)

    async def prepare(self, context: PlaybackContext) -> None:
        """Register exactly the MA-authorized next occurrence without fetching PCM."""
        if self.pending is not None:
            raise ProtocolViolation("source_busy", "A playback command is unresolved")
        if self.current is None or not self.adapter.valid(
            context, successor_of=self.current.context.queue_item_id
        ):
            raise ProtocolViolation("stale_playback", "Not the authorized MA successor")
        if self.next and self.next.context == context and not self.revoking:
            return
        current = self.current
        if self.next is not None:
            await self.clear_next()
        if self.current is None or self.current is not current:
            raise ProtocolViolation("stale_playback", "Current playback changed during enqueue")
        self.next = self._run(context)
        self.buffer.register(
            self.next.descriptor.stream_id, self.next.descriptor.output_format.sample_rate_hz
        )
        await self._announce()

    async def clear_next(self) -> None:
        """Freeze B delivery immediately and retain its authority until conflict resolution."""
        if self.current is None or self.next is None or self.revoking:
            return
        if self.pending is not None:
            raise ProtocolViolation("source_busy", "A playback command is unresolved")
        self.revoking = True
        await self._announce(revoke_next=True)

    async def control(self, kind: str, value: int | bool | None = None) -> None:
        """Send MA transport/device commands without optimistic player-state updates."""
        if kind == "stop" and self.pending is not None:
            self._resolve_pending("rejected")
        identifier = self._command_id()
        if kind == "set_volume":
            payload: CommandPayload = VolumeCommand(identifier, integer(value, 0, 100))
        elif kind == "set_mute":
            if type(value) is not bool:
                raise ProtocolViolation("invalid_message", "Expected mute boolean")
            payload = MuteCommand(identifier, value)
        elif kind == "state_request":
            payload = StateRequest(identifier)
        else:
            payload = StreamCommand(
                identifier, self.current.descriptor.stream_id if self.current else None
            )
        await self.command(kind, payload)

    async def command(self, kind: str, payload: CommandPayload) -> None:
        """Wait for an ACK without holding a lock needed by incoming reports."""
        if self.send_control is None or self.epoch is None:
            raise ProtocolViolation("stream_unavailable", "Control is disconnected")
        if self.pending is not None:
            raise ProtocolViolation("source_busy", "A player command is still unresolved")
        identifier = payload.command_id
        pending = PendingCommand(
            self.epoch, kind, payload, identifier, asyncio.get_running_loop().create_future()
        )
        self.pending = pending
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Sending command %s id=%s for player %s",
            kind,
            identifier,
            self.adapter.player.player_id,
        )
        try:
            async with asyncio.timeout(5):
                await self.send_control(kind, payload)
                result = await asyncio.shield(pending.result)
            if result.status == "rejected":
                raise ProtocolViolation(
                    result.error.code if result.error else "command_failed",
                    "Client rejected command",
                )
        except TimeoutError as err:
            if self.pending is pending and self.epoch == pending.epoch:
                await self.control_lost()
                if self.close_control is not None:
                    await self.close_control()
            raise ProtocolViolation(
                "command_failed", "Command outcome requires reconciliation"
            ) from err

    async def report(self, report: object, *, received_order: int | None = None) -> None:
        """Process reports in their control order with one sequence high-water mark."""
        if isinstance(report, CommandAck):
            await self._command_ack(report, received_order=received_order)
            return
        sequence = getattr(report, "sequence", None)
        if sequence is None:
            raise ProtocolViolation("invalid_message", "Unexpected client report")
        if sequence <= self.sequence:
            return
        self.sequence = sequence
        if isinstance(report, BufferStatus):
            if received_order is not None and received_order <= self.capacity_after_order:
                return
            if (
                report.audio_connection_id != self.buffer.audio_connection_id
                or not self.buffer.ready
            ):
                return
            if self.current is not None and not self.current.accepted:
                return
            self.buffer.update_capacity(report.audio_connection_id, report.buffer_capacity)
            self._cancel_timer(self.capacity_timer)
            self._cancel_timer(self.setup_timer)
            self._log_transport(status=True)
        elif isinstance(report, ChunkAck):
            await self._chunk_ack(report)
        elif isinstance(report, PlayerState):
            if self.current is not None and (
                not self.current.accepted
                or (received_order is not None and received_order <= self.capacity_after_order)
            ):
                return
            await self._player_state(report)
        elif isinstance(report, TransportStatus):
            run = self._find(report.stream_id)
            if run is not None:
                self._log_transport(status=True)
            if run and report.error and not report.error.is_retryable:
                await self._fail(run, report.error)
        elif isinstance(report, TrackEnded):
            self._ended(report.stream_id)
        elif isinstance(report, TrackStarted):
            self._promote(report)
        elif isinstance(report, PreparedActivated):
            self._accept_activation(
                report.activation, report.output_revision, report.position_frame
            )
            await self._player_state(
                PlayerState(
                    report.sequence,
                    report.activation.stream_id,
                    report.output_revision,
                    report.output_state,
                    report.position_frame,
                    self.volume_percent,
                    self.is_muted,
                    None,
                )
            )
        elif isinstance(report, AudioReconnectRequest):
            await self._reconnect(report)
        elif isinstance(report, SetAudioProfile):
            await self._change_profile(report)
        self.notify()

    def snapshot(self, run: StreamRun | None) -> StreamSnapshot | None:
        """Return retained stream metadata without codecbytes or source URLs."""
        if run is None:
            return None
        stream = self.buffer.streams[run.descriptor.stream_id]
        return StreamSnapshot(
            run.descriptor,
            stream.next_received,
            stream.next_produced,
            stream.final_index is not None,
            stream.final_frame_count,
            run.error,
        )

    async def audio_lost(self) -> None:
        """Close connection-local gates while retaining chunks, source and cursors."""
        self.send_audio = None
        self.buffer.disconnect()
        self._cancel_timer(self.ack_timer)
        self._cancel_timer(self.capacity_timer)
        self.notify()

    async def control_lost(self) -> None:
        """Retain the one pending context command for next-handshake reconciliation."""
        self.send_control = None
        await self.audio_lost()

    async def reset(self) -> None:
        """Invalidate A/B atomically before releasing their processing handles."""
        runs = [run for run in (self.current, self.next) if run]
        self.current = self.next = None
        self.activation_target = None
        self.activation = None
        self.revoking = False
        self.reissue_revocation = False
        self.reissue_media = None
        old_buffer = self.buffer
        self.buffer = DeliveryBuffer()
        if old_buffer.audio_connection_id is not None:
            self.buffer.bind(old_buffer.audio_connection_id)
            self.buffer.ready = old_buffer.ready
        self.output_state = "idle"
        if self.pending:
            if not self.pending.result.done():
                self.pending.result.set_result(
                    CommandAck(
                        self.pending.command_id,
                        "rejected",
                        safe_error("stale_playback", "Playback replaced"),
                    )
                )
            self.pending = None
        self.notify()
        for run in runs:
            await self._close_run(run, release=True)

    async def shutdown(self) -> None:
        """Boundedly stop all workers, timers and processing chains."""
        self.closed = True
        self.notify()
        tasks = [
            task
            for task in (
                self.producer,
                self.sender,
                self.setup_timer,
                self.capacity_timer,
                self.ack_timer,
            )
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.reset()

    def notify(self) -> None:
        """Wake each independent worker once when its enabling state changes."""
        self.wake.set()
        self.sender_wake.set()

    def _run(self, context: PlaybackContext) -> StreamRun:
        profile = self.profile
        opus = profile != AudioProfileId.FLAC
        output = OutputFormat(
            profile,
            "ogg" if opus else "flac",
            "opus" if opus else "flac",
            48000,
            None if opus else 16,
            2,
            int(profile.value.removeprefix("opus_").removesuffix("k")) * 1000 if opus else None,
            opus,
        )
        descriptor = StreamDescriptor(
            str(uuid4()),
            PlaybackIdentity(
                self.adapter.player.player_id,
                context.queue_id,
                context.queue_item_id,
                context.session_id,
                self.generation,
                str(uuid4()),
            ),
            output,
            context.seek_seconds * 1000,
            int(context.item.duration * 1000) if context.item.duration is not None else None,
        )
        return StreamRun(descriptor, context, ChunkEncoder(output))

    def _log_transport(
        self,
        *,
        sent_bytes: int = 0,
        send_millis: float = 0.0,
        acknowledged: bool = False,
        status: bool = False,
    ) -> None:
        window = self.transport_log
        if sent_bytes and send_millis >= 500:
            window.slow_sends += 1
            now = time.monotonic()
            if window.warning_at is None or now - window.warning_at >= 30:
                self.logger.warning(
                    "Slow Audio send for player %s: %.1f ms bytes=%s; %s send(s) >=500 ms since last report",
                    self.adapter.player.player_id,
                    send_millis,
                    sent_bytes,
                    window.slow_sends,
                )
                window.warning_at = now
                window.slow_sends = 0
        if not self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            window.summary_at = None
            window.chunks_sent = window.bytes_sent = 0
            window.acks = window.reports = 0
            window.max_send_millis = 0.0
            return
        now = time.monotonic()
        if window.summary_at is None:
            window.summary_at = now
        window.chunks_sent += int(sent_bytes > 0)
        window.bytes_sent += sent_bytes
        window.acks += int(acknowledged)
        window.reports += int(status)
        window.max_send_millis = max(window.max_send_millis, send_millis)
        if now - window.summary_at < 5:
            return
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Transport summary player=%s chunks=%s bytes=%s acks=%s reports=%s "
            "slots=%s retained_bytes=%s capacity_bytes=%s capacity_confirmed=%s "
            "waiting_ack=%s retry_blocked=%s max_send_ms=%.1f",
            self.adapter.player.player_id,
            window.chunks_sent,
            window.bytes_sent,
            window.acks,
            window.reports,
            self.buffer.occupied_slots,
            self.buffer.reserved_bytes,
            self.buffer.capacity,
            self.buffer.capacity_confirmed,
            self.buffer.outstanding is not None,
            self.buffer.retry_blocked,
            window.max_send_millis,
        )
        window.summary_at = now
        window.chunks_sent = window.bytes_sent = 0
        window.acks = window.reports = 0
        window.max_send_millis = 0.0

    async def _change_profile(self, request: SetAudioProfile) -> None:
        if self.send_control is None:
            raise ProtocolViolation("stream_unavailable", "Control is disconnected")
        send, epoch = self.send_control, self.epoch
        if (
            self.last_profile_change is not None
            and self.last_profile_change[0].request_id == request.request_id
        ):
            previous, response = self.last_profile_change
            if previous.audio_profile != request.audio_profile:
                raise ProtocolViolation("invalid_message", "Profile request ID was reused")
            await self.send_control("audio_profile_changed", response)
            return
        previous_profile, run = self.profile, self.current
        error: ProtocolError | None = None
        try:
            await self._apply_profile(request.audio_profile)
            if self.epoch != epoch:
                return
            if self.profile != previous_profile or self.current is not run:
                self.logger.info(
                    "Audio profile for player %s changed to %s",
                    self.adapter.player.player_id,
                    self.profile.value,
                )
        except asyncio.CancelledError:
            if self.epoch == epoch and self.current is run:
                self.profile = previous_profile
            raise
        except (
            ProtocolViolation,
            MusicAssistantError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as err:
            if self.epoch != epoch:
                return
            if self.current is run:
                self.profile = previous_profile
            code = err.code if isinstance(err, ProtocolViolation) else "stream_failed"
            error = safe_error(code, "Audio profile change failed")
            log = self.logger.debug if isinstance(err, ProtocolViolation) else self.logger.warning
            log(
                "Audio profile change rejected for player %s: %s",
                self.adapter.player.player_id,
                code,
            )
        finally:
            if self.profile_restart_run is run:
                self.profile_restart_run = None
                self.profile_restart_paused = False
        if self.epoch != epoch or self.send_control is not send:
            return
        response = AudioProfileChanged(
            request.request_id,
            "rejected" if error else "accepted",
            self.profile,
            self.current.descriptor.stream_id if self.current else None,
            error,
        )
        self.last_profile_change = request, response
        await send("audio_profile_changed", response)

    async def _apply_profile(self, profile: AudioProfileId) -> None:
        if self.pending is not None or self.revoking:
            raise ProtocolViolation("source_busy", "A playback command is unresolved")
        run, epoch = self.current, self.epoch
        restart = (
            run is not None and not run.ended and run.descriptor.output_format.profile_id != profile
        )
        if restart and run is not None:
            if (
                not run.position_validated
                or run.error is not None
                or not self.adapter.valid(run.context)
            ):
                raise ProtocolViolation("stale_playback", "No validated current playback position")
            self.profile_restart_run = run
            self.profile_restart_paused = self.output_state == "paused"
        self.profile = profile
        if not restart or run is None:
            return
        position = (
            run.context.seek_seconds
            + run.position_frame // run.descriptor.output_format.sample_rate_hz
        )
        async with asyncio.timeout(10):
            await self.fallback(position, self.profile_restart_paused)
        if self.epoch != epoch:
            return
        current = self.current
        if (
            current is None
            or current is run
            or not current.accepted
            or current.context.queue_item_id != run.context.queue_item_id
            or current.context.queue_id != run.context.queue_id
            or current.context.seek_seconds != position
            or not self.adapter.valid(current.context)
            or current.descriptor.output_format.profile_id != profile
        ):
            raise ProtocolViolation("stale_playback", "Profile restart was replaced")

    def _command_id(self) -> int:
        self.command_counter = integer(self.command_counter + 1, 1, MAX_INTEGER - 1)
        return self.command_counter

    async def _announce(
        self,
        *,
        autoplay: bool | None = None,
        revoke_next: bool = False,
        advance: bool = True,
    ) -> None:
        if advance:
            self.media_revision = integer(self.media_revision + 1, 1)
        current = self.current
        target = self.next if self.activation_target is not None else current
        prepared = None if revoke_next or self.activation_target is not None else self.next
        initial: Literal["playing", "paused"] = (
            "paused"
            if autoplay is False or (autoplay is None and self.output_state == "paused")
            else "playing"
        )
        self.buffer.load_accepted = False
        await self.command(
            "set_media",
            SetMedia(
                self._command_id(),
                self.media_revision,
                target.descriptor if target else None,
                prepared.descriptor if prepared else None,
                initial,
            ),
        )

    async def _send_activation(self) -> None:
        if self.activation_target is None or self.current is None or self.next is None:
            return
        if not self.adapter.valid(self.activation_target):
            raise ProtocolViolation("stale_playback", "Prepared activation is no longer authorized")
        if self.send_control is None:
            return
        try:
            await self._announce()
        except ProtocolViolation as err:
            if err.code != "stream_unavailable" and not (
                err.code == "command_failed" and self.pending is not None
            ):
                raise
        except ConnectionError, OSError:
            await self.control_lost()

    def _accept_activation(
        self, activation: PreparedActivation, output_revision: int, position_frame: int
    ) -> None:
        if self.current is not None and self.current.descriptor.stream_id == activation.stream_id:
            if self.activation != activation:
                raise ProtocolViolation("stale_playback", "Activation receipt changed")
            return
        if (
            self.current is None
            or activation.previous_stream_id != self.current.descriptor.stream_id
            or activation.previous_position_frame
            > self.buffer.streams[activation.previous_stream_id].produced_frames
        ):
            raise ProtocolViolation("stale_playback", "Activation does not match current stream")
        reauthorized = self.activation_target is not None
        self._promote(
            TrackStarted(
                1,
                activation.stream_id,
                activation.media_revision,
                output_revision,
                position_frame,
            ),
            activation=activation,
        )
        self.activation = activation
        self.activation_target = None
        self.buffer.load_accepted = True
        run = self.current
        assert run is not None
        if reauthorized and self.buffer.streams[activation.stream_id].next_received > 1:
            run.loaded = True
            self.adapter.loaded(run.context)

    def _find(self, identifier: str) -> StreamRun | None:
        return next(
            (
                run
                for run in (self.current, self.next)
                if run and run.descriptor.stream_id == identifier
            ),
            None,
        )

    def _active(self, run: StreamRun, attempt: int | None = None) -> bool:
        return (
            not self.closed
            and self.activation_target is None
            and self._find(run.descriptor.stream_id) is run
            and run.error is None
            and (attempt is None or run.source_attempt_id == attempt)
            and self.adapter.valid(run.context)
            and (run is not self.next or not self.revoking)
        )

    def _selected(self) -> StreamRun | None:
        if self.current is None or not self.current.accepted or not self._active(self.current):
            return None
        stream = self.buffer.streams[self.current.descriptor.stream_id]
        if stream.final_index is None or stream.next_received <= stream.final_index:
            return self.current
        if self.next is not None and self.next.accepted and self._active(self.next):
            if self.adapter.valid(
                self.next.context, successor_of=self.current.context.queue_item_id
            ):
                return self.next
        return None

    def _cursors(
        self,
        cursors: list[ClientStreamCursor],
        *,
        promotion: bool = False,
        activation: bool = False,
    ) -> None:
        identifiers = {cursor.stream_id for cursor in cursors}
        expected = set(self.buffer.streams)
        if promotion and self.current is not None:
            old = self.buffer.streams[self.current.descriptor.stream_id]
            if not activation and (old.final_index is None or old.next_received <= old.final_index):
                raise ProtocolViolation("invalid_message", "Resume promotion lacks final A receipt")
            expected.remove(self.current.descriptor.stream_id)
        if identifiers != expected or len(identifiers) != len(cursors):
            raise ProtocolViolation("cache_unavailable", "Registered playback context was lost")
        for cursor in cursors:
            stream = self.buffer.streams[cursor.stream_id]
            if (
                not stream.next_received
                <= cursor.next_chunk_index
                <= min(stream.next_produced, stream.highest_offered + 1)
            ):
                raise ProtocolViolation("cache_unavailable", "Invalid retained receipt cursor")
        for cursor in cursors:
            self.buffer.resume(cursor.stream_id, cursor.next_chunk_index)

    def _reconcile_activation(self, state: ClientResumeState) -> bool:
        """Apply a manual receipt, returning whether A awaits a new activation command."""
        if state.activation is not None:
            if state.current_stream_id != state.activation.stream_id:
                raise ProtocolViolation(
                    "stale_playback", "Resume activation has a different current stream"
                )
            if self.next is not None and state.current_stream_id == self.next.descriptor.stream_id:
                self._cursors(state.received_streams, promotion=True, activation=True)
            self._accept_activation(state.activation, state.output_revision, state.position_frame)
        if (
            self.activation_target is not None
            and self.current is not None
            and state.current_stream_id == self.current.descriptor.stream_id
        ):
            if (
                not self.adapter.valid(self.activation_target)
                or state.media_revision != self.media_revision - 1
            ):
                raise ProtocolViolation(
                    "stale_playback", "Pending activation is no longer authorized"
                )
            self._cursors(state.received_streams)
            if self.pending is not None and isinstance(self.pending.payload, SetMedia):
                self.reissue_media = self.pending.payload
            self._resolve_pending("accepted")
            self.volume_percent, self.is_muted = state.volume_percent, state.is_muted
            self.output_state = state.output_state
            return True
        return False

    async def _reconcile(self, state: ClientResumeState | None) -> None:  # noqa: PLR0915
        if state is None:
            if self.current is not None or self.pending is not None:
                raise ProtocolViolation("cache_unavailable", "An explicit new MA load is required")
            return
        if state.current_stream_id is None:
            if self.current is not None or self.pending is not None:
                self.suppress_stop_command = True
                try:
                    await self.local_stop()
                finally:
                    self.suppress_stop_command = False
                await self.reset()
            self.volume_percent, self.is_muted = state.volume_percent, state.is_muted
            return
        if self._reconcile_activation(state):
            return
        pending = self.pending
        was_revoking = self.revoking
        promotion = (
            self.next is not None and state.current_stream_id == self.next.descriptor.stream_id
        )
        retained_revocation = (
            self.revoking
            and not promotion
            and self.next is not None
            and state.media_revision == self.media_revision - 1
            and any(
                cursor.stream_id == self.next.descriptor.stream_id
                for cursor in state.received_streams
            )
        )
        expected_revision = self.media_revision - int(
            self.revoking and (promotion or retained_revocation)
        )
        if state.media_revision != expected_revision:
            raise ProtocolViolation("stale_playback", "Resume has a different successor revision")
        if pending and self.revoking and isinstance(pending.payload, SetMedia):
            if (
                pending.payload.current is not None
                and state.current_stream_id == pending.payload.current.stream_id
                and state.media_revision == pending.payload.media_revision
            ):
                if self.next and all(
                    cursor.stream_id != self.next.descriptor.stream_id
                    for cursor in state.received_streams
                ):
                    await self._remove_next()
        candidate = self.next if promotion else self.current
        if (
            candidate is None
            or state.current_stream_id != candidate.descriptor.stream_id
            or not self.adapter.valid(candidate.context)
        ):
            raise ProtocolViolation("stale_playback", "Cannot reconcile current playback identity")
        if pending and pending.kind == "set_media" and not (promotion and was_revoking):
            if (
                not isinstance(pending.payload, SetMedia)
                or pending.payload.current != candidate.descriptor
            ):
                raise ProtocolViolation("stale_playback", "Pending load identity changed")
        self._cursors(state.received_streams, promotion=promotion)
        if pending and pending.kind == "set_media":
            assert self.current is not None
            self.current.accepted = True
            if self.next:
                self.next.accepted = True
            self.buffer.load_accepted = True
        if promotion:
            self._promote(
                TrackStarted(
                    1,
                    state.current_stream_id,
                    state.media_revision,
                    state.output_revision,
                    state.position_frame,
                ),
                resume=True,
            )
        assert self.current is not None
        await self._player_state(
            PlayerState(
                1,
                state.current_stream_id,
                state.output_revision,
                state.output_state,
                state.position_frame,
                state.volume_percent,
                state.is_muted,
                None,
            )
        )
        stream = self.buffer.streams[self.current.descriptor.stream_id]
        if (
            state.output_state in ("ended", "buffering")
            and state.position_frame == stream.final_frame_count
        ):
            self._ended(state.current_stream_id)
        if pending and was_revoking and (self.revoking or promotion):
            if not promotion and not retained_revocation:
                raise ProtocolViolation("cache_unavailable", "Successor revocation is unresolved")
            self.reissue_revocation = retained_revocation
            self.revoking = retained_revocation
            self._resolve_pending("rejected")
        else:
            self._resolve_pending("accepted")

    def _resolve_pending(self, status: str) -> None:
        pending, self.pending = self.pending, None
        if pending is not None and not pending.result.done():
            error = (
                safe_error("stale_playback", "Previous command no longer applies")
                if status == "rejected"
                else None
            )
            pending.result.set_result(
                CommandAck(pending.command_id, "rejected" if error else "accepted", error)
            )

    async def _command_ack(self, report: CommandAck, *, received_order: int | None = None) -> None:
        pending = self.pending
        if pending is None:
            if report.command_id > self.command_counter:
                raise ProtocolViolation("invalid_message", "Command ID was never issued")
            return
        if report.command_id != pending.command_id:
            if report.command_id > self.command_counter:
                raise ProtocolViolation("invalid_message", "Command ID was never issued")
            return
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Command ACK %s id=%s status=%s for player %s",
            pending.kind,
            report.command_id,
            report.status,
            self.adapter.player.player_id,
        )
        if report.status == "accepted":
            if pending.kind == "set_media" and self.current:
                self.current.accepted = True
                if self.next is not None and not self.revoking:
                    self.next.accepted = True
                self.buffer.accept_load()
                self.capacity_after_order = received_order or 0
                if self.buffer.ready:
                    self._capacity_deadline()
            if pending.kind == "set_media" and self.revoking:
                run, self.next = self.next, None
                self.revoking = False
                if run is not None:
                    self.buffer.discard(run.descriptor.stream_id)
                    self.spawn(self._close_run(run, release=True))
        elif pending.kind == "set_media" and not self.revoking:
            run = self.current
            if run is not None:
                run.error = report.error or safe_error(
                    "command_failed", "Client rejected playback context"
                )
                self.buffer.streams[run.descriptor.stream_id].terminal = True
                self.spawn(self._close_run(run, release=True))
        self.pending = None
        if not pending.result.done():
            pending.result.set_result(report)
        self.notify()

    async def _remove_next(self) -> None:
        run, self.next = self.next, None
        self.revoking = False
        if run:
            self.buffer.discard(run.descriptor.stream_id)
            await self._close_run(run, release=True)

    async def _chunk_ack(self, report: ChunkAck) -> None:
        run = self._find(report.stream_id)
        if run is None or (run is self.next and self.revoking):
            return
        accepted = report.status == "accepted"
        resolved = self.buffer.acknowledge(
            report.stream_id,
            report.audio_connection_id,
            report.chunk_index,
            report.next_chunk_index,
            accepted,
            report.buffer_capacity,
        )
        if not resolved:
            return
        self._log_transport(acknowledged=True)
        self._cancel_timer(self.ack_timer)
        if not accepted and report.error and report.error.code != "buffer_capacity_exceeded":
            await self._fail(run, report.error)

    async def _player_state(self, report: PlayerState) -> None:
        if report.stream_id is None:
            if report.output_state != "idle" or report.position_frame or report.output_revision:
                raise ProtocolViolation("invalid_message", "Invalid idle player state")
            if self.output_state != "idle":
                self.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Output state for player %s changed from %s to %s (revision=%s)",
                    self.adapter.player.player_id,
                    self.output_state,
                    "idle",
                    report.output_revision,
                )
            self.volume_percent, self.is_muted = report.volume_percent, report.is_muted
            if self.current is not None:
                self.suppress_stop_command = True
                try:
                    await self.local_stop()
                finally:
                    self.suppress_stop_command = False
                await self.reset()
            self.publish(report, None)
            return
        run = self.current
        if (
            run is None
            or report.stream_id != run.descriptor.stream_id
            or not self.adapter.valid(run.context)
        ):
            return
        stream = self.buffer.streams[report.stream_id]
        if report.output_revision < run.output_revision:
            return
        if report.position_frame > stream.produced_frames:
            raise ProtocolViolation("invalid_message", "Output position exceeds produced audio")
        if (
            report.output_revision == run.output_revision
            and report.position_frame < run.position_frame
        ):
            raise ProtocolViolation(
                "invalid_message", "Output position moved backwards without a seek revision"
            )
        if self.output_state != report.output_state:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Output state for player %s changed from %s to %s (revision=%s)",
                self.adapter.player.player_id,
                self.output_state,
                report.output_state,
                report.output_revision,
            )
        run.output_revision, run.position_frame = report.output_revision, report.position_frame
        run.position_validated = True
        self.output_state = report.output_state
        self.volume_percent, self.is_muted = report.volume_percent, report.is_muted
        self.publish(report, run.context)
        self._log_transport(status=True)
        if report.error is not None and not report.error.is_retryable:
            await self._fail(run, report.error)

    def _ended(self, identifier: str) -> None:
        if identifier in self.tombstones:
            return
        run = self._find(identifier)
        if run is None or run is not self.current or run.error is not None:
            return
        stream = self.buffer.streams[identifier]
        if stream.final_index is None or stream.next_received <= stream.final_index:
            raise ProtocolViolation("invalid_message", "Audible end lacks final receipt")
        run.ended = True
        self.tombstones.append(identifier)

    def _promote(
        self,
        report: TrackStarted,
        *,
        resume: bool = False,
        activation: PreparedActivation | None = None,
    ) -> None:
        if self.current and report.stream_id == self.current.descriptor.stream_id:
            return
        run = self.next
        if run is None or run.descriptor.stream_id != report.stream_id:
            raise ProtocolViolation("stale_playback", "Successor was not authorized")
        expected_revision = self.media_revision - int(
            self.revoking or self.activation_target is not None
        )
        if (
            report.media_revision != expected_revision
            or not run.accepted
            or run.error
            or not self.adapter.valid(run.context)
        ):
            raise ProtocolViolation("stale_playback", "Stale successor promotion")
        stream = self.buffer.streams[report.stream_id]
        if (
            activation is None and stream.next_received <= 1
        ) or report.position_frame > stream.produced_frames:
            raise ProtocolViolation("invalid_message", "Promotion preceded B receipt")
        if report.output_revision < run.output_revision or (
            report.output_revision == run.output_revision
            and report.position_frame < run.position_frame
        ):
            raise ProtocolViolation("invalid_message", "Stale successor output revision")
        if self.current:
            old = self.buffer.streams[self.current.descriptor.stream_id]
            if activation is None and (
                old.final_index is None or old.next_received <= old.final_index
            ):
                raise ProtocolViolation("invalid_message", "Promotion preceded final A receipt")
            if activation is None and not resume and not self.current.ended:
                raise ProtocolViolation("invalid_message", "Promotion preceded A output end")
            self.tombstones.append(self.current.descriptor.stream_id)
            self.buffer.discard(self.current.descriptor.stream_id)
            self.spawn(self._close_run(self.current, release=activation is not None))
        self.current, self.next = run, None
        self.media_revision = max(self.media_revision, report.media_revision)
        self.revoking = self.reissue_revocation = False
        run.position_frame, run.output_revision = report.position_frame, report.output_revision
        run.position_validated = True
        if activation is not None:
            return
        self.output_state = "playing"
        self.publish(
            PlayerState(
                report.sequence,
                report.stream_id,
                report.output_revision,
                "playing",
                report.position_frame,
                self.volume_percent,
                self.is_muted,
                None,
            ),
            run.context,
        )

    async def _reconnect(self, report: AudioReconnectRequest) -> None:
        if self.send_control is None:
            return
        if self.reconnect_request_id == report.request_id:
            if self.binding_consumed or time.monotonic() - self.binding_issued >= 30:
                raise ProtocolViolation("stale_playback", "A new reconnect request is required")
        else:
            await self.audio_lost()
            if self.close_audio is not None:
                await self.close_audio()
            self.reconnect_request_id = report.request_id
            self.binding = self._issue_binding()
        await self.send_control("audio_binding", AudioBinding(report.request_id, self.binding))

    def _issue_binding(self) -> str:
        self.binding_consumed = False
        self.binding_issued = time.monotonic()
        self._cancel_timer(self.setup_timer)
        self.setup_timer = self.spawn(self._deadline(30, "setup"))
        return secrets.token_urlsafe(32)

    def _capacity_deadline(self) -> None:
        self._cancel_timer(self.capacity_timer)
        self.capacity_timer = self.spawn(self._deadline(5, "capacity"))

    async def _deadline(self, seconds: float, kind: str) -> None:
        epoch, binding = self.epoch, self.binding
        await asyncio.sleep(seconds)
        timer = {
            "setup": self.setup_timer,
            "capacity": self.capacity_timer,
            "ack": self.ack_timer,
        }[kind]
        if timer is not asyncio.current_task() or self.epoch != epoch or self.binding != binding:
            return
        reason = {
            "setup": "audio setup incomplete",
            "capacity": "buffer_status not received",
            "ack": "chunk_ack not received",
        }[kind]
        self.logger.warning(
            "Audio response deadline expired for player %s: %s",
            self.adapter.player.player_id,
            reason,
        )
        if kind == "setup":
            self.binding_consumed = True
        await self.audio_lost()
        if self.close_audio is not None:
            await self.close_audio()
        if self.send_control:
            await self.send_control(
                "error",
                ErrorMessage(
                    None,
                    None,
                    self.buffer.audio_connection_id,
                    safe_error(
                        "stream_unavailable",
                        f"Audio response deadline expired: {reason}",
                        retryable=True,
                    ),
                ),
            )

    def _cancel_timer(self, task: asyncio.Task[None] | None) -> None:
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _produce(self) -> None:
        from music_assistant_models.errors import (  # noqa: PLC0415
            AudioError,
            LoginFailed,
            MediaNotFoundError,
        )

        while not self.closed:
            self.wake.clear()
            run = self._selected()
            if run is None or not self.buffer.reserve(run.descriptor.stream_id):
                if run is not None and run.incident is not None:
                    run.incident.wait()
                await self.wake.wait()
                continue
            try:
                if run.incident is not None and run.source is None and not run.incident.fallbacks:
                    if self.buffer.outstanding is not None:
                        self.buffer.reservation = None
                        run.incident.wait()
                        await self.wake.wait()
                        continue
                    await self._repair(run)
                    if not self._active(run):
                        continue
                prepared = await self._read_chunk(run)
                if prepared is None:
                    continue
                pcm, final, payload, attempt = prepared
                if (
                    not self._active(run, attempt)
                    or self.buffer.reservation != run.descriptor.stream_id
                ):
                    continue
                stream = self.buffer.streams[run.descriptor.stream_id]
                chunk = AudioChunk(
                    run.descriptor.stream_id,
                    stream.next_produced,
                    stream.produced_frames,
                    len(pcm) // (run.descriptor.output_format.channels * 4),
                    final,
                    payload,
                )
                self.buffer.commit(chunk)
                run.tail = (run.tail + pcm)[
                    -run.descriptor.output_format.sample_rate_hz
                    * run.descriptor.output_format.channels
                    * 12 :
                ]
                run.encoder.commit_history(pcm)
                run.incident = None
                if final:
                    await self._close_run(run, release=False)
                self.notify()
            except AudioError, TimeoutError, OSError:
                await self._source_lost(run)
            except LoginFailed, MediaNotFoundError:
                if self._find(run.descriptor.stream_id) is run:
                    self.buffer.reservation = None
                    await self._fail(
                        run, safe_error("forbidden", "MA source is not authorized or available")
                    )
            except ProtocolViolation as err:
                if not self._active(run):
                    continue
                if self._find(run.descriptor.stream_id) is run:
                    self.buffer.reservation = None
                    await self._fail(run, safe_error(err.code, str(err)), detail=str(err))
            except MusicAssistantError:
                if self._find(run.descriptor.stream_id) is run:
                    await self._fail(run, safe_error("source_failed", "Unable to open source"))

    async def _source_lost(self, run: StreamRun) -> None:
        if not self._active(run):
            return
        self.buffer.reservation = None
        run.incident = run.incident or RecoveryIncident()
        await self._close_run(run, release=False)
        if run.incident.fallbacks:
            await self._fail(run, safe_error("recovery_failed", "Fallback source failed"))
        else:
            self.notify()

    async def _read_chunk(self, run: StreamRun) -> tuple[bytes, bool, bytes, int] | None:
        if run.incident is not None and not await self._recovery_grant(run):
            return None
        if run.source is None:
            from .adapter import PCMSource  # noqa: PLC0415

            run.source_attempt_id += 1
            source = PCMSource(
                self.adapter, run.context, run.descriptor.output_format, pacing=run.pacing
            )
            run.source = source
            attempt = run.source_attempt_id
            async with asyncio.timeout(run.incident.remaining if run.incident else None):
                await source.open(start_timeout=None if run.incident else 10)
            if not self._active(run, attempt) or run.source is not source:
                await source.close()
                return None
            run.context = source.context
        stream = self.buffer.streams[run.descriptor.stream_id]
        timeout = (
            run.incident.remaining if run.incident else (30 if stream.next_produced > 1 else 10)
        )
        async with asyncio.timeout(timeout):
            source = run.source
            run.context = source.context
            run.owned_buffer = source.owned_buffer
            attempt = run.source_attempt_id
            assert source.reader is not None
            pcm, final = await source.reader.next_chunk()
            payload = await run.encoder.encode(pcm)
        return pcm, final, payload, attempt

    async def _recovery_grant(self, run: StreamRun) -> bool:
        incident = run.incident
        assert incident is not None
        while self._active(run) and self.buffer.reservation == run.descriptor.stream_id:
            self.wake.clear()
            if self.buffer.enabled and self.buffer.reserved_bytes <= self.buffer.capacity:
                incident.start()
                return True
            incident.wait()
            await self.wake.wait()
        return False

    async def _deliver(self) -> None:
        while not self.closed:
            await self.sender_wake.wait()
            self.sender_wake.clear()
            run = self._selected()
            if run is None or self.send_audio is None:
                continue
            chunk = self.buffer.offer(run.descriptor.stream_id)
            if chunk is None:
                continue
            send = self.send_audio
            identifier = self.buffer.audio_connection_id
            self.ack_timer = self.spawn(self._deadline(5, "ack"))
            try:
                encoded = chunk.encode()
                started = time.monotonic()
                await send(encoded)
                if self.send_audio is send:
                    self._log_transport(
                        sent_bytes=len(encoded), send_millis=(time.monotonic() - started) * 1000
                    )
                if (
                    self.buffer.audio_connection_id == identifier
                    and self._active(run)
                    and not run.loaded
                ):
                    run.loaded = True
                    self.adapter.loaded(run.context)
            except ConnectionError, OSError:
                if self.send_audio is send and self.buffer.audio_connection_id == identifier:
                    await self.audio_lost()

    async def _repair(self, run: StreamRun) -> None:  # noqa: PLR0915
        from music_assistant_models.errors import AudioError  # noqa: PLC0415

        incident = run.incident
        assert incident is not None
        if not await self._recovery_grant(run):
            return
        output = run.descriptor.output_format
        stream = self.buffer.streams[run.descriptor.stream_id]
        resume_frame = stream.produced_frames
        self.logger.info(
            "PCM repair started for player %s stream=%s resume_frame=%s",
            self.adapter.player.player_id,
            run.descriptor.stream_id,
            resume_frame,
        )
        while incident.attempts < 2 and incident.remaining > 0:
            if not await self._recovery_grant(run):
                return
            if resume_frame and len(run.tail) != output.sample_rate_hz * output.channels * 12:
                self.logger.debug("PCM repair lacks a complete three-second overlap")
                break
            from .adapter import PCMSource  # noqa: PLC0415

            self.rate.claim()
            incident.attempts += 1
            run.source_attempt_id += 1
            source = PCMSource(self.adapter, run.context, output, pacing=run.pacing)
            run.source = source
            attempt = run.source_attempt_id
            seek = max(
                0,
                (run.context.seek_seconds * output.sample_rate_hz + resume_frame)
                // output.sample_rate_hz
                - 5,
            )
            self.logger.debug(
                "PCM repair attempt %s for player %s stream=%s seek_seconds=%s",
                incident.attempts,
                self.adapter.player.player_id,
                run.descriptor.stream_id,
                seek,
            )
            try:
                async with asyncio.timeout(incident.remaining):
                    await source.open(
                        seek_seconds=seek, repair=bool(resume_frame), start_timeout=None
                    )
                if resume_frame:
                    expected_end = (
                        run.context.seek_seconds - seek
                    ) * output.sample_rate_hz + resume_frame
                    data = bytearray()
                    target = (
                        min(output.sample_rate_hz * 12, expected_end + output.sample_rate_hz)
                        * output.channels
                        * 4
                    )
                    while len(data) < target:
                        if not await self._recovery_grant(run):
                            return
                        if not self._active(run, attempt) or run.source is not source:
                            raise AudioError("Mashin repair source was released")  # noqa: TRY301
                        async with asyncio.timeout(incident.remaining):
                            fragment = await source.read(min(16384, target - len(data)))
                        if not fragment:
                            break
                        data.extend(fragment)
                    async with asyncio.timeout(incident.remaining):
                        end = await asyncio.to_thread(
                            match_pcm,
                            run.tail,
                            bytes(data),
                            output.sample_rate_hz,
                            output.channels,
                            expected_end,
                        )
                    suffix = BytesReader(bytes(data[end * output.channels * 4 :]), source.read)
                    source.reader = PCMChunkReader(
                        suffix.read, output.sample_rate_hz, output.channels
                    )
                    run.encoder.history = run.tail[-3840 * output.channels * 4 :]
                    self.logger.info(
                        "PCM repair succeeded for player %s stream=%s attempt=%s alignment_offset_frames=%s; best-effort match",
                        self.adapter.player.player_id,
                        run.descriptor.stream_id,
                        incident.attempts,
                        end - expected_end,
                    )
                else:
                    self.logger.info(
                        "Source reopen succeeded for player %s stream=%s before first chunk; no PCM match required",
                        self.adapter.player.player_id,
                        run.descriptor.stream_id,
                    )
                return
            except ProtocolViolation as err:
                self.logger.debug(
                    "PCM repair attempt %s failed for player %s stream=%s: %s",
                    incident.attempts,
                    self.adapter.player.player_id,
                    run.descriptor.stream_id,
                    err.code,
                )
                terminal = err.code in (
                    "forbidden",
                    "media_unsupported",
                    "stale_playback",
                    "source_failed",
                )
                await self._close_run(run, release=terminal)
                if terminal:
                    self.logger.warning(
                        "PCM repair failed for player %s stream=%s: %s; fallback not permitted",
                        self.adapter.player.player_id,
                        run.descriptor.stream_id,
                        err.code,
                    )
                    raise
            except (AudioError, TimeoutError, OSError) as err:
                self.logger.debug(
                    "PCM repair attempt %s failed for player %s stream=%s: %s",
                    incident.attempts,
                    self.adapter.player.player_id,
                    run.descriptor.stream_id,
                    type(err).__name__,
                )
                await self._close_run(run, release=False)
        self.logger.warning(
            "PCM repair failed for player %s stream=%s after %s attempt(s); checking bounded MA fallback",
            self.adapter.player.player_id,
            run.descriptor.stream_id,
            incident.attempts,
        )
        if incident.fallbacks or incident.remaining <= 0:
            raise ProtocolViolation("recovery_failed", "Source recovery budget exhausted")
        self.rate.claim()
        incident.fallbacks += 1
        if run is self.next:
            self.logger.info(
                "PCM recovery fallback re-prepares next item for player %s stream=%s",
                self.adapter.player.player_id,
                run.descriptor.stream_id,
            )
            context = run.context
            await self.clear_next()
            await self.prepare(context)
            if self.next:
                self.next.incident = incident
                self.next.pacing = run.pacing
            return
        if not run.position_validated or not self._active(run):
            raise ProtocolViolation("recovery_failed", "No current validated fallback position")
        position = run.context.seek_seconds + run.position_frame // output.sample_rate_hz
        self.recovery_fallback = incident
        self.recovery_pacing = run.pacing
        self.recovery_pause = self.output_state == "paused"
        self.logger.info(
            "PCM recovery MA-seek fallback started for player %s stream=%s position_seconds=%s paused=%s",
            self.adapter.player.player_id,
            run.descriptor.stream_id,
            position,
            self.recovery_pause,
        )
        try:
            async with asyncio.timeout(incident.remaining):
                await self.fallback(position, self.output_state == "paused")
            self.logger.info(
                "PCM recovery MA-seek fallback completed for player %s stream=%s",
                self.adapter.player.player_id,
                run.descriptor.stream_id,
            )
        except (MusicAssistantError, ProtocolViolation, TimeoutError, OSError) as err:
            self.logger.warning(
                "PCM recovery MA-seek fallback failed for player %s stream=%s: %s",
                self.adapter.player.player_id,
                run.descriptor.stream_id,
                type(err).__name__,
            )
            failed = (
                self.current
                if self.current is not None and self.current.incident is incident
                else run
            )
            if self._find(failed.descriptor.stream_id) is failed:
                await self._fail(
                    failed, safe_error("source_failed", "Unable to open fallback source")
                )
            raise ProtocolViolation("source_failed", "Unable to open fallback source") from err
        finally:
            self.recovery_fallback = None
            self.recovery_pacing = None
            self.recovery_pause = False

    async def _close_run(self, run: StreamRun, *, release: bool) -> None:
        run.source_attempt_id += 1
        source, run.source = run.source, None
        owned = (
            source.owned_buffer
            if source is not None and source.owned_buffer is not None
            else run.owned_buffer
        )
        run.owned_buffer = owned
        if source:
            await source.close()
        await run.encoder.close()
        if release:
            run.owned_buffer = None
            details = getattr(run.context.item, "streamdetails", None)
            reclaimed = (
                details is not None
                and details.buffer is owned
                and details.queue_session_id not in (None, run.context.session_id)
            )
            if not reclaimed and (
                self.current is None
                or self.current is run
                or self.current.context.item is not run.context.item
            ):
                await self.adapter.release(owned)

    async def _fail(
        self, run: StreamRun, error: ProtocolError, *, detail: str | None = None
    ) -> None:
        if run.error is not None or self._find(run.descriptor.stream_id) is not run:
            return
        stream = self.buffer.streams[run.descriptor.stream_id]
        self.logger.warning(
            "Stream %s failed on player %s: %s; profile=%s chunk=%s frame=%s; reason=%s",
            run.descriptor.stream_id,
            self.adapter.player.player_id,
            error.code,
            run.descriptor.output_format.profile_id.value,
            stream.next_produced,
            stream.produced_frames,
            detail or "No server-side detail",
        )
        run.error = error
        self.buffer.streams[run.descriptor.stream_id].terminal = True
        if self.buffer.reservation == run.descriptor.stream_id:
            self.buffer.reservation = None
        await self._close_run(run, release=True)
        if run is self.next:
            await self.clear_next()
        if self.send_control:
            try:
                await self.send_control(
                    "error",
                    ErrorMessage(
                        None, run.descriptor.stream_id, self.buffer.audio_connection_id, error
                    ),
                )
            except ConnectionError, OSError:
                self.logger.debug("Stream error retained for disconnected client")


class BytesReader:
    """Bounded verified repair suffix followed by the same live source."""

    def __init__(self, suffix: bytes, read: Callable[[int], Awaitable[bytes]]) -> None:
        """Retain only the unconsumed suffix of one bounded matching attempt."""
        self.suffix = suffix
        self.source_read = read

    async def read(self, size: int) -> bytes:
        """Consume verified suffix before asking the live processing chain."""
        if self.suffix:
            result, self.suffix = self.suffix[:size], self.suffix[size:]
            return result
        return await self.source_read(size)


def safe_error(code: str, message: str, *, retryable: bool = False) -> ProtocolError:
    """Construct one validated safe protocol error without external exception text."""
    return parse_model(
        ProtocolError,
        {
            "code": code,
            "message": message,
            "is_retryable": retryable,
            "retry_after_millis": None,
        },
    )
