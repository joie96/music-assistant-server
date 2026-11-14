"""Test anonymous metadata, audio resolution and disk-cache integration."""

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yt_dlp
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.providers.ytmusic_enhanced import (
    CONF_CACHE_DIR,
    CONF_CACHE_ENABLED,
    CONF_CACHE_MAX_SIZE_MB,
    SUPPORTED_FEATURES,
    YoutubeMusicEnhancedProvider,
)
from music_assistant.providers.ytmusic_enhanced.file_cache import CachedStreamInfo, FileCache
from music_assistant.providers.ytmusic_enhanced.helpers import get_artist, get_home, get_playlist


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncGenerator[YoutubeMusicEnhancedProvider]:
    """Create a provider without starting Music Assistant or contacting YouTube."""
    mass = MagicMock()
    mass.cache_path = str(tmp_path)
    mass.metadata.locale = "en_US"
    mass.player_queues.all.return_value = ()
    manifest = MagicMock(domain="ytmusic_enhanced")
    config = MagicMock(instance_id="ytmusic_enhanced_test")
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    instance = YoutubeMusicEnhancedProvider(mass, manifest, config, SUPPORTED_FEATURES)
    instance.language = "en"
    instance._downloads = set()
    instance._prefetch_tasks = {}
    instance._is_unloading = False
    yield instance
    await asyncio.gather(*instance._prefetch_tasks.values(), return_exceptions=True)


async def test_init_and_settings_need_no_credentials(
    provider: YoutubeMusicEnhancedProvider,
) -> None:
    """Initialization neither reads setup secrets nor probes account/token services."""
    with (
        patch.object(provider, "_install_packages", AsyncMock()),
        patch.object(provider, "get_setup_value", side_effect=AssertionError("No setup secrets")),
    ):
        await provider.handle_async_init()
    entries = {entry.key: entry for entry in await provider.get_config_entries()}
    assert entries[CONF_CACHE_ENABLED].default_value is True
    assert entries[CONF_CACHE_MAX_SIZE_MB].default_value == 1024
    assert entries[CONF_CACHE_DIR].default_value
    assert not {"cookie", "username", "po_token_server_url"}.intersection(entries)
    assert ProviderFeature.LIBRARY_TRACKS not in provider.supported_features
    assert ProviderFeature.FAVORITE_TRACKS_EDIT not in provider.supported_features
    assert provider._file_cache is not None
    cast("MagicMock", provider.mass.http_session.get).assert_not_called()


async def test_resolver_uses_anonymous_default_clients(
    provider: YoutubeMusicEnhancedProvider,
) -> None:
    """yt-dlp receives no credentials, token server or forced player client."""
    options: list[dict[str, Any]] = []
    audio = {"format_id": "251", "url": "https://example.com/audio", "ext": "webm"}

    class FakeYoutubeDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            options.append(opts)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            assert download is False
            return {"formats": [audio], "duration": 12}

        def build_format_selector(self, spec: str) -> Any:
            assert spec == "m4a/bestaudio/best"
            return lambda context: iter(context["formats"])

    provider._yt_dlp_module = SimpleNamespace(  # type: ignore[assignment]
        YoutubeDL=FakeYoutubeDL,
        utils=SimpleNamespace(DownloadError=RuntimeError),
    )
    result = await provider._get_stream_format("abcdefghijk")
    assert result["format_id"] == "251"
    assert result["duration"] == 12
    assert "cookiefile" not in options[0]
    assert "player_client" not in options[0]["extractor_args"]["youtube"]
    assert set(options[0]["extractor_args"]) == {"youtube"}


