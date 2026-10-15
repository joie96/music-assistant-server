"""Mashin binary wire contract tests."""

import unittest
from uuid import uuid4

import pytest

from music_assistant.providers.mashin.wire import (
    CHUNK_HEADER,
    MAX_INTEGER,
    MAX_PAYLOAD_BYTES,
    AudioChunk,
    ProtocolViolation,
    integer,
)


class TestAudioChunk(unittest.TestCase):
    """Validate binary framing independently of codecs and the server."""

    def test_network_header_and_roundtrip(self) -> None:
        """Keep UUID bytes and integer fields in network order."""
        identifier = uuid4()
        chunk = AudioChunk(str(identifier), 240, 11472000, 48000, True, b"fLaC")
        wire = chunk.encode()
        assert CHUNK_HEADER.size == 41
        assert wire[:16] == identifier.bytes
        assert wire[16:24] == (240).to_bytes(8, "big")
        assert wire[40] == 1
        assert AudioChunk.decode(wire) == chunk

    def test_invalid_final_flag(self) -> None:
        """Reject non-boolean binary flags."""
        wire = bytearray(AudioChunk(str(uuid4()), 1, 0, 48000, False, b"data").encode())
        wire[40] = 2
        with pytest.raises(ProtocolViolation):
            AudioChunk.decode(bytes(wire))

    def test_payload_boundaries(self) -> None:
        """Reject empty and oversized chunks without splitting them."""
        for size in (0, MAX_PAYLOAD_BYTES + 1):
            with self.subTest(size=size), pytest.raises(ProtocolViolation):
                AudioChunk(str(uuid4()), 1, 0, 48000, False, bytes(size))
        chunk = AudioChunk(str(uuid4()), 1, 0, 48000, True, bytes(MAX_PAYLOAD_BYTES))
        assert len(chunk.encode()) == 262185

    def test_integer_validation(self) -> None:
        """Reject coercion and integers outside the interoperable range."""
        for value in (True, "1", 1.0, -1, MAX_INTEGER + 1):
            with self.subTest(value=value), pytest.raises(ProtocolViolation):
                integer(value)
        assert integer(MAX_INTEGER) == MAX_INTEGER

    def test_contiguous_frames(self) -> None:
        """Allow short final chunks but not short regular chunks or gaps."""
        final = AudioChunk(str(uuid4()), 2, 48000, 1, True, b"data")
        final.validate_frames(48000, 2, 48000)
        with pytest.raises(ProtocolViolation):
            final.validate_frames(48000, 2, 47999)
        regular = AudioChunk(str(uuid4()), 1, 0, 1, False, b"data")
        with pytest.raises(ProtocolViolation):
            regular.validate_frames(48000, 1, 0)
