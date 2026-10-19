"""Bounded stop-and-wait delivery shared by Mashin A/B streams."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from .wire import MAX_PAYLOAD_BYTES, AudioChunk, ProtocolViolation, integer, stream_uuid


@dataclass(slots=True)
class StreamBuffer:
    """Bounded payload ownership and retained stream cursors."""

    stream_id: str
    sample_rate: int
    chunks: OrderedDict[int, AudioChunk] = field(default_factory=OrderedDict)
    next_received: int = 1
    next_produced: int = 1
    produced_frames: int = 0
    highest_offered: int = 0
    final_index: int | None = None
    final_frame_count: int | None = None
    terminal: bool = False


class DeliveryBuffer:
    """Coordinate production reservations and one outstanding audio send."""

    def __init__(self) -> None:
        """Create a closed delivery gate with no registered streams."""
        self.streams: dict[str, StreamBuffer] = {}
        self.capacity = 0
        self.audio_connection_id: int | None = None
        self.ready = False
        self.capacity_confirmed = False
        self.load_accepted = False
        self.reservation: str | None = None
        self.outstanding: tuple[str, int] | None = None
        self.retry_blocked = False

    @property
    def occupied_slots(self) -> int:
        """Return all committed and currently producing chunk slots."""
        return sum(len(stream.chunks) for stream in self.streams.values()) + int(
            self.reservation is not None
        )

    @property
    def reserved_bytes(self) -> int:
        """Return unacknowledged payload and worst-case production reservations."""
        return sum(
            len(chunk.payload)
            for stream in self.streams.values()
            for chunk in stream.chunks.values()
        ) + (MAX_PAYLOAD_BYTES if self.reservation is not None else 0)

    @property
    def enabled(self) -> bool:
        """Return whether both audio and accepted-load capacity barriers passed."""
        return self.ready and self.capacity_confirmed and self.load_accepted

    def register(self, stream_id: str, sample_rate: int) -> StreamBuffer:
        """Register an authorized descriptor without starting its source."""
        stream_uuid(stream_id)
        if stream_id in self.streams or len(self.streams) >= 2 or sample_rate not in (44100, 48000):
            raise ProtocolViolation("stale_playback", "Invalid stream registration")
        stream = StreamBuffer(stream_id, sample_rate)
        self.streams[stream_id] = stream
        return stream

    def bind(self, audio_connection_id: int) -> None:
        """Reset connection-local gates without discarding retained payload."""
        self.audio_connection_id = integer(audio_connection_id, 1)
        self.ready = False
        self.capacity_confirmed = False
        self.capacity = 0
        self.outstanding = None
        self.retry_blocked = False

    def accept_load(self) -> None:
        """Wait for fresh capacity ordered after accepted-set_media."""
        self.load_accepted = True
        self.capacity_confirmed = False
        self.capacity = 0

    def update_capacity(self, audio_connection_id: int, capacity: int) -> None:
        """Install one absolute snapshot without opening the outstanding send slot."""
        integer(capacity)
        if self.audio_connection_id != audio_connection_id or not self.ready:
            raise ProtocolViolation("stale_playback", "Capacity belongs to an inactive connection")
        self.capacity = capacity
        self.capacity_confirmed = True
        self.retry_blocked = False

    def reserve(self, stream_id: str) -> bool:
        """Reserve a worst-case slot before requesting any PCM."""
        stream = self.streams[stream_id]
        if (
            not self.enabled
            or stream.terminal
            or stream.final_index is not None
            or self.reservation is not None
            or self.occupied_slots >= 10
            or self.reserved_bytes + MAX_PAYLOAD_BYTES > self.capacity
        ):
            return False
        self.reservation = stream_id
        return True

    def commit(self, chunk: AudioChunk) -> None:
        """Commit immutable audio and its EOF metadata under the reserved slot."""
        if self.reservation != chunk.stream_id:
            raise ProtocolViolation("stale_playback", "Missing production reservation")
        stream = self.streams[chunk.stream_id]
        if stream.terminal or stream.final_index is not None:
            raise ProtocolViolation("stale_playback", "Stream is no longer producing")
        chunk.validate_frames(stream.sample_rate, stream.next_produced, stream.produced_frames)
        stream.chunks[chunk.chunk_index] = chunk
        stream.next_produced += 1
        stream.produced_frames += chunk.frame_count
        if chunk.is_final_chunk:
            stream.final_index = chunk.chunk_index
            stream.final_frame_count = stream.produced_frames
        self.reservation = None

    def offer(self, stream_id: str) -> AudioChunk | None:
        """Close the send slot and mark a chunk offered before socket suspension."""
        stream = self.streams[stream_id]
        chunk = stream.chunks.get(stream.next_received)
        if (
            not self.enabled
            or stream.terminal
            or self.outstanding is not None
            or self.retry_blocked
            or chunk is None
            or len(chunk.payload) > self.capacity
        ):
            return None
        self.outstanding = (stream_id, chunk.chunk_index)
        stream.highest_offered = max(stream.highest_offered, chunk.chunk_index)
        return chunk

    def acknowledge(
        self,
        stream_id: str,
        audio_connection_id: int,
        chunk_index: int,
        next_chunk_index: int,
        accepted: bool,
        capacity: int,
    ) -> bool:
        """Resolve exactly the current send, retaining rejected payload and successors."""
        integer(capacity)
        integer(chunk_index, 1)
        integer(next_chunk_index, 1)
        if audio_connection_id != self.audio_connection_id or self.outstanding != (
            stream_id,
            chunk_index,
        ):
            return False
        if next_chunk_index != chunk_index + int(accepted):
            raise ProtocolViolation("invalid_message", "ACK cursor contradicts its status")
        if accepted:
            self.resume(stream_id, next_chunk_index)
        self.outstanding = None
        self.capacity = capacity
        self.retry_blocked = not accepted
        return True

    def resume(self, stream_id: str, next_chunk_index: int) -> None:
        """Validate a received cursor before releasing server-owned bytes."""
        integer(next_chunk_index, 1)
        stream = self.streams[stream_id]
        if next_chunk_index < stream.next_received:
            raise ProtocolViolation("cache_unavailable", "Previously acknowledged context was lost")
        if next_chunk_index > min(stream.next_produced, stream.highest_offered + 1):
            raise ProtocolViolation("invalid_message", "Cursor acknowledges unoffered audio")
        if next_chunk_index > stream.next_received + 1:
            raise ProtocolViolation("invalid_message", "Cursor violates stop-and-wait")
        stream.next_received = next_chunk_index
        while stream.chunks and next(iter(stream.chunks)) < next_chunk_index:
            stream.chunks.popitem(last=False)

    def discard(self, stream_id: str) -> None:
        """Revoke one stream and release only its delivery ownership."""
        self.streams.pop(stream_id, None)
        if self.reservation == stream_id:
            self.reservation = None
        if self.outstanding and self.outstanding[0] == stream_id:
            self.outstanding = None

    def disconnect(self) -> None:
        """Close transport gates without evicting immutable unacknowledged chunks."""
        self.ready = False
        self.capacity_confirmed = False
        self.capacity = 0
        self.outstanding = None
