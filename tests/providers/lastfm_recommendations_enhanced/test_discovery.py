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
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.statistics import TopItemResult

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lastfm_recommendations_enhanced import (
    recommendations as recommendations_module,
)
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import (
    LastFMAPIClient,
    LastFMItem,
)
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import (
    DISCOVERY_FOLDERS,
    RESOLUTION_TARGET_LIMITS,
    LastFMDiscovery,
    PersonalSeed,
    ResolutionState,
    normalize_scores,
    similarity_weights,
)


@pytest.fixture
def discovery(monkeypatch: pytest.MonkeyPatch) -> LastFMDiscovery:
    """Keep real media models; mock only API and controller boundaries."""
    monkeypatch.setattr(Artist, "available", property(lambda _item: True))
    monkeypatch.setattr(Album, "available", property(lambda _item: True))
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


@pytest.mark.parametrize(
    ("normalize_matches", "expected"),
    [(False, [10.95, 5.475, 0, 0.25]), (True, [1, 0.5, 0, 0.25])],
)
def test_similarity_weights_preserve_zero_and_normalize_only_explicit_matches(
    normalize_matches: bool, expected: list[float]
) -> None:
    """Unknown scores use rank fallback; actual zero scores do not become rank contributions."""
    items = [
        LastFMItem("First", similarity_weight=10.95),
        LastFMItem("Second", similarity_weight=5.475),
        LastFMItem("Zero", similarity_weight=0),
        LastFMItem("Unknown"),
    ]
    assert similarity_weights(items, normalize_matches=normalize_matches) == pytest.approx(expected)
    assert similarity_weights([], normalize_matches=normalize_matches) == []
    assert similarity_weights(
        [LastFMItem("Zero", similarity_weight=0)], normalize_matches=normalize_matches
    ) == [0]


@pytest.mark.parametrize("folder_id", ["user_recommendations", "user_artist_recommendations"])
async def test_similar_recommendations_aggregate_match_scores_and_personal_seed_weights(
    discovery: LastFMDiscovery, folder_id: str
) -> None:
    """Match scores outrank list positions, and per-track response scales cannot dominate seeds."""
    is_track = folder_id == "user_recommendations"
    credit = "Related" if is_track else ""
    discovery.get_seeds = AsyncMock(
        return_value=[
            PersonalSeed("First", "Seed" if is_track else "", weight=1),
            PersonalSeed("Second", "Seed" if is_track else "", weight=0.2),
        ]
    )
    first_scale = 100 if is_track else 1
    second_scale = 10 if is_track else 1
    discovery.provider.api.get_items.side_effect = [
        [
            LastFMItem("Rank first", credit, similarity_weight=0.1 * first_scale),
            LastFMItem("Shared", credit, similarity_weight=0.9 * first_scale),
            LastFMItem("Zero", credit, similarity_weight=0),
            LastFMItem("Missing", credit),
        ],
        [
            LastFMItem("Shared", credit, similarity_weight=0.5 * second_scale),
            LastFMItem("Other", credit, similarity_weight=second_scale),
        ],
    ]
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == folder_id)
    candidates = await discovery._candidates(folder)
    assert [candidate.name for candidate in candidates] == [
        "Shared",
        "Missing",
        "Other",
        "Rank first",
        "Zero",
    ]
    assert discovery.provider.api.get_items.await_count == 2
    assert all(
        call.kwargs == {"limit": 50} for call in discovery.provider.api.get_items.await_args_list
    )


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


