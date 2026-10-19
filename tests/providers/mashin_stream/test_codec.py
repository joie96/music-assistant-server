"""Independent EOF assembly and bounded Ogg remux tests."""

import asyncio
import unittest
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mutagen.ogg import OggPage

from music_assistant.providers.mashin_stream.codec import (
    ChunkEncoder,
    PCMChunkReader,
    PCMPacing,
    remux_opus,
    strip_flac_metadata,
)
from music_assistant.providers.mashin_stream.models import AudioProfileId, OutputFormat
from music_assistant.providers.mashin_stream.wire import MAX_PAYLOAD_BYTES, ProtocolViolation

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class TestChunkEncoder(unittest.IsolatedAsyncioTestCase):
    """Preserve actionable encoder diagnostics without exposing raw input failures."""

    async def test_retries_preserve_pcm_and_uncommitted_history(self) -> None:
        """Two transient failures replay the same chunk without advancing codec history."""
        encoder = ChunkEncoder(
            OutputFormat(AudioProfileId.OPUS_96K, "ogg", "opus", 48000, None, 2, 96000, True)
        )
        encoder.history = b"previous"
        pcm = bytes(8)
        with (
            patch.object(
                encoder,
                "_encode_once",
                AsyncMock(
                    side_effect=[
                        ProtocolViolation("encoder_failed", "Invalid Ogg checksum"),
                        OSError("private source detail"),
                        b"valid",
                    ]
                ),
            ) as encode,
            patch(
                "music_assistant.providers.mashin_stream.codec.asyncio.sleep", AsyncMock()
            ) as sleep,
        ):
            assert await encoder.encode(pcm) == b"valid"
        assert encode.await_count == 3
        assert all(call.args == (pcm,) for call in encode.await_args_list)
        assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]
        assert encoder.history == b"previous"

    async def test_retries_stop_after_three_failures(self) -> None:
        """Permanent encoder failures remain terminal and do not reveal raw exceptions."""
        encoder = ChunkEncoder(
            OutputFormat(AudioProfileId.FLAC, "flac", "flac", 48000, 16, 2, None, False)
        )
        with (
            patch.object(
                encoder, "_encode_once", AsyncMock(side_effect=OSError("private-token"))
            ) as encode,
            patch(
                "music_assistant.providers.mashin_stream.codec.asyncio.sleep", AsyncMock()
            ) as sleep,
            pytest.raises(ProtocolViolation, match="after 3 attempts: OSError") as failure,
        ):
            await encoder.encode(bytes(8))
        assert encode.await_count == 3
        assert sleep.await_count == 2
        assert failure.value.code == "encoder_failed"
        assert "private-token" not in str(failure.value)

    async def test_cancellation_and_invalid_pcm_are_not_retried(self) -> None:
        """Stop/seek cancellation propagates and malformed input never starts FFmpeg."""
        encoder = ChunkEncoder(
            OutputFormat(AudioProfileId.FLAC, "flac", "flac", 48000, 16, 2, None, False)
        )
        with patch.object(
            encoder, "_encode_once", AsyncMock(side_effect=asyncio.CancelledError)
        ) as encode:
            with pytest.raises(ProtocolViolation, match="Invalid encoder PCM size"):
                await encoder.encode(b"invalid")
            encode.assert_not_awaited()
            with pytest.raises(asyncio.CancelledError):
                await encoder.encode(bytes(8))
            encode.assert_awaited_once()

    async def test_failure_reports_exit_code_and_sanitized_stderr(self) -> None:
        """Process and feeder failures provide bounded, privacy-safe server diagnostics."""
        for returncode, input_error in ((1, None), (0, OSError("private-feeder-detail"))):
            with self.subTest(returncode=returncode):
                encoder = ChunkEncoder(
                    OutputFormat(AudioProfileId.FLAC, "flac", "flac", 48000, 16, 2, None, False)
                )
                process = AsyncMock()
                process.returncode = returncode
                process.proc = Mock(pid=4321)
                process.stdin_feeder_exception = input_error
                process.log_history = [
                    "earlier omitted entry",
                    "context one",
                    "context two",
                    "context three",
                    "Unrecognized option 'frame_size'.",
                    "https://username:private-password@example.test/audio.flac?token=private-token",
                ]

                async def encoded() -> AsyncGenerator[bytes]:
                    yield b"encoded"

                process.iter_chunked = Mock(return_value=encoded())
                ffmpeg = Mock(return_value=process)
                with (
                    patch.dict(
                        "sys.modules", {"music_assistant.helpers.ffmpeg": Mock(FFMpeg=ffmpeg)}
                    ),
                    patch(
                        "music_assistant.providers.mashin_stream.codec.get_global_cache_value",
                        return_value="7.1.2",
                    ),
                    self.assertLogs(
                        "music_assistant.providers.mashin_stream.codec", level="WARNING"
                    ) as logs,
                    pytest.raises(ProtocolViolation) as failure,
                ):
                    await encoder._encode_once(bytes(8))
                assert failure.value.code == "encoder_failed"
                assert f"exit_code={returncode}" in str(failure.value)
                assert "private-feeder-detail" not in str(failure.value)
                assert "private-token" not in str(failure.value)
                message = logs.records[0].getMessage()
                assert "Unrecognized option 'frame_size'." in message
                assert "profile=flac rate=48000 channels=2" in message
                assert "pcm_bytes=8 encoded_bytes=7" in message
                assert "phase=wait pid=4321" in message
                assert "pcm_frames=1 history_frames=0 target_bit_depth=16" in message
                assert "ffmpeg_version=7.1.2" in message
                assert "-sample_fmt s16" in message
                assert "-frame_size 4096 -lpc_type none -ch_mode indep" in message
                assert "earlier omitted entry" not in message
                assert "private-password" not in message
                assert "private-token" not in message
                assert "private-feeder-detail" not in message
                expected_input_error = "OSError" if input_error else "none"
                assert f"input_error={expected_input_error}" in message
                assert ffmpeg.call_args is not None
                assert ffmpeg.call_args.kwargs["collect_log_history"] is True
                assert ffmpeg.call_args.kwargs["loglevel"] == "error"
                assert encoder.active is None

    async def test_all_encoding_failure_phases_report_process_diagnostics(self) -> None:
        """Validation errors after a successful exit retain the same process context."""
        for phase in ("start", "encode", "wait", "validate_flac", "remux_opus", "payload_limit"):
            with self.subTest(phase=phase):
                is_opus = phase == "remux_opus"
                encoder = ChunkEncoder(
                    OutputFormat(
                        AudioProfileId.OPUS_96K, "ogg", "opus", 48000, None, 2, 96000, True
                    )
                    if is_opus
                    else OutputFormat(
                        AudioProfileId.FLAC, "flac", "flac", 48000, 16, 2, None, False
                    )
                )
                process = AsyncMock()
                process.returncode = 0
                process.proc = Mock(pid=5678)
                process.stdin_feeder_exception = None
                process.log_history = []
                if phase == "start":
                    process.__aenter__.side_effect = OSError("private-start-token")
                if phase == "wait":
                    process.wait_with_timeout.side_effect = TimeoutError("private-timeout-token")

                async def encoded(failure_phase: str = phase) -> AsyncGenerator[bytes]:
                    if failure_phase == "encode":
                        raise OSError("private-read-token")
                    yield (
                        bytes(MAX_PAYLOAD_BYTES + 1)
                        if failure_phase == "payload_limit"
                        else b"invalid"
                    )

                process.iter_chunked = Mock(return_value=encoded())
                with (
                    patch.dict(
                        "sys.modules",
                        {"music_assistant.helpers.ffmpeg": Mock(FFMpeg=Mock(return_value=process))},
                    ),
                    self.assertLogs(
                        "music_assistant.providers.mashin_stream.codec", level="WARNING"
                    ) as logs,
                    pytest.raises((ProtocolViolation, OSError, TimeoutError)),
                ):
                    await encoder._encode_once(bytes(8))
                message = logs.records[0].getMessage()
                expected_phase = "encode" if phase == "payload_limit" else phase
                assert f"phase={expected_phase} pid=5678" in message
                assert "exit_code=0 input_error=none" in message
                assert "pcm_frames=1" in message
                assert "payload_limit=262144" in message
                assert "elapsed_ms=" in message
                assert "No stderr captured" in message
                assert "private-" not in message
                assert encoder.active is None

    async def test_flac_bound_logs_actual_streaminfo_and_metadata_sizes(self) -> None:
        """Oversized FLAC output exposes numeric format evidence, never encoded audio."""
        packed_format = (96000 << 44) | (1 << 41) | (23 << 36) | 48000
        streaminfo = (
            (4096).to_bytes(2, "big") * 2
            + (8192).to_bytes(3, "big")
            + (16920).to_bytes(3, "big")
            + packed_format.to_bytes(8, "big")
            + bytes(16)
        )
        bound = MAX_PAYLOAD_BYTES
        data = (
            b"fLaC\x00\x00\x00\x22"
            + streaminfo
            + b"\x81\x00\x00\x64"
            + bytes(100)
            + bytes(bound + 1 - 42)
        )
        with (
            self.assertLogs(
                "music_assistant.providers.mashin_stream.codec", level="WARNING"
            ) as logs,
            pytest.raises(ProtocolViolation, match="FLAC exceeds chunk payload limit") as failure,
        ):
            strip_flac_metadata(data, 48000, 2)
        assert failure.value.code == "encoder_failed"
        message = logs.records[0].getMessage()
        assert "encoded_bytes=262249 payload_bytes=262145 bound_bytes=262144" in message
        assert "excess_bytes=1 metadata_bytes=146" in message
        assert "expected_rate=48000 expected_channels=2 expected_bit_depth=16" in message
        assert "actual_rate=96000 actual_channels=2 actual_bit_depth=24" in message
        assert "stream_total_samples=48000 min_block_frames=4096 max_block_frames=4096" in message
        assert "min_frame_bytes=8192 max_frame_bytes=16920" in message

    async def test_unreadable_streaminfo_does_not_replace_the_original_failure(self) -> None:
        """Best-effort diagnostics preserve the encoder error even for malformed metadata."""
        data = b"fLaC\x80\x00\x00\x22" + bytes(34 + MAX_PAYLOAD_BYTES)
        with (
            self.assertLogs(
                "music_assistant.providers.mashin_stream.codec", level="WARNING"
            ) as logs,
            pytest.raises(ProtocolViolation, match="FLAC exceeds chunk payload limit"),
        ):
            strip_flac_metadata(data, 48000, 2)
        assert "actual_rate=None actual_channels=None actual_bit_depth=None" in logs.output[0]

    async def test_flac_accepts_payloads_up_to_the_shared_chunk_limit(self) -> None:
        """All FLAC output formats use the same inclusive payload boundary."""
        for rate in (44100, 48000):
            for channels in (1, 2):
                for size in (193328, MAX_PAYLOAD_BYTES):
                    with self.subTest(rate=rate, channels=channels, size=size):
                        data = b"fLaC\x80\x00\x00\x22" + bytes(size - 8)
                        assert strip_flac_metadata(data, rate, channels) == data


