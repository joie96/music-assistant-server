"""Strict JSON and capability negotiation tests."""

import json
import unittest
from dataclasses import asdict, fields
from uuid import uuid4

import pytest

from music_assistant.providers.mashin.config import entries
from music_assistant.providers.mashin.models import (
    AudioProfileChanged,
    AudioProfileId,
    BufferStatus,
    ClientCapabilities,
    ClientResumeState,
    Hello,
    SetAudioProfile,
    Version1Limits,
    decode_message,
    parse_model,
)
from music_assistant.providers.mashin.wire import ProtocolViolation


class TestModels(unittest.TestCase):
    """Reject loose JSON and incomplete V1 handshakes."""

    def test_provider_settings_do_not_duplicate_client_profile_or_core_logging(self) -> None:
        """The client chooses quality; the core owns the standard logging entry."""
        assert [entry.key for entry in entries()] == [
            "max_registered_devices",
            "max_websocket_connections",
        ]

    def test_hello_requires_client_audio_profile(self) -> None:
        """Codec and bitrate are explicitly requested, not inferred from capabilities."""
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        values = json.loads(
            json.dumps(
                asdict(
                    Hello("device", "Client", "0.1.0", None, capabilities, AudioProfileId.OPUS_128K)
                )
            )
        )
        parsed = parse_model(Hello, values)
        assert parsed.audio_profile == AudioProfileId.OPUS_128K
        _, decoded = decode_message(
            json.dumps(
                {
                    "protocol_version": 1,
                    "type": "hello",
                    "message_id": str(uuid4()),
                    "connection_epoch": None,
                    "payload": values,
                }
            ),
            None,
        )
        assert isinstance(decoded, Hello)
        assert decoded.audio_profile == AudioProfileId.OPUS_128K
        for invalid in (
            {**values, "audio_profile": "mp3"},
            {key: value for key, value in values.items() if key != "audio_profile"},
        ):
            with self.subTest(payload=invalid), pytest.raises(ProtocolViolation):
                parse_model(Hello, invalid)

    def test_all_limits_required_and_fixed(self) -> None:
        """Require exactly 39 fields and reject altered constant values."""
        limits = asdict(Version1Limits())
        assert len(fields(Version1Limits)) == 39
        parse_model(Version1Limits, limits)
        for name in limits:
            invalid = dict(limits)
            del invalid[name]
            with self.subTest(name=name), pytest.raises(ProtocolViolation):
                parse_model(Version1Limits, invalid)
        limits["max_unacknowledged_audio_chunks"] = 2
        with pytest.raises(ProtocolViolation):
            parse_model(Version1Limits, limits)

    def test_profile_change_messages_are_strict_and_epoch_bound(self) -> None:
        """Runtime codec selection uses the Control epoch and exact request/reply schemas."""
        envelope = {
            "protocol_version": 1,
            "type": "set_audio_profile",
            "message_id": "profile-message-1",
            "connection_epoch": "epoch",
            "payload": {"sequence": 1, "request_id": "profile-1", "audio_profile": "opus_96k"},
        }
        _, request = decode_message(json.dumps(envelope), "epoch")
        assert isinstance(request, SetAudioProfile)
        assert request.audio_profile == AudioProfileId.OPUS_96K
        with pytest.raises(ProtocolViolation):
            decode_message(json.dumps(envelope), "other")
        with pytest.raises(ProtocolViolation):
            decode_message(json.dumps(envelope), "epoch", audio=True)
        response = {
            "request_id": "profile-1",
            "status": "accepted",
            "selected_audio_profile": "opus_96k",
            "stream_id": None,
            "error": None,
        }
        parse_model(AudioProfileChanged, response)
        with pytest.raises(ProtocolViolation):
            parse_model(AudioProfileChanged, {**response, "status": "rejected"})

    def test_capabilities(self) -> None:
        """Require all five profiles without duplicates and actual booleans."""
        values = json.loads(
            json.dumps(
                asdict(ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True))
            )
        )
        parse_model(ClientCapabilities, values)
        for key, value in (
            ("audio_profiles", ["flac"]),
            ("sample_rates_hz", [44100]),
            ("can_pause", 1),
            ("bit_depths", [16, 24]),
            ("max_channels", "2"),
        ):
            with self.subTest(key=key), pytest.raises(ProtocolViolation):
                parse_model(ClientCapabilities, {**values, key: value})

    def test_exact_fields_and_integer_types(self) -> None:
        """No missing fields, unknown fields or coercion are allowed."""
        valid = {"sequence": 1, "audio_connection_id": 1, "buffer_capacity": 0}
        parse_model(BufferStatus, valid)
        for invalid in ({**valid, "stream_id": str(uuid4())}, {**valid, "sequence": True}, {}):
            with pytest.raises(ProtocolViolation):
                parse_model(BufferStatus, invalid)

    def test_message_channel_epoch_and_duplicate_json(self) -> None:
        """A valid report belongs to exactly one control epoch."""
        envelope = {
            "protocol_version": 1,
            "type": "buffer_status",
            "message_id": "report-1",
            "connection_epoch": "epoch",
            "payload": {
                "sequence": 1,
                "audio_connection_id": 1,
                "buffer_capacity": 0,
            },
        }
        _, report = decode_message(json.dumps(envelope), "epoch")
        assert isinstance(report, BufferStatus)
        with pytest.raises(ProtocolViolation):
            decode_message(json.dumps(envelope), "other")
        with pytest.raises(ProtocolViolation):
            decode_message(json.dumps(envelope), "epoch", audio=True)
        with pytest.raises(ProtocolViolation):
            decode_message('{"type":"hello","type":"hello"}', None)

    def test_idle_snapshot_not_unknown_resume(self) -> None:
        """Preserve known local stop without an audio cursor or old revision."""
        state = ClientResumeState(None, 0, "idle", 0, 50, False, 0, [])
        parse_model(ClientResumeState, asdict(state))
        with pytest.raises(ProtocolViolation):
            parse_model(ClientResumeState, {**asdict(state), "output_revision": 1})