@pytest.mark.parametrize("format_id", ["251", "251-drc", 251])
async def test_resolver_handles_preferred_format_ids(
    provider: YoutubeMusicEnhancedProvider, format_id: str | int
) -> None:
    """Preferred IDs win independently of extractor ordering or format suffixes."""
    downloader = MagicMock()
    downloader.extract_info.return_value = {
        "formats": [
            {"format_id": "140", "url": "https://example.com/aac"},
            {"format_id": format_id, "url": "https://example.com/opus"},
            {"format_id": "999", "url": "https://example.com/other"},
        ]
    }
    module = MagicMock()
    module.YoutubeDL.return_value.__enter__.return_value = downloader
    provider._yt_dlp_module = module
    assert (await provider._get_stream_format("abcdefghijk"))["format_id"] == format_id
    downloader.build_format_selector.assert_not_called()


@pytest.mark.parametrize(
    ("method", "prefix", "level"),
    [
        ("debug", "", logging.DEBUG),
        ("debug", "[debug] ", VERBOSE_LOG_LEVEL),
        ("warning", "", logging.WARNING),
        ("error", "", logging.DEBUG),
    ],
)
def test_ytdlp_logs_use_provider_logger_and_redact_secrets(
    provider: YoutubeMusicEnhancedProvider, method: str, prefix: str, level: int
) -> None:
    """Library errors do not bypass MA or expose signed URLs and credentials."""
    options = provider._ydl_options()
    with patch.object(provider.logger, "log") as log:
        getattr(options["logger"], method)(
            f"{prefix}Request failed: https://example.com/videoplayback?sig=stream-secret "
            "Authorization=header-secret"
        )
    log.assert_called_once()
    assert log.call_args.args[0] == level
    message = log.call_args.args[1] % log.call_args.args[2:]
    assert "Request failed" in message
    assert "stream-secret" not in message
    assert "header-secret" not in message
    assert options["noprogress"] is True


@pytest.mark.parametrize(
    ("preferred", "fallback"),
    [
        ("141", "251"),
        ("251", "140"),
        ("140", "171"),
        ("171", "250"),
        ("250", "249"),
        ("249", "139"),
    ],
)
async def test_resolver_prefers_higher_audio_quality(
    provider: YoutubeMusicEnhancedProvider, preferred: str, fallback: str
) -> None:
    """Higher quality wins even when the lower quality format is listed last."""
    module = MagicMock()
    downloader = module.YoutubeDL.return_value.__enter__.return_value
    downloader.extract_info.return_value = {
        "formats": [
            {"format_id": preferred, "url": "https://example.com/high"},
            {"format_id": fallback, "url": "https://example.com/low"},
        ]
    }
    provider._yt_dlp_module = module
    assert (await provider._get_stream_format("abcdefghijk"))["format_id"] == preferred
    downloader.build_format_selector.assert_not_called()


@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_resolver_retries_complete_extraction(
    provider: YoutubeMusicEnhancedProvider, failures: int
) -> None:
    """Extraction is recreated up to three times, with increasing async delays."""
    module = MagicMock()
    module.utils.DownloadError = RuntimeError
    downloader = module.YoutubeDL.return_value.__enter__.return_value
    downloader.extract_info.side_effect = [RuntimeError("Temporary failure")] * failures + [
        {"formats": [{"format_id": "251", "url": "https://example.com/audio"}]}
    ]
    provider._yt_dlp_module = module
    with patch("music_assistant.providers.ytmusic_enhanced.asyncio.sleep", AsyncMock()) as sleep:
        if failures == 3:
            with pytest.raises(UnplayableMediaError, match="Temporary failure"):
                await provider._get_stream_format("abcdefghijk")
        else:
            assert (await provider._get_stream_format("abcdefghijk"))["format_id"] == "251"
    assert module.YoutubeDL.call_count == min(failures + 1, 3)
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0][:failures]


