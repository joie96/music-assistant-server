"""Tests for the ListenBrainz recommendation row contract."""

import asyncio
from datetime import UTC, date, datetime
from types import MethodType
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.controllers.tasks.helpers import get_task_schedule_delay
from music_assistant.models.media_capabilities import MediaCatalogMixin
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.listenbrainz_recommendations import (
    ListenBrainzRecommendationsProvider,
)
from music_assistant.providers.listenbrainz_recommendations.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz_recommendations.setup_flow import run_setup

if TYPE_CHECKING:
    from pathlib import Path


async def test_recommendation_rows_do_not_include_items() -> None:
    """Return row descriptors while preserving separately requested items."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    folder = RecommendationFolder(
        item_id="top_tracks",
        provider="listenbrainz_recommendations",
        name="Top Tracks",
        icon="mdi:music",
        subtitle="Your most played tracks",
        items=UniqueList(
            [Track(item_id="track", provider="library", name="Track", provider_mappings=set())]
        ),
    )
    provider._recommendation_folders = [folder]

    rows = await ListenBrainzRecommendationsProvider.get_recommendations(provider)
    items = await ListenBrainzRecommendationsProvider.get_recommendation_items(
        provider, folder.item_id
    )

    assert len(rows) == 1
    assert rows[0] is not folder
    assert rows[0].item_id == folder.item_id
    assert rows[0].provider == folder.provider
    assert rows[0].name == folder.name
    assert rows[0].icon == folder.icon
    assert rows[0].subtitle == folder.subtitle
    assert not rows[0].items
    assert items is folder.items
    assert len(items) == 1


async def test_unknown_recommendation_row_is_empty() -> None:
    """Return an empty item list for an unknown recommendation row."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._recommendation_folders = []

    items = await ListenBrainzRecommendationsProvider.get_recommendation_items(provider, "unknown")

    assert items == []


def test_provider_supports_media_catalog() -> None:
    """Make virtual playlists discoverable by the playlist controller."""
    provider = object.__new__(ListenBrainzRecommendationsProvider)

    assert isinstance(provider, MediaCatalogMixin)


async def test_virtual_playlist_lookup_and_pagination() -> None:
    """Serve cached virtual playlist details and tracks through the catalog."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    playlist = Playlist(
        item_id="artist_radio",
        provider="listenbrainz_recommendations",
        name="Artist Radio",
        provider_mappings=set(),
    )
    tracks = [Track(item_id="track", provider="library", name="Track", provider_mappings=set())]
    provider._resolved_virtual_playlists = {playlist.item_id: (playlist, tracks)}

    assert (
        await ListenBrainzRecommendationsProvider.get_playlist(provider, playlist.item_id)
        is playlist
    )
    assert (
        await ListenBrainzRecommendationsProvider.get_playlist_tracks(provider, playlist.item_id)
        is tracks
    )
    assert (
        await ListenBrainzRecommendationsProvider.get_playlist_tracks(
            provider, playlist.item_id, page=1
        )
        == []
    )


async def test_unknown_virtual_playlist_raises() -> None:
    """Report unknown virtual playlists through the standard not-found error."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._resolved_virtual_playlists = {}

    with pytest.raises(MediaNotFoundError):
        await ListenBrainzRecommendationsProvider.get_playlist(provider, "unknown")
    with pytest.raises(MediaNotFoundError):
        await ListenBrainzRecommendationsProvider.get_playlist_tracks(provider, "unknown")


async def test_setup_collects_credentials() -> None:
    """Collect required credentials through the setup flow, with an obscured token."""
    session = MagicMock()
    session.context.setup_data = {}
    session.form = AsyncMock(return_value={"username": "listener", "auth_token": "test-token"})
    session.finish = AsyncMock()

    await run_setup(session)

    session.finish.assert_awaited_once_with({"username": "listener", "auth_token": "test-token"})
    entries = session.form.call_args.args[0]
    assert [entry.key for entry in entries] == ["username", "auth_token"]
    assert all(entry.required for entry in entries)
    assert entries[1].type == ConfigEntryType.SECURE_STRING
    assert session.form.call_args.kwargs["last_step"] is True


async def test_setup_retains_values_after_failure() -> None:
    """Keep submitted credentials and show setup errors when retrying."""
    session = MagicMock()
    session.context.setup_data = {"username": "existing-listener", "auth_token": "old-token"}
    submitted = {"username": "listener", "auth_token": "test-token"}
    session.form = AsyncMock(return_value=submitted)
    error = SetupFlowError("Setup failed")
    session.finish = AsyncMock(side_effect=[error, None])

    await run_setup(session)

    first_entries = session.form.call_args_list[0].args[0]
    assert first_entries[0].value == "existing-listener"
    assert first_entries[1].value == "old-token"
    assert session.finish.await_count == 2
    assert session.form.call_args.kwargs["errors"] == {"base": error}
    assert session.form.call_args.args[0][0].value == "listener"
    assert session.form.call_args.args[0][1].value == "test-token"


