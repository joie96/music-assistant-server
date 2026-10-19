"""Mashin production and delivery boundary tests."""

import unittest
from uuid import uuid4

import pytest

from music_assistant.providers.mashin_stream.buffer import DeliveryBuffer
from music_assistant.providers.mashin_stream.wire import (
    MAX_PAYLOAD_BYTES,
    AudioChunk,
    ProtocolViolation,
)


class TestDeliveryBuffer(unittest.TestCase):
    """Exercise reservations, ACK ownership and reconnect cursors."""

    def setUp(self) -> None:
        """Create one authorized stream and a ready connection."""
        self.identifier = str(uuid4())
        self.buffer = DeliveryBuffer()
        self.stream = self.buffer.register(self.identifier, 48000)
        self.buffer.bind(1)
        self.buffer.ready = True
        self.buffer.accept_load()
        self.buffer.update_capacity(1, MAX_PAYLOAD_BYTES * 20)

    def produce(self, *, final: bool = False) -> AudioChunk:
        """Reserve and commit the next full chunk."""
        assert self.buffer.reserve(self.identifier)
        chunk = AudioChunk(
            self.identifier,
            self.stream.next_produced,
            self.stream.produced_frames,
            48000,
            final,
            bytes(MAX_PAYLOAD_BYTES),
        )
        self.buffer.commit(chunk)
        return chunk

    def test_ten_slots_include_current_production(self) -> None:
        """Count outstanding, successors and an active reservation together."""
        self.produce()
        assert self.buffer.offer(self.identifier) is not None
        for _ in range(8):
            self.produce()
        assert self.buffer.reserve(self.identifier)
        assert self.buffer.occupied_slots == 10
        assert not self.buffer.reserve(self.identifier)
        assert self.buffer.reserved_bytes == MAX_PAYLOAD_BYTES * 10

    def test_capacity_does_not_acknowledge_audio(self) -> None:
        """A fresh absolute snapshot cannot open an occupied send slot."""
        self.produce()
        self.buffer.offer(self.identifier)
        self.produce()
        self.buffer.update_capacity(1, MAX_PAYLOAD_BYTES * 20)
        assert self.buffer.offer(self.identifier) is None
        assert self.stream.next_received == 1

    def test_ack_releases_payload_not_final_metadata(self) -> None:
        """Retain EOF metadata after the final payload is released."""
        self.produce(final=True)
        self.buffer.offer(self.identifier)
        assert self.buffer.acknowledge(self.identifier, 1, 1, 2, True, 0)
        assert self.buffer.occupied_slots == 0
        assert self.stream.final_frame_count == 48000
        assert not self.buffer.reserve(self.identifier)

    def test_rejection_waits_for_new_capacity(self) -> None:
        """Preserve the same immutable chunk without a retry polling loop."""
        chunk = self.produce()
        self.buffer.offer(self.identifier)
        self.buffer.acknowledge(self.identifier, 1, 1, 1, False, MAX_PAYLOAD_BYTES)
        assert self.buffer.offer(self.identifier) is None
        self.buffer.update_capacity(1, MAX_PAYLOAD_BYTES)
        assert self.buffer.offer(self.identifier) is chunk

    def test_lost_ack_resume_and_unoffered_cursor(self) -> None:
        """A handshake may acknowledge only the one actually offered chunk."""
        self.produce()
        self.produce()
        self.buffer.offer(self.identifier)
        with pytest.raises(ProtocolViolation):
            self.buffer.resume(self.identifier, 3)
        self.buffer.disconnect()
        self.buffer.resume(self.identifier, 2)
        assert self.buffer.occupied_slots == 1
        with pytest.raises(ProtocolViolation):
            self.buffer.resume(self.identifier, 1)

    def test_load_requires_fresh_capacity(self) -> None:
        """Existing connection capacity cannot authorize a new load."""
        self.buffer.accept_load()
        assert not self.buffer.reserve(self.identifier)
        self.buffer.update_capacity(1, 0)
        assert self.buffer.capacity_confirmed
        assert not self.buffer.reserve(self.identifier)

    def test_pending_payload_is_subtracted(self) -> None:
        """Capacity received while an ACK is pending still reserves that chunk."""
        self.produce()
        self.buffer.offer(self.identifier)
        self.buffer.update_capacity(1, MAX_PAYLOAD_BYTES)
        assert not self.buffer.reserve(self.identifier)
        assert not self.buffer.acknowledge(self.identifier, 2, 1, 2, True, MAX_PAYLOAD_BYTES)
        assert self.buffer.occupied_slots == 1