async def test_resolution_replaces_previously_accepted_tracks_without_consuming_target(
    discovery: LastFMDiscovery,
) -> None:
    """A duplicate from an earlier artist leaves room for the next ranked candidate."""
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    tracks = [
        Track(
            item_id=str(index),
            provider="library",
            name=f"Song {index}",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
        for index in range(3)
    ]
    discovery._get_cached_resolution = AsyncMock(side_effect=[(track, False) for track in tracks])
    excluded_uri = tracks[0].uri
    assert excluded_uri is not None
    excluded = {excluded_uri}
    result = await discovery.resolve_items(
        [LastFMItem(track.name, artist.name) for track in tracks],
        MediaType.TRACK,
        max_items=1,
        excluded_uris=excluded,
    )
    assert [track.uri for track in result] == [tracks[1].uri]
    assert discovery._get_cached_resolution.await_count == 2
    assert excluded == {excluded_uri}


async def test_resolution_exhausts_bounded_candidates_when_all_items_are_missing(
    discovery: LastFMDiscovery,
) -> None:
    """An empty result requires trying the complete bounded candidate list without paging."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH, ProviderFeature.LIBRARY_ARTISTS}
    discovery.provider.mass.music.providers = [source]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[])
    assert not await discovery.resolve_items(
        [LastFMItem(f"Artist {index}") for index in range(50)], MediaType.ARTIST
    )
    assert controller.search.await_count == 50


@pytest.mark.parametrize(
    ("media_type", "target_limit"),
    [(MediaType.TRACK, 50), (MediaType.ALBUM, 35), (MediaType.ARTIST, 25)],
)
async def test_successful_streaming_resolution_uses_media_type_limit(
    discovery: LastFMDiscovery,
    media_type: MediaType,
    target_limit: int,
) -> None:
    """Fill each media target without stopping at the former ten-item boundary."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    items: list[Artist | Album | Track] = []
    for index in range(50):
        if media_type == MediaType.ARTIST:
            items.append(
                Artist(
                    item_id=str(index),
                    provider="stream--1",
                    name=f"Song {index}",
                    provider_mappings=set(),
                )
            )
        elif media_type == MediaType.ALBUM:
            items.append(
                Album(
                    item_id=str(index),
                    provider="stream--1",
                    name=f"Song {index}",
                    provider_mappings=set(),
                    artists=UniqueList([artist]),
                )
            )
        else:
            items.append(
                Track(
                    item_id=str(index),
                    provider="stream--1",
                    name=f"Song {index}",
                    provider_mappings=set(),
                    artists=UniqueList([artist]),
                )
            )
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(side_effect=[[item] for item in items])
    result = await discovery.resolve_items(
        [LastFMItem(item.name, artist.name) for item in items], media_type
    )
    assert RESOLUTION_TARGET_LIMITS[media_type] == target_limit
    assert [item.uri for item in result] == [item.uri for item in items[:target_limit]]
    assert controller.search.await_count == target_limit
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


@pytest.mark.parametrize("log_level", [logging.DEBUG, VERBOSE_LOG_LEVEL])
async def test_individual_source_search_details_are_only_logged_at_trace(
    discovery: LastFMDiscovery, caplog: pytest.LogCaptureFixture, log_level: int
) -> None:
    """Per-search result details stay below Debug while resolution summaries remain visible."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="stream--1",
        name="Song",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(return_value=[track])

    with caplog.at_level(log_level, logger=discovery.provider.logger.name):
        result = await discovery.resolve_items([LastFMItem("Song", "Artist")], MediaType.TRACK)

    assert [item.uri for item in result] == [track.uri]
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Last.fm source search ")
    ]
    if log_level == logging.DEBUG:
        assert records == []
    else:
        assert all(record.levelno == VERBOSE_LOG_LEVEL for record in records)
        assert [record.getMessage() for record in records] == [
            "Last.fm source search started media_type=track source=stream--1 query=Artist - Song",
            "Last.fm source search completed media_type=track source=stream--1 "
            "query=Artist - Song results=1 reusable=1",
        ]
    assert any(
        record.levelno == logging.DEBUG
        and record.getMessage().startswith("Last.fm resolution candidates=")
        for record in caplog.records
    )


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


@pytest.mark.parametrize("play_scale", [1, 100])
async def test_personal_seeds_combine_month_week_and_favorites(
    discovery: LastFMDiscovery, play_scale: int
) -> None:
    """Recent listening and favorites can change the ranking without rewriting play counts."""
    first = Artist(item_id="a", provider="library", name="First", provider_mappings=set())
    second = Artist(item_id="b", provider="library", name="Second", provider_mappings=set())
    discovery.provider.mass.statistics.get_top_items.side_effect = [
        [
            TopItemResult(item=ItemMapping.from_item(first), play_count=10 * play_scale),
            TopItemResult(item=ItemMapping.from_item(second), play_count=11 * play_scale),
        ],
        [TopItemResult(item=ItemMapping.from_item(first), play_count=2 * play_scale)],
        [],
    ]
    discovery.provider.mass.music.get_controller.return_value.get_library_items_by_query.return_value = [
        second
    ]
    seeds = await discovery.get_seeds(MediaType.ARTIST)
    assert [seed.artist for seed in seeds] == ["First", "Second"]
    assert [seed.weight for seed in seeds] == pytest.approx([1, 11 / 14 + 0.2])
    assert seeds[0].identity == first.uri
    assert [
        call.kwargs["period"]
        for call in discovery.provider.mass.statistics.get_top_items.await_args_list
    ] == ["month", "week", "all_time"]


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ({}, {}),
        ({"first": 0, "second": 0}, {"first": 0, "second": 0}),
        ({"first": 20, "second": 5}, {"first": 1, "second": 0.25}),
    ],
)
def test_score_normalization_handles_empty_zero_and_positive_pools(
    scores: dict[str, float], expected: dict[str, float]
) -> None:
    """Normalization preserves relative strength without a division by zero."""
    assert normalize_scores(scores) == pytest.approx(expected)


@pytest.mark.parametrize(("requested", "expected"), [(3, 3), (10, 10), (15, 15), (100, 15)])
async def test_artist_seed_limit_retains_unmapped_streaming_history(
    discovery: LastFMDiscovery,
    requested: int,
    expected: int,
) -> None:
    """Genre profiles can use fifteen streaming artists without resolving local artist metadata."""
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


@pytest.mark.parametrize("matching_source_index", [0, 1, 2])
async def test_track_resolution_searches_all_music_sources_until_first_match(
    discovery: LastFMDiscovery, matching_source_index: int
) -> None:
    """Search local and streaming sources in order, skipping unavailable or unsupported sources."""
    sources = [
        Mock(
            instance_id=instance_id,
            available=True,
            is_streaming_provider=is_streaming,
            supported_features={ProviderFeature.SEARCH},
        )
        for instance_id, is_streaming in [
            ("stream--1", True),
            ("local--1", False),
            ("stream--2", True),
        ]
    ]
    discovery.provider.mass.music.providers = [
        Mock(
            instance_id="unavailable--1",
            available=False,
            is_streaming_provider=False,
            supported_features={ProviderFeature.SEARCH},
        ),
        Mock(
            instance_id="unsupported--1",
            available=True,
            is_streaming_provider=False,
            supported_features=set(),
        ),
        *sources,
    ]
    matched_source = sources[matching_source_index].instance_id
    artist = Artist(
        item_id="artist", provider=matched_source, name="Artist", provider_mappings=set()
    )
    track = Track(
        item_id="track",
        provider=matched_source,
        name="Song",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(side_effect=[[]] * matching_source_index + [[track]])
    result = await discovery.resolve_items([LastFMItem("Song", "Artist")], MediaType.TRACK)
    assert [item.uri for item in result] == [track.uri]
    assert [call.args for call in controller.search.await_args_list] == [
        ("Artist - Song", source.instance_id) for source in sources[: matching_source_index + 1]
    ]
    assert all(call.kwargs == {"limit": 2} for call in controller.search.await_args_list)


@pytest.mark.parametrize("is_library_item", [False, True])
async def test_local_music_source_resolution_cache_avoids_repeated_searches(
    discovery: LastFMDiscovery, is_library_item: bool
) -> None:
    """Reuse visible local tracks and library tracks mapped to a visible local source."""
    discovery.provider.mass.music.providers = [
        Mock(
            instance_id="local--1",
            available=True,
            is_streaming_provider=False,
            supported_features={ProviderFeature.SEARCH},
        )
    ]
    track = Track(
        item_id="track",
        provider="library" if is_library_item else "local--1",
        name="Song",
        artists=UniqueList(
            [Artist(item_id="artist", provider="local--1", name="Artist", provider_mappings=set())]
        ),
        provider_mappings={
            ProviderMapping(
                item_id="source-track",
                provider_domain="local",
                provider_instance="local--1",
            )
        },
    )
    discovery.provider.mass.cache.get.return_value = track
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock()
    result = await discovery.resolve_items([LastFMItem("Song", "Artist")], MediaType.TRACK)
    assert [item.uri for item in result] == [track.uri]
    controller.search.assert_not_awaited()
    discovery.provider.mass.cache.set.assert_not_awaited()


def test_resolution_cache_identity_includes_local_music_sources(
    discovery: LastFMDiscovery,
) -> None:
    """Adding a visible local source invalidates misses recorded for streaming sources alone."""
    discovery.provider.mass.music.providers = [
        Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    ]
    candidate = LastFMItem("Song", "Artist")
    streaming_key = discovery._resolution_cache_key(candidate, MediaType.TRACK)
    discovery.provider.mass.music.providers.append(
        Mock(instance_id="local--1", available=True, is_streaming_provider=False)
    )
    local_key = discovery._resolution_cache_key(candidate, MediaType.TRACK)
    assert local_key != streaming_key
    discovery.provider.mass.music.providers[-1].available = False
    assert discovery._resolution_cache_key(candidate, MediaType.TRACK) != local_key


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


async def test_cached_misses_are_not_searched_again_on_next_refresh(
    discovery: LastFMDiscovery,
) -> None:
    """Complete misses are cached, so a later refresh never repeats unsuccessful source searches."""
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
    assert controller.search.await_count == 50
    assert discovery.state.retry_after == 0
    second = LastFMDiscovery(discovery.provider)
    assert not await second.resolve_items(candidates, MediaType.ARTIST)
    assert controller.search.await_count == 50
    searched_names = [call.args[0] for call in controller.search.await_args_list]
    assert searched_names == [candidate.name for candidate in candidates]
    assert second.state.retry_after == 0


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


@pytest.mark.parametrize("cached_count", [25, 50])
async def test_higher_ranked_uncached_items_precede_lower_ranked_cached_items(
    discovery: LastFMDiscovery, cached_count: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A cached lower-ranked result never displaces an available higher-ranked candidate."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
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
    controller.search = AsyncMock(
        side_effect=[
            [
                Artist(
                    item_id=str(index),
                    provider="stream--1",
                    name=candidate.name,
                    provider_mappings=set(),
                )
            ]
            for index, candidate in enumerate(candidates[:25])
        ]
    )
    with caplog.at_level(logging.DEBUG, logger=discovery.provider.logger.name):
        result = await discovery.resolve_items(candidates, MediaType.ARTIST)
    assert [item.name for item in result] == [
        item.name for item in candidates[: RESOLUTION_TARGET_LIMITS[MediaType.ARTIST]]
    ]
    assert "duplicates=0" in caplog.text
    assert controller.search.await_count == (25 if cached_count == 25 else 0)
    controller.get_item_by_external_id.assert_not_awaited()


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST])
async def test_resolution_fills_in_rank_order_after_many_misses_and_stops_at_target(
    discovery: LastFMDiscovery, media_type: MediaType
) -> None:
    """Regular misses admit successors, without pre-resolving candidates past the final target."""
    target = RESOLUTION_TARGET_LIMITS[media_type]
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    model = {MediaType.TRACK: Track, MediaType.ALBUM: Album, MediaType.ARTIST: Artist}[media_type]
    items = [
        model(
            item_id=str(index),
            provider="library",
            name=f"Item {index}",
            provider_mappings=set(),
            **({"artists": UniqueList([artist])} if media_type != MediaType.ARTIST else {}),
        )
        for index in range(target + 30)
    ]
    candidates = [
        LastFMItem(item.name, "Artist", mbid="mbid" if index % 2 else "")
        for index, item in enumerate(items)
    ]
    discovery._get_cached_resolution = AsyncMock(return_value=(None, False))
    discovery._resolve_missing = AsyncMock(side_effect=[None] * 12 + items[12:])
    result = await discovery.resolve_items(candidates, media_type)
    assert [item.uri for item in result] == [item.uri for item in items[12 : 12 + target]]
    assert discovery._get_cached_resolution.await_count == target + 12
    assert discovery._resolve_missing.await_count == target + 12
    assert [call.args[0] for call in discovery._resolve_missing.await_args_list] == candidates[
        : 12 + target
    ]


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST])
async def test_resolution_keeps_recent_non_seed_candidates_without_querying_history(
    discovery: LastFMDiscovery,
    media_type: MediaType,
) -> None:
    """Recent non-seed media remain eligible without a separate monthly-history lookup."""
    candidates = [LastFMItem(f"Artist {index}") for index in range(50)]
    model = {MediaType.TRACK: Track, MediaType.ALBUM: Album, MediaType.ARTIST: Artist}[media_type]
    items = [
        model(
            item_id=str(index), provider="stream--1", name=candidate.name, provider_mappings=set()
        )
        for index, candidate in enumerate(candidates)
    ]
    discovery._get_cached_resolution = AsyncMock(side_effect=[(item, False) for item in items])
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=item, play_count=1) for item in items[:25]
    ]
    result = await discovery.resolve_items(candidates, media_type)
    assert [item.uri for item in result] == [
        item.uri for item in items[: RESOLUTION_TARGET_LIMITS[media_type]]
    ]
    discovery.provider.mass.statistics.get_top_items.assert_not_awaited()


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ARTIST])
@pytest.mark.parametrize("candidate_count", [15, 80])
async def test_seed_quota_uses_result_maximum_and_fills_from_remaining_candidates(
    discovery: LastFMDiscovery, media_type: MediaType, candidate_count: int
) -> None:
    """Seed matches use a fixed maximum-based quota, including cross-source matches."""
    items: list[Artist | Track] = []
    for index in range(candidate_count):
        artist = Artist(
            item_id=str(index),
            provider="stream--1",
            name=f"Artist {index}",
            provider_mappings=set(),
        )
        items.append(
            Track(
                item_id=str(index),
                provider="stream--1",
                name=f"Song {index}",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
            if media_type == MediaType.TRACK
            else artist
        )
    seeds = [
        PersonalSeed(
            artist=f"ARTIST {index}",
            track=f"SONG {index}" if media_type == MediaType.TRACK else "",
            identity=items[index].uri if index % 2 else "",
        )
        for index in range(15)
    ]
    candidates = [
        LastFMItem(item.name, f"Artist {index}" if media_type == MediaType.TRACK else "")
        for index, item in enumerate(items)
    ]
    discovery._get_cached_resolution = AsyncMock(side_effect=[(item, False) for item in items])
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=item, play_count=1) for item in items[:15]
    ]
    result = await discovery.resolve_items(candidates, media_type, seeds=seeds)
    target = RESOLUTION_TARGET_LIMITS[media_type]
    quota = target // 5
    assert [item.uri for item in result] == [
        item.uri for item in [*items[:quota], *items[15 : 15 + target - quota]]
    ]


@pytest.mark.parametrize(
    ("folder_id", "seed_count", "matches_per_seed", "expected_seed_count"),
    [("user_recommendations", 15, 4, 10), ("user_artist_recommendations", 10, 4, 5)],
)
async def test_personal_recommendations_apply_seed_quota_after_ranking_and_resolution(
    discovery: LastFMDiscovery,
    folder_id: str,
    seed_count: int,
    matches_per_seed: int,
    expected_seed_count: int,
) -> None:
    """The real folder builder admits seed returns within the quota and fills the media target."""
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == folder_id)
    seeds = [
        PersonalSeed(
            f"Seed artist {index}",
            f"Seed track {index}" if folder.media_type == MediaType.TRACK else "",
        )
        for index in range(seed_count)
    ]
    discovery.get_seeds = AsyncMock(return_value=seeds)
    batches = [
        [LastFMItem(seed.track or seed.artist, seed.artist if seed.track else "")]
        + [
            LastFMItem(f"Other {index}-{rank}", f"Other artist {index}")
            for rank in range(matches_per_seed - 1)
        ]
        for index, seed in enumerate(seeds)
    ]
    discovery.provider.api.get_items.side_effect = batches
    items: dict[str, Artist | Track] = {}
    for batch in batches:
        for candidate in batch:
            artist = Artist(
                item_id=candidate.name,
                provider="stream--1",
                name=candidate.artist or candidate.name,
                provider_mappings=set(),
            )
            items[candidate.name] = (
                Track(
                    item_id=candidate.name,
                    provider="stream--1",
                    name=candidate.name,
                    provider_mappings=set(),
                    artists=UniqueList([artist]),
                )
                if folder.media_type == MediaType.TRACK
                else artist
            )
    discovery._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (items[candidate.name], False)
    )
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=items[seed.track or seed.artist], play_count=1) for seed in seeds
    ]
    result = await discovery.build_items(folder)
    assert len(result) == RESOLUTION_TARGET_LIMITS[folder.media_type]
    assert sum(item.name.startswith("Seed") for item in result) == expected_seed_count
    discovery.get_seeds.assert_awaited_once_with(folder.seed_type, limit=seed_count)
    assert discovery.provider.api.get_items.await_count == seed_count


def test_seed_matching_does_not_confuse_releases_or_same_titles_by_other_artists() -> None:
    """Artist seeds are not their albums or tracks, and a title alone is not a track identity."""
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="library",
        name="Title",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    album = Album(
        item_id="b",
        provider="library",
        name="Artist",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    artist_seeds = [PersonalSeed("Artist", identity=artist.uri)]
    assert not LastFMDiscovery._is_seed(track, artist_seeds)
    assert not LastFMDiscovery._is_seed(album, artist_seeds)
    assert not LastFMDiscovery._is_seed(track, [PersonalSeed("Other Artist", "Title")])
    assert not LastFMDiscovery._is_seed(artist, [PersonalSeed("Artist", "Title")])


async def test_artist_diversity_fills_from_remaining_candidates(discovery: LastFMDiscovery) -> None:
    """Artist-capped tracks do not prevent later playable tracks from filling the playlist."""
    source = Mock(instance_id="stream--1", available=True, is_streaming_provider=True)
    source.supported_features = {ProviderFeature.SEARCH}
    discovery.provider.mass.music.providers = [source]
    items = [
        Track(
            item_id=str(index),
            provider="stream--1",
            name=f"Song {index}",
            provider_mappings=set(),
            artists=UniqueList(
                [
                    Artist(
                        item_id=str(index),
                        provider="stream--1",
                        name="Repeated" if index < 10 else f"Artist {index}",
                        provider_mappings=set(),
                    )
                ]
            ),
        )
        for index in range(60)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.search = AsyncMock(side_effect=[[item] for item in items])
    result = await discovery.resolve_items(
        [LastFMItem(item.name, item.artists[0].name) for item in items],
        MediaType.TRACK,
        max_tracks_per_artist=5,
    )
    assert [item.uri for item in result] == [item.uri for item in [*items[:5], *items[10:55]]]
    assert controller.search.await_count == 55


async def test_fixed_ten_track_artist_cap_fills_fifty_tracks_without_history_filter(
    discovery: LastFMDiscovery,
) -> None:
    """The artist cap admits later tracks while recently played tracks remain eligible."""
    items = [
        Track(
            item_id=str(index),
            provider="stream--1",
            name=f"Song {index}",
            provider_mappings=set(),
            artists=UniqueList(
                [
                    Artist(
                        item_id=str(index // 20),
                        provider="stream--1",
                        name=f"Artist {index // 20}",
                        provider_mappings=set(),
                    )
                ]
            ),
        )
        for index in range(100)
    ]
    candidates = [
        LastFMItem(item.name, f"Last.fm Artist {index // 20}") for index, item in enumerate(items)
    ]
    discovery._get_cached_resolution = AsyncMock(side_effect=[(item, False) for item in items])
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=items[0], play_count=1)
    ]
    result = await discovery.resolve_items(
        candidates,
        MediaType.TRACK,
        max_tracks_per_artist=10,
    )
    assert len(result) == 50
    assert result[0].uri == items[0].uri
    discovery.provider.mass.statistics.get_top_items.assert_not_awaited()
    for index in range(5):
        assert sum(item.artists[0].name == f"Artist {index}" for item in result) == 10


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


@pytest.mark.parametrize(
    ("folder_id", "pool_size"),
    [
        ("user_recommendations", 150),
        ("user_artist_recommendations", 75),
        ("global_top_tracks", 200),
    ],
)
async def test_generation_randomizes_only_bounded_personal_candidate_pools(
    discovery: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
    folder_id: str,
    pool_size: int,
) -> None:
    """Randomize personal selections during generation without mutating charts or API batches."""
    shuffle_candidates = Mock(side_effect=lambda items: items.reverse())
    monkeypatch.setattr(recommendations_module, "shuffle", shuffle_candidates)
    candidates = [LastFMItem(f"Candidate {rank}", "Artist") for rank in range(200)]
    discovery.get_seeds = AsyncMock(return_value=[])
    discovery._candidates = AsyncMock(return_value=candidates)
    discovery.resolve_items = AsyncMock(return_value=[])
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == folder_id)
    await discovery.build_items(folder)
    actual = discovery.resolve_items.await_args.args[0]
    expected = candidates[:pool_size]
    if folder.seed_type is not None:
        expected.reverse()
        shuffle_candidates.assert_called_once()
    else:
        shuffle_candidates.assert_not_called()
    assert actual == expected
    assert candidates[0].name == "Candidate 0"


async def test_personal_seeds_are_bounded_and_progress_is_logged(
    discovery: LastFMDiscovery, caplog: pytest.LogCaptureFixture
) -> None:
    """Make at most ten discovery requests and record seed progress without user identifiers."""
    seeds = [
        Artist(
            item_id=str(index), provider="library", name=f"Seed {index}", provider_mappings=set()
        )
        for index in range(15)
    ]
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=ItemMapping.from_item(seed), play_count=10) for seed in seeds
    ]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_artist_recommendations"
    )
    with caplog.at_level(logging.DEBUG):
        assert not await discovery.build_items(folder)
    assert discovery.provider.api.get_items.await_count == 10
    assert all(
        call.kwargs["limit"] == 50 for call in discovery.provider.api.get_items.await_args_list
    )
    assert "seed=10/10" in caplog.text
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
        "track.getSimilar", {"artist": "Artist", "track": "Song"}, limit=50
    )


async def test_streaming_track_seeds_do_not_require_library(discovery: LastFMDiscovery) -> None:
    """Fetch credits for up to fifteen ranked streaming seeds, preserving their identities."""
    artist = Artist(item_id="a", provider="stream--1", name="Artist", provider_mappings=set())
    tracks = [
        Track(
            item_id=str(index),
            provider="stream--1",
            name=f"Song {index}",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
        for index in range(20)
    ]
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=ItemMapping.from_item(track), play_count=20 - index)
        for index, track in enumerate(tracks)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get.side_effect = tracks[:15]
    seeds = await discovery.get_seeds(MediaType.TRACK, limit=15)
    assert [seed.track for seed in seeds] == [track.name for track in tracks[:15]]
    assert [seed.identity for seed in seeds] == [track.uri for track in tracks[:15]]
    assert controller.get.await_count == 15
    assert all(
        call.kwargs == {"allow_update_metadata": False} for call in controller.get.await_args_list
    )


async def test_track_seeds_include_favorites_in_fifteen_seed_selection(
    discovery: LastFMDiscovery,
) -> None:
    """Favorites without listening history contribute to the expanded weighted selection."""
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    tracks = [
        Track(
            item_id=str(index),
            provider="library",
            name=f"Song {index}",
            artists=UniqueList([artist]),
            provider_mappings=set(),
        )
        for index in range(20)
    ]
    discovery.provider.mass.statistics.get_top_items.side_effect = [
        [
            TopItemResult(item=track, play_count=20 - index)
            for index, track in enumerate(tracks[:12])
        ],
        [],
        [],
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_library_items_by_query.return_value = [tracks[0], *tracks[12:]]
    seeds = await discovery.get_seeds(MediaType.TRACK, limit=100)
    assert [seed.identity for seed in seeds] == [track.uri for track in tracks[:15]]
    assert seeds[0].weight == pytest.approx(1.2)
    assert [seed.weight for seed in seeds[12:]] == pytest.approx([0.2, 0.2, 0.2])
    controller.get_library_items_by_query.assert_awaited_once_with(favorite=True, limit=50)
    controller.get.assert_not_awaited()


@pytest.mark.parametrize("folder_id", ["user_recommendations", "user_artist_recommendations"])
async def test_similar_recommendations_preserve_normalized_seed_strength(
    discovery: LastFMDiscovery, monkeypatch: pytest.MonkeyPatch, folder_id: str
) -> None:
    """Strong seeds outweigh weak seeds, and shared matches combine contributions from both."""
    seed_track = "Seed" if folder_id == "user_recommendations" else ""
    artist_credit = "Related" if seed_track else ""
    monkeypatch.setattr(
        discovery,
        "get_seeds",
        AsyncMock(
            return_value=[
                PersonalSeed("Weak", seed_track, 0.2),
                PersonalSeed("Strong", seed_track, 1),
            ]
        ),
    )
    discovery.provider.api.get_items.side_effect = [
        [LastFMItem("Weak match", artist_credit), LastFMItem("Shared", artist_credit)],
        [LastFMItem("Strong match", artist_credit), LastFMItem("Shared", artist_credit)],
    ]
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == folder_id)
    candidates = await discovery._candidates(folder)
    assert [item.name for item in candidates] == ["Strong match", "Shared", "Weak match"]


async def test_track_recommendations_fetch_fifty_similar_items_for_fifteen_seeds(
    discovery: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the bounded, deduplicated seed pool so final exclusions can be filled afterwards."""
    seeds = [PersonalSeed("Artist", f"Seed {index}") for index in range(15)]
    get_seeds = AsyncMock(return_value=seeds)
    monkeypatch.setattr(discovery, "get_seeds", get_seeds)
    discovery.provider.api.get_items.side_effect = [
        [LastFMItem("Shared", "Related"), LastFMItem(seed.track, seed.artist)]
        + [LastFMItem(f"Candidate {index}-{rank}", "Related") for rank in range(48)]
        for index, seed in enumerate(seeds)
    ]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_recommendations"
    )
    candidates = await discovery._candidates(folder)
    get_seeds.assert_awaited_once_with(MediaType.TRACK, limit=15)
    calls = discovery.provider.api.get_items.await_args_list
    assert len(calls) == 15
    assert all(
        call.args == ("track.getSimilar", {"artist": seed.artist, "track": seed.track})
        and call.kwargs == {"limit": 50}
        for call, seed in zip(calls, seeds, strict=True)
    )
    assert len(candidates) == 736
    assert candidates[0].name == "Shared"
    assert sum(candidate.name == "Shared" for candidate in candidates) == 1
    assert {seed.track for seed in seeds} <= {candidate.name for candidate in candidates}


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


