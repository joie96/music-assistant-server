"""Discovery builders use bounded sources, batched resolution and source attribution."""

import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import (
    ExternalID,
    LinkType,
    MediaType,
    ProviderFeature,
    ProviderStatus,
)
from music_assistant_models.errors import RateLimited, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Artist, ItemMapping, Track, UniqueList
from music_assistant_models.statistics import TopItemResult

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import (
    LastFMAPIClient,
    LastFMItem,
)
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import (
    DISCOVERY_FOLDERS,
    RESOLUTION_MISS_LIMIT,
    RESOLUTION_TARGET_LIMIT,
    LastFMDiscovery,
    PersonalSeed,
    ResolutionState,
)


@pytest.fixture
def discovery(monkeypatch: pytest.MonkeyPatch) -> LastFMDiscovery:
    """Keep real media models; mock only API and controller boundaries."""
    monkeypatch.setattr(Artist, "available", property(lambda _item: True))
    monkeypatch.setattr(Track, "available", property(lambda _item: True))
    provider = Mock()
    provider.get_setup_value.return_value = "selected--1"
    provider.mass.config.get_provider_configs = AsyncMock(return_value=[])
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.logger = logging.getLogger("lastfm_discovery_test")
    provider.api.get_items = AsyncMock(return_value=[])
    provider.mass.statistics.get_top_items = AsyncMock(return_value=[])
    provider.mass.music.providers = []
    controller = provider.mass.music.get_controller.return_value
    controller.db_table = "artists"
    controller.get_library_items_by_query = AsyncMock(return_value=[])
    controller.get = AsyncMock(return_value=None)
    controller.get_item_by_external_id = AsyncMock(return_value=None)
    return LastFMDiscovery(provider)


async def test_missing_track_is_resolved_on_a_visible_streaming_source(
    discovery: LastFMDiscovery,
) -> None:
    """Mirror the official resolver's verified controller search without importing it."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH, ProviderFeature.LIBRARY_TRACKS}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="stream--1",
        name="Song",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[track])
    result = await discovery.resolve_items([LastFMItem("Song", "Artist")], MediaType.TRACK)
    assert result[0].uri == track.uri
    assert result[0].metadata.links
    assert not track.metadata.links
    controller.search.assert_awaited_once_with("Artist - Song", "stream--1", limit=2)


async def test_resolution_stops_after_official_miss_limit(
    discovery: LastFMDiscovery,
) -> None:
    """Even a full external chart cannot trigger a search request for every entry."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH, ProviderFeature.LIBRARY_ARTISTS}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[])
    assert not await discovery.resolve_items(
        [LastFMItem(f"Artist {index}") for index in range(50)], MediaType.ARTIST
    )
    assert controller.search.await_count == RESOLUTION_MISS_LIMIT


async def test_successful_streaming_resolution_is_not_limited_to_six_searches(
    discovery: LastFMDiscovery,
) -> None:
    """Resolve the official target count rather than stopping after six successful searches."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    tracks = [
        Track(
            item_id=str(index),
            provider="stream--1",
            name=f"Song {index}",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
        for index in range(50)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(side_effect=[[track] for track in tracks])
    result = await discovery.resolve_items(
        [LastFMItem(track.name, artist.name) for track in tracks], MediaType.TRACK
    )
    assert [item.uri for item in result] == [
        track.uri for track in tracks[:RESOLUTION_TARGET_LIMIT]
    ]
    assert controller.search.await_count == RESOLUTION_TARGET_LIMIT
    assert all(call.kwargs["limit"] == 2 for call in controller.search.await_args_list)


async def test_refresh_reuses_results_without_sharing_between_users(
    discovery: LastFMDiscovery,
) -> None:
    """Share fetched results only when a refresh explicitly supplies its own result pool."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[artist])
    state = ResolutionState()
    first = LastFMDiscovery(discovery.provider, state=state)
    second = LastFMDiscovery(discovery.provider, state=state)
    candidate = LastFMItem("Artist")
    assert await first.resolve_items([candidate], MediaType.ARTIST)
    assert await second.resolve_items([candidate], MediaType.ARTIST)
    assert controller.search.await_count == 1
    assert await LastFMDiscovery(discovery.provider).resolve_items([candidate], MediaType.ARTIST)
    assert controller.search.await_count == 2


