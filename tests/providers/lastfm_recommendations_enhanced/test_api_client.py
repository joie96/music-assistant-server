"""Last.fm request limits, server-imposed cooldowns and cache policy."""

import asyncio
import logging
import runpy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp import ClientResponseError
from music_assistant_models.errors import InvalidDataError, RateLimited

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers import throttle_retry
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lastfm_recommendations_enhanced import api_client as client_module
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import (
    CACHE_SLOTS,
    ITEM_METHODS,
    MAX_CACHE_ENTRY_BYTES,
    MISSING_ITEM_CACHE_TTL,
    LastFMAPIClient,
)


@pytest.mark.parametrize("method", ITEM_METHODS)
@pytest.mark.parametrize("local_limit", [1, 20, 50, 75, 105, 150, 500])
async def test_all_lastfm_methods_send_fifty_item_request_limit(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], method: str, local_limit: int
) -> None:
    """Only tag lists may increase their single request beyond 50, within endpoint bounds."""
    client, session, _cache = cached_client
    root, item_key, _ttl = ITEM_METHODS[method]
    session.get.return_value.__aenter__.return_value.json.return_value = {
        root: {item_key: [{"name": "Item"}]}
    }
    await client.get_items(method, limit=local_limit)
    maximum = {"tag.getTopTracks": 150, "tag.getTopArtists": 75, "tag.getTopAlbums": 105}.get(
        method, 50
    )
    assert session.get.call_args.kwargs["params"]["limit"] == max(50, min(local_limit, maximum))
    assert "page" not in session.get.call_args.kwargs["params"]


def test_client_import_supports_legacy_throttler_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construct the client throttler with only arguments supported in MA 2.11.0b7."""

    def legacy_constructor(rate_limit: int, period: float = 1) -> ThrottlerManager:
        return ThrottlerManager(rate_limit=rate_limit, period=period)

    monkeypatch.setattr(throttle_retry, "ThrottlerManager", legacy_constructor)
    assert client_module.__file__ is not None
    namespace = runpy.run_path(client_module.__file__)
    throttler = namespace["LastFMAPIClient"].throttler
    assert throttler.throttler.rate_limit == 5
    assert throttler.throttler.period == 1


@pytest.mark.parametrize("seconds", [0.5, 10, 60, 120])
async def test_active_cooldown_neither_waits_nor_requests(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    seconds: float,
) -> None:
    """Fail fast for short and long shared cooldowns without extending their deadlines."""
    client, session, _cache = cached_client
    client.throttler.set_cooldown(seconds)
    deadline = client.throttler._cooldown_until
    sleep = AsyncMock(side_effect=AssertionError("Cooldown must not sleep"))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(RateLimited) as error:
        await client.validate_api_key()
    assert error.value.backoff_time >= max(1, int(seconds))
    assert client.throttler._cooldown_until == deadline
    session.get.assert_not_called()
    sleep.assert_not_awaited()


async def test_cooldown_armed_during_slot_acquisition_prevents_request(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recheck the shared cooldown after an awaited gate, before opening the HTTP response."""
    client, session, _cache = cached_client

    @asynccontextmanager
    async def acquire_slot() -> AsyncIterator[float]:
        client.throttler.set_cooldown(10)
        yield 0.0

    monkeypatch.setattr(client.throttler, "acquire", acquire_slot)
    with pytest.raises(RateLimited):
        await client.validate_api_key()
    session.get.assert_not_called()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100", 100.0),
        (35, 35.0),
        (0, 0.0),
        (None, None),
        ("bad", None),
        (-1, None),
        (True, None),
        ("NaN", None),
        ("Infinity", None),
    ],
)
async def test_artist_tag_weights_preserve_values_and_order(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
    value: object,
    expected: float | None,
) -> None:
    """Keep tag popularity and source order, with invalid weights available for rank fallback."""
    client, session, _cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "toptags": {"tag": [{"name": "Shoegaze", "count": value}, {"name": "Dream Pop"}]}
    }
    tags = await client.get_items("artist.getTopTags", {"artist": "Streaming Artist"})
    assert [tag.name for tag in tags] == ["Shoegaze", "Dream Pop"]
    assert tags[0].tag_weight == expected
    assert tags[1].tag_weight is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.75", 0.75),
        (0.5, 0.5),
        (0, 0.0),
        (None, None),
        ("bad", None),
        (-1, None),
        (True, None),
        ("NaN", None),
        ("Infinity", None),
    ],
)
@pytest.mark.parametrize("method", ["artist.getSimilar", "track.getSimilar"])
async def test_similar_scores_preserve_values_without_using_tag_counts(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
    value: object,
    expected: float | None,
    method: str,
) -> None:
    """Retain numeric match weights independently of tag popularity or list rank."""
    client, session, _cache = cached_client
    root, item_key, _ttl = ITEM_METHODS[method]
    session.get.return_value.__aenter__.return_value.json.return_value = {
        root: {item_key: [{"name": "Similar", "match": value, "count": 99}, {"name": "Other"}]}
    }
    parameters = {"artist": "Seed"}
    if method == "track.getSimilar":
        parameters["track"] = "Track"
    artists = await client.get_items(method, parameters)
    assert artists[0].similarity_weight == expected
    assert artists[0].tag_weight is None
    assert artists[1].similarity_weight is None
    assert session.get.call_args.kwargs["params"]["limit"] == 50


