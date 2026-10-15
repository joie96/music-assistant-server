"""Persistent device ownership and independent runtime registration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from .wire import ProtocolViolation


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    """Stable client identity bound to one MA user, never to a token or socket."""

    device_id: str
    user_id: str
    name: str

    @property
    def player_id(self) -> str:
        """Return the same device-derived player ID across reconnects and restarts."""
        return "mashin_" + hashlib.sha256(self.device_id.encode("ascii")).hexdigest()[:32]


class DeviceRegistry[DeviceT]:
    """Register many devices while serializing only identity creation."""

    def __init__(
        self,
        stored: str | None,
        save: Callable[[str], None],
        create: Callable[[DeviceBinding], Awaitable[DeviceT]],
        *,
        max_devices: int = 32,
        max_devices_per_user: int = 8,
    ) -> None:
        """Restore ownership records without restoring sockets or playback authority."""
        self.bindings: dict[str, DeviceBinding] = {}
        self.devices: dict[str, DeviceT] = {}
        self.save = save
        self.create = create
        self.lock = asyncio.Lock()
        self.closed = False
        self.max_devices = max_devices
        self.max_devices_per_user = max_devices_per_user
        if min(max_devices, max_devices_per_user) < 1:
            raise ProtocolViolation("incompatible_server", "Invalid device admission limits")
        if stored is not None:
            self._restore(stored)

    async def register(
        self,
        device_id: str,
        user_id: str,
        name: str,
        allowed: Callable[[str], bool],
    ) -> DeviceT:
        """Authorize ownership and ACL before persisting or activating a new player."""
        for identifier in (device_id, user_id):
            if not identifier or not identifier.isascii() or len(identifier) > 128:
                raise ProtocolViolation("invalid_message", "Invalid device or user ID")
        async with self.lock:
            if self.closed:
                raise ProtocolViolation("stream_unavailable", "Provider is unloading")
            binding = self.bindings.get(device_id)
            if binding is not None and binding.user_id != user_id:
                raise ProtocolViolation("forbidden", "Device belongs to another MA user")
            binding = binding or DeviceBinding(device_id, user_id, name[:128] or "Mashin")
            if not allowed(binding.player_id):
                raise ProtocolViolation("forbidden", "Player access denied")
            if device_id in self.devices:
                return self.devices[device_id]
            if device_id not in self.bindings:
                if (
                    len(self.bindings) >= self.max_devices
                    or sum(value.user_id == user_id for value in self.bindings.values())
                    >= self.max_devices_per_user
                ):
                    raise ProtocolViolation("rate_limited", "Device admission limit reached")
                updated = {**self.bindings, device_id: binding}
                self.save(
                    json.dumps(
                        {
                            "version": 1,
                            "devices": [asdict(value) for value in updated.values()],
                        },
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                )
                self.bindings = updated
            device = await self.create(binding)
            self.devices[device_id] = device
            return device

    def lookup(self, device_id: str, user_id: str, allowed: Callable[[str], bool]) -> DeviceT:
        """Resolve Audio only to an already registered and authorized Control device."""
        binding = self.bindings.get(device_id)
        if (
            self.closed
            or binding is None
            or binding.user_id != user_id
            or not allowed(binding.player_id)
        ):
            raise ProtocolViolation("forbidden", "Device is not authorized")
        if device_id not in self.devices:
            raise ProtocolViolation("stream_unavailable", "Control registration is required")
        return self.devices[device_id]

    def _restore(self, stored: str) -> None:
        try:
            data = json.loads(stored)
        except (ValueError, TypeError) as err:
            raise ProtocolViolation(
                "incompatible_server", "Invalid stored device bindings"
            ) from err
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "devices"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["devices"], list)
        ):
            raise ProtocolViolation("incompatible_server", "Invalid stored device bindings")
        for value in data["devices"]:
            if not isinstance(value, dict) or set(value) != {"device_id", "user_id", "name"}:
                raise ProtocolViolation("incompatible_server", "Invalid stored device binding")
            for identifier in (value["device_id"], value["user_id"]):
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or not identifier.isascii()
                    or len(identifier) > 128
                ):
                    raise ProtocolViolation("incompatible_server", "Invalid stored device identity")
            if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 128:
                raise ProtocolViolation("incompatible_server", "Invalid stored device name")
            if value["device_id"] in self.bindings:
                raise ProtocolViolation("incompatible_server", "Duplicate stored device identity")
            self.bindings[value["device_id"]] = DeviceBinding(**value)
