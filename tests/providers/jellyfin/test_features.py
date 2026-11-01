"""Tests for Jellyfin fields and endpoints missing from aiojellyfin."""

import logging
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError, ClientSession
from aiojellyfin import Connection, ItemFields, NotFound
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.providers.jellyfin import SUPPORTED_FEATURES, JellyfinProvider
from music_assistant.providers.jellyfin.client import JellyfinClient, rating_sort_key
from music_assistant.providers.jellyfin.const import RECOMMENDATION_ROWS
from music_assistant.providers.jellyfin.parsers import parse_lyrics


@pytest.fixture
def provider() -> MagicMock:
    """Use the actual provider methods with network and cache dependencies mocked."""
    instance = cast("MagicMock", object.__new__(JellyfinProvider))
    instance.config = MagicMock(instance_id="jellyfin-test")
    instance.manifest = MagicMock(domain="jellyfin")
    instance.mass = MagicMock()
    instance.mass.cache.delete = AsyncMock()
    instance.logger = logging.getLogger(__name__)
    instance._supported_features = SUPPORTED_FEATURES
    instance._client = MagicMock(spec=Connection)
    instance._client.audio_url.return_value = "http://jellyfin/audio"
    instance._api = MagicMock(spec=JellyfinClient)
    instance._api.can_edit_playlist.return_value = True
    instance._get_artist_items = AsyncMock(return_value=[])
    instance._get_playlist_items = AsyncMock(return_value=[])
    instance._get_genres = AsyncMock(return_value=[])
    return instance


def media_item(item_id: str, item_type: str = "Audio", **values: Any) -> dict[str, Any]:
    """Build a compact API record accepted by the real media parsers."""
    return {
        "Id": item_id,
        "Name": item_id,
        "Type": item_type,
        "CanDownload": True,
        "ProviderIds": {},
        "ArtistItems": [],
        "ImageTags": {},
        "MediaStreams": [],
        **values,
    }


def uncached_method(method_name: str) -> Callable[..., Any]:
    """Access cached provider methods without a running cache controller."""
    method = getattr(JellyfinProvider, method_name)
    return cast("Callable[..., Any]", method.__wrapped__)


def test_rating_sort_key() -> None:
    """Missing ratings sort after zero, and ties sort by name then identity."""
    items: list[dict[str, Any]] = [
        {"Id": "missing", "Name": "A"},
        {"Id": "zero", "Name": "B", "CommunityRating": 0},
        {"Id": "null", "Name": "Z", "CommunityRating": None},
        {"Id": "best-b", "Name": "Best", "CommunityRating": 9},
        {"Id": "best-a", "Name": "Best", "CommunityRating": 9},
    ]
    assert [item["Id"] for item in sorted(items, key=rating_sort_key)] == [
        "best-a",
        "best-b",
        "zero",
        "missing",
        "null",
    ]


async def test_artist_items_preserve_ratings_and_paginate() -> None:
    """Artist queries carry user identity and keep fields outside the SDK models."""
    connection = MagicMock()
    connection._user_id = "user"
    connection._session.get_json = AsyncMock(
        side_effect=[
            {"Items": [{"Id": "one", "CommunityRating": 8}], "TotalRecordCount": 2},
            {"Items": [{"Id": "two", "CommunityRating": 0}], "TotalRecordCount": 2},
        ]
    )
    client = JellyfinClient(connection)
    items = await client.get_artist_items("artist", "Audio", [ItemFields.ProviderIds])
    assert [item["CommunityRating"] for item in items] == [8, 0]
    calls = connection._session.get_json.call_args_list
    assert calls[0].args[0] == "/Items"
    assert calls[0].args[1]["userId"] == "user"
    assert calls[0].args[1]["artistIds"] == "artist"
    assert "parentId" not in calls[0].args[1]
    assert calls[1].args[1]["startIndex"] == "1"


async def test_recommendation_listing_is_bounded() -> None:
    """Recommendation queries retain caller sorting and stop at the requested limit."""
    connection = MagicMock()
    connection._user_id = "user"
    connection._session.get_json = AsyncMock(
        return_value={"Items": [{"Id": "one"}], "TotalRecordCount": 1000}
    )
    client = JellyfinClient(connection)
    items = await client.get_items(
        "Audio", [], {"sortBy": "PlayCount", "isPlayed": "true"}, limit=1
    )
    assert items == [{"Id": "one"}]
    connection._session.get_json.assert_awaited_once()
    params = connection._session.get_json.call_args.args[1]
    assert params["sortBy"] == "PlayCount"
    assert params["isPlayed"] == "true"
    assert params["limit"] == "1"


