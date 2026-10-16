"""Independent EOF assembly and bounded Ogg remux tests."""

import asyncio
import unittest
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mutagen.ogg import OggPage

from music_assistant.providers.mashin.codec import (
    ChunkEncoder,
    PCMChunkReader,
    PCMPacing,
    remux_opus,
)
from music_assistant.providers.mashin.models import AudioProfileId, OutputFormat
from music_assistant.providers.mashin.wire import ProtocolViolation

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
            patch("music_assistant.providers.mashin.codec.asyncio.sleep", AsyncMock()) as sleep,
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
            patch("music_assistant.providers.mashin.codec.asyncio.sleep", AsyncMock()) as sleep,
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
                    self.assertLogs(
                        "music_assistant.providers.mashin.codec", level="WARNING"
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


class TestPCMPacing(unittest.IsolatedAsyncioTestCase):
    """Keep consumed PCM and the first burst across source reopens."""

    async def test_reopen_and_overlap_share_the_initial_budget(self) -> None:
        """Repair work consumes the same ledger instead of starting another burst."""
        pacing = PCMPacing()
        assert pacing.rate == 1.2
        assert pacing.burst == 120
        with (
            patch("music_assistant.providers.mashin.codec.time.monotonic", return_value=10),
            patch("music_assistant.providers.mashin.codec.asyncio.sleep", AsyncMock()) as sleep,
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
            patch("music_assistant.providers.mashin.codec.time.monotonic", return_value=10),
            patch("music_assistant.providers.mashin.codec.asyncio.sleep", AsyncMock()) as sleep,
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