@pytest.mark.parametrize("audio_only", [True, False])
async def test_resolver_falls_back_to_real_ytdlp_selector(
    provider: YoutubeMusicEnhancedProvider, audio_only: bool
) -> None:
    """Unknown format IDs still resolve through the real m4a/audio/video fallback."""
    stream_format = {
        "format_id": "999",
        "url": "https://example.com/audio",
        "ext": "m4a" if audio_only else "mp4",
        "acodec": "mp4a.40.2",
        "vcodec": "none" if audio_only else "avc1",
    }
    provider._yt_dlp_module = yt_dlp
    with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value={"formats": [stream_format]}):
        assert (await provider._get_stream_format("abcdefghijk"))["format_id"] == "999"


async def test_metadata_stays_on_anonymous_ytmusicapi() -> None:
    """Public metadata and home recommendations use ytmusicapi, not yt-dlp."""
    with patch("music_assistant.providers.ytmusic_enhanced.helpers.ytmusicapi.YTMusic") as client:
        client.return_value.get_artist.return_value = {"name": "Artist"}
        assert (await get_artist("artist"))["name"] == "Artist"
        client.assert_called_with(auth=None, language="en")
        client.return_value.get_home.return_value = []
        assert await get_home() == []
        client.assert_called_with(auth=None, language="en")


@pytest.mark.parametrize("kind", ["album", "artist", "playlist", "track"])
@pytest.mark.parametrize("like_status", ["LIKE", "DISLIKE"])
def test_public_metadata_does_not_import_account_favorites(
    provider: YoutubeMusicEnhancedProvider, kind: str, like_status: str
) -> None:
    """Anonymous metadata cannot overwrite Music Assistant favorites with account likes."""
    payload = {
        "id": "public_id",
        "channelId": "public_id",
        "videoId": "abcdefghijk",
        "name": "Public item",
        "title": "Public item",
        "artists": [{"id": "artist", "name": "Artist"}],
        "likeStatus": like_status,
    }
    parse = getattr(provider, f"_parse_{kind}")
    assert parse(payload).favorite is None


async def test_public_playlist_keeps_its_identity(
    provider: YoutubeMusicEnhancedProvider,
) -> None:
    """Public playlists stay read-only and keep their original IDs and public authors."""
    with patch("music_assistant.providers.ytmusic_enhanced.helpers.ytmusicapi.YTMusic") as client:
        client.return_value.get_playlist.return_value = {
            "title": "Public playlist",
            "author": "Public channel",
            "tracks": [],
        }
        payload = await get_playlist("PLpublic")
        client.assert_called_once_with(auth=None, language="en")
        client.return_value.get_playlist.assert_called_once_with(playlistId="PLpublic", limit=None)
    playlist = provider._parse_playlist(payload)
    assert playlist.item_id == "PLpublic"
    assert playlist.name == "Public playlist"
    assert playlist.owner == "Public channel"
    assert playlist.is_editable is False
    assert "checksum" not in payload
    assert next(iter(playlist.provider_mappings)).item_id == "PLpublic"


