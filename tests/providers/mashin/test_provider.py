"""Device isolation tests against the real provider with explicit MA boundary mocks."""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from aiohttp import WSMessage, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from music_assistant_models.config_entries import PlayerConfig, ProviderConfig
from music_assistant_models.enums import (
    ContentType,
    IdentifierType,
    PlayerFeature,
    ProviderType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.provider import ProviderManifest

from music_assistant.providers.mashin.devices import DeviceBinding, DeviceRegistry
from music_assistant.providers.mashin.models import (
    AudioHello,
    AudioProfileId,
    ClientCapabilities,
    Hello,
    OutputFormat,
    Welcome,
    encode_message,
    parse_model,
)
from music_assistant.providers.mashin.wire import ProtocolViolation

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.providers.mashin.adapter import MAAdapter, PlaybackContext
    from music_assistant.providers.mashin.provider import MashinDevice, MashinProvider
    from music_assistant.providers.mashin.session import SendControl


class TestProvider(unittest.IsolatedAsyncioTestCase):
    """Keep actual devices, players and sessions while mocking MA controller calls."""

    async def asyncSetUp(self) -> None:
        """Require the real server imports rather than pretending missing dependencies work."""
        if sys.platform == "win32":
            self.skipTest("Complete MA runtime requires POSIX signals (SIGKILL)")
        try:
            module = importlib.import_module("music_assistant.providers.mashin.provider")
        except ModuleNotFoundError as err:
            if err.name is None or err.name.startswith("music_assistant"):
                raise
            self.skipTest(f"Complete MA runtime required: missing {err.name}")
        self.mass = Mock()
        self.mass.create_task.side_effect = asyncio.ensure_future
        self.mass.players.register = AsyncMock()
        self.mass.players.unregister = AsyncMock()
        self.registered_players: dict[str, object] = {}
        self.mass.players.register.side_effect = lambda player: self.registered_players.__setitem__(
            player.player_id, player
        )
        self.mass.players.get_player.side_effect = self.registered_players.get
        self.mass.player_queues.stop = AsyncMock()
        self.mass.config.get_raw_player_config_value.return_value = True
        self.mass.config.get_base_player_config.side_effect = lambda player_id, provider: (
            PlayerConfig({}, provider, player_id)
        )
        self.provider = cast(
            "MashinProvider",
            module.MashinProvider(
                self.mass,
                ProviderManifest(ProviderType.PLAYER, "mashin", "Mashin", "Test", []),
                ProviderConfig({}, ProviderType.PLAYER, "mashin", "mashin_instance"),
            ),
        )
        self.provider.server_revision = "test"
        self.provider.unloading = False
        self.provider.max_sockets = 128
        self.provider.removers = []
        self.provider.sockets = set()
        self.provider.registry = DeviceRegistry(None, Mock(), self.provider._create_device)

    async def asyncTearDown(self) -> None:
        """Cancel real session workers even when an isolation assertion fails."""
        if hasattr(self, "provider"):
            await self.provider.unload()

    async def test_socket_quota_rejects_before_upgrade(self) -> None:
        """Connection slots are checked before accepting another WebSocket."""
        self.provider.max_sockets = 1
        socket = AsyncMock()
        socket.prepared = True
        self.provider.sockets.add(socket)
        with pytest.raises(web.HTTPTooManyRequests):
            await self.provider._route(
                make_mocked_request("GET", "/mashin/v1/control"), audio=False
            )
        assert len(self.provider.sockets) == 1

    async def test_setup_uses_cached_build_revision_or_server_version(self) -> None:
        """Setup does not require the optional build-info helper on older server bases."""
        self.mass.version = "2.17.0"
        for build, expected in (({"revision": "abc123"}, "abc123"), (None, "2.17.0")):
            with self.subTest(build=build):
                self.mass.build_info = build
                with (
                    patch(
                        "music_assistant.providers.mashin.provider.check_ffmpeg_version",
                        AsyncMock(),
                    ),
                    patch.object(self.provider, "get_setup_value", return_value=None),
                ):
                    await self.provider.handle_async_init()
                assert self.provider.server_revision == expected
        del self.mass.build_info
        with (
            patch("music_assistant.providers.mashin.provider.check_ffmpeg_version", AsyncMock()),
            patch.object(self.provider, "get_setup_value", return_value=None),
        ):
            await self.provider.handle_async_init()
        assert self.provider.server_revision == "2.17.0"

    async def test_each_client_gets_its_own_player_and_device_info(self) -> None:
        """Shared provider ownership does not imply shared player or transport state."""
        first, second = await self._device("desktop"), await self._device("phone")
        assert first.player.player_id != second.player.player_id
        assert first.player.provider_id == second.player.provider_id
        assert first.session is not second.session
        assert first.session.buffer is not second.session.buffer
        assert first.tasks is not second.tasks
        assert first.handshake_lock is not second.handshake_lock
        assert first.player.device_info.identifiers[IdentifierType.UNKNOWN] == "desktop"
        assert second.player.device_info.identifiers[IdentifierType.UNKNOWN] == "phone"
        assert PlayerFeature.SET_MEMBERS not in first.player.supported_features
        assert not first.player.can_group_with

    async def test_player_commands_target_only_their_own_session(self) -> None:
        """A player's command must not flow through a provider-global session."""
        first, second = await self._device("desktop"), await self._device("phone")
        with (
            patch.object(first.session, "control", AsyncMock()) as first_control,
            patch.object(second.session, "control", AsyncMock()) as second_control,
        ):
            await first.player.volume_set(30)
            first_control.assert_awaited_once_with("set_volume", 30)
            second_control.assert_not_awaited()
        second.player.connected(True)
        first.player.connected(False)
        assert second.player.available
        assert not first.player.available

    async def test_http_and_https_requests_need_no_token_or_proxy(self) -> None:
        """Transport security and forwarded headers do not gate LAN connections."""
        for secure in (False, True):
            with self.subTest(secure=secure):
                request = Mock(secure=secure, query_string="", headers={})
                self.provider.check_request(request)
        self.mass.webserver.auth.authenticate_with_token.assert_not_called()
        with pytest.raises(web.HTTPBadRequest):
            self.provider.check_request(Mock(query_string="token=secret"))
        self.provider.unloading = True
        with pytest.raises(web.HTTPServiceUnavailable):
            self.provider.check_request(Mock(query_string=""))

    async def test_token_free_websocket_control_and_audio_handshake(self) -> None:
        """Real WS upgrades bind Audio to Control without an Authorization header."""
        app = web.Application()
        app.router.add_get("/mashin/v1/control", self.provider._control_route)
        app.router.add_get("/mashin/v1/audio", self.provider._audio_route)
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        async with (
            TestClient(TestServer(app)) as client,
            client.ws_connect("/mashin/v1/control") as control,
        ):
            await control.send_json(
                {
                    "protocol_version": 1,
                    "type": "hello",
                    "message_id": str(uuid4()),
                    "connection_epoch": None,
                    "payload": asdict(
                        Hello(
                            "desktop",
                            "Desktop",
                            "0.1.0",
                            None,
                            capabilities,
                            AudioProfileId.FLAC,
                        )
                    ),
                }
            )
            response = await asyncio.wait_for(control.receive_json(), 1)
            assert response["type"] == "welcome"
            welcome = parse_model(Welcome, response["payload"])
            async with client.ws_connect("/mashin/v1/audio") as audio:
                await audio.send_str(
                    encode_message(
                        "audio_hello",
                        str(uuid4()),
                        response["connection_epoch"],
                        AudioHello("desktop", welcome.audio_binding_id, 1, []),
                    )
                )
                ready = await asyncio.wait_for(audio.receive_json(), 1)
                assert ready["type"] == "audio_ready"
                assert self.provider.registry.devices["desktop"].session.buffer.ready

    async def test_rejected_resume_sends_error_before_welcome(self) -> None:
        """A rejected resume reaches the new socket without an old Control sender."""
        device = await self._device("desktop")
        app = web.Application()
        app.router.add_get("/mashin/v1/control", self.provider._control_route)
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        with patch.object(
            device.session,
            "connect",
            side_effect=ProtocolViolation("stale_playback", "Playback is no longer current"),
        ):
            async with (
                TestClient(TestServer(app)) as client,
                client.ws_connect("/mashin/v1/control") as control,
            ):
                await control.send_json(
                    {
                        "protocol_version": 1,
                        "type": "hello",
                        "message_id": str(uuid4()),
                        "connection_epoch": None,
                        "payload": asdict(
                            Hello(
                                "desktop",
                                "Desktop",
                                "0.1.0",
                                None,
                                capabilities,
                                AudioProfileId.FLAC,
                            )
                        ),
                    }
                )
                response = await asyncio.wait_for(control.receive_json(), 1)
                assert response["type"] == "error"
                assert response["connection_epoch"] is None
                assert response["payload"]["error"]["code"] == "stale_playback"

    async def test_restart_reconciles_idle_state_with_available_player(self) -> None:
        """A new client process can stop stale MA playback before receiving welcome."""
        device = await self._device("desktop")
        device.player.connected(False)
        observed: list[bool] = []
        connect = device.session.connect

        async def reconcile(hello: Hello, send: SendControl, revision: str) -> Welcome:
            observed.append(device.player.available)
            await device._local_stop()
            return await connect(hello, send, revision)

        async def stop(player_id: str) -> None:
            assert player_id == device.player.player_id
            assert device.player.available

        self.mass.player_queues.stop.side_effect = stop
        app = web.Application()
        app.router.add_get("/mashin/v1/control", self.provider._control_route)
        with patch.object(device.session, "connect", side_effect=reconcile):
            async with (
                TestClient(TestServer(app)) as client,
                client.ws_connect("/mashin/v1/control") as control,
            ):
                await control.send_json(
                    {
                        "protocol_version": 1,
                        "type": "hello",
                        "message_id": str(uuid4()),
                        "connection_epoch": None,
                        "payload": {
                            "device_id": "desktop",
                            "client_name": "Desktop",
                            "client_version": "0.1.0",
                            "resume_state": {
                                "current_stream_id": None,
                                "output_revision": 0,
                                "output_state": "idle",
                                "position_frame": 0,
                                "volume_percent": 100,
                                "is_muted": False,
                                "next_revision": 0,
                                "received_streams": [],
                            },
                            "capabilities": asdict(
                                ClientCapabilities(
                                    list(AudioProfileId), [48000], [16], 2, True, True, True
                                )
                            ),
                            "audio_profile": "flac",
                        },
                    }
                )
                response = await asyncio.wait_for(control.receive_json(), 1)
                assert response["type"] == "welcome"
                assert observed == [True]
                self.mass.player_queues.stop.assert_awaited_once_with(device.player.player_id)

    async def test_fallback_uses_own_queue_without_user_scopes(self) -> None:
        """LAN recovery can seek its player while retaining the requested pause."""
        device = await self._device("desktop")
        device.control_socket = AsyncMock()
        device.control_request = make_mocked_request("GET", "/mashin/v1/control")
        self.mass.player_queues.seek = AsyncMock()
        self.mass.player_queues.pause = AsyncMock()
        await device._fallback(42, True)
        self.mass.player_queues.seek.assert_awaited_once_with(device.player.player_id, 42)
        self.mass.player_queues.pause.assert_awaited_once_with(device.player.player_id)
        self.mass.config.get_raw_player_config_value.return_value = False
        with pytest.raises(web.HTTPForbidden):
            await device._check_connection(device.control_request)

    async def test_stop_failure_still_closes_only_the_disabled_device(self) -> None:
        """A failing MA stop does not leave the disabled client's transports alive."""
        first, second = await self._device("desktop"), await self._device("phone")
        first.control_socket, first.audio_socket = AsyncMock(), AsyncMock()
        second.control_socket, second.audio_socket = AsyncMock(), AsyncMock()
        control, audio = first.control_socket, first.audio_socket
        self.mass.player_queues.stop.side_effect = RuntimeError("MA stop failed")
        with pytest.raises(RuntimeError, match="MA stop failed"):
            await first._disable()
        control.close.assert_awaited_once()
        audio.close.assert_awaited_once()
        second.control_socket.close.assert_not_awaited()
        second.audio_socket.close.assert_not_awaited()

    async def test_control_reconnect_does_not_wait_for_retired_sockets(self) -> None:
        """A silent old connection cannot delay the replacement welcome."""
        device = await self._device("desktop")
        old_control, old_audio, replacement = AsyncMock(), AsyncMock(), AsyncMock()
        release, welcomed, receive = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def close(**_kwargs: object) -> None:
            await release.wait()

        async def send(_data: str) -> None:
            welcomed.set()

        async def messages() -> AsyncGenerator[WSMessage]:
            await receive.wait()
            yield WSMessage(WSMsgType.CLOSE, 1000, "")

        old_control.close.side_effect = old_audio.close.side_effect = close
        replacement.send_str.side_effect = send
        replacement.__aiter__.side_effect = messages
        device.control_socket, device.audio_socket = old_control, old_audio
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        hello = Hello("desktop", "Desktop", "0.1.0", None, capabilities, AudioProfileId.FLAC)
        with patch.object(device, "_check_connection", AsyncMock()):
            task = asyncio.ensure_future(device._control_route(Mock(), replacement, hello))
            try:
                await asyncio.wait_for(welcomed.wait(), 1)
                assert device.control_socket is replacement
                assert len(device.retired_sockets) == 2
                assert device.session.send_control is not None
            finally:
                release.set()
                receive.set()
                await asyncio.wait_for(task, 1)
        await asyncio.gather(*list(device.retired_sockets.values()))
        assert not device.retired_sockets

    async def test_audio_replacement_ready_precedes_old_socket_close(self) -> None:
        """Audio rebind can complete while the previous Audio close is blocked."""
        device = await self._device("desktop")
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        welcome = await device.session.connect(
            Hello("desktop", "Desktop", "0.1.0", None, capabilities, AudioProfileId.FLAC),
            AsyncMock(),
            "test",
        )
        assert device.session.epoch is not None
        epoch = device.session.epoch
        previous, replacement = AsyncMock(), AsyncMock()
        release, ready, receive = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def close(**_kwargs: object) -> None:
            await release.wait()

        async def send(_data: str) -> None:
            ready.set()

        async def messages() -> AsyncGenerator[WSMessage]:
            await receive.wait()
            yield WSMessage(WSMsgType.CLOSE, 1000, "")

        previous.close.side_effect = close
        replacement.send_str.side_effect = send
        replacement.__aiter__.side_effect = messages
        device.audio_socket = previous
        with patch.object(device, "_check_connection", AsyncMock()):
            task = asyncio.ensure_future(
                device._audio_route(
                    Mock(),
                    replacement,
                    AudioHello("desktop", welcome.audio_binding_id, 1, []),
                    epoch,
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), 1)
                assert device.audio_socket is replacement
                assert device.session.buffer.ready
                assert previous in device.retired_sockets
            finally:
                release.set()
                receive.set()
                await asyncio.wait_for(task, 1)

    async def test_retired_close_is_deduplicated_bounded_and_releases_transport(self) -> None:
        """A missing close reply cannot retain a socket admission slot forever."""
        device = await self._device("desktop")
        socket, request = AsyncMock(), Mock()
        self.provider.sockets.add(socket)
        blocked = asyncio.Event()
        timeout = asyncio.timeout

        async def close(**_kwargs: object) -> None:
            await blocked.wait()

        socket.close.side_effect = close
        with patch(
            "music_assistant.providers.mashin.provider.asyncio.timeout", lambda _: timeout(0)
        ):
            device._retire_socket(socket, request)
            device._retire_socket(socket, request)
            assert len(device.retired_sockets) == 1
            await asyncio.gather(*list(device.retired_sockets.values()))
        socket.close.assert_awaited_once_with(drain=False)
        request.transport.close.assert_called_once_with()
        assert not device.retired_sockets
        assert socket not in self.provider.sockets

    async def test_old_ping_errors_cannot_disconnect_or_disable_replacement(self) -> None:
        """A superseded ping cannot mutate the replacement connection authority."""
        device = await self._device("desktop")
        for error in (ConnectionError(), web.HTTPForbidden()):
            with self.subTest(error=type(error).__name__):
                old, replacement = AsyncMock(), AsyncMock()
                device.control_socket = old

                async def fail(
                    _data: object,
                    target: web.WebSocketResponse = replacement,
                    failure: Exception = error,
                ) -> None:
                    device.control_socket = target
                    raise failure

                if isinstance(error, ConnectionError):
                    old.ping.side_effect = fail
                with (
                    patch("music_assistant.providers.mashin.provider.asyncio.sleep", AsyncMock()),
                    patch.object(device, "_check_connection", AsyncMock(side_effect=fail)),
                    patch.object(device, "_disable", AsyncMock()) as disable,
                    patch.object(device.session, "control_lost", AsyncMock()) as lost,
                ):
                    await device._pings(Mock(), old, [], asyncio.Event())
                    disable.assert_not_awaited()
                    lost.assert_not_awaited()
                assert device.control_socket is replacement

    async def test_audio_epoch_is_rechecked_after_lock_wait(self) -> None:
        """A waiting Audio hello cannot bind after Control has replaced its epoch."""
        device = await self._device("desktop")
        capabilities = ClientCapabilities(list(AudioProfileId), [48000], [16], 2, True, True, True)
        welcome = await device.session.connect(
            Hello("desktop", "Desktop", "0.1.0", None, capabilities, AudioProfileId.FLAC),
            AsyncMock(),
            "test",
        )
        assert device.session.epoch is not None
        old_epoch = device.session.epoch
        socket = AsyncMock()
        authorized = asyncio.Event()

        async def authorize(_request: object) -> None:
            authorized.set()

        with (
            patch.object(device, "_check_connection", authorize),
            patch.object(device.session, "bind_audio", Mock()) as bind,
        ):
            await device.handshake_lock.acquire()
            task = asyncio.ensure_future(
                device._audio_route(
                    Mock(),
                    socket,
                    AudioHello("desktop", welcome.audio_binding_id, 1, []),
                    old_epoch,
                )
            )
            try:
                await authorized.wait()
                device.session.epoch = str(uuid4())
                device.handshake_lock.release()
                await task
            finally:
                if device.handshake_lock.locked():
                    device.handshake_lock.release()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            bind.assert_not_called()
            socket.close.assert_awaited_once()
            assert device.audio_socket is None

    async def test_registration_failure_cleans_up_the_partial_device(self) -> None:
        """Aborting player registration cannot leave session workers or a player behind."""
        self.mass.players.register.side_effect = RuntimeError("Registration failed")
        binding = DeviceBinding("desktop", "Desktop")
        with pytest.raises(RuntimeError, match="Registration failed"):
            await self.provider.registry.register("desktop", "Desktop")
        self.mass.players.unregister.assert_awaited_once_with(binding.player_id)
        assert not self.provider.registry.devices
        assert self.provider.registry.bindings["desktop"] == binding

    async def test_provider_unload_cancels_all_device_workers(self) -> None:
        """Provider teardown stops all clients without leaving their workers running."""
        first, second = await self._device("desktop"), await self._device("phone")
        tasks = [*first.tasks, *second.tasks]
        assert tasks
        await self.provider.unload()
        assert all(task.done() for task in tasks)
        assert self.provider.registry.closed
        self.mass.players.unregister.assert_any_await(first.player.player_id)
        self.mass.players.unregister.assert_any_await(second.player.player_id)
        self.provider.registry.devices.clear()

    async def test_old_cleanup_cannot_unregister_replacement_player(self) -> None:
        """A reloaded provider's player must survive a previous cleanup task."""
        device = await self._device("desktop")
        self.registered_players[device.player.player_id] = object()
        await device.unload()
        self.mass.players.unregister.assert_not_awaited()

    async def test_slow_shutdown_retains_owned_cleanup_after_deadline(self) -> None:
        """Unload returns at its deadline without abandoning slow cleanup work."""
        device = await self._device("desktop")
        started, finish = asyncio.Event(), asyncio.Event()

        async def shutdown() -> None:
            started.set()
            await finish.wait()

        async def deadline(
            tasks: list[asyncio.Task[None]], **_kwargs: object
        ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
            await started.wait()
            return set(), set(tasks)

        with (
            patch.object(device.session, "shutdown", shutdown),
            patch("music_assistant.providers.mashin.provider.asyncio.wait", deadline),
        ):
            try:
                await device.unload()
                assert device.session.closed
                assert not device.session.buffer.enabled
                assert device.cleanup_task is not None
                assert not device.cleanup_task.done()
            finally:
                finish.set()
                if device.cleanup_task is not None:
                    await asyncio.wait_for(device.cleanup_task, 1)
        self.mass.players.unregister.assert_awaited_once_with(device.player.player_id)

    async def test_repair_keeps_ma_processing_without_dsp_precheck(self) -> None:
        """DSP, normalization, fade and conversion cannot preempt the PCM match."""
        module = importlib.import_module("music_assistant.providers.mashin.adapter")
        adapter = Mock()
        adapter.valid.return_value = True
        context = cast(
            "PlaybackContext",
            module.PlaybackContext(
                media=Mock(),
                item=SimpleNamespace(
                    streamdetails=SimpleNamespace(
                        audio_format=AudioFormat(
                            content_type=ContentType.PCM_F32LE, sample_rate=44100, channels=1
                        ),
                        volume_normalization_mode=VolumeNormalizationMode.DYNAMIC,
                        fade_in=True,
                        is_realtime=False,
                    )
                ),
                queue_id="mashin_test",
                session_id="session",
                queue_item_id="item",
                seek_seconds=10,
                source_identity=("same", "item", "pcm", 48000, 32, 2, "uri", "", 300),
            ),
        )
        adapter.source_identity.return_value = context.source_identity
        audio = adapter.mass.streams.audio
        pcm = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, channels=1)
        audio.select_pcm_format = AsyncMock(return_value=pcm)
        audio.get_audio_buffer = AsyncMock(return_value=Mock(cancelled=False))
        audio.get_player_output_plan.return_value = SimpleNamespace(filter_params=["volume=2dB"])
        process = Mock()
        process.start = AsyncMock()
        process.read = AsyncMock(return_value=bytes(8))
        process.close = AsyncMock()
        source = module.PCMSource(
            cast("MAAdapter", adapter),
            context,
            OutputFormat(
                profile_id=AudioProfileId.FLAC,
                container="flac",
                codec="flac",
                sample_rate_hz=48000,
                bit_depth=16,
                channels=2,
                target_bitrate_bps=None,
                uses_vbr=False,
            ),
        )
        with patch.object(module, "FFMpeg", return_value=process) as ffmpeg:
            try:
                await source.open(seek_seconds=5, repair=True)
                assert await source.read(8) == bytes(8)
                assert ffmpeg.call_args.kwargs["filter_params"] == ["volume=2dB"]
                assert ffmpeg.call_args.kwargs["input_format"] == pcm
                assert ffmpeg.call_args.kwargs["output_format"].sample_rate == 48000
                assert ffmpeg.call_args.kwargs["output_format"].channels == 2
                adapter.player.logger.info.assert_called_once_with(
                    "PCM repair processing snapshot for player %s: active PCM-altering processing; output_filters=%s normalization=%s fade_in=%s resampling=%s channel_conversion=%s; best-effort matching may fail",
                    adapter.player.player_id,
                    1,
                    VolumeNormalizationMode.DYNAMIC,
                    True,
                    True,
                    True,
                )
                audio.get_audio_buffer.assert_awaited_once_with(
                    context.item,
                    seek_position_ms=5000,
                    reason="mashin_recovery",
                    allow_provider_match=False,
                )
                assert "volume=2dB" not in str(adapter.player.logger.mock_calls)
                adapter.player.logger.info.reset_mock()
                details = context.item.streamdetails
                assert details is not None
                details.fade_in = False
                details.volume_normalization_mode = VolumeNormalizationMode.DISABLED
                details.audio_format = AudioFormat(
                    content_type=ContentType.PCM_F32LE,
                    sample_rate=48000,
                    channels=2,
                )
                audio.select_pcm_format.return_value = details.audio_format
                audio.get_player_output_plan.return_value.filter_params = []
                await source.close()
                await source.open(seek_seconds=5, repair=True)
                adapter.player.logger.info.assert_not_called()
                await source.close()
                adapter.source_identity.return_value = (
                    "reselected",
                    "item",
                    "pcm",
                    48000,
                    32,
                    2,
                    "uri",
                    "",
                    300,
                )
                await source.open()
                assert source.context.source_identity == adapter.source_identity.return_value
                assert context.source_identity[0] == "same"
                await source.close()
                adapter.source_identity.return_value = ("unexpected",)
                with pytest.raises(ValueError, match="source identity changed"):
                    await source.open(repair=True)
            finally:
                await source.close()

    async def test_ma_buffer_release_is_source_loss_not_eof(self) -> None:
        """MA cleanup before or during a read cannot masquerade as normal EOF."""
        module = importlib.import_module("music_assistant.providers.mashin.adapter")
        source = module.PCMSource(Mock(), Mock(), Mock())
        process = Mock()
        process.read = AsyncMock(return_value=bytes(8))
        source.process = process
        buffer = Mock(cancelled=True)
        source.owned_buffer = buffer
        with pytest.raises(AudioError, match="MA PCM buffer was released"):
            await source.read(8)
        process.read.assert_not_awaited()
        buffer.cancelled = False
        assert await source.read(8) == bytes(8)

        async def cancelled_read(_size: int) -> bytes:
            buffer.cancelled = True
            return b""

        process.read.side_effect = cancelled_read
        with pytest.raises(AudioError, match="released during read"):
            await source.read(8)

    async def test_completed_ma_buffer_remains_readable_and_ends_normally(self) -> None:
        """A closed upstream producer does not invalidate its healthy buffered PCM."""
        module = importlib.import_module("music_assistant.providers.mashin.adapter")
        source = module.PCMSource(Mock(), Mock(), Mock())
        source.owned_buffer = Mock(cancelled=False, is_buffering=False, eof=True)
        process = Mock(returncode=0, stdin_feeder_exception=None)
        process.read = AsyncMock(side_effect=[bytes(8), b""])
        process.wait_with_timeout = AsyncMock()
        source.process = process
        assert await source.read(8) == bytes(8)
        assert await source.read(8) == b""
        process.wait_with_timeout.assert_awaited_once_with(5)

    async def _device(self, device_id: str) -> MashinDevice:
        device = await self.provider.registry.register(device_id, device_id)
        update = patch.object(device.player, "update_state", Mock())
        update.start()
        self.addCleanup(update.stop)
        return device
