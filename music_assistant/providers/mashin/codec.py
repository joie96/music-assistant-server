"""Bounded independent FLAC/Opus encoding and PCM chunk assembly."""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from io import BytesIO
from typing import TYPE_CHECKING

from mutagen.ogg import OggPage
from mutagen.ogg import error as ogg_error

from .models import AudioProfileId, OutputFormat
from .wire import MAX_PAYLOAD_BYTES, ProtocolViolation

if TYPE_CHECKING:
    from music_assistant.helpers.ffmpeg import FFMpeg


class PCMPacing:
    """Retain one PCM read budget across every source attempt of a stream."""

    def __init__(self) -> None:
        """Initialize an unused stream budget."""
        self.started_at: float | None = None
        self.seconds = 0.0
        self.configure(False)

    def configure(self, realtime: bool) -> None:
        """Select Mashin's normal or realtime budget without resetting consumed PCM."""
        self.rate = 1.01 if realtime else 1.2
        self.burst = 3.0 if realtime else 120.0

    async def wait(self, seconds: float) -> None:
        """Wait until the requested PCM fits the cumulative read budget."""
        now = time.monotonic()
        if self.started_at is None:
            self.started_at = now
        delay = (self.seconds + seconds - self.burst) / self.rate - (now - self.started_at)
        if delay > 0:
            await asyncio.sleep(delay)

    def commit(self, seconds: float) -> None:
        """Charge actual PCM, including discarded repair overlap."""
        self.seconds += seconds


class PCMChunkReader:
    """Assemble a reserved second and retain at most one EOF probe frame."""

    def __init__(
        self, read: Callable[[int], Awaitable[bytes]], sample_rate: int, channels: int
    ) -> None:
        """Bind a reader to Float32 interleaved PCM."""
        self.read = read
        self.sample_rate = sample_rate
        self.frame_bytes = channels * 4
        self.probe = b""
        self.finished = False

    async def next_chunk(self) -> tuple[bytes, bool]:
        """Read one reserved second and establish EOF before returning it."""
        if self.finished:
            raise ProtocolViolation("stream_failed", "PCM stream has already ended")
        target = self.sample_rate * self.frame_bytes
        result = bytearray(self.probe)
        self.probe = b""
        while len(result) < target:
            fragment = await self.read(target - len(result))
            if not fragment:
                self.finished = True
                break
            if len(fragment) > target - len(result):
                raise ProtocolViolation("stream_failed", "PCM reader exceeded its request")
            result.extend(fragment)
        if not result or len(result) % self.frame_bytes:
            raise ProtocolViolation("stream_failed", "Empty or truncated PCM stream")
        if not self.finished:
            while len(self.probe) < self.frame_bytes:
                fragment = await self.read(self.frame_bytes - len(self.probe))
                if not fragment:
                    if self.probe:
                        raise ProtocolViolation("stream_failed", "Truncated EOF probe")
                    self.finished = True
                    break
                if len(fragment) > self.frame_bytes - len(self.probe):
                    raise ProtocolViolation("stream_failed", "PCM reader exceeded its probe")
                self.probe += fragment
        return bytes(result), self.finished


class ChunkEncoder:
    """Encode only the reserved chunk with bounded codec history."""

    def __init__(self, output_format: OutputFormat) -> None:
        """Pin a concrete format for the lifetime of the stream."""
        output_format.validate()
        self.format = output_format
        self.history = b""
        self.active: FFMpeg | None = None

    async def encode(self, pcm: bytes) -> bytes:
        """Encode one independently decodable mini-stream without disk or source URLs."""
        from music_assistant_models.enums import ContentType  # noqa: PLC0415
        from music_assistant_models.media_items import AudioFormat  # noqa: PLC0415

        from music_assistant.helpers.ffmpeg import FFMpeg  # noqa: PLC0415

        output = self.format
        frame_bytes = output.channels * 4
        if not pcm or len(pcm) % frame_bytes or len(pcm) > output.sample_rate_hz * frame_bytes:
            raise ProtocolViolation("encoder_failed", "Invalid encoder PCM size")
        opus = output.profile_id != AudioProfileId.FLAC
        history = self.history if opus else b""
        audio_format = AudioFormat(
            content_type=ContentType.PCM_F32LE,
            sample_rate=output.sample_rate_hz,
            bit_depth=32,
            channels=output.channels,
        )
        args = ["-map_metadata", "-1", "-threads", "1"]
        if opus:
            args += [
                "-c:a",
                "libopus",
                "-f",
                "ogg",
                "-application",
                "audio",
                "-compression_level",
                "10",
                "-frame_duration",
                "20",
                "-vbr",
                "on",
                "-b:a",
                str(output.target_bitrate_bps),
                "-mapping_family",
                "0",
                "-page_duration",
                "20000",
            ]
        else:
            args += [
                "-c:a",
                "flac",
                "-f",
                "flac",
                "-sample_fmt",
                "s16",
                "-compression_level",
                "0",
                "-frame_size",
                "4096",
                "-lpc_type",
                "none",
                "-ch_mode",
                "indep",
            ]

        async def audio_input() -> AsyncGenerator[bytes]:
            if history:
                yield history
            yield pcm

        process = FFMpeg(
            audio_input=audio_input(),
            input_format=audio_format,
            output_format=audio_format,
            extra_output_args=args,
        )
        self.active = process
        encoded = bytearray()
        try:
            async with process:
                async for fragment in process.iter_chunked(16384):
                    encoded.extend(fragment)
                    if len(encoded) > MAX_PAYLOAD_BYTES:
                        raise ProtocolViolation(
                            "encoder_failed", "Encoded chunk exceeds its reservation"
                        )
                await process.wait_with_timeout(5)
                if process.returncode != 0 or process.stdin_feeder_exception is not None:
                    raise ProtocolViolation("encoder_failed", "Audio encoder failed")
            if opus:
                payload = remux_opus(
                    bytes(encoded), len(history) // frame_bytes, len(pcm) // frame_bytes
                )
            else:
                payload = strip_flac_metadata(
                    bytes(encoded), output.sample_rate_hz, output.channels
                )
            return payload
        finally:
            self.active = None

    def commit_history(self, pcm: bytes) -> None:
        """Advance history only after a valid atomic chunk commit."""
        if self.format.profile_id != AudioProfileId.FLAC:
            size = 3840 * self.format.channels * 4
            self.history = pcm[-size:] if len(pcm) >= size else (self.history + pcm)[-size:]

    async def close(self) -> None:
        """Release a currently running encoder."""
        if self.active is not None:
            await self.active.close()