async def test_real_ytdlp_download_is_cached_and_seekable(
    provider: YoutubeMusicEnhancedProvider, tmp_path: Path
) -> None:
    """The real yt-dlp downloader writes complete bytes and a second lookup is local."""
    payload = b"test audio bytes" * 1024

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.headers["X-Audio-Test"] == "expected"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, message: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    provider._yt_dlp_module = yt_dlp
    provider._file_cache = FileCache(tmp_path / "audio-cache")
    stream_format = {
        "url": f"http://127.0.0.1:{server.server_port}/audio.webm",
        "format_id": "251",
        "ext": "webm",
        "acodec": "opus",
        "vcodec": "none",
        "protocol": "http",
        "http_headers": {"X-Audio-Test": "expected"},
        "filesize": len(payload),
        "duration": 12,
    }
    try:
        with (
            patch.object(
                provider, "_get_stream_format", AsyncMock(return_value=stream_format)
            ) as resolve,
            patch.object(provider, "_schedule_prefetch") as prefetch,
        ):
            details = await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
            assert details.stream_type == StreamType.LOCAL_FILE
            assert isinstance(details.path, str)
            assert Path(details.path).read_bytes() == payload
            assert details.can_seek is True
            assert details.duration == 12
            again = await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
            assert again.path == details.path
            resolve.assert_awaited_once()
            assert prefetch.call_count == 2
            prefetch.assert_called_with("abcdefghijk")
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("cache_hit", [True, False])
async def test_playback_logs_cache_decisions_without_stream_secrets(
    provider: YoutubeMusicEnhancedProvider, tmp_path: Path, cache_hit: bool
) -> None:
    """Cache decisions are diagnosable without exposing signed URLs or headers."""
    cached = CachedStreamInfo(tmp_path / "audio", {"audio_ext": "webm"})
    provider._file_cache = MagicMock()
    provider._file_cache.get_hit.return_value = cached if cache_hit else None
    stream_format = {
        "format_id": "251",
        "url": "https://example.com/audio?token=stream-secret",
        "http_headers": {"Authorization": "header-secret"},
    }
    with (
        patch.object(provider.logger, "debug") as debug,
        patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)),
        patch.object(provider, "_download_and_cache", AsyncMock(return_value=cached)),
        patch.object(provider, "_schedule_prefetch"),
    ):
        await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
    expected = "Audio cache hit for %s" if cache_hit else "Audio cache miss for %s"
    debug.assert_any_call(expected, "abcdefghijk")
    messages = "\n".join(call.args[0] % call.args[1:] for call in debug.call_args_list)
    assert "stream-secret" not in messages
    assert "header-secret" not in messages


async def test_cache_failure_falls_back_to_streaming(
    provider: YoutubeMusicEnhancedProvider,
) -> None:
    """A disk failure does not turn a playable remote stream into a failure."""
    provider._file_cache = MagicMock()
    provider._file_cache.get_hit.return_value = None
    stream_format = {"format_id": "251", "url": "https://example.com/audio", "ext": "webm"}
    with (
        patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)),
        patch.object(
            provider,
            "_download_and_cache",
            AsyncMock(
                side_effect=OSError(
                    "Disk full: https://example.com/audio?token=stream-secret "
                    "Authorization=header-secret"
                )
            ),
        ),
        patch.object(provider.logger, "warning") as warning,
    ):
        details = await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
    assert details.stream_type == StreamType.HTTP
    assert not provider._downloads
    warning.assert_called_once()
    message = warning.call_args.args[0] % warning.call_args.args[1:]
    assert "Disk full" in message
    assert "streaming instead" in message
    assert "stream-secret" not in message
    assert "header-secret" not in message


async def test_disabled_cache_never_creates_directory(
    provider: YoutubeMusicEnhancedProvider, tmp_path: Path
) -> None:
    """Disabling the cache prevents directory creation and audio downloads."""
    values = {CONF_CACHE_ENABLED: False, CONF_CACHE_DIR: str(tmp_path / "disabled")}
    with (
        patch.object(
            provider.config,
            "get_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider, "_install_packages", AsyncMock()),
    ):
        await provider.handle_async_init()
    assert provider._file_cache is None
    assert not (tmp_path / "disabled").exists()
    stream_format = {
        "format_id": "251",
        "url": "https://example.com/audio",
        "ext": "webm",
        "http_headers": {"User-Agent": "AudioClient"},
    }
    with (
        patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)),
        patch.object(provider, "_download_and_cache", AsyncMock()) as download,
    ):
        details = await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
    download.assert_not_awaited()
    assert details.stream_type == StreamType.HTTP
    assert details.extra_input_args == ["-headers", "User-Agent: AudioClient\r\n"]


async def test_cache_settings_control_path_and_limit(
    provider: YoutubeMusicEnhancedProvider, tmp_path: Path
) -> None:
    """Custom paths and byte limits are honored during initialization."""
    values = {CONF_CACHE_DIR: str(tmp_path / "custom"), CONF_CACHE_MAX_SIZE_MB: 3}
    with (
        patch.object(
            provider.config,
            "get_value",
            side_effect=lambda key, default=None: values.get(key, default),
        ),
        patch.object(provider, "_install_packages", AsyncMock()),
    ):
        await provider.handle_async_init()
    assert provider._file_cache is not None
    assert provider._file_cache.cache_dir == tmp_path / "custom" / provider.instance_id
    assert provider._file_cache.max_size_bytes == 3 * 1024 * 1024


