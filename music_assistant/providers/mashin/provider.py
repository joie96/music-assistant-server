"""Token-free Mashin LAN Control/Audio routes and provider lifecycle."""

# ruff: noqa: TRY301

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from aiohttp import WSMsgType, web
from music_assistant_models.enums import EventType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.ffmpeg import check_ffmpeg_version
from music_assistant.models.player_provider import PlayerProvider

from .adapter import MAAdapter
from .config import entries
from .devices import DeviceBinding, DeviceRegistry
from .models import (
    AudioHello,
    AudioProfileId,
    CommandAck,
    EmptyPayload,
    ErrorMessage,
    Hello,
    decode_envelope,
    decode_message,
    encode_message,
    parse_model,
)
from .player import MashinPlayer
from .session import MashinSession, safe_error
from .wire import ProtocolViolation

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.event import MassEvent


class MashinProvider(PlayerProvider):
    """Serve independent LAN devices through one pair of shared routes."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return admission settings; profiles are requested by each client."""
        return entries()

    async def handle_async_init(self) -> None:
        """Restore device identities and prepare shared runtime configuration."""
        self.removers: list[Callable[[], None]] = []
        self.sockets: set[web.WebSocketResponse] = set()
        self.unloading = False
        self.max_sockets = self.get_config_value("max_websocket_connections", 128, return_type=int)
        if self.max_sockets < 2:
            raise SetupFailedError("Invalid Mashin WebSocket admission limit")
        await check_ffmpeg_version()
        build = getattr(self.mass, "build_info", None)
        self.server_revision = (
            build["revision"] if build and build.get("revision") else self.mass.version
        )
        stored = self.get_setup_value("device_bindings")
        if stored is not None and not isinstance(stored, str):
            raise SetupFailedError("Invalid stored Mashin device bindings")
        try:
            self.registry = DeviceRegistry[MashinDevice](
                stored,
                self._save_bindings,
                self._create_device,
                max_devices=self.get_config_value("max_registered_devices", 32, return_type=int),
            )
        except ProtocolViolation as err:
            raise SetupFailedError(
                "Invalid Mashin device storage; remove and re-add the provider for LAN access"
            ) from err
        self.logger.debug("Restored %s Mashin device identities", len(self.registry.bindings))

    async def loaded_in_mass(self) -> None:
        """Register shared routes; players are discovered through Control hello."""
        self.removers.extend(
            [
                self.mass.webserver.register_dynamic_route(
                    "/mashin/v1/control", self._control_route, "GET"
                ),
                self.mass.webserver.register_dynamic_route(
                    "/mashin/v1/audio", self._audio_route, "GET"
                ),
            ]
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Stop discovery and release all device runtimes."""
        del is_removed
        self.unloading = True
        for remove in self.removers:
            remove()
        self.removers.clear()
        if not hasattr(self, "registry"):
            return
        self.registry.closed = True
        self.logger.debug("Unloading Mashin provider with %s devices", len(self.registry.devices))
        tasks = [
            self.mass.create_task(device.unload())
            for device in list(self.registry.devices.values())
        ]
        tasks.extend(
            self.mass.create_task(self._shutdown_socket(socket)) for socket in list(self.sockets)
        )
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in done:
                task.result()
            if pending:
                self.logger.warning("Mashin device cleanup continues in %s MA tasks", len(pending))

    def check_request(self, request: web.Request) -> None:
        """Accept token-free HTTP or HTTPS requests while the provider is available."""
        if self.unloading:
            raise web.HTTPServiceUnavailable
        if request.query_string:
            raise web.HTTPBadRequest(reason="Mashin does not accept query parameters")

    def _save_bindings(self, stored: str) -> None:
        self._update_setup_data("device_bindings", stored, immediate=True)

    async def _create_device(self, binding: DeviceBinding) -> MashinDevice:
        if not self.mass.config.get_raw_player_config_value(binding.player_id, "enabled", True):
            raise ProtocolViolation("forbidden", "Mashin player is disabled")
        device = MashinDevice(self, binding)
        registered = False
        try:
            await device.register()
            if self.unloading:
                raise ProtocolViolation("stream_unavailable", "Provider is unloading")
            registered = True
            return device
        finally:
            if not registered:
                await device.unload()

    async def _control_route(self, request: web.Request) -> web.WebSocketResponse:
        return await self._route(request, audio=False)

    async def _audio_route(self, request: web.Request) -> web.WebSocketResponse:
        return await self._route(request, audio=True)

    async def _route(self, request: web.Request, *, audio: bool) -> web.WebSocketResponse:
        self.check_request(request)
        if len(self.sockets) >= self.max_sockets:
            raise web.HTTPTooManyRequests(reason="Mashin connection admission limit reached")
        socket = web.WebSocketResponse(autoping=False, compress=False, max_msg_size=65536)
        device: MashinDevice | None = None
        self.sockets.add(socket)
        try:
            await socket.prepare(request)
            if self.unloading:
                raise web.HTTPServiceUnavailable
            async with asyncio.timeout(5):
                message = await socket.receive()
                if message.type != WSMsgType.TEXT:
                    raise ProtocolViolation("invalid_message", "Expected device handshake")
                hello: AudioHello | Hello
                epoch: str | None = None
                if audio:
                    envelope = decode_envelope(message.data)
                    if envelope.type != "audio_hello" or envelope.connection_epoch is None:
                        raise ProtocolViolation("invalid_message", "Expected audio_hello")
                    epoch = envelope.connection_epoch
                    hello = parse_model(AudioHello, envelope.payload)
                else:
                    _, candidate = decode_message(message.data, None)
                    if not isinstance(candidate, Hello):
                        raise ProtocolViolation("invalid_message", "Expected hello")
                    hello = candidate
                    hello.capabilities.validate()

            if isinstance(hello, AudioHello):
                device = self.registry.lookup(hello.device_id)
                if epoch is None or epoch != device.session.epoch:
                    raise ProtocolViolation(
                        "forbidden", "Audio belongs to a different device epoch"
                    )
                return await device._audio_route(request, socket, hello, epoch)
            async with asyncio.timeout(30):
                device = await self.registry.register(hello.device_id, hello.client_name)
            return await device._control_route(request, socket, hello)
        except (ProtocolViolation, TimeoutError, ConnectionError, web.HTTPException) as err:
            code = err.code if isinstance(err, ProtocolViolation) else type(err).__name__
            self.logger.debug(
                "Mashin %s handshake rejected: %s", "Audio" if audio else "Control", code
            )
            await socket.close(code=1008, message=b"Mashin handshake failed")
            return socket
        finally:
            if device is None or socket not in device.retired_sockets:
                self.sockets.discard(socket)
            if socket.prepared:
                await socket.close()

    async def _shutdown_socket(self, socket: web.WebSocketResponse) -> None:
        if socket.prepared:
            await socket.close()


class MashinDevice:
    """Own one client's player, session, sockets, locks and cancellable workers."""

    def __init__(self, provider: MashinProvider, binding: DeviceBinding) -> None:
        """Initialize an independent runtime without restoring playback or sockets."""
        self.provider = provider
        self.mass = provider.mass
        self.device_id = binding.device_id
        self.tasks: set[asyncio.Task[None]] = set()
        self.removers: list[Callable[[], None]] = []
        self.control_socket: web.WebSocketResponse | None = None
        self.audio_socket: web.WebSocketResponse | None = None
        self.control_request: web.Request | None = None
        self.audio_request: web.Request | None = None
        self.control_workers: set[asyncio.Task[None]] = set()
        self.retired_sockets: dict[web.WebSocketResponse, asyncio.Task[None]] = {}
        self.handshake_lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task[None] | None = None
        self.player = MashinPlayer(provider, self, binding.player_id, binding.name)
        self.session = MashinSession(
            MAAdapter(self.mass, self.player),
            self.device_id,
            AudioProfileId.FLAC,
            self.player.report,
            self._local_stop,
            self._fallback,
            self._spawn,
        )
        self.session.close_control = self._close_control
        self.session.close_audio = self._close_audio

    async def register(self) -> None:
        """Register only this device's player and queue-event subscription."""
        await self.mass.players.register(self.player)
        self.player.logger.debug("Registered Mashin player %s", self.player.player_id)
        self.removers.append(
            self.mass.subscribe(
                self._queue_changed,
                (EventType.QUEUE_UPDATED, EventType.QUEUE_ITEMS_UPDATED),
                self.player.player_id,
            )
        )
        self.session.start_workers()

    async def unload(self) -> None:
        """Quiesce the device while keeping slow process cleanup owned by MA."""
        for remove in self.removers:
            remove()
        self.removers.clear()
        self.session.closed = True
        self.session.buffer.disconnect()
        self.session.send_control = self.session.send_audio = None
        self.player.connected(False)
        if self.cleanup_task is None:
            self.cleanup_task = self.mass.create_task(self._finish_unload())
        done, pending = await asyncio.wait([self.cleanup_task], timeout=5)
        if done:
            self.cleanup_task.result()
        if pending:
            self.player.logger.warning("Player cleanup continues after shutdown deadline")

    def _spawn(self, target: Awaitable[None]) -> asyncio.Task[None]:
        task = self.mass.create_task(target)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _check_connection(self, request: web.Request) -> None:
        self.provider.check_request(request)
        if self.session.closed or not self.mass.config.get_raw_player_config_value(
            self.player.player_id, "enabled", True
        ):
            raise web.HTTPForbidden

    async def _control_route(  # noqa: PLR0915
        self,
        request: web.Request,
        socket: web.WebSocketResponse,
        hello: Hello,
    ) -> web.WebSocketResponse:
        await self._check_connection(request)
        queue: asyncio.Queue[tuple[int, object]] = asyncio.Queue(maxsize=64)
        received_order = 0
        worker: asyncio.Task[None] | None = None
        pinger: asyncio.Task[None] | None = None
        pong = asyncio.Event()
        expected: list[bytes] = []
        control_lock = asyncio.Lock()
        welcome_sent = False
        try:
            async with asyncio.timeout(30), self.handshake_lock:
                await self._check_connection(request)
                await self._close_control()
                await self._close_audio()
                self.control_socket = socket
                self.control_request = request

                async def send(kind: str, payload: object) -> None:
                    async with control_lock:
                        if self.control_socket is not socket or self.session.epoch is None:
                            raise ConnectionError("Stale Control connection")
                        await socket.send_str(
                            encode_message(kind, str(uuid4()), self.session.epoch, payload)
                        )

                self.player.connected(True)
                welcome = await self.session.connect(hello, send, self.provider.server_revision)
                await send("welcome", welcome)
                welcome_sent = True
                self._spawn(self.session.after_welcome())
            epoch = self.session.epoch
            worker = self._spawn(self._reports(socket, queue))
            pinger = self._spawn(self._pings(request, socket, expected, pong))
            self.control_workers.update((worker, pinger))
            worker.add_done_callback(self.control_workers.discard)
            pinger.add_done_callback(self.control_workers.discard)
            async for message in socket:
                if message.type == WSMsgType.PING:
                    await socket.pong(message.data)
                elif message.type == WSMsgType.PONG:
                    if expected and secrets.compare_digest(message.data, expected[0]):
                        pong.set()
                elif message.type == WSMsgType.TEXT:
                    if self.control_socket is not socket or self.session.epoch != epoch:
                        break
                    _, report = decode_message(message.data, epoch)
                    received_order += 1
                    try:
                        if isinstance(report, CommandAck):
                            await self.session.report(report, received_order=received_order)
                        else:
                            queue.put_nowait((received_order, report))
                    except asyncio.QueueFull as err:
                        raise ProtocolViolation(
                            "rate_limited", "Control input budget exhausted"
                        ) from err
                elif message.type in (WSMsgType.BINARY, WSMsgType.ERROR):
                    raise ProtocolViolation("invalid_message", "Unexpected Control frame")
        except (ProtocolViolation, TimeoutError, ConnectionError) as err:
            code = err.code if isinstance(err, ProtocolViolation) else "stream_unavailable"
            self.player.logger.debug(
                "Control connection failed for player %s: %s", self.player.player_id, code
            )
            await self._error(socket, code, welcome=welcome_sent)
        finally:
            for task in (worker, pinger):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (worker, pinger) if task), return_exceptions=True
            )
            if self.control_socket is socket:
                self.control_socket = None
                self.control_request = None
                await self.session.control_lost()
                await self._close_audio()
                self.player.connected(False)
                self.player.logger.debug(
                    "Client disconnected from player %s", self.player.player_id
                )
            await socket.close()
        return socket

    async def _audio_route(
        self,
        request: web.Request,
        socket: web.WebSocketResponse,
        hello: AudioHello,
        epoch: str,
    ) -> web.WebSocketResponse:
        await self._check_connection(request)
        try:
            async with asyncio.timeout(30), self.handshake_lock:
                if epoch != self.session.epoch:
                    raise ProtocolViolation(
                        "forbidden", "Audio belongs to a different device epoch"
                    )
                await self._check_connection(request)

                async def send(data: bytes) -> None:
                    if self.audio_socket is not socket:
                        raise ConnectionError("Stale Audio connection")
                    await socket.send_bytes(data)

                self.session.bind_audio(hello, send)
                previous, self.audio_socket = self.audio_socket, socket
                previous_request, self.audio_request = self.audio_request, request
                if previous is not None:
                    self._retire_socket(previous, previous_request)
                assert self.session.epoch is not None
                await socket.send_str(
                    encode_message("audio_ready", str(uuid4()), self.session.epoch, EmptyPayload())
                )
                self.session.audio_ready()
                self.player.logger.debug("Audio connected for player %s", self.player.player_id)
            async for message in socket:
                if message.type == WSMsgType.PING:
                    await socket.pong(message.data)
                elif message.type not in (WSMsgType.PONG, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    raise ProtocolViolation("invalid_message", "Client Audio data is forbidden")
        except (ProtocolViolation, TimeoutError, ConnectionError) as err:
            code = err.code if isinstance(err, ProtocolViolation) else "stream_unavailable"
            self.player.logger.debug(
                "Audio connection failed for player %s: %s", self.player.player_id, code
            )
            if (
                self.session.epoch == epoch
                and hello.audio_binding_id == self.session.binding
                and self.audio_socket in (None, socket)
                and self.session.send_control
            ):
                await self.session.send_control(
                    "error",
                    ErrorMessage(
                        None,
                        None,
                        self.session.buffer.audio_connection_id,
                        safe_error(
                            err.code
                            if isinstance(err, ProtocolViolation)
                            else "stream_unavailable",
                            "Audio setup failed",
                        ),
                    ),
                )
        finally:
            if self.audio_socket is socket:
                self.audio_socket = None
                self.audio_request = None
                await self.session.audio_lost()
            await socket.close()
        return socket

    async def _reports(
        self, socket: web.WebSocketResponse, queue: asyncio.Queue[tuple[int, object]]
    ) -> None:
        try:
            while self.control_socket is socket:
                received_order, report = await queue.get()
                if self.control_socket is not socket:
                    return
                await self.session.report(report, received_order=received_order)
        except ProtocolViolation as err:
            self.player.logger.debug(
                "Client report rejected for player %s: %s", self.player.player_id, err.code
            )
            await self._error(socket, err.code)
            await socket.close()

    async def _pings(
        self,
        request: web.Request,
        socket: web.WebSocketResponse,
        expected: list[bytes],
        pong: asyncio.Event,
    ) -> None:
        try:
            next_ping = asyncio.get_running_loop().time() + 5
            while self.control_socket is socket:
                await asyncio.sleep(max(0, next_ping - asyncio.get_running_loop().time()))
                if self.control_socket is not socket:
                    return
                pong.clear()
                expected[:] = [secrets.token_bytes(16)]
                async with asyncio.timeout(15):
                    await socket.ping(expected[0])
                    await self._check_connection(request)
                    await pong.wait()
                expected.clear()
                next_ping += 5
                while next_ping <= asyncio.get_running_loop().time():
                    next_ping += 5
        except web.HTTPException:
            if self.control_socket is socket:
                await self._disable()
        except TimeoutError, ConnectionError:
            if self.control_socket is socket:
                self.player.logger.warning(
                    "Control liveness lost for player %s", self.player.player_id
                )
                await self.session.control_lost()
            await socket.close()

    async def _error(
        self, socket: web.WebSocketResponse, code: str, *, welcome: bool = True
    ) -> None:
        if not welcome:
            with suppress(ConnectionError):
                await socket.send_str(
                    encode_message(
                        "error",
                        str(uuid4()),
                        None,
                        ErrorMessage(None, None, None, safe_error(code, "Mashin handshake failed")),
                    )
                )
            return
        if self.control_socket is socket and self.session.send_control:
            with suppress(ConnectionError):
                await self.session.send_control(
                    "error",
                    ErrorMessage(
                        None,
                        None,
                        self.session.buffer.audio_connection_id,
                        safe_error(code, "Mashin operation failed"),
                    ),
                )

    async def _close_control(self) -> None:
        socket, self.control_socket = self.control_socket, None
        request, self.control_request = self.control_request, None
        workers = tuple(self.control_workers)
        self.control_workers.clear()
        for worker in workers:
            if worker is not asyncio.current_task():
                worker.cancel()
        if socket is not None:
            await self.session.control_lost()
            self._retire_socket(socket, request)

    async def _close_audio(self) -> None:
        socket, self.audio_socket = self.audio_socket, None
        request, self.audio_request = self.audio_request, None
        if socket is not None:
            await self.session.audio_lost()
            self._retire_socket(socket, request)

    def _retire_socket(
        self, socket: web.WebSocketResponse, request: web.Request | None = None
    ) -> None:
        if socket in self.retired_sockets:
            return
        task = self._spawn(self._finish_socket_close(socket, request))
        self.retired_sockets[socket] = task

    async def _finish_socket_close(
        self, socket: web.WebSocketResponse, request: web.Request | None
    ) -> None:
        transport = request.transport if request is not None else None
        try:
            async with asyncio.timeout(5):
                await socket.close(drain=False)
        except TimeoutError, ConnectionError, OSError:
            self.player.logger.debug("Retired WebSocket close did not finish normally")
        finally:
            if transport is not None:
                transport.close()
            self.retired_sockets.pop(socket, None)
            self.provider.sockets.discard(socket)

    async def _local_stop(self) -> None:
        await self.mass.player_queues.stop(self.player.player_id)

    async def _fallback(self, position: int, paused: bool) -> None:
        if self.control_request is None or self.control_socket is None:
            raise ProtocolViolation(
                "stream_unavailable", "MA seek requires an active Control connection"
            )
        try:
            epoch = self.session.epoch
            await self._check_connection(self.control_request)
        except web.HTTPException as err:
            raise ProtocolViolation("forbidden", "Mashin player is unavailable") from err
        if self.session.epoch != epoch:
            raise ProtocolViolation("stale_playback", "MA seek belongs to an old Control epoch")
        target = self.session.profile_restart_run
        if target is not None and (
            self.session.current is not target or not self.session.adapter.valid(target.context)
        ):
            raise ProtocolViolation("stale_playback", "Codec restart context changed")
        self.player.logger.debug(
            "Seeking player %s to %s seconds (paused=%s)", self.player.player_id, position, paused
        )
        await self.mass.player_queues.seek(self.player.player_id, position)
        if paused:
            await self.mass.player_queues.pause(self.player.player_id)

    async def _queue_changed(self, event: MassEvent) -> None:
        del event
        run = self.session.next
        current = self.session.current
        if run is not None:
            data = self.mass.player_queues.queue_data_or_none(run.context.queue_id)
            if (
                data is not None
                and data.queue.current_item is run.context.item
                and run.context.item.available
                and not self.player.state.group_members
            ):
                return
            if (
                data is not None
                and current is not None
                and data.queue.current_item is current.context.item
                and self.session.adapter.reauthorize_successor(
                    run.context, replace(current.context, session_id=data.session_id)
                ) is not None
            ):
                return
        if (
            run
            and current
            and not self.session.adapter.valid(
                run.context, successor_of=current.context.queue_item_id
            )
        ):
            await self.session.clear_next()

    async def _disable(self) -> None:
        self.player.logger.info("Mashin player %s disabled", self.player.player_id)
        self.session.buffer.disconnect()
        try:
            with suppress(ConnectionError, ProtocolViolation):
                await self.session.control("stop")
            self.session.suppress_stop_command = True
            await self._local_stop()
        finally:
            self.session.suppress_stop_command = False
            try:
                await self.session.reset()
            finally:
                try:
                    await asyncio.gather(self._close_audio(), self._close_control())
                finally:
                    self.player.connected(False)

    async def _finish_unload(self) -> None:
        closing = set(self.retired_sockets.values())
        tasks = [task for task in self.tasks if task not in closing]
        for task in tasks:
            task.cancel()
        try:
            results = await asyncio.gather(
                self._close_control(),
                self._close_audio(),
                self.session.shutdown(),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            await asyncio.gather(*list(self.retired_sockets.values()), return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
            current = self.mass.players.get_player(self.player.player_id)
            if current is None or current is self.player:
                await self.mass.players.unregister(self.player.player_id)