async def test_api_client_reads_token_from_setup_data() -> None:
    """Use the decrypted setup token when authenticating ListenBrainz requests."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.config = MagicMock()
    provider.config.get_value.return_value = "https://api.listenbrainz.org"
    provider.get_setup_value.return_value = "test-token"
    provider.mass = MagicMock()
    response = provider.mass.http_session.get.return_value.__aenter__.return_value
    response.status = 200
    response.json = AsyncMock(return_value={"payload": {}})
    client = ListenBrainzAPIClient(provider)

    result = await client._request("/1/test", {})

    assert result == {"payload": {}}
    provider.get_setup_value.assert_called_once_with("auth_token")
    assert provider.mass.http_session.get.call_args.kwargs["headers"] == {
        "Authorization": "Token test-token"
    }


@pytest.mark.parametrize(
    ("entity", "identity_field", "name_field"),
    [
        ("artists", "artist_mbid", "artist_name"),
        ("recordings", "recording_mbid", "track_name"),
        ("release-groups", "release_group_mbid", "release_group_name"),
    ],
)
async def test_sitewide_charts_combine_both_weeks(
    entity: str, identity_field: str, name_field: str
) -> None:
    """Merge matching chart entries and rank by combined listen count."""
    client = object.__new__(ListenBrainzAPIClient)
    shared = {identity_field: "shared", name_field: "Shared", "listen_count": 4}
    other = {identity_field: "other", name_field: "Other", "listen_count": 6}
    payload_key = entity.replace("-", "_")
    client._cached_request = AsyncMock(
        side_effect=[
            {"payload": {payload_key: [shared, other]}},
            {"payload": {payload_key: [shared]}},
        ]
    )

    rows = await client.get_sitewide_top_items(entity, count=15)

    assert [row[identity_field] for row in rows] == ["shared", "other"]
    assert rows[0]["listen_count"] == 8
    assert shared["listen_count"] == 4
    assert [call.args[1]["range"] for call in client._cached_request.call_args_list] == [
        "week",
        "this_week",
    ]
    assert all(
        call.args[0] == f"/1/stats/sitewide/{entity}"
        for call in client._cached_request.call_args_list
    )


async def test_refresh_startup_uses_task_manager_and_updates_old_interval() -> None:
    """Track the initial build and replace a persisted six-hour schedule."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.mass = MagicMock()
    provider.mass.tasks.register_scheduled_task.return_value.schedule = TaskSchedule.hourly(every=6)
    namespace = ListenBrainzRecommendationsProvider.handle_async_init.__globals__
    with patch.dict(namespace, {"ListenBrainzAPIClient": MagicMock()}):
        await ListenBrainzRecommendationsProvider.handle_async_init(provider)

    registration = provider.mass.tasks.register_scheduled_task.call_args.kwargs
    assert registration["schedule"].every == 24
    provider.mass.tasks.update_task_schedule.assert_called_once_with(
        registration["task_id"], TaskSchedule.hourly(every=24)
    )
    initial = provider.mass.call_later.call_args
    assert initial.args[0] == 20
    assert initial.args[1].func == provider.mass.tasks.run_task
    assert initial.args[1].args == (registration["task_id"],)
    delay, next_run = get_task_schedule_delay(
        registration["schedule"],
        now=datetime(2026, 10, 20, 20, 55, tzinfo=UTC),
        last_run=datetime(2026, 10, 20, 20, 10, tzinfo=UTC),
    )
    assert delay == 23 * 3600 + 15 * 60
    assert next_run == datetime(2026, 10, 21, 20, 10, tzinfo=UTC)


async def test_refresh_interval_update_preserves_disabled_schedule() -> None:
    """Do not enable automatic refresh when updating the stored interval."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.mass = MagicMock()
    schedule = TaskSchedule.hourly(every=6)
    schedule.enabled = False
    provider.mass.tasks.register_scheduled_task.return_value.schedule = schedule
    namespace = ListenBrainzRecommendationsProvider.handle_async_init.__globals__
    with patch.dict(namespace, {"ListenBrainzAPIClient": MagicMock()}):
        await ListenBrainzRecommendationsProvider.handle_async_init(provider)

    updated = provider.mass.tasks.update_task_schedule.call_args.args[1]
    assert updated.every == 24
    assert updated.enabled is False


async def test_unload_cancels_pending_initial_refresh() -> None:
    """A removed provider must not leave its startup task queued."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.mass = MagicMock()

    await ListenBrainzRecommendationsProvider.unload(provider, is_removed=True)

    provider.mass.cancel_timer.assert_called_once_with(
        "refresh_listenbrainz_recommendations_initial_listenbrainz-instance"
    )


async def test_exploration_uses_latest_playlist_even_when_it_is_old() -> None:
    """Use September's latest Exploration in October rather than dropping it."""
    client = object.__new__(ListenBrainzAPIClient)
    client.provider = MagicMock()
    extension_key = "https://musicbrainz.org/doc/jspf#playlist"
    playlists = [
        {
            "playlist": {
                "date": created,
                "extension": {
                    extension_key: {
                        "additional_metadata": {"algorithm_metadata": {"source_patch": source}}
                    }
                },
            }
        }
        for created, source in [
            ("2026-08-24T00:10:00+00:00", "weekly-exploration"),
            ("2026-09-14T00:10:00+00:00", "weekly-exploration"),
            ("2026-10-19T00:10:00+00:00", "weekly-jams"),
            ("invalid", "weekly-exploration"),
        ]
    ]
    client._cached_request = AsyncMock(return_value={"playlists": playlists})

    result = await client.get_weekly_exploration("listener")

    assert [row["date"][:10] for row in result] == ["2026-09-14"]
    client._cached_request.assert_awaited_once_with(
        "/1/user/listener/playlists/recommendations", {}
    )


async def test_fresh_releases_use_release_dates_and_deduplicate_groups() -> None:
    """Include both calendar weeks, including upcoming releases this week."""
    client = object.__new__(ListenBrainzAPIClient)
    client.provider = MagicMock()
    client._cached_request = AsyncMock(
        return_value={
            "payload": {
                "releases": [
                    {"release_date": released, "release_group_mbid": identity}
                    for released, identity in [
                        ("2026-10-11", "old"),
                        ("2026-10-12", "previous"),
                        ("2026-10-19", "current"),
                        ("2026-10-19", "current"),
                        ("2026-10-25", "upcoming"),
                        ("2026-10-26", "next"),
                        ("2026-10", "incomplete"),
                    ]
                ]
            }
        }
    )

    result = await client.get_fresh_releases(date(2026, 10, 20))

    assert [row["release_group_mbid"] for row in result] == ["upcoming", "current", "previous"]
    assert client._cached_request.call_args.args[0] == "/1/explore/fresh-releases/"