async def test_live_hls_bypasses_cache(provider: YoutubeMusicEnhancedProvider) -> None:
    """Live HLS is handed to the native stream decoder without downloading a file."""
    provider._file_cache = MagicMock()
    provider._file_cache.get_hit.return_value = None
    stream_format = {
        "format_id": "live",
        "url": "https://example.com/live.m3u8",
        "ext": "m4a",
        "is_live": True,
        "protocol": "m3u8_native",
    }
    with (
        patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)),
        patch.object(provider, "_download_and_cache", AsyncMock()) as download,
    ):
        details = await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
    download.assert_not_awaited()
    assert details.stream_type == StreamType.HLS
    assert details.is_realtime
    assert not details.can_seek
    assert not details.allow_seek


async def test_oversized_audio_is_streamed_without_downloading(
    provider: YoutubeMusicEnhancedProvider, tmp_path: Path
) -> None:
    """Known oversized files are streamed without first downloading and discarding them."""
    provider._file_cache = FileCache(tmp_path / "small-cache", max_size_bytes=5)
    provider._yt_dlp_module = MagicMock()
    stream_format = {
        "format_id": "251",
        "url": "https://example.com/audio",
        "ext": "webm",
        "filesize": 10,
    }
    with patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)):
        details = await provider.get_stream_details("abcdefghijk", MediaType.TRACK)
    provider._yt_dlp_module.YoutubeDL.assert_not_called()
    assert details.stream_type == StreamType.HTTP
    assert not (tmp_path / "small-cache").exists()


async def test_cancelled_request_waits_for_disk_writer(
    provider: YoutubeMusicEnhancedProvider,
) -> None:
    """Cancelling playback cannot leave an unobserved cache writer running."""
    started = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()
    provider._file_cache = MagicMock()
    provider._file_cache.get_hit.return_value = None

    async def download(_video_id: str, _stream_format: dict[str, Any]) -> None:
        started.set()
        await finish.wait()
        completed.set()

    stream_format = {"format_id": "251", "url": "https://example.com/audio", "ext": "webm"}
    with (
        patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)),
        patch.object(provider, "_download_and_cache", side_effect=download),
    ):
        request = asyncio.create_task(provider.get_stream_details("abcdefghijk", MediaType.TRACK))
        await asyncio.wait_for(started.wait(), timeout=5)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        assert not completed.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert completed.is_set()
    assert not provider._downloads


