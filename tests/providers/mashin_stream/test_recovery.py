"""PCM matcher positive and deliberately ambiguous negative fixtures."""

import unittest
from unittest.mock import patch

import numpy as np
import pytest

from music_assistant.providers.mashin_stream.recovery import (
    RecoveryIncident,
    RecoveryRateLimit,
    match_pcm,
)
from music_assistant.providers.mashin_stream.wire import ProtocolViolation


class TestPCMMatcher(unittest.TestCase):
    """Verify full-window and all-channel constraints with deterministic fixtures."""

    def test_unique_full_window(self) -> None:
        """Find a single integer alignment rather than estimating a time offset."""
        random = np.random.default_rng(42)
        samples = random.normal(0, 0.1, (48000 * 8, 2)).astype("<f4")
        end = 48000 * 5 + 123
        tail = samples[end - 48000 * 3 : end]
        assert match_pcm(tail.tobytes(), samples.tobytes(), 48000, 2, 48000 * 5) == end

    def test_silence_and_short_window_rejected(self) -> None:
        """Never pad or shorten the three-second comparison window."""
        for duration in (2, 3):
            with self.subTest(duration=duration), pytest.raises(ProtocolViolation):
                match_pcm(bytes(48000 * duration * 8), bytes(48000 * 8 * 8), 48000, 2, 240000)

    def test_periodic_signal_rejected(self) -> None:
        """Repeated exact waveform matches do not prove a unique recording position."""
        waveform = (0.1 * np.sin(np.arange(48000 * 8) * 2 * np.pi / 480)).astype("<f4")
        stereo = np.column_stack((waveform, waveform)).astype("<f4")
        with pytest.raises(ProtocolViolation):
            match_pcm(stereo[96000:240000].tobytes(), stereo.tobytes(), 48000, 2, 240000)

    def test_changed_channel_rejected(self) -> None:
        """Matching one channel cannot hide a large change in the other."""
        random = np.random.default_rng(7)
        samples = random.normal(0, 0.1, (48000 * 8, 2)).astype("<f4")
        tail = samples[96000:240000].copy()
        tail[:, 1] *= 0.5
        with pytest.raises(ProtocolViolation):
            match_pcm(tail.tobytes(), samples.tobytes(), 48000, 2, 240000)

    def test_small_gain_and_noise_changes_accepted(self) -> None:
        """Best-effort matching tolerates bounded differences across all formats."""
        for sample_rate in (44100, 48000):
            for channels in (1, 2):
                with self.subTest(sample_rate=sample_rate, channels=channels):
                    random = np.random.default_rng(23)
                    samples = random.normal(0, 0.1, (sample_rate * 8, channels)).astype("<f4")
                    end = sample_rate * 5 + 123
                    tail = samples[end - sample_rate * 3 : end].copy()
                    tail *= 1.03
                    tail += random.normal(0, 0.003, tail.shape).astype("<f4")
                    assert (
                        match_pcm(
                            tail.tobytes(),
                            samples.tobytes(),
                            sample_rate,
                            channels,
                            sample_rate * 5,
                        )
                        == end
                    )

    def test_local_channel_corruption_rejected(self) -> None:
        """A strong whole-window match cannot hide one damaged half-second block."""
        random = np.random.default_rng(24)
        samples = random.normal(0, 0.1, (48000 * 8, 2)).astype("<f4")
        tail = samples[96000:240000].copy()
        tail[:24000, 1] += random.normal(0, 0.02, 24000).astype("<f4")
        with pytest.raises(ProtocolViolation):
            match_pcm(tail.tobytes(), samples.tobytes(), 48000, 2, 240000)

    def test_time_drift_rejected(self) -> None:
        """A changing frame offset must not pass as one integer alignment."""
        random = np.random.default_rng(25)
        samples = random.normal(0, 0.1, (48000 * 8, 2)).astype("<f4")
        positions = 96000 + np.arange(48000 * 3) * 1.0001
        tail = np.column_stack(
            [
                np.interp(positions, np.arange(len(samples)), samples[:, channel])
                for channel in range(2)
            ]
        ).astype("<f4")
        with pytest.raises(ProtocolViolation):
            match_pcm(tail.tobytes(), samples.tobytes(), 48000, 2, 240000)

    def test_unrelated_recording_rejected(self) -> None:
        """An isolated best score is not sufficient without actual similarity."""
        random = np.random.default_rng(26)
        samples = random.normal(0, 0.1, (48000 * 8, 2)).astype("<f4")
        tail = random.normal(0, 0.1, (48000 * 3, 2)).astype("<f4")
        with pytest.raises(ProtocolViolation):
            match_pcm(tail.tobytes(), samples.tobytes(), 48000, 2, 240000)

    def test_partial_frame_is_protocol_error(self) -> None:
        """Malformed PCM never escapes as a NumPy shape exception."""
        with pytest.raises(ProtocolViolation):
            match_pcm(b"\x00", bytes(48000 * 8 * 8), 48000, 2, 240000)

    def test_active_budget_excludes_real_waits_without_reset(self) -> None:
        """Consumed active seconds survive long waits and the fallback boundary."""
        incident = RecoveryIncident()
        with patch(
            "music_assistant.providers.mashin_stream.recovery.time.monotonic", return_value=100
        ):
            incident.start()
        with patch(
            "music_assistant.providers.mashin_stream.recovery.time.monotonic", return_value=110
        ):
            incident.wait()
        with patch(
            "music_assistant.providers.mashin_stream.recovery.time.monotonic", return_value=1000
        ):
            assert incident.remaining == 20
            incident.start()
        with patch(
            "music_assistant.providers.mashin_stream.recovery.time.monotonic", return_value=1005
        ):
            assert incident.remaining == 15

    def test_rolling_rate_limit_survives_incident_replacement(self) -> None:
        """Exactly six opening claims fit the same rolling minute."""
        limiter = RecoveryRateLimit()
        with patch(
            "music_assistant.providers.mashin_stream.recovery.time.monotonic", return_value=100
        ):
            for _ in range(6):
                limiter.claim()
            with pytest.raises(ProtocolViolation):
                limiter.claim()
        with patch(
            "music_assistant.providers.mashin_stream.recovery.time.monotonic", return_value=160
        ):
            limiter.claim()
        assert len(limiter.openings) == 1