async def test_similar_users_are_sorted_and_invalid_scores_are_excluded() -> None:
    """Use the API similarity ranking, not the response's incidental row order."""
    client = object.__new__(ListenBrainzAPIClient)
    client._cached_request = AsyncMock(
        return_value={
            "payload": [
                {"user_name": "second", "similarity": 0.5},
                {"user_name": "closest", "similarity": 0.9},
                {"user_name": "listener", "similarity": 1.0},
                {"user_name": "", "similarity": 0.99},
                {"user_name": "invalid", "similarity": "0.99"},
                {"user_name": "invalid", "similarity": True},
                {"user_name": "invalid", "similarity": float("nan")},
            ]
        }
    )

    users = await client.get_similar_users("listener")

    assert [user["user_name"] for user in users] == ["closest", "second"]
    client._cached_request.assert_awaited_once_with("/1/user/listener/similar-users", {})


@pytest.mark.parametrize("source", ["weekly-exploration", "weekly-jams"])
async def test_latest_weekly_playlist_compares_dates_across_timezones(source: str) -> None:
    """Select by the actual creation time rather than the textual timestamp."""
    client = object.__new__(ListenBrainzAPIClient)
    client.provider = MagicMock()
    client._cached_request = AsyncMock(
        return_value={
            "playlists": [
                {
                    "playlist": {
                        "date": created,
                        "extension": {
                            "https://musicbrainz.org/doc/jspf#playlist": {
                                "additional_metadata": {
                                    "algorithm_metadata": {"source_patch": source}
                                }
                            }
                        },
                    }
                }
                for created in ["2026-09-14T02:00:00+02:00", "2026-09-14T00:30:00+00:00"]
            ]
        }
    )

    method = client.get_weekly_jams if source == "weekly-jams" else client.get_weekly_exploration
    rows = await method("listener")

    assert rows[0]["date"] == "2026-09-14T00:30:00+00:00"


@pytest.mark.parametrize("first_has_jam", [True, False])
async def test_similar_users_jam_uses_highest_ranked_user_with_latest_jam(
    first_has_jam: bool,
) -> None:
    """Use the closest listener or skip to the next one without a date cutoff."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.domain = "listenbrainz_recommendations"
    provider.get_setup_value.return_value = "joie95"
    provider._resolve_generated_playlist = MethodType(
        ListenBrainzRecommendationsProvider._resolve_generated_playlist, provider
    )
    provider._create_virtual_playlist = MethodType(
        ListenBrainzRecommendationsProvider._create_virtual_playlist, provider
    )
    client = object.__new__(ListenBrainzAPIClient)
    client.provider = provider
    provider.api_client = client
    playlist_mbid = "4f792c35-3885-460a-b58f-1de4a3159d7a"
    jam_rows = {
        "playlists": [
            {
                "playlist": {
                    "identifier": f"https://listenbrainz.org/playlist/{playlist_mbid}",
                    "title": "Weekly Jams",
                    "date": created,
                    "extension": {
                        "https://musicbrainz.org/doc/jspf#playlist": {
                            "additional_metadata": {"algorithm_metadata": {"source_patch": source}}
                        }
                    },
                }
            }
            for created, source in [
                ("2026-08-24T00:00:00+00:00", "weekly-jams"),
                ("2026-09-14T00:00:00+00:00", "weekly-jams"),
                ("2026-10-19T00:00:00+00:00", "weekly-exploration"),
            ]
        ]
    }
    responses = [
        {
            "payload": [
                {"user_name": "ikso800", "similarity": 0.50},
                {"user_name": "TheDeadite", "similarity": 0.51},
                {"user_name": "m0bb", "similarity": 0.53},
            ]
        }
    ]
    if not first_has_jam:
        responses.append({"playlists": []})
    responses.extend(
        [
            jam_rows,
            {
                "playlist": {
                    "track": [
                        {"creator": "Artist", "title": "Second"},
                        {"creator": "Artist", "title": "First"},
                    ]
                }
            },
        ]
    )
    client._cached_request = AsyncMock(side_effect=responses)
    tracks = [
        Track(item_id=name, provider="library", name=name, provider_mappings=set())
        for name in ("Second", "First")
    ]
    provider.resolve_provider_track_by_name.side_effect = tracks

    folder, playlists = await ListenBrainzRecommendationsProvider._build_folder_similar_users_jam(
        provider
    )

    assert folder.item_id == "similarusers_jam_weekly"
    playlist = folder.items[0]
    assert playlist.item_id == f"similarusers_jam_{playlist_mbid}"
    assert playlists[playlist.item_id] == (playlist, tracks)
    paths = [call.args[0] for call in client._cached_request.call_args_list]
    expected = ["/1/user/joie95/similar-users", "/1/user/m0bb/playlists/recommendations"]
    if not first_has_jam:
        expected.append("/1/user/TheDeadite/playlists/recommendations")
    assert paths == [*expected, f"/1/playlist/{playlist_mbid}"]
    provider.logger.debug.assert_any_call(
        "Similar-users Jam build completed: user=%s, similarity=%s, date=%s, tracks=%d",
        "m0bb" if first_has_jam else "TheDeadite",
        0.53 if first_has_jam else 0.51,
        "2026-09-14T00:00:00+00:00",
        2,
    )


@pytest.mark.parametrize("has_users", [True, False])
async def test_similar_users_jam_without_available_jams_is_empty(has_users: bool) -> None:
    """An absent Jam is a valid empty result, not an API failure."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.get_setup_value.return_value = "listener"
    provider.api_client = MagicMock()
    provider.api_client.get_similar_users = AsyncMock(
        return_value=([{"user_name": "closest", "similarity": 0.9}] if has_users else [])
    )
    provider.api_client.get_weekly_jams = AsyncMock(return_value=[])

    folder, playlists = await ListenBrainzRecommendationsProvider._build_folder_similar_users_jam(
        provider
    )

    assert not folder.items
    assert not playlists
    provider._resolve_generated_playlist.assert_not_awaited()