async def test_similar_artists_do_not_inherit_the_seed_identity(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """The same similar artist must aggregate across distinct personal source artists."""
    client, session, _cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "similarartists": {"artist": [{"name": "Similar"}]}
    }
    assert (await client.get_items("artist.getSimilar", {"artist": "Seed"}))[0].artist == ""


async def test_removed_album_information_is_rejected_without_http(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """The retired spotlight endpoint cannot trigger a Last.fm request."""
    client, session, _cache = cached_client
    with pytest.raises(InvalidDataError):
        await client.get_items("album.getInfo", {"artist": "Artist", "album": "Album"})
    session.get.assert_not_called()


@pytest.mark.parametrize("log_level", [logging.DEBUG, VERBOSE_LOG_LEVEL])
async def test_individual_http_and_cache_details_are_only_logged_at_trace(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
    caplog: pytest.LogCaptureFixture,
    log_level: int,
) -> None:
    """Successful request/cache details do not add noise to Debug build progress."""
    client, session, cache = cached_client
    with caplog.at_level(log_level, logger=client.logger.name):
        first = await client.get_items("chart.getTopArtists")
        cache.get.return_value = cache.set.call_args.args[1]
        second = await client.get_items("chart.getTopArtists")
    assert first == second
    session.get.assert_called_once()
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith(("Last.fm cache ", "Last.fm HTTP GET "))
    ]
    if log_level == logging.DEBUG:
        assert records == []
    else:
        assert len(records) == 5
        assert all(record.levelno == VERBOSE_LOG_LEVEL for record in records)
        assert "Last.fm cache hit method=chart.getTopArtists" in caplog.text
    assert "private-key" not in caplog.text


@pytest.fixture
def cached_client(monkeypatch: pytest.MonkeyPatch) -> tuple[LastFMAPIClient, MagicMock, Mock]:
    """Return a real client with isolated throttle, HTTP and shared-cache boundaries."""
    monkeypatch.setattr(LastFMAPIClient, "throttler", ThrottlerManager(rate_limit=100))
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.status = 200
    response.headers = {}
    response.raise_for_status = Mock()
    response.json = AsyncMock(return_value={"artists": {"artist": [{"name": "Example"}]}})
    cache = Mock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return LastFMAPIClient(session, "private-key", cache=cache), session, cache


@pytest.mark.parametrize("error_code", [6, "6", "private-key", "1234567890"])
async def test_discovery_errors_do_not_blame_or_expose_the_api_key(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
    caplog: pytest.LogCaptureFixture,
    error_code: int | str,
) -> None:
    """Report the failing method and safe code, not a key-validation failure or raw message."""
    client, session, cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "error": error_code,
        "message": "private-key",
    }
    with caplog.at_level(logging.DEBUG), pytest.raises(InvalidDataError) as error:
        await client.get_items("track.getSimilar", {"artist": "Artist", "track": "Track"})
    expected_code = "6" if error_code in (6, "6") else "unknown"
    assert f"error code {expected_code}" in str(error.value)
    assert "track.getSimilar" in str(error.value)
    assert "error_code=" + expected_code in caplog.text
    assert "validate the API key" not in str(error.value)
    assert "private-key" not in str(error.value) + caplog.text
    assert "1234567890" not in str(error.value) + caplog.text
    cache.set.assert_not_awaited()


