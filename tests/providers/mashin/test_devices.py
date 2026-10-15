"""Multiple LAN client registration and persistent identity tests."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.mashin.devices import DeviceRegistry
from music_assistant.providers.mashin.wire import ProtocolViolation


class TestDevices(unittest.IsolatedAsyncioTestCase):
    """One device per client does not impose a provider-wide client limit."""

    async def asyncSetUp(self) -> None:
        """Create an explicit side-effect boundary for player creation and persistence."""
        self.save = Mock()
        self.create = AsyncMock(side_effect=lambda binding: binding)
        self.registry = DeviceRegistry(None, self.save, self.create)

    async def test_two_clients_have_independent_players(self) -> None:
        """Register two LAN clients without replacing either player."""
        first, second = await asyncio.gather(
            self.registry.register("desktop", "Desktop"),
            self.registry.register("phone", "Phone"),
        )
        assert first.player_id != second.player_id
        assert set(self.registry.devices) == {"desktop", "phone"}
        assert self.create.await_count == 2

    async def test_reconnect_reuses_runtime_and_identity(self) -> None:
        """Repeated concurrent registration has exactly one creation effect."""
        first, second = await asyncio.gather(
            self.registry.register("desktop", "Desktop"),
            self.registry.register("desktop", "Renamed"),
        )
        assert first is second
        self.create.assert_awaited_once()
        self.save.assert_called_once()

    async def test_identity_survives_registry_reload_without_user_binding(self) -> None:
        """A server reload preserves identity but no old sockets or runtime objects."""
        original = await self.registry.register("phone", "Phone")
        assert json.loads(self.save.call_args.args[0]) == {
            "version": 2,
            "devices": [{"device_id": "phone", "name": "Phone"}],
        }
        restored = DeviceRegistry(self.save.call_args.args[0], self.save, self.create)
        assert not restored.devices
        reconnected = await restored.register("phone", "Other")
        assert reconnected.player_id == original.player_id
        assert reconnected.name == "Phone"

    async def test_invalid_device_ids_cannot_create_devices(self) -> None:
        """Identity validation applies before persistence and player creation."""
        for device_id in ("", "a" * 129, "\N{LATIN SMALL LETTER E WITH ACUTE}"):
            with self.subTest(device_id=device_id), pytest.raises(ProtocolViolation):
                await self.registry.register(device_id, "Phone")
        self.save.assert_not_called()
        self.create.assert_not_awaited()

    async def test_audio_cannot_register_or_take_over_device(self) -> None:
        """Audio cannot create a device before its Control registration."""
        with pytest.raises(ProtocolViolation):
            self.registry.lookup("phone")
        device = await self.registry.register("phone", "Phone")
        assert self.registry.lookup("phone") is device

    async def test_malformed_persistence_fails_closed(self) -> None:
        """Malformed records and the old owner format require explicit reconfiguration."""
        for stored in (
            "{}",
            '{"version":true,"devices":[]}',
            '{"version":1,"devices":[{"device_id":"phone","user_id":"owner","name":"Phone"}]}',
            '{"version":2,"devices":[{}]}',
        ):
            with self.subTest(stored=stored), pytest.raises(ProtocolViolation):
                DeviceRegistry(stored, self.save, self.create)

    async def test_unload_blocks_all_new_registrations(self) -> None:
        """No client can register through a shutting-down provider."""
        self.registry.closed = True
        with pytest.raises(ProtocolViolation):
            await self.registry.register("phone", "Phone")

    async def test_admission_limits_do_not_persist_or_create_rejected_devices(self) -> None:
        """The global quota applies before persistence and runtime creation."""
        registry = DeviceRegistry(None, self.save, self.create, max_devices=2)
        desktop = await registry.register("desktop", "Desktop")
        await registry.register("phone", "Phone")
        with pytest.raises(ProtocolViolation, match="admission limit"):
            await registry.register("tablet", "Tablet")
        assert await registry.register("desktop", "Desktop") is desktop
        assert self.save.call_count == self.create.await_count == 2

    async def test_lowered_limits_preserve_restored_device_reconnects(self) -> None:
        """Lowering an admission quota cannot orphan existing device identities."""
        await self.registry.register("desktop", "Desktop")
        await self.registry.register("phone", "Phone")
        restored = DeviceRegistry(
            self.save.call_args.args[0],
            self.save,
            self.create,
            max_devices=1,
        )
        await restored.register("desktop", "Desktop")
        await restored.register("phone", "Phone")
        with pytest.raises(ProtocolViolation, match="admission limit"):
            await restored.register("tablet", "Tablet")