async def test_streaming_search_matches_normalized_version_titles(
    discovery: LastFMDiscovery,
) -> None:
    """Normalize Last.fm version suffixes without a library preselection."""
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="stream--1",
        name="Song",
        version="Live",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    controller = discovery.provider.mass.music.get_controller.return_value
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller.search = AsyncMock(return_value=[track])
    result = await discovery.resolve_items([LastFMItem("Song (Live)", "Artist")], MediaType.TRACK)
    assert [item.uri for item in result] == [track.uri]
    controller.get_library_items_by_query.assert_not_awaited()


async def test_mbid_does_not_trigger_library_lookup(discovery: LastFMDiscovery) -> None:
    """MusicBrainz identity checks remain matching evidence, not a library lookup."""
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_library_item_by_external_ids = AsyncMock(return_value=artist)
    controller.search = AsyncMock(return_value=[artist])
    mbid = "5a005d57-2835-4c36-bc95-1f58ad2d3304"
    result = await discovery.resolve_items([LastFMItem("Artist", mbid=mbid)], MediaType.ARTIST)
    assert result[0].uri == artist.uri
    controller.get_library_item_by_external_ids.assert_not_awaited()
    controller.get_library_items_by_query.assert_not_awaited()
    controller.search.assert_awaited_once()


@pytest.mark.parametrize(
    ("result_name", "artist_name", "result_mbid", "expected"),
    [
        ("Artist - Song", "Artist", "", True),
        ("Artist - Song", "Other artist", "", False),
        ("Song", "Artist", "conflicting-id", False),
        ("Song", "Artist", "recording-id", True),
    ],
)
async def test_streaming_matching_checks_credited_titles_and_identity(
    discovery: LastFMDiscovery,
    result_name: str,
    artist_name: str,
    result_mbid: str,
    expected: bool,
) -> None:
    """Handle credited result names without accepting covers or contradictory recordings."""
    artist = Artist(item_id="a", provider="stream--1", name=artist_name, provider_mappings=set())
    track = Track(
        item_id="t",
        provider="stream--1",
        name=result_name,
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    if result_mbid:
        track.external_ids.add((ExternalID.MB_RECORDING, result_mbid))
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[track])
    result = await discovery.resolve_items(
        [LastFMItem("Song", "Artist", mbid="recording-id")], MediaType.TRACK
    )
    assert bool(result) is expected
    controller.get_library_items_by_query.assert_not_awaited()


async def test_personal_seeds_combine_month_week_and_favorites(discovery: LastFMDiscovery) -> None:
    """Recent listening and favorites can change the ranking without rewriting play counts."""
    first = Artist(item_id="a", provider="library", name="First", provider_mappings=set())
    second = Artist(item_id="b", provider="library", name="Second", provider_mappings=set())
    discovery.provider.mass.statistics.get_top_items.side_effect = [
        [
            TopItemResult(item=ItemMapping.from_item(first), play_count=10),
            TopItemResult(item=ItemMapping.from_item(second), play_count=11),
        ],
        [TopItemResult(item=ItemMapping.from_item(first), play_count=2)],
        [],
    ]
    discovery.provider.mass.music.get_controller.return_value.get_library_items_by_query.return_value = [
        second
    ]
    seeds = await discovery.get_seeds(MediaType.ARTIST)
    assert [seed.artist for seed in seeds] == ["Second", "First"]
    assert [seed.weight for seed in seeds] == [16, 14]
    assert seeds[0].identity == second.uri
    assert [
        call.kwargs["period"]
        for call in discovery.provider.mass.statistics.get_top_items.await_args_list
    ] == ["month", "week", "all_time"]