class TestPCMPacing(unittest.IsolatedAsyncioTestCase):
    """Keep consumed PCM and the first burst across source reopens."""

    async def test_reopen_and_overlap_share_the_initial_budget(self) -> None:
        """Repair work consumes the same ledger instead of starting another burst."""
        pacing = PCMPacing()
        assert pacing.rate == 1.2
        assert pacing.burst == 120
        with (
            patch("music_assistant.providers.mashin_stream.codec.time.monotonic", return_value=10),
            patch(
                "music_assistant.providers.mashin_stream.codec.asyncio.sleep", AsyncMock()
            ) as sleep,
        ):
            pacing.configure(False)
            await pacing.wait(120)
            sleep.assert_not_awaited()
            pacing.commit(120)
            pacing.configure(False)
            await pacing.wait(1)
            assert sleep.await_args is not None
            assert sleep.await_args.args[0] == pytest.approx(1 / 1.2)
            pacing.commit(5)
            await pacing.wait(1)
            assert sleep.await_args.args[0] == pytest.approx(6 / 1.2)
            assert pacing.seconds == 125

    async def test_realtime_uses_its_smaller_single_burst(self) -> None:
        """Realtime sources retain MA's three-second and 1.01x envelope."""
        pacing = PCMPacing()
        pacing.configure(True)
        with (
            patch("music_assistant.providers.mashin_stream.codec.time.monotonic", return_value=10),
            patch(
                "music_assistant.providers.mashin_stream.codec.asyncio.sleep", AsyncMock()
            ) as sleep,
        ):
            await pacing.wait(3)
            pacing.commit(3)
            await pacing.wait(1)
            sleep.assert_awaited_once_with(1 / 1.01)