async def test_rediscovery_randomizes_after_exclusions_within_the_best_150(
    discovery: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refreshes may choose different historical tracks without expanding history or API reads."""
    shuffle_candidates = Mock()
    shuffle_candidates.side_effect = lambda items: (
        items.reverse() if shuffle_candidates.call_count == 1 else None
    )
    monkeypatch.setattr(recommendations_module, "shuffle", shuffle_candidates)
    mappings = [
        ItemMapping(
            item_id=str(rank), provider="library", name=str(rank), media_type=MediaType.TRACK
        )
        for rank in range(200)
    ]
    previous = [
        TopItemResult(item=item, play_count=200 - rank) for rank, item in enumerate(mappings)
    ]
    recent = previous[:5]
    discovery.provider.mass.statistics.get_top_items.side_effect = [
        recent,
        previous,
        recent,
        previous,
    ]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_rediscovery_tracks"
    )
    first = await discovery.build_items(folder)
    second = await discovery.build_items(folder)
    assert [item.uri for item in first] == [item.uri for item in mappings[105:155][::-1]]
    assert [item.uri for item in second] == [item.uri for item in mappings[5:55]]
    assert all(len(call.args[0]) == 150 for call in shuffle_candidates.call_args_list)
    assert discovery.provider.mass.statistics.get_top_items.await_count == 4
    discovery.provider.api.get_items.assert_not_awaited()
    assert mappings[0].name == "0"


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
