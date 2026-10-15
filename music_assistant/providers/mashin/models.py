"""Typed, strictly validated Mashin V1 control messages."""

from __future__ import annotations

import json
import types
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import StrEnum
from functools import cache
from typing import Any, Literal, TypeAliasType, TypeVar, Union, get_args, get_origin, get_type_hints

from .wire import MAX_INTEGER, ProtocolViolation, integer, stream_uuid

ModelT = TypeVar("ModelT")
type OutputState = Literal["idle", "buffering", "playing", "paused", "ended", "error"]


class AudioProfileId(StrEnum):
    """Mandatory V1 output profiles."""

    FLAC = "flac"
    OPUS_96K = "opus_96k"
    OPUS_128K = "opus_128k"
    OPUS_192K = "opus_192k"
    OPUS_256K = "opus_256k"


@dataclass(frozen=True, slots=True)
class Version1Limits:
    """Fixed V1 limits and the three configurable protection budgets."""

    max_server_buffered_chunks: int = 10
    max_server_buffer_metadata_bytes: int = 8388608
    max_server_pending_audio_bytes: int = 1048576
    max_audio_chunk_payload_bytes: int = 262144
    max_unacknowledged_audio_chunks: int = 1
    target_audio_chunk_duration_millis: int = 1000
    local_seek_step_millis: int = 1000
    opus_preroll_duration_millis: int = 80
    max_control_message_bytes: int = 65536
    max_devices: int = 1
    max_control_connections: int = 1
    max_audio_connections: int = 1
    max_active_source_streams: int = 1
    max_prepared_next_items: int = 1
    max_client_decoders: int = 1
    max_retained_streams_per_player: int = 2
    max_stream_tombstones_per_player: int = 32
    hello_timeout_seconds: int = 5
    welcome_timeout_seconds: int = 30
    audio_binding_response_timeout_seconds: int = 5
    command_ack_timeout_seconds: int = 5
    chunk_ack_timeout_seconds: int = 5
    audio_setup_timeout_seconds: int = 30
    buffer_status_timeout_seconds: int = 5
    control_ping_interval_seconds: int = 5
    control_liveness_timeout_seconds: int = 15
    reconnect_grace_seconds: int = 30
    disconnected_source_retention_seconds: int = 30
    source_idle_timeout_seconds: int = 30
    stream_start_timeout_seconds: int = 10
    source_recovery_tail_duration_millis: int = 3000
    source_recovery_preroll_duration_millis: int = 5000
    source_recovery_search_radius_millis: int = 1000
    max_source_recovery_decode_duration_millis: int = 12000
    max_source_recovery_attempts_per_incident: int = 2
    max_source_recovery_fallbacks_per_incident: int = 1
    max_source_recovery_duration_seconds: int = 30
    max_source_recovery_attempts_per_player_per_minute: int = 6
    shutdown_timeout_seconds: int = 5

    def validate(self) -> None:
        """Validate fixed constants and the declared positive protection-budget ranges."""
        configurable = {
            "max_server_buffer_metadata_bytes",
            "max_server_pending_audio_bytes",
            "max_control_message_bytes",
        }
        for entry in fields(self):
            value = integer(getattr(self, entry.name))
            if entry.name in configurable:
                if not 1 <= value <= integer(entry.default):
                    raise ProtocolViolation("incompatible_server", "Invalid protection budget")
            elif value != entry.default:
                raise ProtocolViolation("incompatible_server", "Changed fixed V1 limit")


@dataclass(frozen=True, slots=True)
class ProtocolError:
    """Safe error crossing the provider boundary."""

    code: Literal[
        "invalid_message",
        "unsupported_version",
        "unauthorized",
        "forbidden",
        "device_conflict",
        "stale_playback",
        "command_failed",
        "media_unsupported",
        "profile_unsupported",
        "encoder_failed",
        "source_busy",
        "source_failed",
        "stream_failed",
        "stream_unavailable",
        "cache_unavailable",
        "buffer_capacity_exceeded",
        "rate_limited",
        "recovery_failed",
        "incompatible_server",
    ]
    message: str
    is_retryable: bool
    retry_after_millis: int | None


@dataclass(frozen=True, slots=True)
class PlaybackIdentity:
    """MA-owned playback identity with a provider generation and occurrence."""

    player_id: str
    queue_id: str
    queue_item_id: str
    queue_session_id: str
    playback_generation: int
    playback_instance_id: str