@pytest.mark.parametrize(("requested", "expected"), [(3, 3), (10, 10), (100, 10)])
async def test_artist_seed_limit_retains_unmapped_streaming_history(
    discovery: LastFMDiscovery,
    requested: int,
    expected: int,
) -> None:
    """Genre profiles can use ten streaming artists without resolving local artist metadata."""
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(
            item=ItemMapping(
                item_id=str(index),
                provider="stream--1",
                name=f"Artist {index}",
                media_type=MediaType.ARTIST,
            ),
            play_count=20 - index,
        )
        for index in range(20)
    ]
    seeds = await discovery.get_seeds(MediaType.ARTIST, limit=requested)
    assert len(seeds) == expected
    assert [seed.artist for seed in seeds] == [f"Artist {index}" for index in range(expected)]
    assert [seed.identity for seed in seeds] == [
        row.item.uri
        for row in discovery.provider.mass.statistics.get_top_items.return_value[:expected]
    ]
    calls = discovery.provider.mass.music.get_controller.return_value.get_library_items_by_query.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs == {"favorite": True, "limit": 50}


@pytest.mark.parametrize("error_type", [RateLimited, ResourceTemporarilyUnavailable])
async def test_source_failures_are_not_successful_empty_rows(
    discovery: LastFMDiscovery, error_type: type[Exception]
) -> None:
    """A blocked or incomplete provider search must not masquerade as an empty catalogue."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH, ProviderFeature.LIBRARY_ARTISTS}
    discovery.provider.mass.music.providers = [source]
    discovery.provider.mass.music.get_controller.return_value.search = AsyncMock(
        side_effect=error_type("private")
    )
    assert not await discovery.resolve_items([LastFMItem("Artist")], MediaType.ARTIST)
    assert discovery.state.retry_after >= 300
    discovery.provider.mass.cache.set.assert_not_awaited()
    retry = LastFMDiscovery(discovery.provider, state=discovery.state)
    assert not await retry.resolve_items([LastFMItem("Other artist")], MediaType.ARTIST)
    discovery.provider.mass.music.get_controller.return_value.search.assert_awaited_once()


async def test_resolution_cache_hit_survives_refresh_without_source_search(
    discovery: LastFMDiscovery,
) -> None:
    """Persist verified streaming results for ninety days without persisting attribution."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[artist])
    candidate = LastFMItem("Artist")
    assert await discovery.resolve_items([candidate], MediaType.ARTIST)
    write = discovery.provider.mass.cache.set.await_args
    assert write.kwargs["expiration"] == 90 * 24 * 60 * 60
    assert write.kwargs["data"] == artist.to_dict()
    discovery.provider.mass.cache.get.return_value = Artist.from_dict(write.kwargs["data"])
    assert await LastFMDiscovery(discovery.provider).resolve_items([candidate], MediaType.ARTIST)
    assert controller.search.await_count == 1
    controller.get_library_items_by_query.assert_not_awaited()


async def test_confirmed_miss_expires_and_is_searched_again(discovery: LastFMDiscovery) -> None:
    """Remember only complete misses for one day, then allow another streaming lookup."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[])
    candidate = LastFMItem("Artist")
    assert not await discovery.resolve_items([candidate], MediaType.ARTIST)
    write = discovery.provider.mass.cache.set.await_args
    assert write.kwargs["expiration"] == 24 * 60 * 60
    assert write.kwargs["key"].startswith("miss_resolution_")
    assert write.kwargs["data"] is True
    discovery.provider.mass.cache.get.side_effect = [None, True]
    assert not await LastFMDiscovery(discovery.provider).resolve_items(
        [candidate], MediaType.ARTIST
    )
    assert controller.search.await_count == 1
    discovery.provider.mass.cache.get.side_effect = None
    assert not await LastFMDiscovery(discovery.provider).resolve_items(
        [candidate], MediaType.ARTIST
    )
    assert controller.search.await_count == 2


async def test_loading_music_source_does_not_create_a_confirmed_miss(
    discovery: LastFMDiscovery,
) -> None:
    """Incomplete source setup must not suppress tomorrow's recommendations."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    discovery.provider.mass.config.get_provider_configs.return_value = [
        Mock(status=ProviderStatus.LOADING)
    ]
    discovery.provider.mass.music.get_controller.return_value.search = AsyncMock(return_value=[])
    assert not await discovery.resolve_items([LastFMItem("Artist")], MediaType.ARTIST)
    discovery.provider.mass.cache.set.assert_not_awaited()
    assert discovery.state.retry_after >= 300


