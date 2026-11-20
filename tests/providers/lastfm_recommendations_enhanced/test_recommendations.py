"""Personal top-items folders and their generation diagnostics."""

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import (
    AuthenticationFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)
from music_assistant_models.statistics import TopItemResult
from PIL import Image

from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    user_auth_context,
)
from music_assistant.providers import lastfm_recommendations_enhanced as provider_module
from music_assistant.providers.lastfm_recommendations_enhanced import (
    SUPPORTED_FEATURES,
    TOP_ITEMS_FOLDERS,
    LastFMRecommendationsEnhancedProvider,
    _recommendation_user_context,
    get_recommendation_user_entry,
)
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMItem
from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    EXTRA_FOLDERS,
    GENRE_FOLDER_FAMILIES,
    GenreProfile,
    genre_for_tag,
)
from music_assistant.providers.lastfm_recommendations_enhanced.collections import LastFMCollections
from music_assistant.providers.lastfm_recommendations_enhanced.constants import (
    CONF_RECOMMENDATION_USER_ID,
    REFRESH_TASK_ID,
)
from music_assistant.providers.lastfm_recommendations_enhanced.cover_art import (
    build_split_cover_png,
)
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import (
    DISCOVERY_FOLDERS,
    LastFMDiscovery,
)

JAZZ = genre_for_tag("jazz")
assert JAZZ is not None
GENRE_PROFILE = GenreProfile((JAZZ,), (), (JAZZ,))
GENRE_FOLDER_IDS = tuple(f"{prefix}_{JAZZ.key}" for prefix, _label in GENRE_FOLDER_FAMILIES)


@pytest.fixture
def provider(tmp_path: Path) -> LastFMRecommendationsEnhancedProvider:
    """Return a real provider with an isolated, mocked statistics controller."""
    mass = Mock()
    mass.cache_path = str(tmp_path)
    mass.config.get.return_value = {CONF_RECOMMENDATION_USER_ID: "DEBUG"}
    mass.config.decrypt_string.side_effect = lambda value: value
    mass.statistics.get_top_items = AsyncMock(return_value=[])
    mass.music.providers = []
    controller = mass.music.get_controller.return_value
    controller.db_table = "tracks"
    controller.get_library_items_by_query = AsyncMock(return_value=[])
    config = Mock()
    config.name = "Last.fm folder tests"
    config.instance_id = "lastfm_recommendations_enhanced--folders"
    config.values = {}
    config.get_value.return_value = "DEBUG"
    manifest = Mock()
    manifest.domain = "lastfm_recommendations_enhanced"
    result = LastFMRecommendationsEnhancedProvider(mass, manifest, config, SUPPORTED_FEATURES)
    result._recommendation_folders = []
    result._resolved_virtual_playlists = {}
    result._refresh_lock = asyncio.Lock()
    result._is_unloading = False
    result._recommendation_user_id = "DEBUG"
    return result