async def test_similar_users_api_error_aborts_instead_of_publishing_empty_data() -> None:
    """Keep transport errors distinct from users that simply have no Jam."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.get_setup_value.return_value = "listener"
    provider.api_client = MagicMock()
    provider.api_client.get_similar_users = AsyncMock(side_effect=MusicAssistantError("API failed"))

    with pytest.raises(MusicAssistantError, match="API failed"):
        await ListenBrainzRecommendationsProvider._build_folder_similar_users_jam(provider)


async def test_playlist_tracks_load_full_playlist() -> None:
    """Fetch recordings separately from the recommendation metadata listing."""
    client = object.__new__(ListenBrainzAPIClient)
    tracks = [{"title": "Track", "creator": "Artist"}]
    client._cached_request = AsyncMock(return_value={"playlist": {"track": tracks}})

    assert await client.get_playlist_tracks("playlist-id") is tracks
    client._cached_request.assert_awaited_once_with("/1/playlist/playlist-id", {})


async def test_discovery_requests_use_server_cache() -> None:
    """Reuse cached API responses rather than repeating external requests."""
    client = object.__new__(ListenBrainzAPIClient)
    client.base_url = "https://api.listenbrainz.org"
    client.provider = MagicMock()
    client.provider.instance_id = "listenbrainz-instance"
    data = {"payload": {"artists": []}}
    client.provider.mass.cache.get = AsyncMock(side_effect=[None, data])
    client.provider.mass.cache.set = AsyncMock()
    client._throttler = MagicMock()
    client._request = AsyncMock(return_value=data)

    assert await client._cached_request("/charts", {"range": "week"}) == data
    assert await client._cached_request("/charts", {"range": "week"}) == data

    client._request.assert_awaited_once_with("/charts", {"range": "week"})
    client._throttler.__aenter__.assert_awaited_once()
    client.provider.mass.cache.set.assert_awaited_once_with(
        "https://api.listenbrainz.org/charts?range=week",
        data,
        expiration=10800,
        provider="listenbrainz-instance",
    )


async def test_existing_recommendations_keep_lb_radio() -> None:
    """Rename the folder without replacing LB-Radio with generated weekly playlists."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.get_setup_value.return_value = "listener"
    provider._get_lb_radio_mode.return_value = "easy"
    provider.api_client = MagicMock()
    provider.api_client.get_explore_lb_radio = AsyncMock(
        return_value=[{"title": "Track", "creator": "Artist"}]
    )
    provider._normalize_text.side_effect = ListenBrainzRecommendationsProvider._normalize_text
    track = Track(item_id="track", provider="library", name="Track", provider_mappings=set())
    provider.resolve_provider_track_by_name.return_value = track

    folder = await ListenBrainzRecommendationsProvider._build_folder_recommendations(provider)

    assert folder.item_id == "user_recommendations"
    assert folder.items == [track]
    provider.api_client.get_explore_lb_radio.assert_awaited_once_with(
        prompt="recs:listener::unlistened", mode="easy"
    )


@pytest.mark.parametrize(
    ("builder", "folder_id"),
    [
        ("_build_folder_top_artists", "user_top_artists_monthly"),
        ("_build_folder_top_tracks", "user_top_tracks_monthly"),
        ("_build_folder_recent_listens", "user_recent_artists"),
    ],
)
async def test_existing_user_folders_keep_sources(builder: str, folder_id: str) -> None:
    """Keep monthly statistics and the last 100 listens without new date filters."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.get_setup_value.return_value = "listener"
    provider.api_client = MagicMock()
    provider.api_client.get_user_top_artists = AsyncMock(return_value=[])
    provider.api_client.get_user_top_tracks = AsyncMock(return_value=[])
    provider.api_client.get_user_listens = AsyncMock(return_value=[])

    folder = await getattr(ListenBrainzRecommendationsProvider, builder)(provider)

    assert folder.item_id == folder_id
    if builder == "_build_folder_recent_listens":
        provider.api_client.get_user_listens.assert_awaited_once_with(
            username="listener", count=100
        )
    else:
        method = (
            provider.api_client.get_user_top_artists
            if builder == "_build_folder_top_artists"
            else provider.api_client.get_user_top_tracks
        )
        assert method.call_args.kwargs["stats_ranges"] == ["month", "this_month"]


async def test_exploration_folder_exposes_playable_playlists() -> None:
    """Keep playlist track order and expose tracks through the catalog."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._resolve_generated_playlist = MethodType(
        ListenBrainzRecommendationsProvider._resolve_generated_playlist, provider
    )
    provider.instance_id = "listenbrainz-instance"
    provider.get_setup_value.return_value = "listener"
    provider.api_client = MagicMock()
    playlist_mbid = "7c05e920-b9be-4b05-a583-a97ad78eae00"
    provider.api_client.get_weekly_exploration = AsyncMock(
        return_value=[
            {
                "identifier": f"https://listenbrainz.org/playlist/{playlist_mbid}",
                "title": "Weekly Exploration",
            }
        ]
    )
    provider.api_client.get_playlist_tracks = AsyncMock(
        return_value=[
            {"title": "Second", "creator": "Artist"},
            {"title": "First", "creator": "Artist"},
        ]
    )
    tracks = [
        Track(item_id=name, provider="library", name=name, provider_mappings=set())
        for name in ("Second", "First")
    ]
    provider.resolve_provider_track_by_name.side_effect = tracks
    playlist = Playlist(
        item_id=f"user_exploration_{playlist_mbid}",
        provider=provider.instance_id,
        name="Weekly Exploration",
        provider_mappings=set(),
    )
    provider._create_virtual_playlist.return_value = playlist

    folder, playlists = await ListenBrainzRecommendationsProvider._build_folder_exploration(
        provider
    )

    assert folder.item_id == "user_exploration_weekly"
    assert folder.items == [playlist]
    assert playlists[playlist.item_id] == (playlist, tracks)
    provider._resolved_virtual_playlists = playlists
    assert (
        await ListenBrainzRecommendationsProvider.get_playlist_tracks(provider, playlist.item_id)
        == tracks
    )
    provider.api_client.get_playlist_tracks.assert_awaited_once_with(playlist_mbid)
    provider.logger.debug.assert_any_call("Weekly Exploration playlist rows received: %d", 1)
    provider.logger.debug.assert_any_call(
        "%s playlist %s: %d track candidates, %d tracks resolved",
        "Exploration",
        playlist_mbid,
        2,
        2,
    )
    provider.logger.debug.assert_any_call(
        "Weekly Exploration build completed: %d playlist candidates, %d playlists created", 1, 1
    )