async def test_miss_budget_continues_with_later_candidates_on_next_refresh(
    discovery: LastFMDiscovery,
) -> None:
    """Cached complete misses let the next bounded run continue rather than repeat its first ten."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[])
    stored: dict[str, object] = {}

    def remember(*, key: str, data: object, **_kwargs: object) -> None:
        stored[key] = data

    def recalled(*, key: str, **_kwargs: object) -> object:
        return stored.get(key)

    discovery.provider.mass.cache.set.side_effect = remember
    discovery.provider.mass.cache.get.side_effect = recalled
    candidates = [LastFMItem(f"Artist {index}") for index in range(50)]
    assert not await discovery.resolve_items(candidates, MediaType.ARTIST)
    assert controller.search.await_count == RESOLUTION_MISS_LIMIT
    assert discovery.state.retry_after >= 300
    second = LastFMDiscovery(discovery.provider)
    assert not await second.resolve_items(candidates, MediaType.ARTIST)
    assert controller.search.await_count == 2 * RESOLUTION_MISS_LIMIT
    searched_names = [call.args[0] for call in controller.search.await_args_list]
    assert searched_names == [candidate.name for candidate in candidates[:20]]
    assert second.state.retry_after >= 300


def test_resolution_cache_identity_is_private_to_user_and_visible_sources(
    discovery: LastFMDiscovery,
) -> None:
    """Changing the selected user or their available sources invalidates old resolution keys."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    discovery.provider.mass.music.providers = [source]
    candidate = LastFMItem("Artist")
    original = discovery._resolution_cache_key(candidate, MediaType.ARTIST)
    discovery.provider.get_setup_value.return_value = "selected--2"
    assert discovery._resolution_cache_key(candidate, MediaType.ARTIST) != original
    discovery.provider.get_setup_value.return_value = "selected--1"
    discovery.provider.mass.music.providers = []
    assert discovery._resolution_cache_key(candidate, MediaType.ARTIST) != original


async def test_musicbrainz_match_uses_controller_without_name_search(
    discovery: LastFMDiscovery,
) -> None:
    """A verified MBID may resolve to a library item through the public controller API."""
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_item_by_external_id.return_value = artist
    controller.search = AsyncMock()
    candidate = LastFMItem("Artist", mbid="6db0ef84-7cdb-45d4-bb64-6b889ceda8d8")
    result = await discovery.resolve_items([candidate], MediaType.ARTIST)
    assert [item.uri for item in result] == [artist.uri]
    controller.get_item_by_external_id.assert_awaited_once_with(
        candidate.mbid, ExternalID.MB_ARTIST
    )
    controller.search.assert_not_awaited()


@pytest.mark.parametrize("lookup_failed", [False, True])
async def test_musicbrainz_miss_or_outage_falls_back_to_streaming_search(
    discovery: LastFMDiscovery, lookup_failed: bool
) -> None:
    """MusicBrainz is an identity route, never a requirement for finding a streaming result."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    controller = discovery.provider.mass.music.get_controller.return_value
    if lookup_failed:
        controller.get_item_by_external_id.side_effect = ResourceTemporarilyUnavailable("private")
    controller.search = AsyncMock(return_value=[artist])
    result = await discovery.resolve_items(
        [LastFMItem("Artist", mbid="6db0ef84-7cdb-45d4-bb64-6b889ceda8d8")], MediaType.ARTIST
    )
    assert [item.uri for item in result] == [artist.uri]
    controller.search.assert_awaited_once_with("Artist", "stream--1", limit=2)


async def test_musicbrainz_outage_and_streaming_miss_remain_retryable(
    discovery: LastFMDiscovery,
) -> None:
    """A failed MBID route cannot become a cached complete miss after an empty name search."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_item_by_external_id.side_effect = ResourceTemporarilyUnavailable("private")
    controller.search = AsyncMock(return_value=[])
    assert not await discovery.resolve_items(
        [LastFMItem("Artist", mbid="6db0ef84-7cdb-45d4-bb64-6b889ceda8d8")], MediaType.ARTIST
    )
    discovery.provider.mass.cache.set.assert_not_awaited()
    assert discovery.state.retry_after >= 300


