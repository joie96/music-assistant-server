"""Independent EOF assembly and bounded Ogg remux tests."""

import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

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