async def test_folder_listing_is_lightweight_and_translatable(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """List only implemented folders without doing statistics or network work."""
    folders = await provider._get_recommendation_folders(genre_profile=GENRE_PROFILE)
    assert [folder.item_id for folder in folders] == [
        "user_top_artists_monthly",
        "user_top_tracks_monthly",
        "user_top_albums_monthly",
        "global_top_tracks",
        "de_top_tracks",
        "global_top_artists",
        "de_top_artists",
        "user_recommendations",
        "user_artist_recommendations",
        "user_album_recommendations",
        "user_rediscovery_tracks",
        *(folder_id for folder_id, _name in EXTRA_FOLDERS),
        *GENRE_FOLDER_IDS,
    ]
    assert all(folder.provider == provider.instance_id for folder in folders)
    assert all(folder.translation_key and not folder.items for folder in folders)
    assert len({folder.item_id for folder in folders}) == 24
    assert all(folder.item_id.startswith(("user_", "global_", "de_")) for folder in folders)
    provider.mass.statistics.get_top_items.assert_not_awaited()
    provider.mass.http_session.get.assert_not_called()


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_recommendation_user_context_restores_nested_user_and_impersonation(
    failure: type[BaseException] | None,
) -> None:
    """Never retain the selected user or discard an existing caller impersonation."""
    requester = Mock(user_id="requester")
    impersonated = Mock(user_id="impersonated-requester")
    selected = Mock(user_id="selected")
    nested = Mock(user_id="nested")
    requester_token = auth_middleware.current_user.set(requester)
    impersonation_token = auth_middleware.impersonated_user.set(impersonated)

    def run_in_context() -> None:
        with _recommendation_user_context(selected):
            assert get_current_user() is selected
            assert auth_middleware.impersonated_user.get() is None
            with _recommendation_user_context(nested):
                assert get_current_user() is nested
            assert get_current_user() is selected
            if failure is not None:
                raise failure()

    try:
        if failure is None:
            run_in_context()
        else:
            with pytest.raises(failure):
                run_in_context()
        assert auth_middleware.current_user.get() is requester
        assert auth_middleware.impersonated_user.get() is impersonated
        assert get_current_user() is impersonated
    finally:
        auth_middleware.impersonated_user.reset(impersonation_token)
        auth_middleware.current_user.reset(requester_token)


async def test_recommendation_user_context_is_isolated_between_parallel_tasks() -> None:
    """Concurrent user-specific builds must not overwrite one another's context."""
    both_entered = asyncio.Event()
    entered: list[str] = []
    original_user = get_current_user()

    async def run_as(user_id: str) -> str:
        selected = Mock(user_id=user_id)
        with _recommendation_user_context(selected):
            entered.append(user_id)
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()
            assert get_current_user() is selected
        assert get_current_user() is original_user
        return user_id

    assert await asyncio.gather(run_as("first-user"), run_as("second-user")) == [
        "first-user",
        "second-user",
    ]
    assert get_current_user() is original_user


@pytest.mark.parametrize(
    "folder_id",
    [
        *(folder_id for folder_id, _media_type, _name in TOP_ITEMS_FOLDERS),
        *(folder.item_id for folder in DISCOVERY_FOLDERS),
        *(folder_id for folder_id, _name in EXTRA_FOLDERS),
        *GENRE_FOLDER_IDS,
    ],
)
async def test_every_advertised_folder_has_a_builder_for_empty_sources(
    provider: LastFMRecommendationsEnhancedProvider,
    folder_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All concrete ids route to their real builders even when no listening/source data exists."""
    provider.api = Mock()
    provider.api.get_items = AsyncMock(return_value=[])
    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        result = await provider._build_recommendation_items(folder_id, genre_profile=GENRE_PROFILE)
    assert isinstance(result, list)
    assert "reason=unknown_folder" not in caplog.text


@pytest.mark.parametrize(
    ("folder_id", "media_type"),
    [
        ("user_top_artists_monthly", MediaType.ARTIST),
        ("user_top_tracks_monthly", MediaType.TRACK),
        ("user_top_albums_monthly", MediaType.ALBUM),
    ],
)
async def test_monthly_folder_preserves_ranking_and_filters_unavailable_items(
    provider: LastFMRecommendationsEnhancedProvider,
    caplog: pytest.LogCaptureFixture,
    folder_id: str,
    media_type: MediaType,
) -> None:
    """Keep the statistics ranking, discard unavailable/duplicate references and log totals."""
    most_played = ItemMapping(
        item_id="1", provider="library", name="Most played", media_type=media_type
    )
    unavailable = ItemMapping(
        item_id="2", provider="library", name="Unavailable", media_type=media_type, available=False
    )
    next_played = ItemMapping(
        item_id="3", provider="library", name="Next played", media_type=media_type
    )
    provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=most_played, play_count=30),
        TopItemResult(item=unavailable, play_count=20),
        TopItemResult(item=next_played, play_count=10),
        TopItemResult(item=most_played, play_count=5),
    ]
    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        items = await provider._build_recommendation_items(folder_id)
    assert list(items) == [most_played, next_played]
    provider.mass.statistics.get_top_items.assert_awaited_once_with(
        media_type=media_type, period="month", limit=50
    )
    assert "Building Last.fm folder=" + folder_id in caplog.text
    assert "rows=4" in caplog.text
    assert "received=4 unavailable=1 duplicates=1 items=2 reason=ready" in caplog.text
    assert "elapsed=" in caplog.text
    provider.mass.http_session.get.assert_not_called()


async def test_empty_history_is_logged(
    provider: LastFMRecommendationsEnhancedProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty personal history is distinct from a failed statistics request."""
    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        assert not await provider._build_recommendation_items("user_top_tracks_monthly")
    assert "items=0 reason=no_history" in caplog.text


async def test_unavailable_history_is_distinct_from_empty_history(
    provider: LastFMRecommendationsEnhancedProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Do not confuse an unavailable historical item with an absent play history."""
    item = ItemMapping(
        item_id="1", provider="library", name="Removed", media_type=MediaType.TRACK, available=False
    )
    provider.mass.statistics.get_top_items.return_value = [TopItemResult(item=item, play_count=10)]
    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        assert not await provider._build_recommendation_items("user_top_tracks_monthly")
    assert "received=1 unavailable=1 duplicates=0 items=0 reason=no_available_items" in caplog.text


async def test_unknown_folder_does_not_query_statistics(
    provider: LastFMRecommendationsEnhancedProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Do not run a default builder for unknown or still-unimplemented folders."""
    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        assert not await provider._build_recommendation_items("unknown_folder")
    provider.mass.statistics.get_top_items.assert_not_awaited()
    assert "reason=unknown_folder" in caplog.text


async def test_folder_contents_are_prebuilt_and_do_not_query_history_on_read(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Keep serving the prepared generation without rebuilding when the UI requests it."""
    first = ItemMapping(
        item_id="1", provider="library", name="First user", media_type=MediaType.TRACK
    )
    second = ItemMapping(
        item_id="2", provider="library", name="Second user", media_type=MediaType.TRACK
    )
    provider.mass.statistics.get_top_items.side_effect = [
        [TopItemResult(item=first, play_count=10)],
        [TopItemResult(item=second, play_count=5)],
    ]
    folder = RecommendationFolder(
        item_id="user_top_tracks_monthly", provider=provider.instance_id, name="Top tracks"
    )
    folder.items = await provider._build_recommendation_items(folder.item_id)
    provider._recommendation_folders = [folder]
    with user_auth_context(Mock(user_id="DEBUG")):
        assert list(await provider.get_recommendation_items(folder.item_id)) == [first]
        assert list(await provider.get_recommendation_items(folder.item_id)) == [first]
        rows = await provider.get_recommendations()
        assert len(rows) == 1
        assert not rows[0].items
    assert provider.mass.statistics.get_top_items.await_count == 1


async def test_statistics_error_is_logged_and_propagated(
    provider: LastFMRecommendationsEnhancedProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Do not report failed generation as successfully built empty content or log raw error text."""
    provider.mass.statistics.get_top_items.side_effect = ResourceTemporarilyUnavailable(
        "private-data"
    )
    with (
        caplog.at_level(logging.DEBUG, logger=provider.logger.name),
        pytest.raises(ResourceTemporarilyUnavailable),
    ):
        await provider._build_recommendation_items("user_top_tracks_monthly")
    assert "folder build failed folder=user_top_tracks_monthly" in caplog.text
    assert "error_type=ResourceTemporarilyUnavailable" in caplog.text
    assert "private-data" not in caplog.text
    assert "build completed" not in caplog.text


async def test_recommendation_playlists_use_native_catalog_contract(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Follow a recommendation recipe through playlist details without fetching tracks."""
    items = await provider._build_recommendation_items(
        "global_genre_playlists", genre_profile=GENRE_PROFILE
    )
    assert items
    assert all(
        isinstance(item, Playlist) and item.is_dynamic and not item.is_editable for item in items
    )
    provider._resolved_virtual_playlists[items[0].item_id] = (items[0], [])
    playlist = await provider.get_playlist(items[0].item_id)
    assert playlist.uri == items[0].uri
    assert await provider.get_playlist_tracks(playlist.item_id, page=1) == []
    provider.mass.http_session.get.assert_not_called()


async def test_generated_playlist_returns_existing_ma_tracks_with_positions(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Preserve playable MA identities and leave the stored model untouched."""
    mapping = ProviderMapping(
        item_id="source-id", provider_domain="source", provider_instance="source--1"
    )
    artist = Artist(item_id="2", provider="library", name="Artist", provider_mappings={mapping})
    track = Track(
        item_id="1",
        provider="library",
        name="Song",
        artists=UniqueList([artist]),
        provider_mappings={mapping},
    )
    provider.api = Mock()
    provider.api.get_items = AsyncMock(return_value=[LastFMItem("Song", "Artist")])
    controller = provider.mass.music.get_controller.return_value
    controller.db_table = "tracks"
    controller.get_library_items_by_query = AsyncMock(return_value=[track])
    items = await provider._build_recommendation_items(
        "global_genre_playlists", genre_profile=GENRE_PROFILE
    )
    playlist = next(item for item in items if item.item_id == f"global_genre_playlist_{JAZZ.key}")
    built_tracks = await LastFMCollections(
        LastFMDiscovery(provider), genre_profile=GENRE_PROFILE
    ).get_playlist_tracks(playlist.item_id)
    provider._resolved_virtual_playlists[playlist.item_id] = (playlist, built_tracks)
    tracks = await provider.get_playlist_tracks(playlist.item_id)
    assert len(tracks) == 1
    assert tracks[0].uri == track.uri
    assert tracks[0].position == 1
    assert track.position is None
    assert not track.metadata.links
    provider.api.get_items.assert_awaited_once_with("tag.getTopTracks", {"tag": "jazz"}, limit=50)


async def test_refresh_uses_selected_user_and_restores_request_context(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Build as the configured user even when another user initiated the refresh."""
    selected = Mock(user_id="selected-user", enabled=True)
    requester = Mock(user_id="requesting-user")
    provider.mass.config.get.return_value[CONF_RECOMMENDATION_USER_ID] = selected.user_id
    provider.mass.webserver.auth.get_user = AsyncMock(return_value=selected)
    provider._refresh_lock = asyncio.Lock()
    provider.api = Mock()
    provider.api.get_items = AsyncMock(return_value=[])

    async def get_top_items(**_kwargs: object) -> list[TopItemResult]:
        assert get_current_user() is selected
        return []

    provider.mass.statistics.get_top_items.side_effect = get_top_items
    with user_auth_context(requester):
        await provider._refresh_recommendations()
        assert get_current_user() is requester
    assert len(provider._recommendation_folders) == 21
    assert provider._resolved_virtual_playlists
    provider.mass.webserver.auth.get_user.assert_awaited_once_with(selected.user_id)


@pytest.mark.parametrize(
    ("folder_id", "name", "left_title", "right_title"),
    [
        ("user_genre_playlists_monthly", "Jazz", "Genre", "Jazz"),
        ("global_genre_playlists", "Rock", "Genre", "Rock"),
        ("user_related_genre_playlists", "Soul", "Genre", "Soul"),
        ("user_artist_playlists_monthly", "Featuring Radiohead", "K\u00fcnstler", "Radiohead"),
        ("global_artist_radio_playlists", "Radiohead & similar artists", "Radio", "Radiohead"),
        ("global_mood_playlists", "Party", "Stimmung", "Party"),
        ("global_decade_playlists", "1980s", "Jahrzehnt", "1980s"),
    ],
)
async def test_playlist_cover_titles_and_cached_image_reads(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
    folder_id: str,
    name: str,
    left_title: str,
    right_title: str,
) -> None:
    """Publish a real cover for every playlist family without fetching anything on reads."""
    playlist = Playlist(
        item_id="cover-test", provider=provider.instance_id, name=name, provider_mappings=set()
    )
    renderer = Mock(wraps=build_split_cover_png)
    monkeypatch.setattr(provider_module, "build_split_cover_png", renderer)
    await provider._set_playlist_cover(playlist, [], folder_id)
    renderer.assert_called_once_with(left_title, [], right_title)
    assert playlist.image is not None
    assert playlist.image.provider == provider.instance_id
    assert not playlist.image.remotely_accessible
    provider._resolved_virtual_playlists[playlist.item_id] = (playlist, [])
    with user_auth_context(Mock(user_id="DEBUG")):
        payload = await provider.resolve_image(playlist.image.path)
        prepared = await provider.get_playlist(playlist.item_id)
        assert prepared.image == playlist.image
        assert await provider.get_playlist_tracks(playlist.item_id) == []
    with Image.open(BytesIO(payload)) as cover:
        assert cover.size == (1200, 1200)
    provider.mass.http_session.get.assert_not_called()
    provider.mass.statistics.get_top_items.assert_not_awaited()


def track_with_cover(item_id: str, path: str) -> Track:
    """Return a prepared track with a provider-owned artwork reference."""
    track = Track(item_id=item_id, provider="library", name=item_id, provider_mappings=set())
    track.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path=path, provider="source--1")]
    )
    return track


async def test_cover_uses_four_distinct_successful_images_and_preserves_originals(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip missing artwork and duplicates without fetching a music catalogue."""
    images = []
    for color in ("red", "green", "blue", "yellow"):
        with Image.new("RGB", (20, 20), color) as source, BytesIO() as buffer:
            source.save(buffer, format="PNG")
            images.append(buffer.getvalue())
    fetch = AsyncMock(side_effect=[FileNotFoundError(), images[0], images[0], *images[1:]])
    monkeypatch.setattr(provider_module, "get_image_thumb", fetch)
    paths = ["missing", "red", "red", "duplicate-red", "green", "blue", "yellow", "unused"]
    tracks = [track_with_cover(str(index), path) for index, path in enumerate(paths)]
    original_images = [track.image for track in tracks]
    playlist = Playlist(
        item_id="genre", provider=provider.instance_id, name="Jazz", provider_mappings=set()
    )
    await provider._set_playlist_cover(playlist, tracks, "global_genre_playlists")
    assert fetch.await_count == 6
    assert [call.args[1] for call in fetch.await_args_list] == [
        "missing",
        "red",
        "duplicate-red",
        "green",
        "blue",
        "yellow",
    ]
    assert [track.image for track in tracks] == original_images
    assert playlist.image is not None
    payload = await provider.resolve_image(playlist.image.path)
    assert payload == build_split_cover_png("Genre", images, "Jazz")
    provider.mass.http_session.get.assert_not_called()


async def test_failed_artwork_attempts_are_bounded_and_generate_placeholders(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never walk an entire result list looking for unavailable artwork."""
    fetch = AsyncMock(side_effect=MediaNotFoundError("unavailable"))
    monkeypatch.setattr(provider_module, "get_image_thumb", fetch)
    playlist = Playlist(
        item_id="genre", provider=provider.instance_id, name="Jazz", provider_mappings=set()
    )
    await provider._set_playlist_cover(
        playlist,
        [track_with_cover(str(index), str(index)) for index in range(50)],
        "global_genre_playlists",
    )
    assert fetch.await_count == 8
    assert playlist.image is not None
    assert await provider.resolve_image(playlist.image.path) == build_split_cover_png(
        "Genre", [], "Jazz"
    )


@pytest.mark.parametrize(
    "path",
    [
        "../private.png",
        "..\\private.png",
        "/private.png",
        "https://example.com/image.png",
        "a" * 64 + ".png",
    ],
)
async def test_image_resolution_rejects_invalid_or_missing_paths(
    provider: LastFMRecommendationsEnhancedProvider, path: str
) -> None:
    """Reject arbitrary paths and map missing images to the public media error."""
    with pytest.raises(MediaNotFoundError):
        await provider.resolve_image(path)


async def test_covers_are_private_and_isolated_between_provider_instances(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """An image filename does not grant access across users or provider instances."""
    playlist = Playlist(
        item_id="genre", provider=provider.instance_id, name="Jazz", provider_mappings=set()
    )
    await provider._set_playlist_cover(playlist, [], "global_genre_playlists")
    assert playlist.image is not None
    with user_auth_context(Mock(user_id="other-user")), pytest.raises(MediaNotFoundError):
        await provider.resolve_image(playlist.image.path)
    provider.config.instance_id = "lastfm_recommendations_enhanced--other"
    with pytest.raises(MediaNotFoundError):
        await provider.resolve_image(playlist.image.path)


async def test_cover_write_failure_preserves_previous_published_generation(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed image write cannot publish new rows or replace playable playlists."""
    previous_folders = provider._recommendation_folders
    previous_playlists = provider._resolved_virtual_playlists
    playlist = Playlist(
        item_id="global_mood_playlist_party",
        provider=provider.instance_id,
        name="Party",
        provider_mappings=set(),
    )
    folder = RecommendationFolder(
        item_id="global_mood_playlists", provider=provider.instance_id, name="Moods"
    )
    provider.api = Mock(get_items=AsyncMock(return_value=[]))
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    monkeypatch.setattr(provider, "_get_recommendation_folders", AsyncMock(return_value=[folder]))
    monkeypatch.setattr(
        provider, "_build_recommendation_items", AsyncMock(return_value=UniqueList([playlist]))
    )
    monkeypatch.setattr(LastFMCollections, "get_playlist_tracks", AsyncMock(return_value=[]))
    monkeypatch.setattr(Path, "write_bytes", Mock(side_effect=OSError("disk full")))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._refresh_recommendations()
    assert provider._recommendation_folders is previous_folders
    assert provider._resolved_virtual_playlists is previous_playlists
    assert not list(provider._cover_directory().iterdir())


async def test_cancellation_during_cover_cleanup_does_not_publish_partial_generation(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep publication after the final await so cancelled work cannot replace prepared results."""
    previous_folders = provider._recommendation_folders
    previous_playlists = provider._resolved_virtual_playlists
    provider.api = Mock(get_items=AsyncMock(return_value=[]))
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    monkeypatch.setattr(provider, "_get_recommendation_folders", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        provider, "_remove_obsolete_covers", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await provider._refresh_recommendations()
    assert provider._recommendation_folders is previous_folders
    assert provider._resolved_virtual_playlists is previous_playlists


async def test_cover_cleanup_keeps_two_generations_and_other_providers(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Only discard obsolete files in this provider instance's image directory."""
    directory = provider._cover_directory()
    directory.mkdir(parents=True)
    current, previous, obsolete = (letter * 64 + ".png" for letter in "abc")
    for filename in (current, previous, obsolete):
        (directory / filename).write_bytes(b"test")
    unrelated = Path(provider.mass.cache_path) / "other-provider.png"
    unrelated.write_bytes(b"keep")
    await provider._remove_obsolete_covers({current, previous})
    assert {path.name for path in directory.iterdir()} == {current, previous}
    assert unrelated.read_bytes() == b"keep"


async def test_refresh_builds_dynamic_genres_from_unmapped_streaming_artist(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Global Explore and personal artist tags remain separate in one prepared generation."""
    selected = Mock(user_id="selected-user", enabled=True)
    provider.mass.config.get.return_value[CONF_RECOMMENDATION_USER_ID] = selected.user_id
    provider.mass.webserver.auth.get_user = AsyncMock(return_value=selected)
    artist = ItemMapping(
        item_id="external",
        provider="stream--1",
        name="Streaming Artist",
        media_type=MediaType.ARTIST,
    )

    async def top_items(**kwargs: object) -> list[TopItemResult]:
        assert get_current_user() is selected
        if kwargs["media_type"] == MediaType.ARTIST and kwargs["period"] == "month":
            return [TopItemResult(item=artist, play_count=12)]
        return []

    async def items(
        method: str, parameters: dict[str, str] | None = None, **_kwargs: object
    ) -> list[LastFMItem]:
        assert get_current_user() is selected
        if method == "chart.getTopTags":
            return [LastFMItem("Afrobeat"), LastFMItem("Bluegrass")]
        if method == "artist.getTopTags":
            assert parameters == {"artist": "Streaming Artist"}
            return [LastFMItem("Shoegaze", tag_weight=100), LastFMItem("Dream Pop", tag_weight=50)]
        return []

    provider.mass.statistics.get_top_items.side_effect = top_items
    provider.api = Mock(get_items=AsyncMock(side_effect=items))
    await provider._refresh_recommendations()
    rows = [
        folder
        for folder in provider._recommendation_folders
        if folder.item_id.startswith("global_genre_artists_")
    ]
    assert [folder.translation_params for folder in rows] == [["Afrobeat"], ["Bluegrass"]]
    assert all("explore artists" in folder.name for folder in rows)
    personal_rows = [
        folder
        for folder in provider._recommendation_folders
        if folder.item_id.startswith("user_genre_artists_")
    ]
    assert [folder.translation_params for folder in personal_rows] == [["Shoegaze"], ["Dream Pop"]]
    assert (
        sum(call.args[0] == "chart.getTopTags" for call in provider.api.get_items.await_args_list)
        == 1
    )
    assert (
        sum(call.args[0] == "artist.getTopTags" for call in provider.api.get_items.await_args_list)
        == 1
    )
    tags = [genre_for_tag("Shoegaze"), genre_for_tag("Dream Pop")]
    for category in tags:
        assert category is not None
        assert f"global_genre_playlist_{category.key}" not in provider._resolved_virtual_playlists
        assert f"user_genre_playlist_{category.key}" in provider._resolved_virtual_playlists
    for tag in ("Afrobeat", "Bluegrass"):
        category = genre_for_tag(tag)
        assert category is not None
        assert f"global_genre_playlist_{category.key}" in provider._resolved_virtual_playlists
    provider.api.get_items.reset_mock()
    provider.mass.statistics.get_top_items.reset_mock()
    with user_auth_context(selected):
        assert await provider.get_recommendations()
        for folder in rows:
            await provider.get_recommendation_items(folder.item_id)
        for playlist_id in provider._resolved_virtual_playlists:
            await provider.get_playlist_tracks(playlist_id)
    provider.api.get_items.assert_not_awaited()
    provider.mass.statistics.get_top_items.assert_not_awaited()


async def test_failed_global_tag_refresh_preserves_previous_generation(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """A global Explore failure cannot replace the last completed personal or general rows."""
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    previous = [
        RecommendationFolder(
            item_id="global_genre_artists_previous",
            provider=provider.instance_id,
            name="Previous Explore",
        )
    ]
    provider._recommendation_folders = previous
    provider.api = Mock(get_items=AsyncMock(side_effect=ResourceTemporarilyUnavailable("private")))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._refresh_recommendations()
    assert provider._recommendation_folders is previous
    provider.api.get_items.assert_awaited_once_with("chart.getTopTags", limit=50)


async def test_failed_refresh_preserves_previous_generation(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """A failed build must neither publish partial folders nor replace playable playlists."""
    previous_folders = []
    previous_playlists = {}
    selected = Mock(user_id="selected-user", enabled=True)
    provider.mass.config.get.return_value[CONF_RECOMMENDATION_USER_ID] = selected.user_id
    provider.mass.webserver.auth.get_user = AsyncMock(return_value=selected)
    provider._refresh_lock = asyncio.Lock()
    provider._recommendation_folders = previous_folders
    provider._resolved_virtual_playlists = previous_playlists
    provider.mass.statistics.get_top_items.side_effect = ResourceTemporarilyUnavailable("private")
    requester = Mock(user_id="requesting-user")
    with user_auth_context(requester), pytest.raises(ResourceTemporarilyUnavailable):
        await provider._refresh_recommendations()
    assert provider._recommendation_folders is previous_folders
    assert provider._resolved_virtual_playlists is previous_playlists


async def test_refresh_rejects_missing_user(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Never fall back to the requester or all users when no MA user was selected."""
    provider.mass.config.get.return_value[CONF_RECOMMENDATION_USER_ID] = None
    provider._refresh_lock = asyncio.Lock()
    with pytest.raises(AuthenticationFailed):
        await provider._refresh_recommendations()
    provider.mass.statistics.get_top_items.assert_not_awaited()


async def test_prepared_results_are_hidden_from_other_users(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Neither folder contents nor playlist tracks can reveal the selected user's profile."""
    provider._recommendation_folders = await provider._get_recommendation_folders(
        genre_profile=GENRE_PROFILE
    )
    items = await provider._build_recommendation_items(
        "global_genre_playlists", genre_profile=GENRE_PROFILE
    )
    provider._resolved_virtual_playlists[items[0].item_id] = (items[0], [])
    for user in (None, Mock(user_id="other-user")):
        with user_auth_context(user):
            assert not await provider.get_recommendations()
            assert not await provider.get_recommendation_items("global_genre_playlists")
    with user_auth_context(Mock(user_id="other-user")):
        with pytest.raises(MediaNotFoundError):
            await provider.get_playlist(items[0].item_id)
        with pytest.raises(MediaNotFoundError):
            await provider.get_playlist_tracks(items[0].item_id)
    with user_auth_context(Mock(user_id="DEBUG")):
        assert len(await provider.get_recommendations()) == 24
        provider.mass.config.get.return_value[CONF_RECOMMENDATION_USER_ID] = "other-user"
        assert not await provider.get_recommendations()


async def test_user_setting_only_offers_enabled_ma_users(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Persist a selected MA id, not a Last.fm username or a disabled account."""
    provider.mass.webserver.auth.list_users = AsyncMock(
        return_value=[
            Mock(user_id="alice", display_name="Alice", username="alice", enabled=True),
            Mock(user_id="disabled", display_name="Disabled", username="disabled", enabled=False),
        ]
    )
    entry = await get_recommendation_user_entry(provider.mass)
    assert entry.key == CONF_RECOMMENDATION_USER_ID
    assert entry.required
    assert [(option.value, option.title) for option in entry.options] == [("alice", "Alice")]


async def test_initialization_registers_daily_refresh_and_delayed_startup(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use MA's task manager for the 24-hour interval and the first startup build."""
    api = Mock(validate_api_key=AsyncMock())
    monkeypatch.setattr(
        "music_assistant.providers.lastfm_recommendations_enhanced.LastFMAPIClient",
        Mock(return_value=api),
    )
    provider.mass.config.get.return_value["api_key"] = "test-key"
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    provider.mass.tasks.register_scheduled_task.return_value = Mock(schedule=None)
    await provider.handle_async_init()
    registered = provider.mass.tasks.register_scheduled_task.call_args.kwargs
    assert registered["schedule"] == TaskSchedule.hourly(every=24)
    assert registered["handler"] == provider._refresh_recommendations
    assert registered["task_id"] == f"{REFRESH_TASK_ID}_{provider.instance_id}"
    startup = provider.mass.call_later.call_args
    assert startup.args[0] == 20
    startup.args[1]()
    provider.mass.tasks.run_task.assert_called_once_with(registered["task_id"])
    assert not provider._recommendation_folders


async def test_unload_cancels_startup_and_waits_for_refresh(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Prevent background builds from publishing after the provider has been unloaded."""
    provider.mass.tasks.unregister_scheduled_task_and_wait = AsyncMock(return_value=True)
    await provider.unload(is_removed=True)
    provider.mass.cancel_timer.assert_called_once_with(
        f"{REFRESH_TASK_ID}_initial_{provider.instance_id}"
    )
    provider.mass.tasks.unregister_scheduled_task_and_wait.assert_awaited_once_with(
        f"{REFRESH_TASK_ID}_{provider.instance_id}", clear_persisted_state=True
    )
    assert provider._is_unloading
    await provider._refresh_recommendations()
    provider.mass.statistics.get_top_items.assert_not_awaited()


async def test_cancelled_refresh_does_not_publish_partial_results(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Cancellation preserves the last completed generation and unwinds the user context."""
    selected = Mock(user_id="DEBUG", enabled=True)
    provider.mass.webserver.auth.get_user = AsyncMock(return_value=selected)
    provider.api = Mock(get_items=AsyncMock(return_value=[]))
    previous_folders = provider._recommendation_folders
    previous_playlists = provider._resolved_virtual_playlists
    building = asyncio.Event()

    async def blocked_build(_folder_id: str, **_kwargs: object) -> UniqueList:
        building.set()
        await asyncio.Future()
        return UniqueList()

    provider._build_recommendation_items = AsyncMock(side_effect=blocked_build)
    task = asyncio.create_task(provider._refresh_recommendations())
    await building.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider._recommendation_folders is previous_folders
    assert provider._resolved_virtual_playlists is previous_playlists


async def test_overlapping_refreshes_are_serialized(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Only one generation may query and resolve data at a time."""
    selected = Mock(user_id="DEBUG", enabled=True)
    provider.mass.webserver.auth.get_user = AsyncMock(return_value=selected)
    provider.api = Mock(get_items=AsyncMock(return_value=[]))
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    highest_active = 0

    async def build_folders(**_kwargs: object) -> list[RecommendationFolder]:
        nonlocal active, highest_active
        active += 1
        highest_active = max(highest_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return []

    provider._get_recommendation_folders = AsyncMock(side_effect=build_folders)
    first = asyncio.create_task(provider._refresh_recommendations())
    await entered.wait()
    second = asyncio.create_task(provider._refresh_recommendations())
    release.set()
    await asyncio.gather(first, second)
    assert provider._get_recommendation_folders.await_count == 2
    assert highest_active == 1