@pytest.mark.parametrize(
    "method",
    [
        method
        for method in ITEM_METHODS
        if method.startswith(("artist.", "track.", "album.", "tag."))
    ],
)
@pytest.mark.parametrize("error_code", [6, "7"])
async def test_explicitly_missing_catalogue_items_are_cached_as_empty_lists(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], method: str, error_code: int | str
) -> None:
    """Skip only explicitly missing resources and briefly cache their normalized empty responses."""
    client, session, cache = cached_client
    resource_type = method.split(".", 1)[0]
    parameters = {
        "artist": {"artist": "Artist"},
        "track": {"artist": "Artist", "track": "Track"},
        "album": {"artist": "Artist", "album": "Album"},
        "tag": {"tag": "Tag"},
    }[resource_type]
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "error": error_code,
        "message": f" {resource_type.capitalize()} not found ",
    }
    assert await client.get_items(method, parameters) == []
    root, item_key, _ttl = ITEM_METHODS[method]
    entry = cache.set.call_args.args[1]
    assert entry["data"] == {root: {item_key: []}}
    assert cache.set.call_args.kwargs["expiration"] == MISSING_ITEM_CACHE_TTL
    cache.get.return_value = entry
    assert await client.get_items(method, parameters) == []
    session.get.assert_called_once()


@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("track.getSimilar", "Your request is missing a required parameter"),
        ("track.getSimilar", "Artist not found"),
        ("chart.getTopTracks", "Track not found"),
    ],
)
async def test_parameter_errors_are_not_silently_treated_as_missing_items(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], method: str, message: str
) -> None:
    """Do not hide bad parameters, mismatched resource errors or failed chart requests."""
    client, session, cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "error": 6,
        "message": message,
    }
    with pytest.raises(InvalidDataError, match="error code 6"):
        await client.get_items(method, {"artist": "Artist", "track": "Track"})
    cache.set.assert_not_awaited()


@pytest.mark.parametrize("control", ["no-store", "max-age=120"])
async def test_missing_item_cache_respects_origin_cache_policy(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], control: str
) -> None:
    """An empty catalogue result must not override cache prohibitions or extend origin TTLs."""
    client, session, cache = cached_client
    response = session.get.return_value.__aenter__.return_value
    response.headers = {"Cache-Control": control}
    response.json.return_value = {"error": 6, "message": "Track not found"}
    assert await client.get_items("track.getSimilar", {"artist": "Artist", "track": "Track"}) == []
    if control == "no-store":
        cache.set.assert_not_awaited()
    else:
        assert cache.set.call_args.kwargs["expiration"] == 120


async def test_cache_hit_skips_network_and_validation_always_checks_key(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """Reuse a public response but never let that response authenticate another API key."""
    client, session, cache = cached_client
    first = await client.get_items("chart.getTopArtists")
    cache.get.return_value = cache.set.call_args.args[1]
    assert await client.get_items("chart.getTopArtists") == first
    assert session.get.call_count == 1
    await client.validate_api_key()
    assert session.get.call_count == 2
    assert cache.set.await_count == 1
    assert "private-key" not in str(cache.set.call_args)


@pytest.mark.parametrize("control", ["no-store", "no-cache", "private", "max-age=0"])
async def test_cache_prohibitions_are_honored(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], control: str
) -> None:
    """Do not persist a response when the origin forbids reuse."""
    client, session, cache = cached_client
    session.get.return_value.__aenter__.return_value.headers = {"Cache-Control": control}
    await client.get_items("chart.getTopArtists")
    cache.set.assert_not_awaited()