class TestPCMChunkReader(unittest.IsolatedAsyncioTestCase):
    """Observe exact read sizes rather than treating decoder EOS as track EOF."""

    async def test_probe_is_exactly_one_frame_and_preserved(self) -> None:
        """A full final second is marked final before its first commit."""
        data = BytesIO(bytes(48000 * 8 * 2))
        sizes: list[int] = []

        async def read(size: int) -> bytes:
            sizes.append(size)
            return data.read(size)

        reader = PCMChunkReader(read, 48000, 2)
        first, final = await reader.next_chunk()
        assert len(first) == 48000 * 8
        assert not final
        assert sizes == [48000 * 8, 8]
        second, final = await reader.next_chunk()
        assert len(second) == 48000 * 8
        assert final
        assert sizes[-2:] == [48000 * 8 - 8, 8]

    async def test_truncation_does_not_become_eof(self) -> None:
        """A partial sampleframe cannot create a final chunk."""
        data = BytesIO(bytes(9))

        async def read(size: int) -> bytes:
            return data.read(size)

        with pytest.raises(ProtocolViolation):
            await PCMChunkReader(read, 48000, 2).next_chunk()


class TestOpusRemux(unittest.TestCase):
    """Check trim metadata using library-produced Ogg pages."""

    def encoded(self, packets: int) -> bytes:
        """Build container fixtures, not synthetic decoded audio."""
        head = b"OpusHead\x01\x02" + (312).to_bytes(2, "little") + (48000).to_bytes(4, "little")
        head += b"\x00\x00\x00"
        result = bytearray()
        for index, packet in enumerate([head, b"OpusTags", *([b"\xfc\x00"] * packets)]):
            page = OggPage()
            page.serial = 42
            page.sequence = index
            page.first = index == 0
            page.last = index == packets + 1
            page.packets = [packet]
            result.extend(page.write())
        return bytes(result)

    def test_history_and_end_granule(self) -> None:
        """Trim history plus lookahead and preserve exactly 48000 valid frames."""
        result = remux_opus(self.encoded(55), 3840, 48000)
        source = BytesIO(result)
        pages = []
        while source.tell() < len(result):
            pages.append(OggPage(source))
        assert int.from_bytes(pages[0].packets[0][10:12], "little") == 4152
        assert pages[-1].position == 52152
        assert pages[-1].last
        assert len(pages) == 57

    def test_no_extra_flush_packets(self) -> None:
        """An unexpected packet count is encoder failure, not a shorter chunk."""
        with pytest.raises(ProtocolViolation):
            remux_opus(self.encoded(52), 0, 48000)

    def test_history_copy_preserves_short_final_chunk_preroll(self) -> None:
        """History truncation preserves the exact suffix for full and short chunks."""
        encoder = ChunkEncoder(
            OutputFormat(
                profile_id=AudioProfileId.OPUS_96K,
                container="ogg",
                codec="opus",
                sample_rate_hz=48000,
                bit_depth=None,
                channels=2,
                target_bitrate_bps=96000,
                uses_vbr=True,
            )
        )
        encoder.history = b"a" * 30720
        encoder.commit_history(b"b" * 40000)
        assert encoder.history == b"b" * 30720
        encoder.commit_history(b"c" * 8)
        assert encoder.history == b"b" * 30712 + b"c" * 8