@pytest.mark.parametrize(
    ("entity", "folder_id", "item_class", "resolver", "title_field"),
    [
        (
            "artists",
            "sitewide_top_artists_weekly",
            Artist,
            "resolve_provider_artist_by_name",
            "artist_name",
        ),
        (
            "recordings",
            "sitewide_top_tracks_weekly",
            Track,
            "resolve_provider_track_by_name",
            "track_name",
        ),
        (
            "release-groups",
            "sitewide_top_albums_weekly",
            Album,
            "resolve_provider_album_by_name",
            "release_group_name",
        ),
    ],
)
async def test_sitewide_folders_resolve_playable_items(
    entity: str,
    folder_id: str,
    item_class: type[Artist | Track | Album],
    resolver: str,
    title_field: str,
) -> None:
    """Return connected-provider items, not unplayable ListenBrainz placeholders."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.api_client = MagicMock()
    provider.api_client.get_sitewide_top_items = AsyncMock(
        return_value=[
            {
                "artist_name": "Artist",
                title_field: "Title",
            }
        ]
    )
    item = item_class(item_id="item", provider="library", name="Title", provider_mappings=set())
    getattr(provider, resolver).return_value = item

    folder = await ListenBrainzRecommendationsProvider._build_folder_sitewide_chart(
        provider, entity, folder_id, "Chart"
    )

    assert folder.item_id == folder_id
    assert folder.items == [item]
    getattr(provider, resolver).assert_awaited_once()
    provider.logger.debug.assert_any_call("Sitewide %s chart rows received: %d", entity, 1)
    provider.logger.debug.assert_any_call(
        "Sitewide %s charts build completed: %d candidates, %d items resolved", entity, 1, 1
    )


async def test_new_release_folder_resolves_albums() -> None:
    """Build the new-release folder from fresh releases rather than release charts."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.api_client = MagicMock()
    provider.api_client.get_fresh_releases = AsyncMock(
        return_value=[
            {
                "release_name": "Album",
                "artist_credit_name": "Artist",
            }
        ]
    )
    album = Album(item_id="album", provider="library", name="Album", provider_mappings=set())
    provider.resolve_provider_album_by_name.return_value = album

    folder = await ListenBrainzRecommendationsProvider._build_folder_fresh_releases(provider)

    assert folder.item_id == "sitewide_releases_weekly"
    assert folder.items == [album]
    provider.resolve_provider_album_by_name.assert_awaited_once_with("Artist", "Album")
    provider.logger.debug.assert_any_call(
        "Fresh release rows received: %d; selected %d candidates (limit=%d)", 1, 1, 30
    )
    provider.logger.debug.assert_any_call(
        "Fresh releases build completed: %d albums resolved, %d albums included (limit=%d)",
        1,
        1,
        15,
    )


