"""Best-effort full-window PCM matching and bounded recovery incidents."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy.signal import correlate

from .wire import ProtocolViolation

MIN_CORRELATION = 0.995
MIN_ALIGNMENT_MARGIN = 0.005
MAX_NORMALIZED_ERROR = 0.01
MIN_SIGNAL_ENERGY = 1e-7


@dataclass(slots=True)
class RecoveryIncident:
    """One source-loss budget that survives reconnect and a fallback load."""

    attempts: int = 0
    fallbacks: int = 0
    active_seconds: float = 0.0
    active_since: float | None = None

    @property
    def remaining(self) -> float:
        """Return remaining active time, excluding capacity/authority waits."""
        running = time.monotonic() - self.active_since if self.active_since is not None else 0.0
        return max(0.0, 30.0 - self.active_seconds - running)

    def start(self) -> None:
        """Enter active work without resetting previously consumed time."""
        if self.active_since is None:
            self.active_since = time.monotonic()

    def wait(self) -> None:
        """Suspend budget accounting only for a real missing production grant."""
        if self.active_since is not None:
            self.active_seconds += time.monotonic() - self.active_since
            self.active_since = None


class RecoveryRateLimit:
    """Retain at most six opening timestamps, independent of playback loads."""

    def __init__(self) -> None:
        """Initialize the player-wide rolling opening budget."""
        self.openings: deque[float] = deque()

    def claim(self) -> None:
        """Claim one repair or fallback opening without creating a source slot."""
        now = time.monotonic()
        while self.openings and self.openings[0] <= now - 60:
            self.openings.popleft()
        if len(self.openings) >= 6:
            raise ProtocolViolation("recovery_failed", "Source recovery rate limit reached")
        self.openings.append(now)


def match_pcm(
    tail: bytes, candidate: bytes, sample_rate: int, channels: int, expected_end: int
) -> int:
    """Verify exactly one full-resolution three-second PCM overlap in every channel."""
    if (
        sample_rate not in (44100, 48000)
        or channels not in (1, 2)
        or len(tail) % (channels * 4)
        or len(candidate) % (channels * 4)
        or type(expected_end) is not int
        or expected_end < 0
    ):
        raise ProtocolViolation("recovery_failed", "Invalid PCM comparison format")
    window = sample_rate * 3
    reference = np.frombuffer(tail, dtype="<f4").reshape(-1, channels).astype(np.float64)
    samples = np.frombuffer(candidate, dtype="<f4").reshape(-1, channels).astype(np.float64)
    if reference.shape[0] != window or samples.shape[0] > sample_rate * 12:
        raise ProtocolViolation("recovery_failed", "Missing complete PCM comparison window")
    if not np.isfinite(reference).all() or not np.isfinite(samples).all():
        raise ProtocolViolation("recovery_failed", "Invalid PCM comparison values")
    lower = max(0, expected_end - sample_rate - window)
    upper = min(samples.shape[0] - window, expected_end + sample_rate - window)
    if upper < lower:
        raise ProtocolViolation("recovery_failed", "Missing PCM overlap")
    segment = samples[lower : upper + window]
    scores = np.zeros(upper - lower + 1, dtype=np.float64)
    for channel in range(channels):
        signal = reference[:, channel] - reference[:, channel].mean()
        energy = np.dot(signal, signal)
        if energy / window < MIN_SIGNAL_ENERGY:
            raise ProtocolViolation("recovery_failed", "Insufficient comparison signal energy")
        data = segment[:, channel]
        cumulative = np.concatenate(([0.0], np.cumsum(data)))
        cumulative_squared = np.concatenate(([0.0], np.cumsum(data * data)))
        sums = cumulative[window:] - cumulative[:-window]
        energies = cumulative_squared[window:] - cumulative_squared[:-window] - sums * sums / window
        correlations = correlate(data, signal, mode="valid", method="fft")
        scores += correlations / np.sqrt(np.maximum(energies, 1e-30) * energy)
    scores /= channels
    best = int(np.argmax(scores))
    if scores[best] < MIN_CORRELATION:
        raise ProtocolViolation("recovery_failed", "PCM correlation is not sufficient")
    alternatives = scores.copy()
    alternatives[max(0, best - 5) : best + 6] = -1
    if float(scores[best] - np.max(alternatives)) < MIN_ALIGNMENT_MARGIN:
        raise ProtocolViolation("recovery_failed", "PCM alignment is ambiguous")
    start = lower + best
    aligned = samples[start : start + window]
    for block in range(6):
        first, last = block * window // 6, (block + 1) * window // 6
        for channel in range(channels):
            original = reference[first:last, channel]
            reopened = aligned[first:last, channel]
            error = np.mean((original - reopened) ** 2) / max(float(np.mean(original**2)), 1e-30)
            original_centered = original - original.mean()
            reopened_centered = reopened - reopened.mean()
            original_energy = float(np.dot(original_centered, original_centered))
            reopened_energy = float(np.dot(reopened_centered, reopened_centered))
            correlation = float(np.dot(original_centered, reopened_centered)) / max(
                (original_energy * reopened_energy) ** 0.5, 1e-30
            )
            if (
                original_energy / (last - first) < MIN_SIGNAL_ENERGY
                or reopened_energy / (last - first) < MIN_SIGNAL_ENERGY
                or correlation < MIN_CORRELATION
                or error > MAX_NORMALIZED_ERROR
            ):
                raise ProtocolViolation("recovery_failed", "PCM block verification failed")
    return start + window
