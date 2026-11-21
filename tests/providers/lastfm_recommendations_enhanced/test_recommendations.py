"""Personal top-items folders and their generation diagnostics."""

import asyncio
import json
import logging
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ImageType, MediaType, ProviderFeature, ProviderType
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
from music_assistant_models.translations import TRANSLATION_RESOLVER
from PIL import Image

from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    user_auth_context,
)
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers import lastfm_recommendations_enhanced as provider_module
from music_assistant.providers.lastfm_recommendations_enhanced import (
    SUPPORTED_FEATURES,
    TOP_ITEMS_FOLDERS,
    LastFMRecommendationsEnhancedProvider,
    _recommendation_user_context,
    get_recommendation_user_entry,
)
from music_assistant.providers.lastfm_recommendations_enhanced import (
    recommendations as recommendations_module,
)
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMItem
from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    EXTRA_FOLDERS,
    GENRE_FOLDER_FAMILIES,
    MOODS,
    GenreProfile,
    PlaylistSpec,
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
GENRE_PROFILE = GenreProfile((JAZZ,), explore_genres=(JAZZ,))
GENRE_FOLDER_IDS = tuple(f"{prefix}_{JAZZ.key}" for prefix, _label in GENRE_FOLDER_FAMILIES)


@pytest.fixture
def provider(tmp_path: Path) -> LastFMRecommendationsEnhancedProvider:
    """Return a real provider with an isolated, mocked statistics controller."""
    mass = Mock()
    mass.cache_path = str(tmp_path)
    mass.config.get.return_value = {CONF_RECOMMENDATION_USER_ID: "DEBUG"}
    mass.config.decrypt_string.side_effect = lambda value: value
    mass.statistics.get_top_items = AsyncMock(return_value=[])
    mass.config.get_provider_configs = AsyncMock(return_value=[])
    mass.translations = Mock(spec=[])
    mass.metadata.locale = "en_US"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
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
    manifest.type = ProviderType(
        json.loads(
            Path(provider_module.__file__).with_name("manifest.json").read_text(encoding="utf-8")
        )["type"]
    )
    result = LastFMRecommendationsEnhancedProvider(mass, manifest, config, SUPPORTED_FEATURES)
    result._recommendation_folders = []
    result._resolved_virtual_playlists = {}
    result._refresh_lock = asyncio.Lock()
    result._is_unloading = False
    result._recommendation_user_id = "DEBUG"
    return result