async def test_musicbrainz_rate_limit_stops_further_external_resolution(
    discovery: LastFMDiscovery,
) -> None:
    """Defer all remaining identity and source lookups until the requested wait has elapsed."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_item_by_external_id.side_effect = RateLimited("private", backoff_time=900)
    controller.search = AsyncMock()
    candidates = [
        LastFMItem(f"Artist {index}", mbid=f"6db0ef84-7cdb-45d4-bb64-{index:012d}")
        for index in range(50)
    ]
    assert not await discovery.resolve_items(candidates, MediaType.ARTIST)
    controller.get_item_by_external_id.assert_awaited_once()
    controller.search.assert_not_awaited()
    discovery.provider.mass.cache.set.assert_not_awaited()
    assert discovery.state.retry_after == 900


@pytest.mark.parametrize("cached_count", [10, 50])
async def test_cached_target_is_resolved_before_uncached_candidates(
    discovery: LastFMDiscovery, cached_count: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Use the official cache-first strategy even when cached entries occur later in a list."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    discovery.provider.mass.music.providers = [source]
    candidates = [LastFMItem(f"Artist {index}") for index in range(50)]
    cached = {
        discovery._resolution_cache_key(candidate, MediaType.ARTIST): Artist(
            item_id=str(index),
            provider="stream--1",
            name=candidate.name,
            provider_mappings=set(),
        )
        for index, candidate in enumerate(candidates[-cached_count:])
    }

    def recalled(*, key: str, **_kwargs: object) -> Artist | None:
        return cached.get(key)

    discovery.provider.mass.cache.get.side_effect = recalled
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock()
    with caplog.at_level(logging.DEBUG, logger=discovery.provider.logger.name):
        result = await discovery.resolve_items(candidates, MediaType.ARTIST)
    assert [item.name for item in result] == [
        item.name for item in candidates[-cached_count:][:RESOLUTION_TARGET_LIMIT]
    ]
    assert "duplicates=0" in caplog.text
    controller.search.assert_not_awaited()
    controller.get_item_by_external_id.assert_not_awaited()


@pytest.mark.parametrize(
    ("folder_id", "country"), [("global_top_artists", ""), ("de_top_artists", "Germany")]
)
async def test_chart_preserves_order_and_adds_links_without_mutating_search_results(
    discovery: LastFMDiscovery, folder_id: str, country: str
) -> None:
    """Reuse search results, preserve charts and attach Last.fm links only to returned copies."""
    first = Artist(item_id="1", provider="stream--1", name="First", provider_mappings=set())
    second = Artist(item_id="2", provider="stream--1", name="Second", provider_mappings=set())
    discovery.provider.api.get_items.return_value = [
        LastFMItem("Second"),
        LastFMItem("Missing"),
        LastFMItem("First"),
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    controller.search = AsyncMock(return_value=[first, second])
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == folder_id)
    result = await discovery.build_items(folder)
    assert [item.name for item in result] == ["Second", "First"]
    assert all(any(link.type == LinkType.LASTFM for link in item.metadata.links) for item in result)
    assert not first.metadata.links
    assert not second.metadata.links
    controller.get_library_items_by_query.assert_not_awaited()
    discovery.provider.api.get_items.assert_awaited_once_with(
        folder.method, {"country": country} if country else {}, limit=50
    )


async def test_track_artist_mismatch_is_not_resolved(discovery: LastFMDiscovery) -> None:
    """A cover with the same title is not the chart entry."""
    cover = Track(
        item_id="1",
        provider="stream--1",
        name="Song",
        provider_mappings=set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="2", provider="stream--1", name="Cover artist", provider_mappings=set()
                )
            ]
        ),
    )
    discovery.provider.api.get_items.return_value = [LastFMItem("Song", "Original artist")]
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    discovery.provider.mass.music.get_controller.return_value.search = AsyncMock(
        return_value=[cover]
    )
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == "global_top_tracks")
    assert not await discovery.build_items(folder)


async def test_personal_seeds_are_bounded_and_progress_is_logged(
    discovery: LastFMDiscovery, caplog: pytest.LogCaptureFixture
) -> None:
    """Make at most three discovery requests and record seed progress without user identifiers."""
    seeds = [
        Artist(
            item_id=str(index), provider="library", name=f"Seed {index}", provider_mappings=set()
        )
        for index in range(3)
    ]
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=ItemMapping.from_item(seed), play_count=10) for seed in seeds
    ]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_artist_recommendations"
    )
    with caplog.at_level(logging.DEBUG):
        assert not await discovery.build_items(folder)
    assert discovery.provider.api.get_items.await_count == 3
    assert "seed=3/3" in caplog.text
    assert "phase=resolve" in caplog.text
    assert "discovery completed" in caplog.text


async def test_rediscovery_does_not_claim_complete_rotation_at_limit(
    discovery: LastFMDiscovery, caplog: pytest.LogCaptureFixture
) -> None:
    """An incomplete recent rotation cannot safely identify long-unheard tracks."""
    mapping = ItemMapping(
        item_id="1", provider="library", name="Recent", media_type=MediaType.TRACK
    )
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=mapping, play_count=1)
    ] * 500
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_rediscovery_tracks"
    )
    with caplog.at_level(logging.DEBUG):
        assert not await discovery.build_items(folder)
    assert "reason=recent_history_limit" in caplog.text
    assert discovery.provider.mass.statistics.get_top_items.await_count == 1
    discovery.provider.api.get_items.assert_not_awaited()


async def test_external_seed_id_is_not_treated_as_a_library_id(discovery: LastFMDiscovery) -> None:
    """An external history identity must not select an unrelated library track with the same id."""
    external = ItemMapping(
        item_id="1", provider="other-provider", name="Song", media_type=MediaType.TRACK
    )
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=external, play_count=10)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_recommendations"
    )
    assert not await discovery.build_items(folder)
    controller.get.assert_awaited_once_with("1", "other-provider", allow_update_metadata=False)
    discovery.provider.api.get_items.assert_not_awaited()


async def test_track_seed_uses_verified_artist_and_title(discovery: LastFMDiscovery) -> None:
    """Retrieve track seed metadata by its exact identity, never by a guessed artist name."""
    artist = Artist(item_id="2", provider="library", name="Artist", provider_mappings=set())
    track = Track(
        item_id="1",
        provider="library",
        name="Song",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=ItemMapping.from_item(track), play_count=10)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get.return_value = track
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_recommendations"
    )
    assert not await discovery.build_items(folder)
    discovery.provider.api.get_items.assert_awaited_once_with(
        "track.getSimilar", {"artist": "Artist", "track": "Song"}, limit=20
    )


async def test_streaming_track_seeds_do_not_require_library(discovery: LastFMDiscovery) -> None:
    """Fetch credits for only three ranked streaming seeds, preserving their original identities."""
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    tracks = [
        Track(
            item_id=str(index),
            provider="stream--1",
            name=f"Song {index}",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
        for index in range(10)
    ]
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=ItemMapping.from_item(track), play_count=10 - index)
        for index, track in enumerate(tracks)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get.side_effect = tracks[:3]
    seeds = await discovery.get_seeds(MediaType.TRACK, limit=10)
    assert [seed.track for seed in seeds] == [track.name for track in tracks[:3]]
    assert [seed.identity for seed in seeds] == [track.uri for track in tracks[:3]]
    assert controller.get.await_count == 3
    assert all(
        call.kwargs == {"allow_update_metadata": False} for call in controller.get.await_args_list
    )


@pytest.mark.parametrize("error_type", [RateLimited, ResourceTemporarilyUnavailable])
async def test_streaming_seed_source_failures_propagate(
    discovery: LastFMDiscovery, error_type: type[Exception]
) -> None:
    """Unavailable seed metadata must not silently turn into an empty listening profile."""
    mapping = ItemMapping(
        item_id="1", provider="stream--1", name="Song", media_type=MediaType.TRACK
    )
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=mapping, play_count=1)
    ]
    discovery.provider.mass.music.get_controller.return_value.get.side_effect = error_type("test")
    with pytest.raises(error_type):
        await discovery.get_seeds(MediaType.TRACK)


async def test_missing_third_track_seed_preserves_previous_candidates(
    discovery: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real Last.fm missing-track response must not discard another seed's valid matches."""
    monkeypatch.setattr(LastFMAPIClient, "throttler", ThrottlerManager(rate_limit=100))
    monkeypatch.setattr(
        discovery,
        "get_seeds",
        AsyncMock(
            return_value=[PersonalSeed("Artist", name) for name in ("First", "Second", "Missing")]
        ),
    )
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.status = 200
    response.headers = {}
    response.raise_for_status = Mock()
    response.json = AsyncMock(
        side_effect=[
            {"similartracks": {"track": []}},
            {"similartracks": {"track": [{"name": "Candidate", "artist": "Related"}]}},
            {"error": 6, "message": "Track not found"},
        ]
    )
    discovery.provider.api = LastFMAPIClient(session, "test-key", discovery.provider.logger)
    artist = Artist(item_id="a", provider="stream--1", name="Related", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="stream--1",
        name="Candidate",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    discovery.provider.mass.music.get_controller.return_value.search = AsyncMock(
        return_value=[track]
    )
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_recommendations"
    )
    with caplog.at_level(logging.DEBUG):
        assert [item.uri for item in await discovery.build_items(folder)] == [track.uri]
    assert session.get.call_count == 3
    assert "seed=3/3 received=0" in caplog.text
    assert "discovery completed" in caplog.text
    assert "test-key" not in caplog.text