async def test_album_resolution_requires_matching_artist_and_title() -> None:
    """Avoid returning unrelated albums when a search has no exact match."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.mass = MagicMock()
    provider._normalize_text.side_effect = ListenBrainzRecommendationsProvider._normalize_text
    artist = Artist(item_id="artist", provider="library", name="Artist", provider_mappings=set())
    wrong = Album(item_id="wrong", provider="library", name="Other", provider_mappings=set())
    correct = Album(
        item_id="correct",
        provider="library",
        name="Album",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    provider.mass.music.search = AsyncMock()
    provider.mass.music.search.return_value.albums = [wrong, correct]

    result = await ListenBrainzRecommendationsProvider.resolve_provider_album_by_name(
        provider, "Artist", "Album"
    )

    assert result is correct
    assert provider.mass.music.search.call_args.kwargs["media_types"] == [MediaType.ALBUM]
    provider.mass.music.search.return_value.albums = [wrong]
    assert (
        await ListenBrainzRecommendationsProvider.resolve_provider_album_by_name(
            provider, "Artist", "Album"
        )
        is None
    )


async def test_build_recommendations_includes_all_target_folders() -> None:
    """Publish the renamed user folders and all five new discovery folders."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider._run_build_step = MethodType(
        ListenBrainzRecommendationsProvider._run_build_step, provider
    )
    item = Track(item_id="track", provider="library", name="Track", provider_mappings=set())

    def folder(folder_id: str) -> RecommendationFolder:
        return RecommendationFolder(
            item_id=folder_id,
            provider=provider.instance_id,
            name=folder_id,
            items=UniqueList([item]),
        )

    single_builders = {
        "_build_folder_top_artists": "user_top_artists_monthly",
        "_build_folder_top_tracks": "user_top_tracks_monthly",
        "_build_folder_recent_listens": "user_recent_artists",
        "_build_folder_recommendations": "user_recommendations",
        "_build_folder_fresh_releases": "sitewide_releases_weekly",
    }
    for builder, folder_id in single_builders.items():
        getattr(provider, builder).return_value = folder(folder_id)
    provider._build_folder_similar_artists.return_value = [
        folder("user_similar_artists_seed_monthly")
    ]
    provider._build_folder_genre_artists.return_value = [folder("user_genre_artists_rock_monthly")]
    for builder, folder_id in (
        ("_build_folder_genre_playlists", "user_genre_playlists_monthly"),
        ("_build_folder_artist_playlists", "user_artist_playlists_monthly"),
        ("_build_folder_exploration", "user_exploration_weekly"),
        ("_build_folder_similar_users_jam", "similarusers_jam_weekly"),
    ):
        getattr(provider, builder).return_value = (folder(folder_id), {})
    provider._build_folder_sitewide_chart.side_effect = lambda _entity, folder_id, _name: folder(
        folder_id
    )

    folders, _playlists = await ListenBrainzRecommendationsProvider._build_recommendations(provider)

    assert {entry.item_id for entry in folders} == {
        *single_builders.values(),
        "user_similar_artists_seed_monthly",
        "user_genre_artists_rock_monthly",
        "user_genre_playlists_monthly",
        "user_artist_playlists_monthly",
        "user_exploration_weekly",
        "similarusers_jam_weekly",
        "sitewide_top_tracks_weekly",
        "sitewide_top_albums_weekly",
        "sitewide_top_artists_weekly",
    }


async def test_discovery_resolution_limits_concurrent_searches() -> None:
    """Resolve a chart in bounded batches while preserving ranking order."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.api_client = MagicMock()
    provider.api_client.get_sitewide_top_items = AsyncMock(
        return_value=[{"track_name": str(index), "artist_name": "Artist"} for index in range(12)]
    )
    active = 0
    maximum = 0

    async def resolve_track(_artist: str, name: str) -> Track:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return Track(item_id=name, provider="library", name=name, provider_mappings=set())

    provider.resolve_provider_track_by_name.side_effect = resolve_track

    folder = await ListenBrainzRecommendationsProvider._build_folder_sitewide_chart(
        provider, "recordings", "sitewide_top_tracks_weekly", "Top Tracks"
    )

    assert maximum == 5
    assert [item.name for item in folder.items] == [str(index) for index in range(12)]


async def test_fresh_releases_limit_unresolved_candidates() -> None:
    """Avoid searching the entire global release catalogue when nothing matches."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.api_client = MagicMock()
    provider.api_client.get_fresh_releases = AsyncMock(
        return_value=[
            {"release_name": str(index), "artist_credit_name": "Artist"} for index in range(100)
        ]
    )
    provider.resolve_provider_album_by_name.return_value = None

    folder = await ListenBrainzRecommendationsProvider._build_folder_fresh_releases(provider)

    assert not folder.items
    assert provider.resolve_provider_album_by_name.await_count == 30
    provider.logger.debug.assert_any_call(
        "Fresh release rows received: %d; selected %d candidates (limit=%d)", 100, 30, 30
    )
    provider.logger.debug.assert_any_call(
        "Fresh releases build completed: %d albums resolved, %d albums included (limit=%d)",
        0,
        0,
        15,
    )


@pytest.mark.parametrize("error", [MusicAssistantError("API failed"), ValueError("Invalid data")])
async def test_failed_build_step_does_not_return_empty_replacement(error: Exception) -> None:
    """Abort a refresh when a folder builder fails instead of publishing defaults."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    builder = AsyncMock(side_effect=error)

    with pytest.raises(type(error), match=str(error)):
        await ListenBrainzRecommendationsProvider._run_build_step(provider, "charts", builder)


async def test_refresh_keeps_published_data_until_complete() -> None:
    """Keep folders and playlist tracks readable while their replacements are built."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._refresh_lock = asyncio.Lock()
    old_track = Track(item_id="old", provider="library", name="Old", provider_mappings=set())
    new_track = Track(item_id="new", provider="library", name="New", provider_mappings=set())
    old_folder = RecommendationFolder(
        item_id="user_recommendations",
        provider="listenbrainz",
        name="Recommendations",
        items=UniqueList([old_track]),
    )
    new_folder = RecommendationFolder(
        item_id=old_folder.item_id,
        provider=old_folder.provider,
        name=old_folder.name,
        items=UniqueList([new_track]),
    )
    playlist = Playlist(
        item_id="playlist",
        provider="listenbrainz",
        name="Playlist",
        provider_mappings=set(),
    )
    old_folders = [old_folder]
    old_playlists = {playlist.item_id: (playlist, [old_track])}
    new_folders = [new_folder]
    new_playlists = {playlist.item_id: (playlist, [new_track])}
    provider._recommendation_folders = old_folders
    provider._resolved_virtual_playlists = old_playlists
    started = asyncio.Event()
    complete = asyncio.Event()

    async def build() -> tuple[list[RecommendationFolder], dict[str, tuple[Playlist, list[Track]]]]:
        started.set()
        await complete.wait()
        return new_folders, new_playlists

    provider._build_recommendations.side_effect = build
    task = asyncio.create_task(
        ListenBrainzRecommendationsProvider._refresh_recommendations(provider)
    )
    try:
        await started.wait()
        assert provider._recommendation_folders is old_folders
        assert provider._resolved_virtual_playlists is old_playlists
        assert await ListenBrainzRecommendationsProvider.get_recommendation_items(
            provider, old_folder.item_id
        ) == [old_track]
        assert await ListenBrainzRecommendationsProvider.get_playlist_tracks(
            provider, playlist.item_id
        ) == [old_track]
    finally:
        complete.set()
        await task

    assert provider._recommendation_folders is new_folders
    assert provider._resolved_virtual_playlists is new_playlists