async def test_generated_playlist_passes_legacy_plugin_playback_gate(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Keep generated playlists reachable through b7's plugin source exception."""
    assert isinstance(provider, PluginProvider)
    assert provider.type == ProviderType.PLUGIN
    playlist = provider._playlist(PlaylistSpec("user_genre_playlist_metal", "Metal", tag="metal"))
    track = Track(
        item_id="source-track",
        provider="source--1",
        name="Song",
        artists=UniqueList(),
        provider_mappings={
            ProviderMapping(
                item_id="source-track", provider_domain="source", provider_instance="source--1"
            )
        },
    )
    provider.mass.providers = [provider]
    plugin_instances = {
        instance.instance_id
        for instance in provider.mass.providers
        if instance.type == ProviderType.PLUGIN
    }
    assert playlist.provider in plugin_instances
    assert playlist.provider != track.provider
    provider._resolved_virtual_playlists[playlist.item_id] = (playlist, [track])
    assert await provider.get_playlist(playlist.item_id) == playlist
    assert await provider.get_playlist_tracks(playlist.item_id) == [track]
    assert await provider.get_playlist_tracks(playlist.item_id, page=1) == []


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
    assert all(folder.translation_key is None and not folder.items for folder in folders)
    assert all(folder.subtitle for folder in folders)
    assert len({folder.item_id for folder in folders}) == 22
    assert all(folder.item_id.startswith(("user_", "global_", "de_")) for folder in folders)
    provider.mass.statistics.get_top_items.assert_not_awaited()
    provider.mass.http_session.get.assert_not_called()


@pytest.mark.parametrize("locale", ["en_US", "de_DE", "de-DE", "fr_FR"])
async def test_folder_catalogs_and_localized_serialization(
    provider: LastFMRecommendationsEnhancedProvider,
    locale: str,
) -> None:
    """Build all folders using server language without any server translation API or catalogs."""
    directory = Path(provider_module.__file__).parent / "translations"
    source = json.loads((directory / "en.json").read_text(encoding="utf-8"))["media"][
        "recommendations"
    ]
    german = json.loads((directory / "de.json").read_text(encoding="utf-8"))["media"][
        "recommendations"
    ]
    assert len(source) == 22
    assert source.keys() == german.keys()
    catalog = german if locale.startswith("de") else source
    provider.mass.metadata.locale = locale
    folders = await provider._get_recommendation_folders(genre_profile=GENRE_PROFILE)
    assert len(folders) == 22
    resolver = Mock(side_effect=AssertionError("Folder texts must not use shared translations"))
    token = TRANSLATION_RESOLVER.set(resolver)
    try:
        for folder in folders:
            key = next(
                (
                    prefix
                    for prefix, _label in GENRE_FOLDER_FAMILIES
                    if folder.item_id.startswith(f"{prefix}_")
                ),
                folder.item_id,
            )
            params = ["Jazz"] if key != folder.item_id else []
            localized = folder.to_dict()
            assert localized["name"] == catalog[key]["name"].format(*params)
            assert localized["subtitle"] == catalog[key]["subtitle"]
            assert "Last.fm" not in localized["subtitle"]
            assert "https://" not in localized["subtitle"]
            assert folder.translation_key is None
            assert folder.translation_params is None
            assert folder.name == localized["name"]
            if params:
                assert localized["name"].startswith("Jazz:")
            if locale.startswith("de"):
                assert localized["name"] != source[key]["name"].format(*params)
        resolver.assert_not_called()
        assert not provider.mass.translations.mock_calls
    finally:
        TRANSLATION_RESOLVER.reset(token)


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), ValueError("invalid JSON")])
async def test_missing_or_invalid_local_catalog_falls_back_to_english(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Missing or broken localized files retain English names and descriptions."""
    source = json.loads(
        (Path(provider_module.__file__).parent / "translations" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    provider.mass.metadata.locale = "de_DE"
    monkeypatch.setattr(provider_module, "load_json_dict", AsyncMock(side_effect=[source, error]))
    warning = Mock(wraps=provider.logger.warning)
    monkeypatch.setattr(provider.logger, "warning", warning)
    assert await provider._get_media_texts("recommendations") == source["media"]["recommendations"]
    if isinstance(error, FileNotFoundError):
        warning.assert_not_called()
    else:
        warning.assert_called_once()


async def test_partial_catalog_keeps_english_fields(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Translate an available name without losing an untranslated subtitle or folder."""
    source = json.loads(
        (Path(provider_module.__file__).parent / "translations" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    translated = {"media": {"recommendations": {"user_recommendations": {"name": "Empfehlungen"}}}}
    provider.mass.metadata.locale = "de_DE"
    monkeypatch.setattr(
        provider_module, "load_json_dict", AsyncMock(side_effect=[source, translated])
    )
    texts = await provider._get_media_texts("recommendations")
    assert texts["user_recommendations"]["name"] == "Empfehlungen"
    assert (
        texts["user_recommendations"]["subtitle"]
        == source["media"]["recommendations"]["user_recommendations"]["subtitle"]
    )
    assert texts["global_top_tracks"] == source["media"]["recommendations"]["global_top_tracks"]


async def test_server_language_is_read_again_for_each_folder_build(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """A changed server language applies on the next build without changing folder identities."""
    english = await provider._get_recommendation_folders(genre_profile=GENRE_PROFILE)
    provider.mass.metadata.locale = "de_DE"
    german = await provider._get_recommendation_folders(genre_profile=GENRE_PROFILE)
    assert [folder.item_id for folder in english] == [folder.item_id for folder in german]
    assert all(first.name != second.name for first, second in zip(english, german, strict=True))


@pytest.mark.parametrize("genre_count", [0, 1, 3, 5])
async def test_global_genre_rows_are_limited_to_three_without_limiting_playlists(
    provider: LastFMRecommendationsEnhancedProvider, genre_count: int
) -> None:
    """Advertise the first three album and artist genres without limiting genre playlists."""
    categories = []
    for name in ("jazz", "rock", "pop", "classical", "blues"):
        category = genre_for_tag(name)
        assert category is not None
        categories.append(category)
    genres = tuple(categories[:genre_count])
    profile = GenreProfile(genres, explore_genres=genres)

    folders = await provider._get_recommendation_folders(genre_profile=profile)

    for prefix, expected in (
        ("global_genre_albums", genres[:3]),
        ("global_genre_artists", genres[:3]),
        ("user_genre_artists", genres[:3]),
    ):
        assert [
            folder.item_id for folder in folders if folder.item_id.startswith(f"{prefix}_")
        ] == [f"{prefix}_{category.key}" for category in expected]
    collections = LastFMCollections(LastFMDiscovery(provider), genre_profile=profile)
    recipes = await collections.get_playlist_specs("global_genre_playlists")
    assert [recipe.item_id for recipe in recipes] == [
        f"global_genre_playlist_{category.key}" for category in genres
    ]
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


@pytest.mark.parametrize("fails", [False, True])
async def test_refresh_info_logs_only_folder_start_and_completed_summary(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fails: bool,
) -> None:
    """Folder completion follows track resolution; failed folders have no success summary."""
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    monkeypatch.setattr(
        LastFMCollections, "get_genre_profile", AsyncMock(return_value=GENRE_PROFILE)
    )
    monkeypatch.setattr(LastFMCollections, "get_explore_genres", AsyncMock(return_value=()))
    folder_ids = ["user_top_artists_monthly", "global_mood_playlists"]
    folders = [
        RecommendationFolder(item_id=folder_id, provider=provider.instance_id, name=folder_id)
        for folder_id in folder_ids
    ]
    monkeypatch.setattr(provider, "_get_recommendation_folders", AsyncMock(return_value=folders))
    playlist = provider._playlist(PlaylistSpec("global_mood_playlist_chill", "Chill", tag="chill"))
    tracks = [
        Track(
            item_id=str(index), provider="library", name=f"Track {index}", provider_mappings=set()
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        provider,
        "_build_recommendation_items",
        AsyncMock(side_effect=[UniqueList(), UniqueList([playlist])]),
    )
    monkeypatch.setattr(provider, "_set_playlist_cover", AsyncMock())
    monkeypatch.setattr(provider, "_remove_obsolete_covers", AsyncMock())
    monkeypatch.setattr(provider, "_schedule_refresh_retry", Mock())

    async def resolve_tracks(_item_id: str) -> list[Track]:
        assert "Last.fm folder build started folder=global_mood_playlists" in caplog.text
        assert "Last.fm folder build completed folder=global_mood_playlists" not in caplog.text
        if fails:
            raise ResourceTemporarilyUnavailable("Source unavailable")
        return tracks

    monkeypatch.setattr(
        LastFMCollections, "get_playlist_tracks", AsyncMock(side_effect=resolve_tracks)
    )
    with caplog.at_level(logging.INFO, logger=provider.logger.name):
        if fails:
            with pytest.raises(ResourceTemporarilyUnavailable):
                await provider._refresh_recommendations()
        else:
            await provider._refresh_recommendations()
    messages = [
        record.getMessage().split(" elapsed=")[0]
        for record in caplog.records
        if record.name == provider.logger.name and record.levelno == logging.INFO
    ]
    expected = [
        "Last.fm folder build started folder=user_top_artists_monthly",
        "Last.fm folder build completed folder=user_top_artists_monthly "
        "items=0 playlists=0 playlist_tracks=0",
        "Last.fm folder build started folder=global_mood_playlists",
    ]
    if not fails:
        expected.extend(
            [
                "Last.fm folder build completed folder=global_mood_playlists "
                "items=1 playlists=1 playlist_tracks=2",
            ]
        )
    assert messages == expected


async def test_refresh_changes_random_selection_but_opening_keeps_the_generation(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a completed refresh replaces a random selection; reads never generate candidates."""
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    monkeypatch.setattr(
        LastFMCollections, "get_genre_profile", AsyncMock(return_value=GENRE_PROFILE)
    )
    monkeypatch.setattr(LastFMCollections, "get_explore_genres", AsyncMock(return_value=()))
    folder_id = "user_artist_recommendations"
    monkeypatch.setattr(
        provider,
        "_get_recommendation_folders",
        AsyncMock(
            side_effect=lambda **_kwargs: [
                RecommendationFolder(
                    item_id=folder_id, provider=provider.instance_id, name="Artists"
                )
            ]
        ),
    )
    monkeypatch.setattr(provider, "_remove_obsolete_covers", AsyncMock())
    candidates = [LastFMItem(f"Artist {rank}") for rank in range(100)]
    items = {
        candidate.name: Artist(
            item_id=str(rank), provider="library", name=candidate.name, provider_mappings=set()
        )
        for rank, candidate in enumerate(candidates)
    }
    fetch_candidates = AsyncMock(return_value=candidates)
    monkeypatch.setattr(LastFMDiscovery, "get_seeds", AsyncMock(return_value=[]))
    monkeypatch.setattr(LastFMDiscovery, "_candidates", fetch_candidates)
    monkeypatch.setattr(
        LastFMDiscovery,
        "resolve_items",
        AsyncMock(
            side_effect=lambda pool, *_args, **_kwargs: UniqueList(
                items[candidate.name] for candidate in pool[:25]
            )
        ),
    )
    shuffle_candidates = Mock()
    shuffle_candidates.side_effect = lambda pool: (
        pool.reverse() if shuffle_candidates.call_count == 1 else None
    )
    monkeypatch.setattr(recommendations_module, "shuffle", shuffle_candidates)
    await provider._refresh_recommendations()
    with user_auth_context(Mock(user_id="DEBUG")):
        first = await provider.get_recommendation_items(folder_id)
        again = await provider.get_recommendation_items(folder_id)
        assert [item.name for item in first] == [f"Artist {rank}" for rank in range(74, 49, -1)]
        assert again == first
        shuffle_candidates.assert_called_once()
        fetch_candidates.assert_awaited_once()
        await provider._refresh_recommendations()
        second = await provider.get_recommendation_items(folder_id)
        assert [item.name for item in second] == [f"Artist {rank}" for rank in range(25)]
        assert await provider.get_recommendation_items(folder_id) == second
    assert shuffle_candidates.call_count == fetch_candidates.await_count == 2
    assert candidates[0].name == "Artist 0"
    provider.mass.http_session.get.assert_not_called()


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


async def test_generated_playlist_returns_streaming_tracks_with_positions(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve playable streaming identities and leave the search result untouched."""
    shuffle_candidates = Mock()
    monkeypatch.setattr(recommendations_module, "shuffle", shuffle_candidates)
    mapping = ProviderMapping(
        item_id="source-id", provider_domain="source", provider_instance="source--1"
    )
    artist = Artist(item_id="2", provider="source--1", name="Artist", provider_mappings={mapping})
    track = Track(
        item_id="1",
        provider="source--1",
        name="Song",
        artists=UniqueList([artist]),
        provider_mappings={mapping},
    )
    provider.api = Mock()
    provider.api.get_items = AsyncMock(return_value=[LastFMItem("Song", "Artist")])
    controller = provider.mass.music.get_controller.return_value
    controller.db_table = "tracks"
    source = Mock(instance_id="source--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    provider.mass.music.providers = [source]
    controller.search = AsyncMock(return_value=[track])
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
    controller.get_library_items_by_query.assert_not_awaited()
    assert await provider.get_playlist_tracks(playlist.item_id) == tracks
    assert await provider.get_playlist_tracks(playlist.item_id) == tracks
    shuffle_candidates.assert_called_once()
    provider.api.get_items.assert_awaited_once_with("tag.getTopTracks", {"tag": "jazz"}, limit=150)


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
    assert len(provider._recommendation_folders) == 19
    assert provider._resolved_virtual_playlists
    provider.mass.webserver.auth.get_user.assert_awaited_once_with(selected.user_id)


@pytest.mark.parametrize("locale", ["en", "de"])
@pytest.mark.parametrize(
    ("folder_id", "name", "left_title", "right_title"),
    [
        ("user_genre_playlists_monthly", "Jazz", "Genre", "Jazz"),
        ("global_genre_playlists", "Rock", "Genre", "Rock"),
        ("user_artist_playlists_monthly", "Featuring Radiohead", "Feat.", "Radiohead"),
        ("global_artist_radio_playlists", "Featuring Radiohead", "Feat.", "Radiohead"),
        ("global_mood_playlists", "Party", "Mood", "Party"),
        ("global_decade_playlists", "1980s", "Decade", "1980s"),
    ],
)
async def test_playlist_cover_titles_and_cached_image_reads(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
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
    provider.mass.metadata.locale = locale
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


@pytest.mark.parametrize("locale", ["en_US", "de_DE", "de-DE", "fr_FR"])
async def test_mood_playlist_names_are_localized_but_covers_remain_english(
    provider: LastFMRecommendationsEnhancedProvider,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
) -> None:
    """Translate all mood names without changing IDs, English artwork or cached reads."""
    directory = Path(provider_module.__file__).parent / "translations"
    english = json.loads((directory / "en.json").read_text(encoding="utf-8"))["media"][
        "mood_playlists"
    ]
    german = json.loads((directory / "de.json").read_text(encoding="utf-8"))["media"][
        "mood_playlists"
    ]
    assert set(english) == set(german) == {mood.key for mood in MOODS}
    provider.mass.metadata.locale = locale
    playlists = await provider._build_recommendation_items("global_mood_playlists")
    catalog = german if locale.startswith("de") else english
    assert len(playlists) == 10
    renderer = Mock(wraps=build_split_cover_png)
    monkeypatch.setattr(provider_module, "build_split_cover_png", renderer)
    for playlist, mood in zip(playlists, MOODS, strict=True):
        assert isinstance(playlist, Playlist)
        assert playlist.item_id == f"global_mood_playlist_{mood.key}"
        assert playlist.name == playlist.to_dict()["name"] == catalog[mood.key]["name"]
        assert english[mood.key]["name"] == mood.name
        await provider._set_playlist_cover(playlist, [], "global_mood_playlists")
        renderer.assert_called_with("Mood", [], mood.name)
        assert playlist.image is not None
        provider._resolved_virtual_playlists[playlist.item_id] = (playlist, [])
        with user_auth_context(Mock(user_id="DEBUG")):
            assert (await provider.get_playlist(playlist.item_id)).name == catalog[mood.key]["name"]
            assert await provider.resolve_image(playlist.image.path) == build_split_cover_png(
                "Mood", [], mood.name
            )
    assert not provider.mass.translations.mock_calls
    provider.mass.statistics.get_top_items.assert_not_awaited()
    provider.mass.http_session.get.assert_not_called()


async def test_partial_mood_catalog_falls_back_to_english_names(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing mood translations retain their English names rather than hiding playlists."""
    source = json.loads(
        (Path(provider_module.__file__).parent / "translations" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    translated = {"media": {"mood_playlists": {"relax": {"name": "Entspannung"}}}}
    provider.mass.metadata.locale = "de_DE"
    monkeypatch.setattr(
        provider_module, "load_json_dict", AsyncMock(side_effect=[source, translated])
    )
    playlists = await provider._build_recommendation_items("global_mood_playlists")
    assert playlists[0].name == "Entspannung"
    assert [playlist.name for playlist in playlists[1:]] == [mood.name for mood in MOODS[1:]]


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


async def test_refresh_publishes_prepared_genre_playlists_without_fetching_tracks_twice(
    provider: LastFMRecommendationsEnhancedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish only completed genre selections and reuse their already prepared tracks."""
    selected = Mock(user_id="selected-user", enabled=True)
    provider.mass.config.get.return_value[CONF_RECOMMENDATION_USER_ID] = selected.user_id
    provider.mass.webserver.auth.get_user = AsyncMock(return_value=selected)
    genres = tuple(category for index in range(15) if (category := genre_for_tag(f"Genre {index}")))
    monkeypatch.setattr(
        LastFMCollections, "get_genre_profile", AsyncMock(return_value=GenreProfile(genres))
    )
    monkeypatch.setattr(LastFMCollections, "get_explore_genres", AsyncMock(return_value=()))
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="library",
        name="Track",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    completed = [
        (
            PlaylistSpec(
                f"user_genre_playlist_{genre.key}", genre.name, tag=genre.tag, personal=True
            ),
            [track],
        )
        for genre in genres[:10]
    ]
    prepare = AsyncMock(return_value=completed)
    fetch_tracks = AsyncMock(side_effect=AssertionError("Prepared tracks must be reused"))
    monkeypatch.setattr(LastFMCollections, "get_personal_genre_playlists", prepare)
    monkeypatch.setattr(LastFMCollections, "get_playlist_tracks", fetch_tracks)
    monkeypatch.setattr(
        provider, "_build_recommendation_items", AsyncMock(return_value=UniqueList())
    )
    monkeypatch.setattr(provider, "_set_playlist_cover", AsyncMock())
    monkeypatch.setattr(provider, "_remove_obsolete_covers", AsyncMock())
    await provider._refresh_recommendations()
    folder = next(
        folder
        for folder in provider._recommendation_folders
        if folder.item_id == "user_genre_playlists_monthly"
    )
    assert [item.item_id for item in folder.items] == [
        recipe.item_id for recipe, _tracks in completed
    ]
    assert len(provider._resolved_virtual_playlists) == 10
    assert all(
        tracks == [track] for _playlist, tracks in provider._resolved_virtual_playlists.values()
    )
    prepare.assert_awaited_once()
    fetch_tracks.assert_not_awaited()


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
    assert [folder.name for folder in rows] == [
        "Afrobeat: explore artists",
        "Bluegrass: explore artists",
    ]
    assert all(folder.translation_params is None for folder in rows)
    personal_rows = [
        folder
        for folder in provider._recommendation_folders
        if folder.item_id.startswith("user_genre_artists_")
    ]
    assert [folder.name for folder in personal_rows] == [
        "Shoegaze: artists from your favorite genre",
        "Dream Pop: artists from your favorite genre",
    ]
    assert all(folder.translation_params is None for folder in personal_rows)
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
        assert f"user_genre_playlist_{category.key}" not in provider._resolved_virtual_playlists
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
        assert len(await provider.get_recommendations()) == 22
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
    provider.mass.translations = Mock(spec=[])
    await provider.handle_async_init()
    assert not provider.mass.translations.mock_calls
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
    provider.mass.cancel_timer.assert_any_call(f"{REFRESH_TASK_ID}_initial_{provider.instance_id}")
    provider.mass.cancel_timer.assert_any_call(f"{REFRESH_TASK_ID}_retry_{provider.instance_id}")
    provider.mass.tasks.unregister_scheduled_task_and_wait.assert_awaited_once_with(
        f"{REFRESH_TASK_ID}_{provider.instance_id}", clear_persisted_state=True
    )
    assert provider._is_unloading
    await provider._refresh_recommendations()
    provider.mass.statistics.get_top_items.assert_not_awaited()


async def test_incomplete_resolution_retries_later_without_publishing(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Preserve the playable generation and debounce a delayed retry of an incomplete build."""
    provider.mass.webserver.auth.get_user = AsyncMock(
        return_value=Mock(user_id="DEBUG", enabled=True)
    )
    provider.api = Mock(get_items=AsyncMock(return_value=[]))
    previous_folders = provider._recommendation_folders
    previous_playlists = provider._resolved_virtual_playlists

    async def build(
        _folder_id: str, *, discovery: LastFMDiscovery, **_kwargs: object
    ) -> UniqueList:
        discovery.state.has_incomplete_searches = True
        discovery.state.retry_after = 900
        return UniqueList()

    provider._build_recommendation_items = AsyncMock(side_effect=build)
    await provider._refresh_recommendations()
    assert provider._recommendation_folders is previous_folders
    assert provider._resolved_virtual_playlists is previous_playlists
    scheduled = provider.mass.call_later.call_args
    assert scheduled.args[0] == 900
    assert scheduled.kwargs["task_id"] == f"{REFRESH_TASK_ID}_retry_{provider.instance_id}"
    scheduled.args[1]()
    provider.mass.tasks.run_task.assert_called_once_with(
        f"{REFRESH_TASK_ID}_{provider.instance_id}"
    )


def test_refresh_retry_respects_minimum_delay_and_unload(
    provider: LastFMRecommendationsEnhancedProvider,
) -> None:
    """Never retry in a tight loop or arm a new timer after unload."""
    provider._schedule_refresh_retry(1)
    assert provider.mass.call_later.call_args.args[0] == 300
    provider._is_unloading = True
    provider.mass.call_later.reset_mock()
    provider._schedule_refresh_retry(900)
    provider.mass.call_later.assert_not_called()


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
