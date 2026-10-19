"""Real codec gates using synthetic PCM, never provider music or stored audio."""

from __future__ import annotations

import asyncio
import shutil
import unittest

import numpy as np

from music_assistant.providers.mashin_stream.codec import ChunkEncoder
from music_assistant.providers.mashin_stream.models import AudioProfileId, OutputFormat
from music_assistant.providers.mashin_stream.wire import MAX_PAYLOAD_BYTES


@unittest.skipUnless(shutil.which("ffmpeg"), "Requires the pinned Linux MA/FFmpeg runtime")
class TestCodecRuntime(unittest.IsolatedAsyncioTestCase):
    """Decode each mini-stream with a new actual decoder and verify its frame range."""

    async def decode(self, payload: bytes) -> bytes:
        """Decode a single bounded synthetic chunk entirely through pipes."""
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(5):
                decoded, error = await process.communicate(payload)
            assert process.returncode == 0, error.decode("utf-8", errors="replace")
            return decoded
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_flac_rates_channels_and_short_eof(self) -> None:
        """Exercise mandatory FLAC combinations and exact, unpadded EOF lengths."""
        random = np.random.default_rng(91)
        for rate in (44100, 48000):
            for channels in (1, 2):
                for frames in (1, 4096, rate):
                    with self.subTest(rate=rate, channels=channels, frames=frames):
                        pcm = random.uniform(-0.9, 0.9, (frames, channels)).astype("<f4")
                        encoder = ChunkEncoder(
                            OutputFormat(
                                AudioProfileId.FLAC,
                                "flac",
                                "flac",
                                rate,
                                16,
                                channels,
                                None,
                                False,
                            )
                        )
                        try:
                            payload = await encoder.encode(pcm.tobytes())
                            decoded = await self.decode(payload)
                        finally:
                            await encoder.close()
                        assert len(decoded) == frames * channels * 4
                        assert len(payload) <= MAX_PAYLOAD_BYTES
                        assert (
                            np.max(np.abs(np.frombuffer(decoded, dtype="<f4") - pcm.reshape(-1)))
                            <= 1 / 32768
                        )

    async def test_all_opus_profiles_history_and_eof(self) -> None:
        """Verify standalone decoding after preroll plus exact short-tail trimming."""
        random = np.random.default_rng(94)
        for profile in AudioProfileId:
            if profile == AudioProfileId.FLAC:
                continue
            bitrate = int(profile.value.removeprefix("opus_").removesuffix("k")) * 1000
            encoder = ChunkEncoder(
                OutputFormat(profile, "ogg", "opus", 48000, None, 2, bitrate, True)
            )
            try:
                for frames in (48000, 48000, 12345, 1):
                    with self.subTest(profile=profile, frames=frames):
                        pcm = random.uniform(-0.1, 0.1, (frames, 2)).astype("<f4").tobytes()
                        payload = await encoder.encode(pcm)
                        assert len(await self.decode(payload)) == frames * 8
                        assert len(payload) <= MAX_PAYLOAD_BYTES
                        encoder.commit_history(pcm)
            finally:
                await encoder.close()