@pytest.mark.parametrize("error", [MusicAssistantError("API failed"), ValueError("Invalid data")])
async def test_failed_refresh_keeps_previous_snapshot(error: Exception) -> None:
    """Do not replace either published collection after a failed rebuild."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._refresh_lock = asyncio.Lock()
    old_folders = [
        RecommendationFolder(
            item_id="user_recommendations",
            provider="listenbrainz",
            name="Recommendations",
        )
    ]
    old_playlists = {"playlist": MagicMock()}
    provider._recommendation_folders = old_folders
    provider._resolved_virtual_playlists = old_playlists
    provider._build_recommendations.side_effect = error

    await ListenBrainzRecommendationsProvider._refresh_recommendations(provider)

    assert provider._recommendation_folders is old_folders
    assert provider._resolved_virtual_playlists is old_playlists


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("get_explore_lb_radio", {"prompt": "recs:listener::unlistened", "mode": "easy"}),
        (
            "get_lb_radio_artist",
            {
                "seed_artist_mbid": "seed",
                "mode": "easy",
                "max_similar_artists": 5,
                "max_recordings_per_artist": 1,
                "pop_begin": 75,
                "pop_end": 100,
            },
        ),
        ("get_lb_radio_tags", {"tags": ["rock"], "pop_begin": 75, "pop_end": 100}),
    ],
)
async def test_lb_radio_errors_abort_the_build(method: str, kwargs: dict[str, object]) -> None:
    """Do not turn failed radio requests into valid empty or partial folders."""
    client = object.__new__(ListenBrainzAPIClient)
    client._request = AsyncMock(side_effect=MusicAssistantError("Radio failed"))

    with pytest.raises(MusicAssistantError, match="Radio failed"):
        await getattr(client, method)(**kwargs)


@pytest.mark.parametrize(
    "method", ["resolve_provider_artist_by_name", "resolve_provider_track_by_name"]
)
async def test_search_errors_abort_the_build(method: str) -> None:
    """Distinguish failed searches from successful searches with no match."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._artist_cache = {}
    provider._normalize_text.side_effect = ListenBrainzRecommendationsProvider._normalize_text
    provider.mass = MagicMock()
    provider.mass.music.search = AsyncMock(side_effect=MusicAssistantError("Search failed"))
    args = ("Artist",) if method == "resolve_provider_artist_by_name" else ("Artist", "Track")

    with pytest.raises(MusicAssistantError, match="Search failed"):
        await getattr(ListenBrainzRecommendationsProvider, method)(provider, *args)


@pytest.mark.parametrize("error", [MusicAssistantError("Radio failed"), ValueError("Invalid data")])
async def test_genre_artist_errors_abort_the_build(error: Exception) -> None:
    """Do not publish the earlier genres when a later genre fails."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.get_setup_value.return_value = "listener"
    provider._get_lb_radio_pop_range.return_value = (75, 100)
    provider.api_client = MagicMock()
    provider.api_client.get_user_genre_activity = AsyncMock(return_value=[{"genre": "rock"}])
    provider.api_client.get_lb_radio_tags = AsyncMock(side_effect=error)

    with pytest.raises(type(error), match=str(error)):
        await ListenBrainzRecommendationsProvider._build_folder_genre_artists(provider)


async def test_unavailable_statistics_are_not_empty_success() -> None:
    """Treat statistics still being calculated as a failed refresh."""
    client = object.__new__(ListenBrainzAPIClient)
    client.base_url = "https://api.listenbrainz.org"
    client.provider = MagicMock()
    response = client.provider.mass.http_session.get.return_value.__aenter__.return_value
    response.status = 204
    response.text = AsyncMock(return_value="")

    with pytest.raises(MusicAssistantError, match="204"):
        await client._request("/1/stats/sitewide/artists", {})


async def test_late_build_failure_does_not_publish_earlier_results() -> None:
    """Exercise the real build pipeline when its final step fails."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz"
    provider._refresh_lock = asyncio.Lock()
    provider._run_build_step = MethodType(
        ListenBrainzRecommendationsProvider._run_build_step, provider
    )
    provider._build_recommendations = MethodType(
        ListenBrainzRecommendationsProvider._build_recommendations, provider
    )
    empty_folder = RecommendationFolder(item_id="empty", provider="listenbrainz", name="Empty")
    for builder in (
        "_build_folder_top_artists",
        "_build_folder_top_tracks",
        "_build_folder_recent_listens",
        "_build_folder_recommendations",
        "_build_folder_sitewide_chart",
    ):
        getattr(provider, builder).return_value = empty_folder
    provider._build_folder_similar_artists.return_value = []
    provider._build_folder_genre_artists.return_value = []
    for builder in (
        "_build_folder_genre_playlists",
        "_build_folder_artist_playlists",
        "_build_folder_exploration",
        "_build_folder_similar_users_jam",
    ):
        getattr(provider, builder).return_value = (empty_folder, {})
    provider._build_folder_fresh_releases.side_effect = MusicAssistantError("Final step failed")
    old_folders = [RecommendationFolder(item_id="old", provider="listenbrainz", name="Old")]
    old_playlists = {"old": MagicMock()}
    provider._recommendation_folders = old_folders
    provider._resolved_virtual_playlists = old_playlists

    await ListenBrainzRecommendationsProvider._refresh_recommendations(provider)

    provider._build_folder_fresh_releases.assert_awaited_once()
    assert provider._recommendation_folders is old_folders
    assert provider._resolved_virtual_playlists is old_playlists