@dataclass(frozen=True, slots=True)
class OutputFormat:
    """Immutable concrete stream output format."""

    profile_id: AudioProfileId
    container: Literal["flac", "ogg"]
    codec: Literal["flac", "opus"]
    sample_rate_hz: int
    bit_depth: int | None
    channels: int
    target_bitrate_bps: int | None
    uses_vbr: bool

    def validate(self) -> None:
        """Validate the exact profile-dependent output combination."""
        if self.profile_id == AudioProfileId.FLAC:
            valid = (
                self.container == self.codec == "flac"
                and self.sample_rate_hz in (44100, 48000)
                and self.bit_depth == 16
                and self.channels in (1, 2)
                and self.target_bitrate_bps is None
                and not self.uses_vbr
            )
        else:
            bitrate = int(self.profile_id.value.removeprefix("opus_").removesuffix("k")) * 1000
            valid = (
                self.container == "ogg"
                and self.codec == "opus"
                and self.sample_rate_hz == 48000
                and self.bit_depth is None
                and self.channels == 2
                and self.target_bitrate_bps == bitrate
                and self.uses_vbr
            )
        if not valid:
            raise ProtocolViolation("profile_unsupported", "Invalid output format")


@dataclass(frozen=True, slots=True)
class StreamDescriptor:
    """Descriptor registered before any PCM source is opened."""

    stream_id: str
    playback: PlaybackIdentity
    output_format: OutputFormat
    media_start_position_millis: int
    duration_millis: int | None


@dataclass(frozen=True, slots=True)
class StreamSnapshot:
    """Produced and received stream state, not audible completion."""

    stream: StreamDescriptor
    next_received_chunk_index: int
    next_produced_chunk_index: int
    is_complete: bool
    final_frame_count: int | None
    error: ProtocolError | None


@dataclass(frozen=True, slots=True)
class ClientStreamCursor:
    """First missing chunk of an already registered stream."""

    stream_id: str
    next_chunk_index: int


@dataclass(frozen=True, slots=True)
class ClientResumeState:
    """Current local output and receipt state retained across reconnects."""

    current_stream_id: str | None
    output_revision: int
    output_state: OutputState
    position_frame: int
    volume_percent: int
    is_muted: bool
    next_revision: int
    received_streams: list[ClientStreamCursor]


@dataclass(frozen=True, slots=True)
class ClientCapabilities:
    """All seven mandatory client capabilities."""

    audio_profiles: list[AudioProfileId]
    sample_rates_hz: list[int]
    bit_depths: list[int]
    max_channels: int
    can_pause: bool
    can_set_volume: bool
    can_mute: bool

    def validate(self) -> None:
        """Require every V1 profile and all mandatory rendering capabilities."""
        if (
            len(self.audio_profiles) != len(AudioProfileId)
            or set(self.audio_profiles) != set(AudioProfileId)
            or not self.sample_rates_hz
            or len(set(self.sample_rates_hz)) != len(self.sample_rates_hz)
            or not set(self.sample_rates_hz) <= {44100, 48000}
            or 48000 not in self.sample_rates_hz
            or self.bit_depths != [16]
            or self.max_channels != 2
        ):
            raise ProtocolViolation("profile_unsupported", "All V1 audio profiles are required")
        if not (self.can_pause and self.can_set_volume and self.can_mute):
            raise ProtocolViolation("incompatible_server", "Missing mandatory player capability")


@dataclass(frozen=True, slots=True)
class Hello:
    """Control handshake."""

    device_id: str
    client_name: str
    client_version: str
    resume_state: ClientResumeState | None
    capabilities: ClientCapabilities
    audio_profile: AudioProfileId


@dataclass(frozen=True, slots=True)
class AudioHello:
    """Single-use audio binding without a capacity grant."""

    device_id: str
    audio_binding_id: str
    audio_connection_id: int
    received_streams: list[ClientStreamCursor]


@dataclass(frozen=True, slots=True)
class CommandAck:
    """Atomic command acceptance, not output completion."""

    command_id: int
    status: Literal["accepted", "rejected"]
    error: ProtocolError | None


@dataclass(frozen=True, slots=True)
class ChunkAck:
    """Receipt and absolute capacity of exactly one offered chunk."""

    sequence: int
    stream_id: str
    audio_connection_id: int
    chunk_index: int
    next_chunk_index: int
    status: Literal["accepted", "rejected"]
    buffer_capacity: int
    error: ProtocolError | None


@dataclass(frozen=True, slots=True)
class BufferStatus:
    """Connection-wide capacity snapshot."""

    sequence: int
    audio_connection_id: int
    buffer_capacity: int


@dataclass(frozen=True, slots=True)
class PlayerState:
    """Actual local output and device controls."""

    sequence: int
    stream_id: str | None
    output_revision: int
    output_state: OutputState
    position_frame: int
    volume_percent: int
    is_muted: bool
    error: ProtocolError | None


@dataclass(frozen=True, slots=True)
class TransportStatus:
    """Diagnostic receipt state, never a capacity grant."""

    sequence: int
    stream_id: str
    audio_connection_id: int | None
    audio_state: Literal["connecting", "streaming", "eof", "interrupted", "backpressured", "closed"]
    buffered_duration_millis: int
    error: ProtocolError | None


