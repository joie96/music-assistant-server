"""Session lifecycle tests with an explicit mocked MA boundary."""

from __future__ import annotations

import asyncio
import importlib
import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.providers.mashin.models import (
    AudioHello,
    AudioProfileChanged,
    AudioProfileId,
    BufferStatus,
    ClientCapabilities,
    ClientResumeState,
    ClientStreamCursor,
    CommandAck,
    Hello,
    PlayerState,
    SetAudioProfile,
    SetMedia,
    TrackStarted,
    TransportStatus,
)
from music_assistant.providers.mashin.recovery import RecoveryIncident
from music_assistant.providers.mashin.session import MashinSession, safe_error
from music_assistant.providers.mashin.wire import AudioChunk, ProtocolViolation

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from music_assistant.providers.mashin.adapter import MAAdapter, PlaybackContext


class TestSession(unittest.IsolatedAsyncioTestCase):
    """Observe load barriers, binding consumption and actual player-state authority."""

    async def asyncSetUp(self) -> None:
        """Create callbacks that cannot open real source URLs."""
        self.tasks: list[asyncio.Task[None]] = []
        self.sent = asyncio.Event()
        self.messages: list[tuple[str, object]] = []
        self.adapter = Mock()
        self.adapter.player.player_id = "mashin_test"
        self.adapter.valid.return_value = True
        self.adapter.release = AsyncMock()
        self.publish = Mock()
        self.stop = AsyncMock()
        self.fallback = AsyncMock()
        self.session = MashinSession(
            cast("MAAdapter", self.adapter),
            "device",
            AudioProfileId.FLAC,
            self.publish,
            self.stop,
            self.fallback,
            self.spawn,
        )
        self.context = cast(
            "PlaybackContext",
            SimpleNamespace(
                media=SimpleNamespace(),
                queue_id="mashin_test",
                queue_item_id="item",
                session_id="ma-session",
                seek_seconds=150,
                item=SimpleNamespace(duration=300),
            ),
        )
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        welcome = await self.session.connect(
            Hello("device", "Client", "0.1.0", None, capabilities, AudioProfileId.FLAC),
            self.send,
            "test",
        )
        self.session.bind_audio(AudioHello("device", welcome.audio_binding_id, 1, []), AsyncMock())
        self.session.audio_ready()

    async def asyncTearDown(self) -> None:
        """Release all timers and unfinished commands created by this test."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.session.shutdown()

    def spawn(self, target: Awaitable[None]) -> asyncio.Task[None]:
        """Retain test task ownership through teardown."""
        task = asyncio.ensure_future(target)
        self.tasks.append(task)
        return task

    async def send(self, kind: str, payload: object) -> None:
        """Capture commands and signal their actual send boundary."""
        self.messages.append((kind, payload))
        self.sent.set()

    async def accept_load(self) -> None:
        """Issue a real session load and atomically acknowledge its descriptor."""
        self.sent.clear()
        task = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        assert self.session.pending is not None
        await self.session.report(CommandAck(self.session.pending.command_id, "accepted", None))
        await task

    async def test_idle_report_before_load_ack_cannot_reset_new_stream(self) -> None:
        """Queued idle state cannot revoke a subsequently acknowledged MA load."""
        self.sent.clear()
        task = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        assert self.session.pending is not None
        await self.session.report(
            CommandAck(self.session.pending.command_id, "accepted", None), received_order=10
        )
        await task
        run = self.session.current
        assert run is not None
        await self.session.report(
            PlayerState(1, None, 0, "idle", 0, 100, False, None), received_order=9
        )
        assert self.session.current is run
        self.stop.assert_not_awaited()
        await self.session.report(BufferStatus(2, 1, 262144), received_order=11)
        assert self.session.buffer.capacity_confirmed

    async def test_idle_report_during_pending_load_cannot_reset_new_stream(self) -> None:
        """The client's old idle state remains valid until it applies set_media."""
        self.sent.clear()
        task = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        run = self.session.current
        assert run is not None
        assert self.session.pending is not None
        await self.session.report(
            PlayerState(1, None, 0, "idle", 0, 100, False, None), received_order=9
        )
        assert self.session.current is run
        self.stop.assert_not_awaited()
        await self.session.report(
            CommandAck(self.session.pending.command_id, "accepted", None), received_order=10
        )
        await task
        await self.session.report(
            PlayerState(2, None, 0, "idle", 0, 100, False, None), received_order=11
        )
        self.assertIsNone(self.session.current)
        self.stop.assert_awaited_once()

    async def test_stale_deadlines_cannot_close_replacement_audio(self) -> None:
        """Late timers cannot mutate a newer epoch, binding or timer owner."""
        for change in ("epoch", "binding", "timer"):
            for kind in ("setup", "capacity", "ack"):
                with self.subTest(change=change, kind=kind):
                    entered, release = asyncio.Event(), asyncio.Event()

                    async def sleep(
                        _seconds: float,
                        started: asyncio.Event = entered,
                        finish: asyncio.Event = release,
                    ) -> None:
                        started.set()
                        await finish.wait()

                    self.session.close_audio = AsyncMock()
                    self.session.binding_consumed = False
                    self.session.buffer.ready = True
                    self.messages.clear()
                    with patch("music_assistant.providers.mashin.session.asyncio.sleep", sleep):
                        task = self.spawn(self.session._deadline(5, kind))
                        setattr(self.session, f"{kind}_timer", task)
                        await entered.wait()
                        if change == "epoch":
                            self.session.epoch = str(self.session.epoch) + "new"
                        elif change == "binding":
                            self.session.binding += "new"
                        else:
                            setattr(self.session, f"{kind}_timer", None)
                        release.set()
                        await task
                    self.session.close_audio.assert_not_awaited()
                    assert self.session.buffer.ready
                    assert not self.session.binding_consumed
                    assert not self.messages

    async def test_current_deadline_still_closes_audio(self) -> None:
        """The ownership check must not suppress the current connection timeout."""
        self.session.close_audio = AsyncMock()
        self.messages.clear()
        task = self.spawn(self.session._deadline(0, "capacity"))
        self.session.capacity_timer = task
        await task
        self.session.close_audio.assert_awaited_once()
        assert not self.session.buffer.ready
        assert self.messages[-1][0] == "error"

    async def test_disconnect_retains_source_without_cleanup_timer(self) -> None:
        """Transport loss cannot start a source-closing task, even across long gaps."""
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        source = Mock()
        source.close = AsyncMock()
        run.source = source
        run.owned_buffer = Mock()
        tasks_before = len(self.tasks)
        with patch(
            "music_assistant.providers.mashin.session.time.monotonic", return_value=0
        ) as clock:
            await self.session.audio_lost()
            clock.return_value = 1000
            await self.session.control_lost()
        assert len(self.tasks) == tasks_before
        assert run.source is source
        assert run.incident is None
        source.close.assert_not_awaited()
        self.adapter.release.assert_not_awaited()
        assert not self.session.buffer.enabled

    async def test_actual_source_loss_retains_chunks_and_waits_for_production_grant(self) -> None:
        """Resource loss is repairable, but disconnected transport must not reopen it."""
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        source = Mock()
        source.close = AsyncMock()
        run.source = source
        self.session.buffer.reservation = run.descriptor.stream_id
        chunk = AudioChunk(run.descriptor.stream_id, 1, 0, 48000, False, b"retained")
        self.session.buffer.commit(chunk)
        await self.session.audio_lost()
        await self.session._source_lost(run)
        assert run.incident is not None
        assert self.session.current.source is None
        assert run.error is None
        assert self.session.buffer.streams[run.descriptor.stream_id].chunks[1] is chunk
        assert await self.session._read_chunk(run) is None
        assert run.incident.attempts == 0
        source.close.assert_awaited_once()
        self.fallback.assert_not_awaited()

    async def test_rejected_pcm_repair_uses_pause_safe_seek_fallback(self) -> None:
        """Both failed PCM attempts lead to exactly one validated paused fallback."""
        try:
            module = importlib.import_module("music_assistant.providers.mashin.adapter")
        except ModuleNotFoundError as err:
            if err.name is None or err.name.startswith("music_assistant"):
                raise
            self.skipTest(f"Complete MA runtime required: missing {err.name}")
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        output = run.descriptor.output_format
        self.session.buffer.streams[run.descriptor.stream_id].produced_frames = (
            output.sample_rate_hz * 5
        )
        run.tail = bytes(output.sample_rate_hz * output.channels * 12)
        run.incident = RecoveryIncident()
        run.position_validated = True
        run.position_frame = output.sample_rate_hz * 2
        self.session.output_state = "paused"
        source = Mock()
        source.owned_buffer = None
        source.open = AsyncMock()
        source.read = AsyncMock(side_effect=lambda size: bytes(size))
        source.close = AsyncMock()
        with (
            patch.object(module, "PCMSource", return_value=source),
            patch.object(self.session, "_recovery_grant", AsyncMock(return_value=True)),
        ):
            await self.session._repair(run)
        assert run.incident.attempts == 2
        assert run.incident.fallbacks == 1
        assert source.open.await_count == 2
        assert source.close.await_count == 2
        self.fallback.assert_awaited_once_with(152, True)
        run.incident = RecoveryIncident()
        self.fallback.reset_mock()
        with (
            patch.object(module, "PCMSource", return_value=source),
            patch.object(self.session, "_recovery_grant", AsyncMock(return_value=True)),
            patch(
                "music_assistant.providers.mashin.session.match_pcm",
                return_value=output.sample_rate_hz * 5 + 7,
            ),
        ):
            await self.session._repair(run)
        self.adapter.player.logger.info.assert_any_call(
            "PCM repair succeeded for player %s stream=%s attempt=%s alignment_offset_frames=%s; best-effort match",
            "mashin_test",
            run.descriptor.stream_id,
            1,
            7,
        )
        self.fallback.assert_not_awaited()

    async def test_pcm_repair_logs_start_failure_and_seek_fallback(self) -> None:
        """An exhausted incident announces repair and the successful fallback distinctly."""
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        run.incident = RecoveryIncident(attempts=2)
        run.position_validated = True
        run.position_frame = run.descriptor.output_format.sample_rate_hz * 2
        self.session.output_state = "paused"
        logger = self.adapter.player.logger
        logger.reset_mock()
        with patch.object(self.session, "_recovery_grant", AsyncMock(return_value=True)):
            await self.session._repair(run)
        logger.info.assert_any_call(
            "PCM repair started for player %s stream=%s resume_frame=%s",
            "mashin_test",
            run.descriptor.stream_id,
            0,
        )
        logger.warning.assert_called_once_with(
            "PCM repair failed for player %s stream=%s after %s attempt(s); checking bounded MA fallback",
            "mashin_test",
            run.descriptor.stream_id,
            2,
        )
        logger.info.assert_any_call(
            "PCM recovery MA-seek fallback started for player %s stream=%s position_seconds=%s paused=%s",
            "mashin_test",
            run.descriptor.stream_id,
            152,
            True,
        )
        logger.info.assert_any_call(
            "PCM recovery MA-seek fallback completed for player %s stream=%s",
            "mashin_test",
            run.descriptor.stream_id,
        )
        self.fallback.assert_awaited_once_with(152, True)

    async def test_pcm_repair_fallback_failure_does_not_log_error_payload(self) -> None:
        """A failed MA seek logs its safe exception class, not secret exception text."""
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        run.incident = RecoveryIncident(attempts=2)
        run.position_validated = True
        secret = "Bearer private-token https://private-source.example/audio"
        self.fallback.side_effect = MusicAssistantError(secret)
        logger = self.adapter.player.logger
        logger.reset_mock()
        with (
            patch.object(self.session, "_recovery_grant", AsyncMock(return_value=True)),
            pytest.raises(ProtocolViolation, match="Unable to open fallback source"),
        ):
            await self.session._repair(run)
        logger.warning.assert_any_call(
            "PCM recovery MA-seek fallback failed for player %s stream=%s: %s",
            "mashin_test",
            run.descriptor.stream_id,
            "MusicAssistantError",
        )
        assert not self.session.recovery_pause
        assert self.session.recovery_fallback is None
        assert secret not in str(logger.mock_calls)
        assert not any("fallback completed" in str(call) for call in logger.info.call_args_list)

    async def test_fallback_failure_reports_source_error_and_keeps_producer_alive(self) -> None:
        """A failed MA seek is terminal for its stream, not for the producer worker."""
        await self.accept_load()
        await self.session.report(BufferStatus(1, 1, 262144))
        assert self.session.current is not None
        run = self.session.current
        run.position_validated = True
        run.incident = RecoveryIncident(attempts=2)
        self.fallback.side_effect = MusicAssistantError("private source detail")
        failed = asyncio.Event()

        async def send(kind: str, payload: object) -> None:
            await self.send(kind, payload)
            if kind == "error":
                failed.set()

        self.session.send_control = send
        producer = self.spawn(self.session._produce())
        await asyncio.wait_for(failed.wait(), 1)
        assert not producer.done()
        assert run.error is not None
        assert run.error.code == "source_failed"
        assert self.session.buffer.reservation is None
        assert self.session.buffer.streams[run.descriptor.stream_id].terminal
        assert "private source detail" not in str(self.messages[-1])

    async def test_fallback_failure_after_load_fails_the_replacement_source(self) -> None:
        """A partially completed fallback cannot leave its replacement silently stalled."""
        await self.accept_load()
        await self.session.report(BufferStatus(1, 1, 262144))
        assert self.session.current is not None
        self.session.current.position_validated = True
        self.session.current.incident = RecoveryIncident(attempts=2)
        original_pacing = self.session.current.pacing
        failed = asyncio.Event()

        async def restart(_position: int, _paused: bool) -> None:
            await self.session.load(self.context)
            raise MusicAssistantError("MA fallback failed after load")

        async def send(kind: str, payload: object) -> None:
            await self.send(kind, payload)
            if isinstance(payload, SetMedia):
                await self.session.report(CommandAck(payload.command_id, "accepted", None))
            if kind == "error":
                failed.set()

        self.fallback.side_effect = restart
        self.session.send_control = send
        producer = self.spawn(self.session._produce())
        await asyncio.wait_for(failed.wait(), 1)
        assert not producer.done()
        assert self.session.current is not None
        assert self.session.current.error is not None
        assert self.session.current.error.code == "source_failed"
        assert self.session.current.pacing is original_pacing
        assert self.session.buffer.reservation is None

    async def test_verbose_commands_and_acks_log_only_safe_fields(self) -> None:
        """Command diagnostics contain type, ID and status, never payloads or error text."""
        await self.accept_load()
        self.adapter.player.logger.log.assert_any_call(
            VERBOSE_LOG_LEVEL,
            "Sending command %s id=%s for player %s",
            "set_media",
            1,
            "mashin_test",
        )
        self.adapter.player.logger.log.assert_any_call(
            VERBOSE_LOG_LEVEL,
            "Command ACK %s id=%s status=%s for player %s",
            "set_media",
            1,
            "accepted",
            "mashin_test",
        )

    async def test_transport_summary_is_verbose_and_five_second_limited(self) -> None:
        """Many transport events share one bounded summary window without per-chunk logs."""
        logger = self.adapter.player.logger
        logger.isEnabledFor.return_value = True
        with patch(
            "music_assistant.providers.mashin.session.time.monotonic", return_value=10
        ) as clock:
            self.session._log_transport(sent_bytes=100, send_millis=20)
            self.session._log_transport(acknowledged=True)
            self.session._log_transport(status=True)
            clock.return_value = 14.99
            self.session._log_transport(sent_bytes=200)
            logger.log.assert_not_called()
            clock.return_value = 15
            self.session._log_transport(status=True)
            logger.log.assert_called_once()
            arguments = logger.log.call_args.args
            assert arguments[0] == VERBOSE_LOG_LEVEL
            assert arguments[2:7] == ("mashin_test", 2, 300, 1, 2)
            assert arguments[-1] == 20
            clock.return_value = 19.99
            self.session._log_transport(status=True)
            assert logger.log.call_count == 1
            clock.return_value = 20
            self.session._log_transport(status=True)
            assert logger.log.call_count == 2
            assert logger.log.call_args.args[3:7] == (0, 0, 0, 2)
        logger.warning.assert_not_called()

    async def test_transport_summary_collects_nothing_without_verbose(self) -> None:
        """DEBUG/INFO do not enable high-frequency diagnostics or retain old samples."""
        logger = self.adapter.player.logger
        logger.isEnabledFor.return_value = False
        with patch("music_assistant.providers.mashin.session.time.monotonic") as clock:
            self.session._log_transport(sent_bytes=100, acknowledged=True, status=True)
            clock.assert_not_called()
        logger.isEnabledFor.assert_called_with(VERBOSE_LOG_LEVEL)
        logger.log.assert_not_called()
        assert self.session.transport_log.chunks_sent == 0
        assert self.session.transport_log.reports == 0
        assert self.session.transport_log.summary_at is None

    async def test_slow_send_warnings_are_thirty_second_limited_without_verbose(self) -> None:
        """Stalls remain visible at WARNING, with suppressed occurrences counted per device."""
        logger = self.adapter.player.logger
        logger.isEnabledFor.return_value = False
        with patch(
            "music_assistant.providers.mashin.session.time.monotonic", return_value=10
        ) as clock:
            self.session._log_transport(sent_bytes=100, send_millis=500)
            logger.warning.assert_called_once()
            assert logger.warning.call_args.args[-1] == 1
            clock.return_value = 20
            self.session._log_transport(sent_bytes=100, send_millis=700)
            clock.return_value = 39.99
            self.session._log_transport(sent_bytes=100, send_millis=900)
            assert logger.warning.call_count == 1
            clock.return_value = 40
            self.session._log_transport(sent_bytes=100, send_millis=600)
            assert logger.warning.call_count == 2
            assert logger.warning.call_args.args[-1] == 3
        logger.log.assert_not_called()

    async def test_transport_summary_windows_are_device_local(self) -> None:
        """One client's activity cannot trigger or clear another client's summary window."""
        adapter = Mock()
        adapter.player.player_id = "mashin_other"
        other = MashinSession(
            cast("MAAdapter", adapter),
            "other",
            AudioProfileId.FLAC,
            Mock(),
            AsyncMock(),
            AsyncMock(),
            self.spawn,
        )
        self.adapter.player.logger.isEnabledFor.return_value = True
        adapter.player.logger.isEnabledFor.return_value = True
        try:
            with patch(
                "music_assistant.providers.mashin.session.time.monotonic", return_value=10
            ) as clock:
                self.session._log_transport(sent_bytes=100)
                clock.return_value = 12
                other._log_transport(sent_bytes=200)
                clock.return_value = 15
                self.session._log_transport(acknowledged=True)
                other._log_transport(acknowledged=True)
                self.adapter.player.logger.log.assert_called_once()
                adapter.player.logger.log.assert_not_called()
                assert other.transport_log.bytes_sent == 200
                clock.return_value = 17
                other._log_transport(status=True)
                adapter.player.logger.log.assert_called_once()
                assert adapter.player.logger.log.call_args.args[2:7] == (
                    "mashin_other",
                    1,
                    200,
                    1,
                    1,
                )
        finally:
            await other.shutdown()

    async def test_verbose_output_state_logs_only_transitions(self) -> None:
        """Repeated progress reports do not emit individual state logs; Idle does."""
        await self.accept_load()
        assert self.session.current is not None
        stream_id = self.session.current.descriptor.stream_id
        self.adapter.player.logger.log.reset_mock()
        await self.session.report(PlayerState(1, stream_id, 0, "paused", 0, 100, False, None))
        await self.session.report(PlayerState(2, stream_id, 0, "paused", 0, 100, False, None))
        assert self.adapter.player.logger.log.call_count == 1
        await self.session.report(PlayerState(3, None, 0, "idle", 0, 100, False, None))
        assert self.adapter.player.logger.log.call_count == 2
        self.adapter.player.logger.log.assert_called_with(
            VERBOSE_LOG_LEVEL,
            "Output state for player %s changed from %s to %s (revision=%s)",
            "mashin_test",
            "paused",
            "idle",
            0,
        )

    async def test_hello_profile_is_used_for_new_streams(self) -> None:
        """Capabilities do not override the client's explicit codec and bitrate choice."""
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        welcome = await self.session.connect(
            Hello("device", "Client", "0.1.0", None, capabilities, AudioProfileId.OPUS_192K),
            self.send,
            "test",
        )
        await self.accept_load()
        assert welcome.selected_audio_profile == AudioProfileId.OPUS_192K
        assert self.session.current is not None
        assert self.session.current.descriptor.output_format.profile_id == AudioProfileId.OPUS_192K
        assert self.session.current.descriptor.output_format.target_bitrate_bps == 192000

    async def test_idle_profile_change_and_retry_do_not_seek(self) -> None:
        """An idle preference change and a retried request have no MA seek side effect."""
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_96K))
        await self.session.report(SetAudioProfile(2, "profile-1", AudioProfileId.OPUS_96K))
        assert self.session.profile == AudioProfileId.OPUS_96K
        self.fallback.assert_not_awaited()
        responses = [payload for kind, payload in self.messages if kind == "audio_profile_changed"]
        assert len(responses) == 2
        assert responses[0] == responses[1]
        assert isinstance(responses[0], AudioProfileChanged)
        assert responses[0].status == "accepted"
        assert responses[0].stream_id is None
        self.adapter.player.logger.info.assert_called_once()

    async def test_profile_request_id_cannot_change_meaning(self) -> None:
        """The same request ID cannot be repurposed for a different quality."""
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_96K))
        with pytest.raises(ProtocolViolation):
            await self.session.report(SetAudioProfile(2, "profile-1", AudioProfileId.FLAC))
        assert self.session.profile == AudioProfileId.OPUS_96K

    async def test_active_profile_change_restarts_at_position_and_keeps_pause(self) -> None:
        """A codec switch uses a fresh stream at the validated whole-second MA position."""
        await self.accept_load()
        assert self.session.current is not None
        original = self.session.current
        original.position_frame = 72000
        original.position_validated = True
        self.session.output_state = "paused"

        async def restart(position: int, paused: bool) -> None:
            assert paused
            context = cast(
                "PlaybackContext",
                SimpleNamespace(
                    **{
                        **vars(self.context),
                        "seek_seconds": position,
                        "session_id": "ma-session-after-seek",
                    }
                ),
            )
            await self.session.load(context)

        self.fallback.side_effect = restart
        self.sent.clear()
        operation = self.spawn(
            self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_128K))
        )
        await self.sent.wait()
        command = cast("SetMedia", self.messages[-1][1])
        assert self.messages[-1][0] == "set_media"
        assert not command.autoplay
        assert command.stream.stream_id != original.descriptor.stream_id
        assert command.stream.output_format.profile_id == AudioProfileId.OPUS_128K
        assert original.descriptor.output_format.profile_id == AudioProfileId.FLAC
        await self.session.report(CommandAck(command.command_id, "accepted", None))
        await operation
        self.fallback.assert_awaited_once_with(151, True)
        assert self.session.current is not None
        assert self.session.current.descriptor.media_start_position_millis == 151000
        assert self.session.current.context.session_id != original.context.session_id
        response = cast("AudioProfileChanged", self.messages[-1][1])
        assert self.messages[-1][0] == "audio_profile_changed"
        assert response.status == "accepted"
        assert response.stream_id == self.session.current.descriptor.stream_id
        assert not self.session.buffer.capacity_confirmed
        assert self.session.profile_restart_run is None

    async def test_profile_change_after_track_end_does_not_start_playback(self) -> None:
        """Changing quality after the output ended is a preference, not a play command."""
        await self.accept_load()
        assert self.session.current is not None
        self.session.current.ended = True
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_96K))
        self.fallback.assert_not_awaited()
        assert self.session.profile == AudioProfileId.OPUS_96K
        assert self.session.current.descriptor.output_format.profile_id == AudioProfileId.FLAC
        response = cast("AudioProfileChanged", self.messages[-1][1])
        assert response.status == "accepted"

    async def test_profile_change_does_not_override_pending_commands(self) -> None:
        """A codec request cannot steal the unresolved transport-command slot."""
        self.sent.clear()
        command = self.spawn(self.session.control("pause"))
        await self.sent.wait()
        pending = self.session.pending
        assert pending is not None
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_96K))
        assert self.session.pending is pending
        assert self.session.profile == AudioProfileId.FLAC
        self.fallback.assert_not_awaited()
        response = cast("AudioProfileChanged", self.messages[-1][1])
        assert response.error is not None
        assert response.error.code == "source_busy"
        await self.session.report(CommandAck(pending.command_id, "accepted", None))
        await command

    async def test_unexpected_profile_restart_failure_logs_no_exception_payload(self) -> None:
        """Operational failures are warnings without leaking source URLs or credentials."""
        await self.accept_load()
        assert self.session.current is not None
        self.session.current.position_validated = True
        secret = "Bearer secret https://example.invalid/audio?token=secret"
        self.fallback.side_effect = MusicAssistantError(secret)
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_128K))
        self.adapter.player.logger.warning.assert_called_once_with(
            "Audio profile change rejected for player %s: %s",
            "mashin_test",
            "stream_failed",
        )
        assert secret not in str(self.adapter.player.logger.mock_calls)
        assert secret not in str(self.messages[-1])
        assert self.session.profile == AudioProfileId.FLAC

    async def test_stream_failure_logs_player_stream_and_safe_code(self) -> None:
        """Warnings identify a failed stream without audio bytes or raw error messages."""
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        await self.session._fail(
            run, safe_error("stream_failed", "Untrusted private source detail")
        )
        self.adapter.player.logger.warning.assert_called_once_with(
            "Stream %s failed on player %s: %s",
            run.descriptor.stream_id,
            "mashin_test",
            "stream_failed",
        )

    async def test_profile_change_without_seek_authority_preserves_old_stream(self) -> None:
        """A rejected public MA seek cannot secretly change an active stream's codec."""
        await self.accept_load()
        assert self.session.current is not None
        original = self.session.current
        original.position_validated = True
        self.fallback.side_effect = ProtocolViolation("forbidden", "queues.control required")
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_128K))
        assert self.session.current is original
        assert self.session.profile == AudioProfileId.FLAC
        assert self.session.profile_restart_run is None
        assert not self.session.profile_restart_paused
        response = cast("AudioProfileChanged", self.messages[-1][1])
        assert response.status == "rejected"
        assert response.error is not None
        assert response.error.code == "forbidden"
        self.adapter.player.logger.info.assert_not_called()

    async def test_profile_change_requires_a_validated_output_position(self) -> None:
        """No speculative seek or codec replacement without actual client progress."""
        await self.accept_load()
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_256K))
        self.fallback.assert_not_awaited()
        response = cast("AudioProfileChanged", self.messages[-1][1])
        assert response.error is not None
        assert response.error.code == "stale_playback"
        assert self.session.profile == AudioProfileId.FLAC

    async def test_old_profile_change_cannot_reply_to_new_control_epoch(self) -> None:
        """Replacing Control while MA seek waits cannot receive an old request result."""
        await self.accept_load()
        assert self.session.current is not None
        self.session.current.position_validated = True
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        new_send = AsyncMock()

        async def replace_control(_position: int, _paused: bool) -> None:
            await self.session.connect(
                Hello(
                    "device",
                    "Client",
                    "0.1.0",
                    ClientResumeState(None, 0, "idle", 0, 100, False, 0, []),
                    capabilities,
                    AudioProfileId.OPUS_256K,
                ),
                new_send,
                "test",
            )

        self.fallback.side_effect = replace_control
        await self.session.report(SetAudioProfile(1, "profile-1", AudioProfileId.OPUS_128K))
        assert self.session.profile == AudioProfileId.OPUS_256K
        new_send.assert_not_awaited()
        assert self.session.last_profile_change is None

    async def test_reconnect_and_disconnect_are_device_local(self) -> None:
        """Replacing one client's epoch cannot alter another client's Audio grant."""
        adapter = Mock()
        adapter.release = AsyncMock()
        other = MashinSession(
            adapter,
            "phone",
            AudioProfileId.FLAC,
            Mock(),
            AsyncMock(),
            AsyncMock(),
            self.spawn,
        )
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        try:
            welcome = await other.connect(
                Hello("phone", "Phone", "0.1.0", None, capabilities, AudioProfileId.OPUS_256K),
                self.send,
                "test",
            )
            other.bind_audio(AudioHello("phone", welcome.audio_binding_id, 1, []), AsyncMock())
            other.audio_ready()
            epoch, binding = other.epoch, other.binding
            await self.session.connect(
                Hello("device", "Client", "0.1.0", None, capabilities, AudioProfileId.FLAC),
                self.send,
                "test",
            )
            await self.session.control_lost()
            await self.session.reset()
            assert other.epoch == epoch
            assert other.binding == binding
            assert other.buffer.ready
            assert other.buffer.audio_connection_id == 1
            assert other.audio_counter == 1
            assert other.profile == AudioProfileId.OPUS_256K
            assert other.send_control is not None
        finally:
            await other.shutdown()

    async def test_commands_and_acknowledgements_are_device_local(self) -> None:
        """The same command ID on two devices represents independent pending commands."""
        adapter = Mock()
        adapter.release = AsyncMock()
        other = MashinSession(
            adapter,
            "phone",
            AudioProfileId.FLAC,
            Mock(),
            AsyncMock(),
            AsyncMock(),
            self.spawn,
        )
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        try:
            await other.connect(
                Hello("phone", "Phone", "0.1.0", None, capabilities, AudioProfileId.FLAC),
                self.send,
                "test",
            )
            self.sent.clear()
            first_command = self.spawn(self.session.control("set_volume", 40))
            await self.sent.wait()
            self.sent.clear()
            second_command = self.spawn(other.control("pause"))
            await self.sent.wait()
            assert self.session.pending is not None
            assert other.pending is not None
            assert self.session.pending.command_id == other.pending.command_id
            await self.session.report(CommandAck(self.session.pending.command_id, "accepted", None))
            await first_command
            assert other.pending is not None
            assert not second_command.done()
            await other.report(CommandAck(other.pending.command_id, "accepted", None))
            await second_command
        finally:
            await other.shutdown()

    async def test_load_keeps_audio_identity_but_requires_new_capacity(self) -> None:
        """A new MA stream does not force an otherwise ready audio socket to rebind."""
        await self.accept_load()
        assert self.session.buffer.audio_connection_id == 1
        assert self.session.buffer.ready
        assert not self.session.buffer.capacity_confirmed
        await self.session.report(BufferStatus(1, 1, 262144))
        assert self.session.current is not None
        assert self.session.buffer.reserve(self.session.current.descriptor.stream_id)
        assert self.session.current.descriptor.media_start_position_millis == 150000

    async def test_audio_connection_id_consumed_before_cursor_failure(self) -> None:
        """A setup failure cannot make a claimed audio ID reusable."""
        await self.accept_load()
        self.session.binding = self.session._issue_binding()
        with pytest.raises(ProtocolViolation):
            self.session.bind_audio(AudioHello("device", self.session.binding, 2, []), AsyncMock())
        assert self.session.audio_counter == 2
        assert self.session.binding_consumed

    async def test_new_identical_capacity_snapshot_is_processed(self) -> None:
        """Report high-water state deduplicates sequences, not repeated values."""
        await self.accept_load()
        await self.session.report(BufferStatus(1, 1, 262144))
        self.session.buffer.capacity_confirmed = False
        await self.session.report(BufferStatus(1, 1, 262144))
        assert not self.session.buffer.capacity_confirmed
        await self.session.report(BufferStatus(2, 1, 262144))
        assert self.session.buffer.capacity_confirmed

    async def test_pre_ack_capacity_cannot_open_new_load(self) -> None:
        """ACK fast-path ordering must not turn an older snapshot into a fresh grant."""
        self.sent.clear()
        load = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        assert self.session.pending is not None
        await self.session.report(
            CommandAck(self.session.pending.command_id, "accepted", None), received_order=2
        )
        await load
        await self.session.report(BufferStatus(1, 1, 262144), received_order=1)
        assert not self.session.buffer.enabled
        await self.session.report(BufferStatus(2, 1, 262144), received_order=3)
        assert self.session.buffer.enabled

    async def test_old_send_failure_does_not_disconnect_replacement(self) -> None:
        """A delayed old socket failure cannot clear a replacement sender or its grant."""
        await self.accept_load()
        await self.session.report(BufferStatus(1, 1, 262144))
        assert self.session.current is not None
        identifier = self.session.current.descriptor.stream_id
        assert self.session.buffer.reserve(identifier)
        self.session.buffer.commit(AudioChunk(identifier, 1, 0, 48000, False, b"encoded"))
        started, fail, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def old_send(_data: bytes) -> None:
            started.set()
            await fail.wait()
            finished.set()
            raise ConnectionError("Old socket closed")

        self.session.send_audio = old_send
        sender = self.spawn(self.session._deliver())
        self.session.sender_wake.set()
        await asyncio.wait_for(started.wait(), 1)
        await self.session.audio_lost()
        self.session.binding = self.session._issue_binding()
        replacement = AsyncMock()
        self.session.bind_audio(
            AudioHello("device", self.session.binding, 2, [ClientStreamCursor(identifier, 1)]),
            replacement,
        )
        self.session.audio_ready()
        await self.session.report(BufferStatus(2, 2, 262144))
        fail.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert self.session.send_audio is replacement
        assert self.session.buffer.enabled
        assert not sender.done()

    async def test_known_offline_stop_cleans_up_without_replay(self) -> None:
        """A known idle snapshot wins against previously accepted playback."""
        await self.accept_load()
        await self.session._reconcile(ClientResumeState(None, 0, "idle", 0, 35, True, 0, []))
        self.stop.assert_awaited_once()
        assert self.session.current is None
        assert self.session.volume_percent == 35
        assert self.session.is_muted

    async def test_position_revision_and_captured_seek_origin(self) -> None:
        """Report stream time and permit backwards movement only in a new revision."""
        await self.accept_load()
        assert self.session.current is not None
        identifier = self.session.current.descriptor.stream_id
        self.session.buffer.streams[identifier].produced_frames = 48000 * 100
        await self.session.report(
            PlayerState(1, identifier, 0, "playing", 48000 * 10, 50, False, None)
        )
        with pytest.raises(ProtocolViolation):
            await self.session.report(PlayerState(2, identifier, 0, "playing", 0, 50, False, None))
        await self.session.report(PlayerState(3, identifier, 1, "paused", 0, 50, False, None))
        assert self.session.current.position_frame == 0
        assert self.session.current.context.seek_seconds == 150
        assert self.publish.call_count == 2

    async def test_registered_empty_stream_is_required_on_resume(self) -> None:
        """Cursor one means descriptor registration, not a missing context."""
        await self.accept_load()
        assert self.session.current is not None
        identifier = self.session.current.descriptor.stream_id
        self.session._cursors([ClientStreamCursor(identifier, 1)])
        with pytest.raises(ProtocolViolation):
            self.session._cursors([])

    async def test_lost_load_ack_is_resolved_by_descriptor_snapshot(self) -> None:
        """A registered descriptor at cursor one can explain lost atomic acceptance."""
        self.sent.clear()
        load = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        assert self.session.current is not None
        identifier = self.session.current.descriptor.stream_id
        await self.session._reconcile(
            ClientResumeState(
                identifier,
                0,
                "buffering",
                0,
                50,
                False,
                0,
                [ClientStreamCursor(identifier, 1)],
            )
        )
        await load
        assert self.session.pending is None
        assert self.session.current.accepted

    async def test_stop_wins_against_unresolved_load(self) -> None:
        """A regular MA stop revokes the older command before sending its own."""
        self.sent.clear()
        load = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        self.sent.clear()
        stop = self.spawn(self.session.control("stop"))
        await self.sent.wait()
        assert self.session.pending is not None
        assert self.session.pending.kind == "stop"
        await self.session.report(CommandAck(self.session.pending.command_id, "accepted", None))
        await stop
        with pytest.raises(ProtocolViolation):
            await load

    async def test_lost_prepare_ack_registers_b_before_audio(self) -> None:
        """Pending successor acceptance requires its exact revision and cursor."""
        await self.accept_load()
        successor = cast(
            "PlaybackContext",
            SimpleNamespace(
                media=SimpleNamespace(),
                queue_id="mashin_test",
                queue_item_id="next-item",
                session_id="ma-session",
                seek_seconds=0,
                item=SimpleNamespace(duration=200),
            ),
        )
        self.sent.clear()
        prepare = self.spawn(self.session.prepare(successor))
        await self.sent.wait()
        assert self.session.current is not None
        assert self.session.next is not None
        current = self.session.current.descriptor.stream_id
        next_id = self.session.next.descriptor.stream_id
        await self.session._reconcile(
            ClientResumeState(
                current,
                0,
                "buffering",
                0,
                50,
                False,
                1,
                [ClientStreamCursor(current, 1), ClientStreamCursor(next_id, 1)],
            )
        )
        await prepare
        assert self.session.next.accepted

    async def prepare_transition(self) -> tuple[str, str]:
        """Retain completed A and one actually offered, unacknowledged B chunk."""
        await self.accept_load()
        self.sent.clear()
        prepare = self.spawn(self.session.prepare(self.context))
        await self.sent.wait()
        assert self.session.pending is not None
        await self.session.report(CommandAck(self.session.pending.command_id, "accepted", None))
        await prepare
        assert self.session.current is not None
        assert self.session.next is not None
        current, successor = (
            self.session.current.descriptor.stream_id,
            self.session.next.descriptor.stream_id,
        )
        old = self.session.buffer.streams[current]
        old.final_index = 1
        old.final_frame_count = old.produced_frames = 48000
        old.next_received = old.next_produced = 2
        old.highest_offered = 1
        self.session.buffer.reservation = successor
        self.session.buffer.commit(AudioChunk(successor, 1, 0, 48000, False, b"encoded"))
        self.session.buffer.streams[successor].highest_offered = 1
        return current, successor

    async def test_promotion_resume_accepts_retired_a_and_lost_b_ack(self) -> None:
        """A valid hello retains only B after the atomic local transition."""
        current, successor = await self.prepare_transition()
        await self.session._reconcile(
            ClientResumeState(
                successor,
                0,
                "playing",
                0,
                50,
                False,
                1,
                [ClientStreamCursor(successor, 2)],
            )
        )
        assert self.session.current is not None
        assert self.session.current.descriptor.stream_id == successor
        assert current not in self.session.buffer.streams
        assert self.session.next is None

    async def test_invalid_promotion_does_not_mutate_a(self) -> None:
        """Check B's audible position before releasing any A ownership."""
        current, successor = await self.prepare_transition()
        self.session.buffer.resume(successor, 2)
        self.session._ended(current)
        with pytest.raises(ProtocolViolation):
            self.session._promote(TrackStarted(1, successor, 1, 0, 48001))
        assert self.session.current is not None
        assert self.session.current.descriptor.stream_id == current
        assert current in self.session.buffer.streams

    async def test_promotion_wins_against_pending_revocation(self) -> None:
        """Historical B authority explains promotion without resurrecting a new stream."""
        current, successor = await self.prepare_transition()
        self.sent.clear()
        clear = self.spawn(self.session.clear_next())
        await self.sent.wait()
        await self.session._reconcile(
            ClientResumeState(
                successor,
                0,
                "playing",
                0,
                50,
                False,
                1,
                [ClientStreamCursor(successor, 2)],
            )
        )
        with pytest.raises(ProtocolViolation):
            await clear
        assert self.session.current is not None
        assert self.session.current.descriptor.stream_id == successor
        assert current not in self.session.buffer.streams
        assert not self.session.revoking

    async def test_live_promotion_resolves_revocation_in_both_ack_orders(self) -> None:
        """Winning B promotion unblocks later preparation without an Audio rebind."""
        for ack_first in (False, True):
            with self.subTest(ack_first=ack_first):
                await self.session.reset()
                current, successor = await self.prepare_transition()
                self.session.buffer.resume(successor, 2)
                self.session._ended(current)
                self.sent.clear()
                clear = self.spawn(self.session.clear_next())
                await self.sent.wait()
                assert self.session.pending is not None
                ack = CommandAck(
                    self.session.pending.command_id,
                    "rejected",
                    safe_error("stale_playback", "B already promoted"),
                )
                promotion = TrackStarted(self.session.sequence + 1, successor, 1, 0, 0)
                if ack_first:
                    await self.session.report(ack)
                await self.session.report(promotion)
                if not ack_first:
                    await self.session.report(ack)
                with pytest.raises(ProtocolViolation):
                    await clear
                assert not self.session.revoking
                assert self.session.next_revision == 1
                assert self.session.buffer.ready
                self.sent.clear()
                preparing = self.spawn(self.session.prepare(self.context))
                await self.sent.wait()
                assert self.session.pending is not None
                await self.session.report(
                    CommandAck(self.session.pending.command_id, "accepted", None)
                )
                await preparing
                assert self.session.next is not None
                assert self.session._active(self.session.next)

    async def test_unaccepted_revocation_is_reissued_only_after_welcome(self) -> None:
        """Reconnect A/B with the old revision freezes B until a fresh clear command."""
        current, successor = await self.prepare_transition()
        self.sent.clear()
        clear = self.spawn(self.session.clear_next())
        await self.sent.wait()
        await self.session._reconcile(
            ClientResumeState(
                current,
                0,
                "buffering",
                48000,
                50,
                False,
                1,
                [ClientStreamCursor(current, 2), ClientStreamCursor(successor, 1)],
            )
        )
        with pytest.raises(ProtocolViolation):
            await clear
        assert self.session.reissue_revocation
        assert self.session.revoking
        assert self.session.pending is None

    async def test_older_load_cleanup_cannot_replace_newer_generation(self) -> None:
        """An older await continuation cannot publish a replacement load descriptor."""
        await self.accept_load()
        releasing = asyncio.Event()
        finish_release = asyncio.Event()

        async def release(_buffer: object) -> None:
            releasing.set()
            await finish_release.wait()

        self.adapter.release.side_effect = release
        first = self.spawn(self.session.load(self.context))
        await releasing.wait()
        self.sent.clear()
        second = self.spawn(self.session.load(self.context))
        await self.sent.wait()
        assert self.session.pending is not None
        await self.session.report(CommandAck(self.session.pending.command_id, "accepted", None))
        await second
        newest = self.session.current
        finish_release.set()
        with pytest.raises(ProtocolViolation):
            await first
        assert self.session.current is newest

    async def test_terminal_error_precedes_later_capacity(self) -> None:
        """Fresh capacity cannot resurrect a client-declared terminal stream."""
        await self.accept_load()
        assert self.session.current is not None
        identifier = self.session.current.descriptor.stream_id
        await self.session.report(
            TransportStatus(
                1,
                identifier,
                1,
                "closed",
                0,
                safe_error("stream_failed", "Decoder failed"),
            )
        )
        await self.session.report(BufferStatus(2, 1, 262144))
        assert self.session.current.error is not None
        assert not self.session.buffer.reserve(identifier)
        assert self.session.buffer.offer(identifier) is None
        self.adapter.release.assert_awaited_once()

    async def test_old_session_cleanup_cannot_clear_new_queue_session_buffer(self) -> None:
        """Deferred cleanup cannot clear a PCM buffer reclaimed across provider reload."""
        await self.accept_load()
        assert self.session.current is not None
        run = self.session.current
        owned = Mock()
        run.owned_buffer = owned
        run.context.item.streamdetails = Mock(buffer=owned, queue_session_id="new-session")
        await self.session._close_run(run, release=True)
        self.adapter.release.assert_not_awaited()

    async def test_old_cleanup_does_not_clear_a_reclaimed_ma_buffer(self) -> None:
        """A newer load of the same item may legitimately reuse MA's raw buffer."""
        await self.accept_load()
        assert self.session.current is not None
        previous = self.session.current
        self.session.current = self.session._run(self.context)
        self.adapter.release.reset_mock()
        await self.session._close_run(previous, release=True)
        self.adapter.release.assert_not_awaited()