async def test_cache_age_shortens_ttl_and_entries_have_fixed_capacity(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """Bound the lifetime and total possible cache payload, including expired slots."""
    client, session, cache = cached_client
    session.get.return_value.__aenter__.return_value.headers = {
        "Cache-Control": "max-age=120",
        "Age": "20",
    }
    await client.get_items("chart.getTopArtists")
    assert cache.set.call_args.kwargs["expiration"] == 100
    assert CACHE_SLOTS * MAX_CACHE_ENTRY_BYTES < 100_000_000
    response = session.get.return_value.__aenter__.return_value
    response.json.return_value = {"artists": {"artist": [{"name": "x" * MAX_CACHE_ENTRY_BYTES}]}}
    cache.set.reset_mock()
    await client.get_items("chart.getTopArtists")
    cache.set.assert_not_awaited()


async def test_global_tags_use_documented_envelope_and_shared_cache(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """Global Explore tags preserve source rank and never require personal data."""
    client, session, cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "tags": {"tag": [{"name": "rock", "taggings": "1000"}, {"name": "jazz"}]}
    }
    tags = await client.get_items("chart.getTopTags", limit=50)
    assert [tag.name for tag in tags] == ["rock", "jazz"]
    cache.get.return_value = cache.set.call_args.args[1]
    assert await client.get_items("chart.getTopTags", limit=50) == tags
    session.get.assert_called_once()
    assert cache.set.call_args.kwargs["expiration"] == 21600
    with pytest.raises(InvalidDataError):
        await client.get_items("chart.getTopTags", {"country": "Germany"})
    session.get.assert_called_once()


async def test_cached_slot_collision_never_returns_other_query(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """A fixed-slot collision causes a miss rather than cross-query data reuse."""
    client, session, cache = cached_client
    cache.get.return_value = {"request": "different-query", "data": {"artists": {"artist": []}}}
    assert await client.get_items("chart.getTopArtists")
    session.get.assert_called_once()


async def test_unsupported_method_and_credential_overrides_are_rejected(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """Never turn the client into a write API or allow request credentials to be overridden."""
    client, session, _cache = cached_client
    with pytest.raises(InvalidDataError):
        await client.get_items("track.scrobble")
    with pytest.raises(InvalidDataError):
        await client.get_items("chart.getTopArtists", {"api_key": "another-key"})
    session.get.assert_not_called()


@pytest.mark.parametrize("method", list(ITEM_METHODS))
async def test_supported_response_envelopes_and_candidate_limit(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], method: str
) -> None:
    """Normalize every supported endpoint and never fetch another page."""
    client, session, _cache = cached_client
    root, item_key, _ttl = ITEM_METHODS[method]
    session.get.return_value.__aenter__.return_value.json.return_value = {
        root: {
            item_key: [{"name": str(index), "artist": {"name": "Artist"}} for index in range(200)]
        }
    }
    items = await client.get_items(method, limit=500)
    maximum = {"tag.getTopTracks": 150, "tag.getTopArtists": 75, "tag.getTopAlbums": 105}.get(
        method, 50
    )
    assert len(items) == maximum
    assert items[0].artist == "Artist"
    assert session.get.call_args.kwargs["params"]["limit"] == maximum
    assert "page" not in session.get.call_args.kwargs["params"]
    session.get.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/music/Artist",
        "https://private@last.fm/music/Artist",
        "javascript:alert(1)",
        "http://[invalid",
    ],
)
async def test_unsafe_source_links_are_not_exposed(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock], url: str
) -> None:
    """Reject non-Last.fm, credential-bearing and malformed attribution URLs."""
    client, session, _cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "artists": {"artist": [{"name": "Artist", "url": url}]}
    }
    assert (await client.get_items("chart.getTopArtists"))[0].url == ""


async def test_valid_catalogue_links_are_upgraded_and_stripped_of_query_parameters(
    cached_client: tuple[LastFMAPIClient, MagicMock, Mock],
) -> None:
    """Do not propagate credentials or tracking parameters from source links."""
    client, session, _cache = cached_client
    session.get.return_value.__aenter__.return_value.json.return_value = {
        "artists": {
            "artist": [{"name": "Artist", "url": "http://last.fm/music/Artist?key=private#track"}]
        }
    }
    assert (await client.get_items("chart.getTopArtists"))[
        0
    ].url == "https://www.last.fm/music/Artist"


@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize("retry_after", ["120", None])
async def test_rate_limit_blocks_other_instances(
    monkeypatch: pytest.MonkeyPatch, status: int, retry_after: str | None
) -> None:
    """Honor HTTP/JSON rate limits across instances without sleeping or sending another request."""
    throttler = ThrottlerManager(rate_limit=100)
    monkeypatch.setattr(LastFMAPIClient, "throttler", throttler)
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.status = status
    response.headers = {"Retry-After": retry_after} if retry_after else {}
    response.raise_for_status = Mock()
    response.json = AsyncMock(return_value={"error": 29, "message": "private-key"})
    if status == 429:
        response.raise_for_status.side_effect = ClientResponseError(Mock(), (), status=429)
    first = LastFMAPIClient(session, "private-key")
    second_session = MagicMock()
    second = LastFMAPIClient(second_session, "other-private-key")
    with pytest.raises(RateLimited) as error:
        await first.validate_api_key()
    expected = int(retry_after) if retry_after else 60
    assert error.value.backoff_time >= expected
    assert throttler.cooldown_remaining > expected - 1
    with pytest.raises(RateLimited):
        await second.validate_api_key()
    second_session.get.assert_not_called()
    assert "private-key" not in str(error.value)
