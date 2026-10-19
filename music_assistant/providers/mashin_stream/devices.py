"""Persistent LAN device identities and independent runtime registration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from .wire import ProtocolViolation


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    """Stable client identity independent of MA users and sockets."""

    device_id: str
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
    ) -> None:
        """Restore device identities without restoring sockets or playback state."""
        self.bindings: dict[str, DeviceBinding] = {}
        self.devices: dict[str, DeviceT] = {}
        self.save = save
        self.create = create
        self.lock = asyncio.Lock()
        self.closed = False
        self.max_devices = max_devices
        if max_devices < 1:
            raise ProtocolViolation("incompatible_server", "Invalid device admission limits")
        if stored is not None:
            self._restore(stored)

    async def register(
        self,
        device_id: str,
        name: str,
    ) -> DeviceT:
        """Persist a LAN device identity and activate its independent player."""
        if not device_id or not device_id.isascii() or len(device_id) > 128:
            raise ProtocolViolation("invalid_message", "Invalid device ID")
        async with self.lock:
            if self.closed:
                raise ProtocolViolation("stream_unavailable", "Provider is unloading")
            binding = self.bindings.get(device_id)
            binding = binding or DeviceBinding(device_id, name[:128] or "Mashin")
            if device_id in self.devices:
                return self.devices[device_id]
            if device_id not in self.bindings:
                if len(self.bindings) >= self.max_devices:
                    raise ProtocolViolation("rate_limited", "Device admission limit reached")
                updated = {**self.bindings, device_id: binding}
                self.save(
                    json.dumps(
                        {
                            "version": 2,
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

    def lookup(self, device_id: str) -> DeviceT:
        """Resolve Audio only to a device already registered through Control."""
        binding = self.bindings.get(device_id)
        if self.closed or binding is None:
            raise ProtocolViolation("forbidden", "Device is not registered")
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
            or data["version"] != 2
            or not isinstance(data["devices"], list)
        ):
            raise ProtocolViolation(
                "incompatible_server",
                "Recreate the Mashin Stream provider for LAN device registration",
            )
        for value in data["devices"]:
            if not isinstance(value, dict) or set(value) != {"device_id", "name"}:
                raise ProtocolViolation("incompatible_server", "Invalid stored device binding")
            identifier = value["device_id"]
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
