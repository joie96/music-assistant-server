"""Discovery builders use bounded sources, batched resolution and source attribution."""

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ExternalID, LinkType, MediaType, ProviderFeature
from music_assistant_models.errors import RateLimited, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import Artist, ItemMapping, Track, UniqueList
from music_assistant_models.statistics import TopItemResult

from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMItem
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import (
    DISCOVERY_FOLDERS,
    SOURCE_SEARCH_BUDGET,
    LastFMDiscovery,
)


@pytest.fixture
def discovery(monkeypatch: pytest.MonkeyPatch) -> LastFMDiscovery:
    """Keep real media models; mock only API and controller boundaries."""
    monkeypatch.setattr(Artist, "available", property(lambda _item: True))
    monkeypatch.setattr(Track, "available", property(lambda _item: True))
    provider = Mock()
    provider.logger = logging.getLogger("lastfm_discovery_test")
    provider.api.get_items = AsyncMock(return_value=[])
    provider.mass.statistics.get_top_items = AsyncMock(return_value=[])
    provider.mass.music.providers = []
    controller = provider.mass.music.get_controller.return_value
    controller.db_table = "artists"
    controller.get_library_items_by_query = AsyncMock(return_value=[])
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


async def test_source_search_budget_does_not_grow_with_candidate_count(
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
    assert controller.search.await_count == SOURCE_SEARCH_BUDGET


async def test_mbid_library_hit_spares_streaming_search(discovery: LastFMDiscovery) -> None:
    """The official strategy prefers a library identity before asking a music service."""
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_library_item_by_external_ids = AsyncMock(return_value=artist)
    controller.search = AsyncMock()
    mbid = "5a005d57-2835-4c36-bc95-1f58ad2d3304"
    result = await discovery.resolve_items([LastFMItem("Artist", mbid=mbid)], MediaType.ARTIST)
    assert result[0].uri == artist.uri
    controller.get_library_item_by_external_ids.assert_awaited_once_with(
        {(ExternalID.MB_ARTIST, mbid)}
    )
    controller.search.assert_not_awaited()


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
    with pytest.raises(error_type):
        await discovery.resolve_items([LastFMItem("Artist")], MediaType.ARTIST)


@pytest.mark.parametrize(
    ("folder_id", "country"), [("global_top_artists", ""), ("de_top_artists", "Germany")]
)
async def test_chart_preserves_order_and_adds_links_without_mutating_library(
    discovery: LastFMDiscovery, folder_id: str, country: str
) -> None:
    """Do one batch query, preserve charts and attach Last.fm links only to returned copies."""
    first = Artist(item_id="1", provider="library", name="First", provider_mappings=set())
    second = Artist(item_id="2", provider="library", name="Second", provider_mappings=set())
    discovery.provider.api.get_items.return_value = [
        LastFMItem("Second"),
        LastFMItem("Missing"),
        LastFMItem("First"),
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_library_items_by_query.return_value = [first, second]
    folder = next(folder for folder in DISCOVERY_FOLDERS if folder.item_id == folder_id)
    result = await discovery.build_items(folder)
    assert [item.name for item in result] == ["Second", "First"]
    assert all(any(link.type == LinkType.LASTFM for link in item.metadata.links) for item in result)
    assert not first.metadata.links
    assert not second.metadata.links
    assert controller.get_library_items_by_query.await_count == 1
    discovery.provider.api.get_items.assert_awaited_once_with(
        folder.method, {"country": country} if country else {}, limit=50
    )


async def test_track_artist_mismatch_is_not_resolved(discovery: LastFMDiscovery) -> None:
    """A cover with the same title is not the chart entry."""
    cover = Track(
        item_id="1",
        provider="library",
        name="Song",
        provider_mappings=set(),
        artists=UniqueList(
            [Artist(item_id="2", provider="library", name="Cover artist", provider_mappings=set())]
        ),
    )
    discovery.provider.api.get_items.return_value = [LastFMItem("Song", "Original artist")]
    discovery.provider.mass.music.get_controller.return_value.get_library_items_by_query.return_value = [
        cover
    ]
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
    artist = Artist(item_id="2", provider="library", name="Artist", provider_mappings=set())
    track = Track(
        item_id="1",
        provider="library",
        name="Song",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    external = ItemMapping(
        item_id="1", provider="other-provider", name="Song", media_type=MediaType.TRACK
    )
    discovery.provider.mass.statistics.get_top_items.return_value = [
        TopItemResult(item=external, play_count=10)
    ]
    controller = discovery.provider.mass.music.get_controller.return_value
    controller.get_library_items_by_query.side_effect = [[track], []]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_recommendations"
    )
    assert not await discovery.build_items(folder)
    discovery.provider.api.get_items.assert_not_awaited()


async def test_track_seed_uses_verified_artist_and_title(discovery: LastFMDiscovery) -> None:
    """Retrieve track seed metadata in a batch rather than guessing artist names from titles."""
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
    controller.get_library_items_by_query.side_effect = [[track], []]
    folder = next(
        folder for folder in DISCOVERY_FOLDERS if folder.item_id == "user_recommendations"
    )
    assert not await discovery.build_items(folder)
    discovery.provider.api.get_items.assert_awaited_once_with(
        "track.getSimilar", {"artist": "Artist", "track": "Song"}, limit=20
    )


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
    assert list(await discovery.build_items(folder)) == [previous]
    discovery.provider.api.get_items.assert_not_awaited()