@pytest.mark.parametrize("mode", ["normal", "cached", "live", "failure", "foreign"])
async def test_prefetch_scans_only_three_upcoming_entries(
    provider: YoutubeMusicEnhancedProvider, mode: str
) -> None:
    """Queue scans use the synchronous MA API and preserve provider ownership."""
    video_ids = ["abcdefghijk", "bcdefghijkl", "cdefghijklm", "defghijklmn", "efghijklmno"]
    items = [
        SimpleNamespace(
            queue_item_id=video_id,
            media_item=ItemMapping(
                item_id=video_id,
                provider=provider.instance_id,
                name=video_id,
                media_type=MediaType.TRACK,
            ),
        )
        for video_id in video_ids
    ]
    queue = SimpleNamespace(queue_id="queue", current_index=0, current_item=items[0])
    queues = cast("MagicMock", provider.mass.player_queues)
    if mode == "foreign":
        items[1].media_item.provider = "another_instance"
    queues.all.return_value = (queue,)
    queues.get_next_item.side_effect = lambda _queue_id, index: items[
        (video_ids.index(index) if isinstance(index, str) else index) + 1
    ]
    provider._file_cache = MagicMock()
    provider._file_cache.get_hit.side_effect = lambda video_id: (
        object() if mode == "cached" and video_id == video_ids[1] else None
    )
    stream_format = {"is_live": mode == "live"}
    with (
        patch.object(
            provider, "_get_stream_format", AsyncMock(return_value=stream_format)
        ) as resolve,
        patch.object(provider, "_download_and_cache", AsyncMock()) as download,
    ):
        if mode == "failure":
            resolve.side_effect = [
                UnplayableMediaError("Unavailable"),
                stream_format,
                stream_format,
            ]
        await provider._prefetch_upcoming(video_ids[0])
    expected = video_ids[2:4] if mode in ("cached", "foreign") else video_ids[1:4]
    assert [call.args[0] for call in resolve.await_args_list] == expected
    if mode == "failure":
        expected = video_ids[2:4]
    assert [call.args[0] for call in download.await_args_list] == (
        [] if mode == "live" else expected
    )
    assert queues.get_next_item.call_count == 3
    items[0].media_item.provider = "another_instance"
    resolve.reset_mock()
    with patch.object(provider, "_get_stream_format", resolve):
        await provider._prefetch_upcoming(video_ids[0])
    resolve.assert_not_awaited()


def test_prefetch_resolves_instance_mapping(provider: YoutubeMusicEnhancedProvider) -> None:
    """Library track IDs are resolved through this instance's provider mapping."""
    track = Track(
        item_id="42",
        provider="library",
        name="Track",
        provider_mappings={
            ProviderMapping(
                item_id="abcdefghijk",
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            ),
            ProviderMapping(
                item_id="bcdefghijkl",
                provider_domain=provider.domain,
                provider_instance="another_instance",
            ),
        },
    )
    assert provider._video_id_for_instance(track) == "abcdefghijk"


async def test_prefetch_scheduling_deduplicates_and_honors_disabled_cache(
    provider: YoutubeMusicEnhancedProvider,
) -> None:
    """Repeated playback requests cannot start duplicate concurrent queue scans."""
    provider._file_cache = MagicMock()
    with patch.object(provider, "_prefetch_upcoming", AsyncMock()) as prefetch:
        provider._schedule_prefetch("abcdefghijk")
        provider._schedule_prefetch("abcdefghijk")
        await asyncio.gather(*provider._prefetch_tasks.values())
        prefetch.assert_awaited_once_with("abcdefghijk")
        assert not provider._prefetch_tasks
        provider._file_cache = None
        provider._schedule_prefetch("bcdefghijkl")
        provider._file_cache = MagicMock()
        provider._is_unloading = True
        provider._schedule_prefetch("bcdefghijkl")
        prefetch.assert_awaited_once()


async def test_unload_waits_for_prefetch_writer(provider: YoutubeMusicEnhancedProvider) -> None:
    """Unloading cancels prefetch scans but waits for an already started disk writer."""
    started = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()
    provider._file_cache = MagicMock()

    async def download(_video_id: str, _stream_format: dict[str, Any]) -> None:
        started.set()
        await finish.wait()
        completed.set()

    async def prefetch(_video_id: str) -> None:
        await provider._cache_stream("bcdefghijkl", {})

    with (
        patch.object(provider, "_prefetch_upcoming", side_effect=prefetch),
        patch.object(provider, "_download_and_cache", side_effect=download),
        patch("music_assistant.models.music_provider.MusicProvider.unload", AsyncMock()),
    ):
        provider._schedule_prefetch("abcdefghijk")
        await asyncio.wait_for(started.wait(), timeout=5)
        unloading = asyncio.create_task(provider.unload())
        await asyncio.sleep(0)
        assert not unloading.done()
        assert not completed.is_set()
        finish.set()
        await asyncio.wait_for(unloading, timeout=5)
    assert completed.is_set()
    assert not provider._downloads
    assert not provider._prefetch_tasks