async def test_empty_page_stops_pagination() -> None:
    """A changing library cannot cause endless requests after an empty page."""
    connection = MagicMock()
    connection._session.get_json = AsyncMock(return_value={"Items": [], "TotalRecordCount": 1000})
    assert await JellyfinClient(connection).get_items("Audio", [], {}, limit=None) == []
    connection._session.get_json.assert_awaited_once()


@pytest.mark.parametrize("method", ["get_artist_toptracks", "get_artist_topalbums"])
async def test_artist_toplists(provider: MagicMock, method: str) -> None:
    """Top tracks and albums share the community-rating ordering contract."""
    item_type = "Audio" if method == "get_artist_toptracks" else "MusicAlbum"
    provider._get_artist_items.return_value = [
        media_item("missing", item_type),
        media_item("zero", item_type, CommunityRating=0),
        media_item("best", item_type, CommunityRating=9),
    ]
    items = await getattr(JellyfinProvider, method).__wrapped__(provider, "artist")
    assert [item.item_id for item in items] == ["best", "zero", "missing"]
    assert provider._get_artist_items.call_args.args[:2] == ("artist", item_type)


@pytest.mark.parametrize(
    ("method", "item_type"),
    [("get_similar_tracks", "Audio"), ("get_similar_artists", "MusicArtist")],
)
async def test_similarity_excludes_seed_duplicates_and_wrong_types(
    provider: MagicMock, method: str, item_type: str
) -> None:
    """Similarity preserves backend order but excludes unrelated or repeated items."""
    provider._api.get_json.return_value = {
        "Items": [
            media_item("seed", item_type),
            media_item("first", item_type),
            media_item("first", item_type),
            media_item("wrong", "Movie"),
            media_item("second", item_type),
            media_item("third", item_type),
        ]
    }
    items = await getattr(JellyfinProvider, method).__wrapped__(provider, "seed", 2)
    assert [item.item_id for item in items] == ["first", "second"]
    provider._api.get_json.reset_mock()
    assert await getattr(JellyfinProvider, method).__wrapped__(provider, "seed", 0) == []
    provider._api.get_json.assert_not_awaited()


async def test_recommendation_descriptors_do_not_fetch(provider: MagicMock) -> None:
    """Row discovery remains fast and static even before a backend request."""
    rows = await provider.get_recommendations()
    assert {row.item_id for row in rows} == {*RECOMMENDATION_ROWS, "genres"}
    provider._api.get_items.assert_not_awaited()
    provider._get_genres.assert_not_awaited()


@pytest.mark.parametrize("row_id", list(RECOMMENDATION_ROWS))
async def test_recommendation_row_query(provider: MagicMock, row_id: str) -> None:
    """Rating and playback rows select appropriate item types and filters."""
    _, item_type, sort_by = RECOMMENDATION_ROWS[row_id]
    provider._api.get_items.return_value = [media_item("item", item_type)]
    items = await uncached_method("get_recommendation_items")(provider, row_id)
    assert items[0].item_id == "item"
    query = provider._api.get_items.call_args.args
    assert query[0] == item_type
    assert query[2]["sortBy"] == f"{sort_by},SortName"
    if sort_by == "CommunityRating":
        assert query[2]["minCommunityRating"] == "0"
    if sort_by in ("PlayCount", "DatePlayed"):
        assert query[2]["isPlayed"] == "true"


async def test_unknown_recommendation_row(provider: MagicMock) -> None:
    """Unknown row identifiers are errors, not silently empty results."""
    with pytest.raises(MediaNotFoundError):
        await uncached_method("get_recommendation_items")(provider, "unknown")


async def test_genre_browse_routes(provider: MagicMock) -> None:
    """Genre folders remain on provider-owned paths and keep existing library navigation."""
    provider._get_genres.return_value = [{"Id": "rock", "Name": "Rock"}]
    root = await provider.browse("jellyfin-test://")
    assert {folder.item_id for folder in root} >= {
        "artists",
        "albums",
        "tracks",
        "playlists",
        "genres",
    }
    genres = await provider.browse("jellyfin-test://genres")
    assert genres[0].path == "jellyfin-test://genres/rock"
    sections = await provider.browse(genres[0].path)
    assert {folder.path.rsplit("/", 1)[-1] for folder in sections} == {"albums", "tracks", "mix"}
    with pytest.raises(MediaNotFoundError):
        await provider.browse("other-provider://genres")