@dataclass(frozen=True, slots=True)
class TrackStarted:
    """Audible promotion of the authorized successor."""

    sequence: int
    stream_id: str
    next_revision: int
    output_revision: int
    position_frame: int


@dataclass(frozen=True, slots=True)
class TrackEnded:
    """Audible end, independent of codec or transport EOF."""

    sequence: int
    stream_id: str


@dataclass(frozen=True, slots=True)
class AudioReconnectRequest:
    """Request one replacement audio binding."""

    sequence: int
    request_id: str


@dataclass(frozen=True, slots=True)
class SetAudioProfile:
    """Request a device-local codec change and immediate authorized stream restart."""

    sequence: int
    request_id: str
    audio_profile: AudioProfileId


@dataclass(frozen=True, slots=True)
class Envelope:
    """Common JSON envelope without implicit MA JSON-RPC behavior."""

    protocol_version: int
    type: str
    message_id: str
    connection_epoch: str | None
    payload: dict[str, Any]


CLIENT_MESSAGES = {
    "hello": Hello,
    "audio_hello": AudioHello,
    "command_ack": CommandAck,
    "chunk_ack": ChunkAck,
    "buffer_status": BufferStatus,
    "player_state": PlayerState,
    "transport_status": TransportStatus,
    "track_started": TrackStarted,
    "track_ended": TrackEnded,
    "audio_reconnect_request": AudioReconnectRequest,
    "set_audio_profile": SetAudioProfile,
}


@dataclass(frozen=True, slots=True)
class Welcome:
    """Reconciled state and one fresh audio binding, never an autoplay command."""

    player_id: str
    provider_version: str
    server_revision: str
    limits: Version1Limits
    audio_profiles: list[AudioProfileId]
    selected_audio_profile: AudioProfileId
    audio_binding_id: str
    current_stream: StreamSnapshot | None
    desired_output_state: Literal["idle", "playing", "paused"]
    next_revision: int
    prepared_next: StreamSnapshot | None


@dataclass(frozen=True, slots=True)
class AudioBinding:
    """Replacement single-use audio binding."""

    request_id: str
    audio_binding_id: str


@dataclass(frozen=True, slots=True)
class AudioProfileChanged:
    """Confirm the selected profile and its fresh stream, or explain a rejection."""

    request_id: str
    status: Literal["accepted", "rejected"]
    selected_audio_profile: AudioProfileId
    stream_id: str | None
    error: ProtocolError | None


@dataclass(frozen=True, slots=True)
class EmptyPayload:
    """An explicitly empty audio_ready payload."""


@dataclass(frozen=True, slots=True)
class SetMedia:
    """Atomically replace the current playback context."""

    command_id: int
    stream: StreamDescriptor
    autoplay: bool


@dataclass(frozen=True, slots=True)
class PrepareNext:
    """Register one MA-authorized successor."""

    command_id: int
    current_stream_id: str
    stream: StreamDescriptor
    next_revision: int


@dataclass(frozen=True, slots=True)
class ClearNext:
    """Revoke one unpromoted successor without replacing the current stream."""

    command_id: int
    current_stream_id: str
    next_revision: int


@dataclass(frozen=True, slots=True)
class StreamCommand:
    """Play, pause or stop a specific current stream."""

    command_id: int
    stream_id: str | None


@dataclass(frozen=True, slots=True)
class VolumeCommand:
    """Set actual device volume independently of stream state."""

    command_id: int
    volume_percent: int


@dataclass(frozen=True, slots=True)
class MuteCommand:
    """Set actual device mute independently of configured volume."""

    command_id: int
    is_muted: bool


@dataclass(frozen=True, slots=True)
class StateRequest:
    """Request current reports without changing playback."""

    command_id: int


@dataclass(frozen=True, slots=True)
class ErrorMessage:
    """Safe operation-scoped failure on Control."""

    correlation_id: str | None
    stream_id: str | None
    audio_connection_id: int | None
    error: ProtocolError


def parse_model(model: type[ModelT], value: object) -> ModelT:
    """Parse exactly the declared fields with strict recursive type validation."""
    if not is_dataclass(model):
        raise ProtocolViolation("invalid_message", "Unknown message model")
    names, hints = _model_schema(model)
    if type(value) is not dict or value.keys() != names:
        raise ProtocolViolation("invalid_message", "Missing or unknown message fields")
    parsed = {name: _parse_value(hints[name], item, name) for name, item in value.items()}
    result = model(**parsed)
    if isinstance(result, ClientCapabilities | OutputFormat | Version1Limits):
        result.validate()
    if isinstance(result, ClientResumeState | AudioHello):
        identifiers = [cursor.stream_id for cursor in result.received_streams]
        if len(identifiers) > 2 or len(identifiers) != len(set(identifiers)):
            raise ProtocolViolation("invalid_message", "Invalid received stream set")
    if isinstance(result, ClientResumeState) and result.current_stream_id is None:
        if (
            result.output_state != "idle"
            or result.position_frame != 0
            or result.output_revision != 0
            or result.next_revision != 0
            or result.received_streams
        ):
            raise ProtocolViolation("invalid_message", "Invalid idle snapshot")
    if isinstance(result, CommandAck | ChunkAck | AudioProfileChanged):
        if (result.status == "accepted") != (result.error is None):
            raise ProtocolViolation("invalid_message", "ACK status contradicts error")
    return result