def strip_flac_metadata(data: bytes, sample_rate: int, channels: int) -> bytes:
    """Keep only STREAMINFO and enforce the conservative FLAC size bound."""
    if not data.startswith(b"fLaC"):
        raise ProtocolViolation("encoder_failed", "Invalid FLAC signature")
    cursor = 4
    streaminfo: bytes | None = None
    while True:
        if cursor + 4 > len(data):
            raise ProtocolViolation("encoder_failed", "Truncated FLAC metadata")
        flag = data[cursor]
        size = int.from_bytes(data[cursor + 1 : cursor + 4], "big")
        cursor += 4
        if cursor + size > len(data):
            raise ProtocolViolation("encoder_failed", "Truncated FLAC metadata")
        if flag & 127 == 0:
            if streaminfo is not None or size != 34:
                raise ProtocolViolation("encoder_failed", "Invalid FLAC STREAMINFO")
            streaminfo = data[cursor : cursor + size]
        cursor += size
        if flag & 128:
            break
    if streaminfo is None or cursor == len(data):
        raise ProtocolViolation("encoder_failed", "Missing FLAC audio")
    result = b"fLaC\x80\x00\x00\x22" + streaminfo + data[cursor:]
    bound = sample_rate * channels * 2 + ((sample_rate + 4095) // 4096) * 20 + 1024
    if len(result) > bound:
        raise ProtocolViolation("encoder_failed", "FLAC exceeds VERBATIM bound")
    return result


def remux_opus(data: bytes, history_frames: int, frame_count: int) -> bytes:
    """Use Mutagen pages to trim history and enforce all V1 Opus container bounds."""
    if not 0 <= history_frames <= 3840 or not 1 <= frame_count <= 48000:
        raise ProtocolViolation("encoder_failed", "Invalid Opus frame range")
    source = BytesIO(data)
    pages: list[OggPage] = []
    try:
        while source.tell() < len(data):
            start = source.tell()
            page = OggPage(source)
            if (
                not page.complete
                or page.continued
                or not page.packets
                or page.sequence != len(pages)
                or (pages and page.serial != pages[0].serial)
            ):
                raise ProtocolViolation("encoder_failed", "Unsupported Ogg page structure")
            if page.write() != data[start : source.tell()]:
                raise ProtocolViolation("encoder_failed", "Invalid Ogg checksum or flags")
            pages.append(page)
    except (EOFError, ogg_error, ValueError) as err:
        raise ProtocolViolation("encoder_failed", "Invalid encoded Ogg stream") from err
    if len(pages) < 3 or not pages[0].first or not pages[-1].last:
        raise ProtocolViolation("encoder_failed", "Missing Ogg boundaries")
    packets = [packet for page in pages for packet in page.packets]
    head = bytearray(packets[0])
    if len(head) != 19 or head[:10] != b"OpusHead\x01\x02" or head[18] != 0:
        raise ProtocolViolation("encoder_failed", "Invalid stereo OpusHead")
    if int.from_bytes(head[12:16], "little") != 48000 or head[16:18] != b"\x00\x00":
        raise ProtocolViolation("encoder_failed", "Unexpected Opus rate or output gain")
    lookahead = int.from_bytes(head[10:12], "little")
    if lookahead > 960 or not packets[1].startswith(b"OpusTags"):
        raise ProtocolViolation("encoder_failed", "Invalid Opus metadata or lookahead")
    pre_skip = history_frames + lookahead
    expected_packets = (frame_count + pre_skip + 959) // 960
    audio = packets[2:]
    if len(audio) != expected_packets or len(audio) > 55:
        raise ProtocolViolation("encoder_failed", "Unexpected Opus flush packets")
    for packet in audio:
        if not packet or len(packet) > 1276 or packet[0] & 3 or not _opus_twenty_millis(packet[0]):
            raise ProtocolViolation("encoder_failed", "Invalid Opus packet framing")
    head[10:12] = pre_skip.to_bytes(2, "little")
    vendor = b"Mashin V1"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    encoded = bytearray()
    for index, packet in enumerate([bytes(head), tags, *audio]):
        page = OggPage()
        page.serial = pages[0].serial
        page.sequence = index
        page.first = index == 0
        page.last = index == len(audio) + 1
        page.position = 0 if index < 2 else (index - 1) * 960
        if page.last:
            page.position = pre_skip + frame_count
        page.packets = [packet]
        encoded.extend(page.write())
    if len(encoded) > 87278:
        raise ProtocolViolation("encoder_failed", "Opus exceeds its container bound")
    return bytes(encoded)


def _opus_twenty_millis(toc: int) -> bool:
    config = toc >> 3
    return (
        (config < 12 and config % 4 == 1)
        or (12 <= config < 16 and config % 2 == 1)
        or (config >= 16 and config % 4 == 3)
    )