async def test_genre_mix_escapes_names(provider: MagicMock) -> None:
    """Genre names cannot change the instant-mix URL path."""
    provider._get_genres.return_value = [{"Id": "genre", "Name": "R&B / Soul"}]
    provider._api.get_json.return_value = {"Items": [media_item("track")]}
    items = await uncached_method("_get_genre_items")(provider, "genre", "mix")
    assert items[0].item_id == "track"
    assert provider._api.get_json.call_args.args[0] == "/MusicGenres/R%26B%20%2F%20Soul/InstantMix"


async def test_playlist_removal_uses_entry_ids(provider: MagicMock) -> None:
    """Only selected occurrences are removed when a playlist repeats a track."""
    provider._get_playlist_items.return_value = [
        {"Id": "same-track", "PlaylistItemId": "first-entry"},
        {"Id": "same-track", "PlaylistItemId": "second-entry"},
    ]
    await provider.remove_playlist_tracks("playlist", (2, 2))
    provider._api.request.assert_awaited_once_with(
        "DELETE", "/Playlists/playlist/Items", {"entryIds": "second-entry"}
    )
    provider.mass.cache.delete.assert_awaited_once_with(None, provider="jellyfin-test")


async def test_readonly_playlist_rejects_mutations(provider: MagicMock) -> None:
    """A read-only playlist never reaches the write endpoint."""
    provider._api.can_edit_playlist.return_value = False
    with pytest.raises(ProviderPermissionDenied):
        await provider.add_playlist_tracks("playlist", ["track"])
    with pytest.raises(ProviderPermissionDenied):
        await provider.remove_playlist_tracks("playlist", (1,))
    provider._api.request.assert_not_awaited()
    provider.mass.cache.delete.assert_not_awaited()


async def test_invalid_playlist_position(provider: MagicMock) -> None:
    """Out-of-range positions cannot accidentally target a different entry."""
    provider._get_playlist_items.return_value = [{"PlaylistItemId": "entry"}]
    for position in (0, -1, 2):
        with pytest.raises(InvalidDataError):
            await provider.remove_playlist_tracks("playlist", (position,))
    provider._api.request.assert_not_awaited()


async def test_playlist_addition_preserves_duplicates(provider: MagicMock) -> None:
    """Adding the same track twice preserves both occurrences."""
    await provider.add_playlist_tracks("playlist", ["track", "track"])
    provider._api.request.assert_awaited_once_with(
        "POST", "/Playlists/playlist/Items", {"ids": "track,track"}
    )


async def test_playlist_page_positions(provider: MagicMock) -> None:
    """Positions after the first hundred entries stay one-based and absolute."""
    provider._get_playlist_items.return_value = [media_item(str(index)) for index in range(103)]
    items = await provider.get_playlist_tracks("playlist", 1)
    assert [item.position for item in items] == [101, 102, 103]
    with pytest.raises(InvalidDataError):
        await provider.get_playlist_tracks("playlist", -1)


async def test_playlist_creation(provider: MagicMock) -> None:
    """Creation uses an audio playlist and returns its actual provider object."""
    provider._api.request.return_value = {"Id": "new-playlist"}
    provider.get_playlist = AsyncMock(return_value="created")
    assert await provider.create_playlist("Mix", {MediaType.TRACK}) == "created"
    provider._api.request.assert_awaited_once_with(
        "POST", "/Playlists", {"name": "Mix", "mediaType": "Audio"}
    )
    provider.get_playlist.assert_awaited_once_with("new-playlist")
    with pytest.raises(UnsupportedFeaturedException):
        await provider.create_playlist("Mix", {MediaType.PODCAST_EPISODE})
    with pytest.raises(InvalidDataError):
        await provider.create_playlist(" ", {MediaType.TRACK})


