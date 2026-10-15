"""Multiple client registration and persistent ownership boundary tests."""

from __future__ import annotations

import asyncio
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
        """Register two clients under one user without replacing either player."""
        first, second = await asyncio.gather(
            self.registry.register("desktop", "user", "Desktop", lambda _: True),
            self.registry.register("phone", "user", "Phone", lambda _: True),
        )
        assert first.player_id != second.player_id
        assert set(self.registry.devices) == {"desktop", "phone"}
        assert self.create.await_count == 2

    async def test_reconnect_reuses_runtime_and_identity(self) -> None:
        """Repeated concurrent registration has exactly one creation effect."""
        first, second = await asyncio.gather(
            self.registry.register("desktop", "user", "Desktop", lambda _: True),
            self.registry.register("desktop", "user", "Renamed", lambda _: True),
        )
        assert first is second
        self.create.assert_awaited_once()
        self.save.assert_called_once()

    async def test_ownership_survives_registry_reload(self) -> None:
        """A server reload preserves identity but no old sockets or runtime objects."""
        original = await self.registry.register("phone", "owner", "Phone", lambda _: True)
        restored = DeviceRegistry(self.save.call_args.args[0], self.save, self.create)
        assert not restored.devices
        with pytest.raises(ProtocolViolation):
            await restored.register("phone", "other-user", "Other", lambda _: True)
        reconnected = await restored.register("phone", "owner", "Phone", lambda _: True)
        assert reconnected.player_id == original.player_id

    async def test_acl_is_checked_before_creation_and_on_reconnect(self) -> None:
        """Denied registrations cannot create or claim devices."""
        with pytest.raises(ProtocolViolation):
            await self.registry.register("phone", "owner", "Phone", lambda _: False)
        self.save.assert_not_called()
        self.create.assert_not_awaited()
        await self.registry.register("phone", "owner", "Phone", lambda _: True)
        with pytest.raises(ProtocolViolation):
            await self.registry.register("phone", "owner", "Phone", lambda _: False)

    async def test_audio_cannot_register_or_take_over_device(self) -> None:
        """Audio lookup is limited to its authenticated Control owner."""
        with pytest.raises(ProtocolViolation):
            self.registry.lookup("phone", "owner", lambda _: True)
        device = await self.registry.register("phone", "owner", "Phone", lambda _: True)
        assert self.registry.lookup("phone", "owner", lambda _: True) is device
        with pytest.raises(ProtocolViolation):
            self.registry.lookup("phone", "other-user", lambda _: True)
        with pytest.raises(ProtocolViolation):
            self.registry.lookup("phone", "owner", lambda _: False)

    async def test_malformed_persistence_fails_closed(self) -> None:
        """Malformed owner records are not silently treated as unclaimed devices."""
        for stored in ("{}", '{"version":true,"devices":[]}', '{"version":1,"devices":[{}]}'):
            with self.subTest(stored=stored), pytest.raises(ProtocolViolation):
                DeviceRegistry(stored, self.save, self.create)

    async def test_unload_blocks_all_new_registrations(self) -> None:
        """No client can register through a shutting-down provider."""
        self.registry.closed = True
        with pytest.raises(ProtocolViolation):
            await self.registry.register("phone", "owner", "Phone", lambda _: True)

    async def test_admission_limits_do_not_persist_or_create_rejected_devices(self) -> None:
        """User and global quotas apply before persistence and runtime creation."""
        registry = DeviceRegistry(
            None, self.save, self.create, max_devices=2, max_devices_per_user=1
        )
        desktop = await registry.register("desktop", "alice", "Desktop", lambda _: True)
        with pytest.raises(ProtocolViolation, match="admission limit"):
            await registry.register("phone", "alice", "Phone", lambda _: True)
        await registry.register("phone", "bob", "Phone", lambda _: True)
        with pytest.raises(ProtocolViolation, match="admission limit"):
            await registry.register("tablet", "carol", "Tablet", lambda _: True)
        assert await registry.register("desktop", "alice", "Desktop", lambda _: True) is desktop
        assert self.save.call_count == self.create.await_count == 2

    async def test_lowered_limits_preserve_restored_device_reconnects(self) -> None:
        """Lowering an admission quota cannot orphan existing ownership records."""
        await self.registry.register("desktop", "alice", "Desktop", lambda _: True)
        await self.registry.register("phone", "alice", "Phone", lambda _: True)
        restored = DeviceRegistry(
            self.save.call_args.args[0],
            self.save,
            self.create,
            max_devices=1,
            max_devices_per_user=1,
        )
        await restored.register("desktop", "alice", "Desktop", lambda _: True)
        await restored.register("phone", "alice", "Phone", lambda _: True)
        with pytest.raises(ProtocolViolation, match="admission limit"):
            await restored.register("tablet", "alice", "Tablet", lambda _: True)
