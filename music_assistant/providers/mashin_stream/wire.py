"""Strict Mashin V3 wire primitives."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from uuid import UUID

PROTOCOL_VERSION = 3
MAX_INTEGER = 9007199254740991
MAX_PAYLOAD_BYTES = 262144
CHUNK_HEADER = struct.Struct("!16sQQQB")


class ProtocolViolation(ValueError):
    """An invalid or unauthorized protocol operation."""

    def __init__(self, code: str, message: str) -> None:
        """Create a safe error suitable for the protocol boundary."""
        super().__init__(message)
        self.code = code


def integer(value: object, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    """Validate a wire integer without boolean or string coercion."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolViolation("invalid_message", "Integer outside the permitted range")
    return value


def stream_uuid(value: str) -> UUID:
    """Validate a canonical UUIDv4 stream identifier."""
    try:
        result = UUID(value)
    except ValueError as err:
        raise ProtocolViolation("invalid_message", "Invalid stream identifier") from err
    if result.version != 4 or str(result) != value:
        raise ProtocolViolation("invalid_message", "Stream identifier must be canonical UUIDv4")
    return result


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """An immutable, independently decodable audio chunk."""

    stream_id: str
    chunk_index: int
    first_frame: int
    frame_count: int
    is_final_chunk: bool
    payload: bytes

    def __post_init__(self) -> None:
        """Validate the chunk before it can enter the delivery buffer."""
        stream_uuid(self.stream_id)
        integer(self.chunk_index, 1, MAX_INTEGER - 1)
        integer(self.first_frame)
        integer(self.frame_count, 1)
        integer(self.first_frame + self.frame_count)
        if type(self.is_final_chunk) is not bool:
            raise ProtocolViolation("invalid_message", "Invalid final flag")
        if type(self.payload) is not bytes or not 1 <= len(self.payload) <= MAX_PAYLOAD_BYTES:
            raise ProtocolViolation("encoder_failed", "Invalid audio payload size")

    def encode(self) -> bytes:
        """Return one complete audio WebSocket message."""
        return (
            CHUNK_HEADER.pack(
                stream_uuid(self.stream_id).bytes,
                self.chunk_index,
                self.first_frame,
                self.frame_count,
                int(self.is_final_chunk),
            )
            + self.payload
        )

    @classmethod
    def decode(cls, data: bytes) -> AudioChunk:
        """Validate and decode a complete binary message."""
        if not CHUNK_HEADER.size < len(data) <= CHUNK_HEADER.size + MAX_PAYLOAD_BYTES:
            raise ProtocolViolation("stream_failed", "Invalid binary message size")
        identifier, index, first, count, final = CHUNK_HEADER.unpack_from(data)
        if final not in (0, 1):
            raise ProtocolViolation("stream_failed", "Invalid final flag")
        return cls(str(UUID(bytes=identifier)), index, first, count, bool(final), data[41:])

    def validate_frames(self, sample_rate: int, expected_index: int, expected_frame: int) -> None:
        """Validate contiguous one-second chunks and the optional EOF remainder."""
        if (
            sample_rate not in (44100, 48000)
            or self.chunk_index != expected_index
            or self.first_frame != expected_frame
            or self.frame_count > sample_rate
            or (not self.is_final_chunk and self.frame_count != sample_rate)
        ):
            raise ProtocolViolation("stream_failed", "Invalid chunk frame range")