async def test_cancelled_refresh_keeps_previous_snapshot() -> None:
    """Cancellation must not publish partially constructed state or retain the lock."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._refresh_lock = asyncio.Lock()
    old_folders: list[RecommendationFolder] = []
    old_playlists: dict[str, tuple[Playlist, list[Track]]] = {}
    provider._recommendation_folders = old_folders
    provider._resolved_virtual_playlists = old_playlists
    started = asyncio.Event()

    async def build() -> None:
        started.set()
        await asyncio.Event().wait()

    provider._build_recommendations.side_effect = build
    task = asyncio.create_task(
        ListenBrainzRecommendationsProvider._refresh_recommendations(provider)
    )
    try:
        await started.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert provider._recommendation_folders is old_folders
    assert provider._resolved_virtual_playlists is old_playlists
    assert not provider._refresh_lock.locked()


async def test_concurrent_refreshes_build_serially() -> None:
    """A second refresh must wait until the first has committed its snapshot."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider._refresh_lock = asyncio.Lock()
    provider._recommendation_folders = []
    provider._resolved_virtual_playlists = {}
    started = [asyncio.Event(), asyncio.Event()]
    complete = [asyncio.Event(), asyncio.Event()]
    snapshots = [
        [RecommendationFolder(item_id=str(index), provider="listenbrainz", name=str(index))]
        for index in range(2)
    ]
    calls = 0

    async def build() -> tuple[list[RecommendationFolder], dict[str, tuple[Playlist, list[Track]]]]:
        nonlocal calls
        index = calls
        calls += 1
        started[index].set()
        await complete[index].wait()
        return snapshots[index], {}

    provider._build_recommendations.side_effect = build
    first = asyncio.create_task(
        ListenBrainzRecommendationsProvider._refresh_recommendations(provider)
    )
    tasks = [first]
    try:
        await started[0].wait()
        tasks.append(
            asyncio.create_task(
                ListenBrainzRecommendationsProvider._refresh_recommendations(provider)
            )
        )
        await asyncio.sleep(0)
        assert calls == 1
        assert not started[1].is_set()
        complete[0].set()
        await started[1].wait()
        assert provider._recommendation_folders is snapshots[0]
    finally:
        for event in complete:
            event.set()
        await asyncio.gather(*tasks)

    assert calls == 2
    assert provider._recommendation_folders is snapshots[1]


async def test_new_cover_does_not_overwrite_published_cover() -> None:
    """Use different image paths while an existing playlist remains published."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz"
    provider.domain = "listenbrainz_recommendations"
    provider.mass = MagicMock()
    provider.mass.cache_path = "cache"

    with patch.object(asyncio, "to_thread", new_callable=AsyncMock) as write:
        previous = await ListenBrainzRecommendationsProvider._create_virtual_playlist(
            provider, "playlist", "Playlist", b"old-cover"
        )
        replacement = await ListenBrainzRecommendationsProvider._create_virtual_playlist(
            provider, "playlist", "Playlist", b"new-cover"
        )

    assert previous.metadata.images[0].path != replacement.metadata.images[0].path
    assert write.call_args_list[1].args[1] != write.call_args_list[3].args[1]
    for playlist in (previous, replacement):
        image = playlist.metadata.images[0]
        assert image.provider == provider.instance_id
        assert image.path.startswith("lb_playlist_")
        assert "/" not in image.path
        assert not image.remotely_accessible


async def test_generated_cover_resolves_through_provider(tmp_path: Path) -> None:
    """Read a generated cover using the image reference published by the provider."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.instance_id = "listenbrainz-instance"
    provider.domain = "listenbrainz_recommendations"
    provider.mass = MagicMock()
    provider.mass.cache_path = str(tmp_path)
    cover_bytes = b"generated-cover"

    playlist = await ListenBrainzRecommendationsProvider._create_virtual_playlist(
        provider, "playlist", "Playlist", cover_bytes
    )
    image = playlist.metadata.images[0]

    assert image.provider == provider.instance_id
    assert await ListenBrainzRecommendationsProvider.resolve_image(provider, image.path) == (
        cover_bytes
    )


@pytest.mark.parametrize("is_absolute", [False, True])
async def test_resolve_cover_rejects_paths_outside_cache(
    tmp_path: Path, is_absolute: bool
) -> None:
    """Refuse existing files outside the cover directory."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.mass = MagicMock()
    provider.mass.cache_path = str(tmp_path)
    outside_file = tmp_path / "outside.png"
    outside_file.write_bytes(b"private-file")
    image_path = str(outside_file) if is_absolute else "../outside.png"

    with pytest.raises(FileNotFoundError, match="Invalid ListenBrainz cover path"):
        await ListenBrainzRecommendationsProvider.resolve_image(provider, image_path)


async def test_resolve_missing_cover_raises(tmp_path: Path) -> None:
    """Report missing generated covers without falling back to another image source."""
    provider = MagicMock(spec=ListenBrainzRecommendationsProvider)
    provider.mass = MagicMock()
    provider.mass.cache_path = str(tmp_path)

    with pytest.raises(FileNotFoundError, match="ListenBrainz cover not found"):
        await ListenBrainzRecommendationsProvider.resolve_image(provider, "missing.png")