async def test_rediscovery_excludes_current_rotation_without_lastfm_requests(
    discovery: LastFMDiscovery,
) -> None:
    """Keep the old ranking, omit recent identities and never query Last.fm for MA history."""
    recent = Track(item_id="1", provider="library", name="Recent", provider_mappings=set())
    previous = Track(item_id="2", provider="library", name="Previous", provider_mappings=set())
    recent_result = TopItemResult(item=ItemMapping.from_item(recent), play_count=20)
    old_result = TopItemResult(item=ItemMapping.from_item(previous), play_count=10)
    discovery.provider.mass.statistics.get_top_items.side_effect = [
        [recent_result],
        [recent_result, old_result],
    ]
    discovery.provider.mass.music.get_controller.return_value.get_library_items_by_query.return_value = [
        previous
    ]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_rediscovery_tracks"
    )
    assert [item.uri for item in await discovery.build_items(folder)] == [previous.uri]
    discovery.provider.api.get_items.assert_not_awaited()


async def test_rediscovery_retains_unmapped_streaming_history(discovery: LastFMDiscovery) -> None:
    """Old available streaming identities do not need a library record or new catalogue lookup."""
    mapping = ItemMapping(
        item_id="old", provider="stream--1", name="Old song", media_type=MediaType.TRACK
    )
    discovery.provider.mass.statistics.get_top_items.side_effect = [
        [],
        [TopItemResult(item=mapping, play_count=3)],
    ]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_rediscovery_tracks"
    )
    result = await discovery.build_items(folder)
    assert [item.uri for item in result] == [mapping.uri]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_library_items_by_query.assert_not_awaited()
    controller.get.assert_not_awaited()
    discovery.provider.api.get_items.assert_not_awaited()