async def test_favorite_is_separate_from_library_membership(provider: MagicMock) -> None:
    """Unfavoriting never calls a library deletion endpoint."""
    await provider.set_favorite("track", MediaType.TRACK, False)
    provider._api.set_favorite.assert_awaited_once_with("track", False)
    provider._api.request.assert_not_awaited()
    provider.mass.cache.delete.assert_awaited_once_with(None, provider="jellyfin-test")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, (None, None)),
        ({"Lyrics": [{"Text": "Plain"}]}, ("Plain", None)),
        (
            {"Lyrics": [{"Text": "Start", "Start": 0}, {"Text": "Later", "Start": 612300000}]},
            ("Start\nLater", "[00:00.00]Start\n[01:01.23]Later"),
        ),
    ],
)
def test_lyrics_conversion(
    payload: dict[str, Any], expected: tuple[str | None, str | None]
) -> None:
    """Lyrics use 100-nanosecond ticks, including a valid zero timestamp."""
    assert parse_lyrics(payload) == expected


async def test_track_detail_attaches_lyrics_and_genres(provider: MagicMock) -> None:
    """Only full detail retrieval requests lyrics and retains unmodelled genre fields."""
    provider._api.get_item.return_value = media_item("track", Genres=["Rock"])
    provider._api.get_lyrics.return_value = {"Lyrics": [{"Text": "Line", "Start": 0}]}
    track = await uncached_method("get_track")(provider, "track")
    assert track.metadata.genres == {"Rock"}
    assert track.metadata.lyrics == "Line"
    assert track.metadata.lrc_lyrics == "[00:00.00]Line"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, LoginFailed),
        (403, ProviderPermissionDenied),
        (404, MediaNotFoundError),
        (500, ProviderUnavailableError),
    ],
)
async def test_client_maps_http_errors(status: int, error_type: type[Exception]) -> None:
    """HTTP failures surface as Music Assistant errors without credentials in messages."""
    connection = MagicMock()
    connection._session.get_json = AsyncMock(
        side_effect=ClientResponseError(MagicMock(), (), status=status)
    )
    with pytest.raises(error_type):
        await JellyfinClient(connection).get_json("/Items", {})


async def test_missing_lyrics_and_legacy_permissions() -> None:
    """Missing lyrics and unavailable permission endpoints are explicit optional cases."""
    connection = MagicMock()
    connection._session.get_json = AsyncMock(side_effect=NotFound())
    client = JellyfinClient(connection)
    assert await client.get_lyrics("track") is None
    assert await client.can_edit_playlist("playlist") is False


async def test_legacy_favorite_route() -> None:
    """Older Jellyfin favorite routes remain usable without duplicating successful writes."""
    connection = MagicMock()
    connection._user_id = "user"
    client = JellyfinClient(connection)
    with patch.object(
        client, "request", AsyncMock(side_effect=[MediaNotFoundError(), {}])
    ) as request:
        await client.set_favorite("track", False)
        assert request.call_args_list[0].args == ("DELETE", "/UserFavoriteItems/track", {})
        assert request.call_args_list[1].args == ("DELETE", "/Users/user/FavoriteItems/track", {})


async def test_mutation_reuses_authentication_and_ssl() -> None:
    """Writes reuse the shared HTTP session and honor its configured SSL policy."""
    session = MagicMock(spec=ClientSession)
    config = SessionConfiguration(
        session, "https://jellyfin", "MA", "1", "test", "test", verify_ssl=False
    )
    connection = Connection(config, "user", "test-token")
    response = MagicMock(status=204)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session.request.return_value = response
    assert await JellyfinClient(connection).request("POST", "/Playlists", {}) == {}
    request = session.request.call_args
    assert request.args == ("POST", "https://jellyfin/Playlists")
    assert request.kwargs["ssl"] is False
    assert request.kwargs["params"]["userId"] == "user"
    assert request.kwargs["headers"]["Authorization"] == config.authentication_header("test-token")


async def test_failed_mutation_is_not_retried() -> None:
    """A failed playlist write is not replayed after a potentially successful request."""
    session = MagicMock(spec=ClientSession)
    config = SessionConfiguration(session, "http://jellyfin", "MA", "1", "test", "test")
    response = MagicMock()
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    response.raise_for_status.side_effect = ClientResponseError(MagicMock(), (), status=503)
    session.request.return_value = response
    client = JellyfinClient(Connection(config, "user", "test-token"))
    with pytest.raises(ProviderUnavailableError):
        await client.request("POST", "/Playlists", {})
    session.request.assert_called_once()