def decode_message(text: str, epoch: str | None, *, audio: bool = False) -> tuple[Envelope, object]:
    """Decode a bounded client message on its declared channel and epoch."""
    envelope = decode_envelope(text)
    expected = {"audio_hello"} if audio else set(CLIENT_MESSAGES) - {"audio_hello"}
    if envelope.type not in expected or envelope.connection_epoch != epoch:
        raise ProtocolViolation("invalid_message", "Wrong message channel or epoch")
    if (epoch is None) != (envelope.type == "hello"):
        raise ProtocolViolation("invalid_message", "Unexpected handshake")
    return envelope, parse_model(CLIENT_MESSAGES[envelope.type], envelope.payload)


def decode_envelope(text: str) -> Envelope:
    """Validate framing before routing Audio to the device that owns its epoch."""
    if len(text.encode("utf-8")) > 65536:
        raise ProtocolViolation("invalid_message", "Control message too large")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as err:
        raise ProtocolViolation("invalid_message", "Invalid JSON") from err
    envelope = parse_model(Envelope, value)
    if envelope.protocol_version != 1:
        raise ProtocolViolation("unsupported_version", "Mashin protocol version 1 required")
    return envelope


def encode_message(message_type: str, message_id: str, epoch: str | None, payload: object) -> str:
    """Serialize a typed provider payload at the WebSocket boundary."""
    if not is_dataclass(payload) or isinstance(payload, type):
        raise ProtocolViolation("invalid_message", "Expected typed message payload")
    envelope = Envelope(1, message_type, message_id, epoch, asdict(payload))
    result = json.dumps(asdict(envelope), separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(result) > 65536:
        raise ProtocolViolation("incompatible_server", "Control message too large")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field")
        result[name] = value
    return result


def _parse_value(hint: Any, value: object, name: str) -> Any:
    if isinstance(hint, TypeAliasType):
        hint = hint.__value__
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        for option in get_args(hint):
            try:
                return _parse_value(option, value, name)
            except ProtocolViolation:
                continue
        raise ProtocolViolation("invalid_message", "Invalid nullable field")
    if origin is Literal:
        if not any(type(value) is type(option) and value == option for option in get_args(hint)):
            raise ProtocolViolation("invalid_message", "Unknown enumeration value")
        return value
    if hint is type(None):
        if value is not None:
            raise ProtocolViolation("invalid_message", "Expected null")
        return None
    if hint is int:
        minimum = int(
            name
            in {
                "sequence",
                "command_id",
                "audio_connection_id",
                "next_chunk_index",
                "chunk_index",
                "playback_generation",
                "next_received_chunk_index",
                "next_produced_chunk_index",
            }
        )
        return integer(value, minimum, 100 if name == "volume_percent" else MAX_INTEGER)
    if hint is bool:
        if type(value) is not bool:
            raise ProtocolViolation("invalid_message", "Expected boolean")
        return value
    if hint is str or hint is AudioProfileId:
        if type(value) is not str or not value:
            raise ProtocolViolation("invalid_message", "Expected nonempty string")
        if name not in ("client_name", "client_version", "message") and (
            not value.isascii() or len(value) > 128
        ):
            raise ProtocolViolation("invalid_message", "Invalid ASCII identifier")
        if name in ("stream_id", "current_stream_id", "playback_instance_id"):
            stream_uuid(value)
        if hint is AudioProfileId:
            try:
                return AudioProfileId(value)
            except ValueError as err:
                raise ProtocolViolation("profile_unsupported", "Unknown audio profile") from err
        return value
    if origin is list:
        if type(value) is not list or len(value) > 128:
            raise ProtocolViolation("invalid_message", "Invalid array")
        return [_parse_value(get_args(hint)[0], item, name) for item in value]
    if origin is dict:
        if type(value) is not dict:
            raise ProtocolViolation("invalid_message", "Expected object")
        return value
    return parse_model(hint, value)


@cache
def _model_schema(model: type[Any]) -> tuple[frozenset[str], dict[str, Any]]:
    return frozenset(entry.name for entry in fields(model)), get_type_hints(model)
