"""Bounded generated collections and dynamic playlist contracts."""

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, Artist, Track, UniqueList
from music_assistant_models.statistics import TopItemResult

from music_assistant.providers.lastfm_recommendations_enhanced import (
    collections as collections_module,
)
from music_assistant.providers.lastfm_recommendations_enhanced import (
    recommendations as recommendations_module,
)
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMItem
from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    GenreProfile,
    genre_for_tag,
)
from music_assistant.providers.lastfm_recommendations_enhanced.collections import LastFMCollections
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import (
    LastFMDiscovery,
    PersonalSeed,
)


@pytest.fixture
def collections() -> LastFMCollections:
    """Keep the generator real and mock only its resolver and source boundaries."""
    discovery = Mock()
    discovery.get_seeds = AsyncMock(return_value=[PersonalSeed("Seed", weight=10)])
    discovery.get_known_items = AsyncMock(return_value=[])
    discovery.resolve_items = AsyncMock(return_value=UniqueList())
    discovery.provider.api.get_items = AsyncMock(return_value=[])
    discovery.provider.mass.music.get_controller.return_value.get_library_items_by_query = (
        AsyncMock(return_value=[])
    )
    return LastFMCollections(discovery)


@pytest.fixture
def artist_playlist_resolver(collections: LastFMCollections) -> LastFMDiscovery:
    """Use the real quota resolver with cached source identities supplied by each test."""
    resolver = LastFMDiscovery(collections.provider)
    resolver._resolve_missing = AsyncMock(
        side_effect=AssertionError("Cached source results require no external search")
    )
    collections.provider.mass.config.get_provider_configs = AsyncMock(return_value=[])
    collections.discovery.resolve_items = resolver.resolve_items
    return resolver


async def test_personal_artist_playlist_recipes_use_ten_ma_seeds(
    collections: LastFMCollections,
) -> None:
    """Ten weighted MA artists create ten distinct lightweight recipes, not fifteen."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed(f"Artist {index}", identity=f"artist://library/{index}") for index in range(15)
    ]
    recipes = await collections.get_playlist_specs("user_artist_playlists_monthly")
    assert len(recipes) == len({recipe.item_id for recipe in recipes}) == 10
    assert [recipe.artist for recipe in recipes] == [f"Artist {index}" for index in range(10)]
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=10)
    collections.provider.api.get_items.assert_not_awaited()


@pytest.mark.parametrize("artist_count", [0, 2, 15, 50])
async def test_global_artist_radios_create_up_to_fifteen_ranked_playlists(
    collections: LastFMCollections, artist_count: int
) -> None:
    """One chart response supplies at most fifteen public radio recipes in chart order."""
    collections.provider.api.get_items.return_value = [
        LastFMItem(f"Artist {rank}", mbid=f"artist-{rank}") for rank in range(artist_count)
    ]

    recipes = await collections.get_playlist_specs("global_artist_radio_playlists")

    assert len(recipes) == len({recipe.item_id for recipe in recipes}) == min(artist_count, 15)
    assert [recipe.artist for recipe in recipes] == [
        f"Artist {rank}" for rank in range(min(artist_count, 15))
    ]
    assert all(not recipe.personal for recipe in recipes)
    collections.provider.api.get_items.assert_awaited_once_with("chart.getTopArtists", limit=50)
    collections.discovery.get_seeds.assert_not_awaited()


@pytest.mark.parametrize("is_personal", [True, False])
async def test_artist_playlist_weights_randomizes_and_stops_before_next_source(
    collections: LastFMCollections,
    artist_playlist_resolver: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
    is_personal: bool,
) -> None:
    """Resolve each weighted random pool before fetching another artist, stopping at fifty."""
    monkeypatch.setattr(
        collections_module,
        "shuffle",
        lambda items: items.reverse(),
    )
    items: dict[str, Track] = {}
    batches: dict[str, list[LastFMItem]] = {}
    for name in ["Seed", "Strong", "Weak"]:
        artist = Artist(item_id=name, provider="library", name=name, provider_mappings=set())
        batches[name] = []
        for rank in range(50):
            track = Track(
                item_id=f"{name}-{rank}",
                provider="library",
                name=f"{name} {rank}",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
            items[track.name] = track
            batches[name].append(LastFMItem(track.name, name))
    artist_playlist_resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (items[candidate.name], False)
    )
    fetched = []

    async def get_items(
        method: str, parameters: dict[str, str] | None = None, *, limit: int
    ) -> list[LastFMItem]:
        assert limit == 50
        if method == "chart.getTopArtists":
            return [LastFMItem("Seed")]
        if method == "artist.getSimilar":
            return [
                LastFMItem("Weak", similarity_weight=0.25),
                LastFMItem("Strong", similarity_weight=0.75),
                LastFMItem("seed", similarity_weight=0.1),
                LastFMItem("STRONG", similarity_weight=0.5),
                LastFMItem("Zero", similarity_weight=0),
                LastFMItem("Invalid"),
            ]
        assert method == "artist.getTopTracks"
        assert parameters is not None
        name = parameters["artist"]
        assert (
            artist_playlist_resolver._get_cached_resolution.await_count
            == {"Seed": 0, "Strong": 25, "Weak": 44}[name]
        )
        fetched.append(name)
        return batches[name]

    collections.provider.api.get_items.side_effect = get_items
    folder_id = "user_artist_playlists_monthly" if is_personal else "global_artist_radio_playlists"
    recipe = (await collections.get_playlist_specs(folder_id))[0]
    result = await collections.get_playlist_tracks(recipe.item_id)
    assert fetched == ["Seed", "Strong", "Weak"]
    assert len(result) == len({track.uri for track in result}) == 50
    assert [sum(track.artists[0].name == name for track in result) for name in fetched] == [
        25,
        19,
        6,
    ]
    assert [track.name for track in result] == [
        *(f"Weak {rank}" for rank in range(15, 21)),
        *(f"Strong {rank}" for rank in range(6, 25)),
        *(f"Seed {rank}" for rank in range(25)),
    ]
    assert [track.position for track in result] == list(range(1, 51))
    assert batches["Seed"][0].name == "Seed 0"
    assert artist_playlist_resolver._get_cached_resolution.await_count == 50
    artist_playlist_resolver._resolve_missing.assert_not_awaited()
    assert collections.provider.api.get_items.await_count == (4 if is_personal else 6)
    if not is_personal:
        collections.discovery.get_seeds.assert_not_awaited()


@pytest.mark.parametrize("is_personal", [True, False])
async def test_artist_playlist_replaces_misses_and_duplicates_without_artist_cap(
    collections: LastFMCollections,
    artist_playlist_resolver: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
    is_personal: bool,
) -> None:
    """Zero-score neighbors get one slot each; missing sources and duplicates allow successors."""
    monkeypatch.setattr(
        collections_module,
        "shuffle",
        lambda _items: None,
    )
    items: dict[str, Track] = {}
    for name, count in [("Seed", 24), *((f"Neighbor {index}", 1) for index in range(50))]:
        artist = Artist(item_id=name, provider="library", name=name, provider_mappings=set())
        for rank in range(count):
            track = Track(
                item_id=f"{name}-{rank}",
                provider="library",
                name=f"{name} {rank}",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
            items[track.name] = track
    artist_playlist_resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (
            (items[candidate.name], False) if candidate.name in items else (None, True)
        )
    )
    fetched = []

    async def get_items(
        method: str, parameters: dict[str, str] | None = None, *, limit: int
    ) -> list[LastFMItem]:
        assert limit == 50
        if method == "chart.getTopArtists":
            return [LastFMItem("Seed")]
        if method == "artist.getSimilar":
            return [LastFMItem("Missing match")] + [
                LastFMItem(f"Neighbor {index}", similarity_weight=0) for index in range(49)
            ]
        assert method == "artist.getTopTracks"
        assert parameters is not None
        name = parameters["artist"]
        fetched.append(name)
        if name == "Seed":
            return [LastFMItem("Missing", name)] + [
                LastFMItem(f"Seed {rank}", name) for rank in range(24)
            ]
        if name == "Neighbor 0":
            return [
                LastFMItem("Seed 0", "Seed"),
                LastFMItem("Missing", name),
                LastFMItem(f"{name} 0", name),
                LastFMItem("Outside random pool", name),
            ]
        if name == "Neighbor 1":
            raise MediaNotFoundError("Artist not found")
        return [LastFMItem(f"{name} 0", name)]

    collections.provider.api.get_items.side_effect = get_items
    folder_id = "user_artist_playlists_monthly" if is_personal else "global_artist_radio_playlists"
    recipe = (await collections.get_playlist_specs(folder_id))[0]
    result = await collections.get_playlist_tracks(recipe.item_id)
    assert len(result) == len({track.uri for track in result}) == 50
    assert fetched == ["Seed", *(f"Neighbor {index}" for index in range(27))]
    assert sum(track.artists[0].name == "Seed" for track in result) == 24
    assert len({track.artists[0].name for track in result}) == 27
    assert artist_playlist_resolver._get_cached_resolution.await_count == 53
    artist_playlist_resolver._resolve_missing.assert_not_awaited()
    if not is_personal:
        collections.discovery.get_seeds.assert_not_awaited()


@pytest.mark.parametrize("is_personal", [True, False])
@pytest.mark.parametrize("error_type", [RateLimited, ResourceTemporarilyUnavailable])
async def test_artist_playlist_does_not_continue_after_source_errors(
    collections: LastFMCollections, error_type: type[Exception], is_personal: bool
) -> None:
    """Rate limits and temporary failures are not treated as ordinary absent artist tracks."""
    collections.provider.api.get_items.side_effect = [
        *([] if is_personal else [[LastFMItem("Seed")], [LastFMItem("Seed")]]),
        [LastFMItem("Neighbor", similarity_weight=0.5), LastFMItem("Tail", similarity_weight=0)],
        [],
        error_type("Unavailable"),
    ]
    folder_id = "user_artist_playlists_monthly" if is_personal else "global_artist_radio_playlists"
    recipe = (await collections.get_playlist_specs(folder_id))[0]
    with pytest.raises(error_type):
        await collections.get_playlist_tracks(recipe.item_id)
    assert collections.provider.api.get_items.await_count == (3 if is_personal else 5)


async def test_personal_album_sources_aggregate_match_weights_without_more_lookups(
    collections: LastFMCollections,
) -> None:
    """Combine seed self-weights and neighbor matches, without duplicate artists per seed."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed("First", weight=1),
        PersonalSeed("Second", weight=0.5),
    ]
    collections.provider.api.get_items.side_effect = [
        [
            LastFMItem("FIRST", similarity_weight=1),
            LastFMItem("Rank first", similarity_weight=0.1),
            LastFMItem("Shared", similarity_weight=0.5),
        ],
        [LastFMItem("Other", similarity_weight=1), LastFMItem("SHARED", similarity_weight=0.8)],
        [],
        [],
        [],
        [],
        [],
    ]
    await collections.get_personal_albums()
    calls = collections.provider.api.get_items.await_args_list
    assert [call.args[:2] for call in calls] == [
        ("artist.getSimilar", {"artist": "First"}),
        ("artist.getSimilar", {"artist": "Second"}),
        ("artist.getTopAlbums", {"artist": "First"}),
        ("artist.getTopAlbums", {"artist": "Shared"}),
        ("artist.getTopAlbums", {"artist": "Second"}),
        ("artist.getTopAlbums", {"artist": "Other"}),
        ("artist.getTopAlbums", {"artist": "Rank first"}),
    ]
    assert all(call.kwargs == {"limit": 50} for call in calls)


async def test_personal_albums_use_ten_seeds_and_rank_all_album_sources(
    collections: LastFMCollections,
) -> None:
    """Ten seeds and all their distinct neighbors can contribute without a three-source cutoff."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed(f"Seed {index}", weight=1) for index in range(15)
    ]

    async def get_items(method: str, parameters: dict[str, str], *, limit: int) -> list[LastFMItem]:
        assert limit == 50
        if method == "artist.getSimilar":
            index = int(parameters["artist"].split()[-1])
            return [LastFMItem(f"Neighbor {index}", similarity_weight=(index + 1) / 10)]
        assert method == "artist.getTopAlbums"
        return []

    collections.provider.api.get_items.side_effect = get_items
    await collections.get_personal_albums()
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=10)
    calls = collections.provider.api.get_items.await_args_list
    assert [call.args[1]["artist"] for call in calls if call.args[0] == "artist.getSimilar"] == [
        f"Seed {index}" for index in range(10)
    ]
    assert [call.args[1]["artist"] for call in calls if call.args[0] == "artist.getTopAlbums"] == [
        *(f"Seed {index}" for index in range(10)),
        *(f"Neighbor {index}" for index in range(9, -1, -1)),
    ]
    assert collections.discovery.resolve_items.await_args.kwargs == {
        "max_items": 1,
        "excluded_uris": set(),
    }


@pytest.mark.parametrize(
    ("artist_count", "quota"), [(8, 5), (5, 7), (1, 35), (18, 2), (12, 3), (9, 4), (35, 1)]
)
async def test_personal_album_quotas_use_random_top_ten_and_stop_before_next_artist(
    collections: LastFMCollections,
    artist_playlist_resolver: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
    artist_count: int,
    quota: int,
) -> None:
    """Shuffle the quota-sized top pool and resolve up to the quota before the final target."""
    monkeypatch.setattr(collections_module, "shuffle", lambda items: items.reverse())
    names = ["Seed", *(f"Artist {index}" for index in range(1, artist_count))]
    target_limit = 35
    pool_size = min(3 * quota, 10)
    per_artist = min(quota, pool_size)
    items: dict[str, Album] = {}
    batches: dict[str, list[LastFMItem]] = {}
    for name in names:
        artist = Artist(item_id=name, provider="library", name=name, provider_mappings=set())
        batches[name] = []
        for rank in range(50):
            album = Album(
                item_id=f"{name}-{rank}",
                provider="library",
                name=f"{name} {rank}",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
            items[album.name] = album
            batches[name].append(LastFMItem(album.name, name))
    artist_playlist_resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (items[candidate.name], False)
    )
    fetched = []

    async def get_items(method: str, parameters: dict[str, str], *, limit: int) -> list[LastFMItem]:
        assert limit == 50
        if method == "artist.getSimilar":
            return [
                *(LastFMItem(name, similarity_weight=1) for name in names[1:]),
                LastFMItem("seed", similarity_weight=1),
                LastFMItem(names[-1].upper(), similarity_weight=0.5),
            ]
        assert method == "artist.getTopAlbums"
        assert (
            artist_playlist_resolver._get_cached_resolution.await_count == len(fetched) * per_artist
        )
        fetched.append(parameters["artist"])
        return batches[parameters["artist"]]

    collections.provider.api.get_items.side_effect = get_items
    result = await collections.get_personal_albums()
    expected_artists = names[: (target_limit + per_artist - 1) // per_artist]
    assert fetched == expected_artists
    counts = [min(per_artist, target_limit - index * per_artist) for index in range(len(fetched))]
    assert len(result) == len({album.uri for album in result}) == sum(counts)
    assert [sum(album.artists[0].name == name for album in result) for name in fetched] == counts
    expected = [
        f"{name} {rank}"
        for name, count in reversed(list(zip(expected_artists, counts, strict=True)))
        for rank in range(pool_size - count, pool_size)
    ]
    assert [album.name for album in result] == expected
    assert batches["Seed"][0].name == "Seed 0"
    assert artist_playlist_resolver._get_cached_resolution.await_count == len(result)
    artist_playlist_resolver._resolve_missing.assert_not_awaited()


async def test_personal_albums_replace_misses_and_duplicates_only_within_top_ten(
    collections: LastFMCollections,
    artist_playlist_resolver: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace failures within the quota-sized pool, without falling back to rank eleven."""
    monkeypatch.setattr(collections_module, "shuffle", lambda _items: None)
    items: dict[str, Album] = {}
    for name, count in [("Seed", 8), *((f"Neighbor {index}", 1) for index in range(50))]:
        artist = Artist(item_id=name, provider="library", name=name, provider_mappings=set())
        for rank in range(count):
            album = Album(
                item_id=f"{name}-{rank}",
                provider="library",
                name=f"{name} {rank}",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
            items[album.name] = album
    artist_playlist_resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (
            (items[candidate.name], False) if candidate.name in items else (None, True)
        )
    )
    fetched = []

    async def get_items(method: str, parameters: dict[str, str], *, limit: int) -> list[LastFMItem]:
        assert limit == 50
        if method == "artist.getSimilar":
            return [LastFMItem(f"Neighbor {index}", similarity_weight=0) for index in range(50)]
        assert method == "artist.getTopAlbums"
        name = parameters["artist"]
        fetched.append(name)
        if name == "Seed":
            return [LastFMItem("Missing", name), LastFMItem("Seed 0", name)] + [
                LastFMItem(f"Seed {rank}", name) for rank in range(8)
            ]
        if name == "Neighbor 0":
            return [
                LastFMItem("Seed 0", "Seed"),
                LastFMItem("Missing", name),
                LastFMItem(f"{name} 0", name),
            ]
        if name == "Neighbor 1":
            raise MediaNotFoundError("Artist not found")
        if name == "Neighbor 2":
            return [LastFMItem("Missing", name)] * 10 + [LastFMItem(f"{name} 0", name)]
        return [LastFMItem(f"{name} 0", name)]

    collections.provider.api.get_items.side_effect = get_items
    result = await collections.get_personal_albums()
    assert len(result) == len({album.uri for album in result}) == 35
    assert fetched == ["Seed", *(f"Neighbor {index}" for index in range(29))]
    assert sum(album.artists[0].name == "Seed" for album in result) == 8
    assert len({album.artists[0].name for album in result}) == 28
    assert all(album.artists[0].name != "Neighbor 2" for album in result)
    assert artist_playlist_resolver._get_cached_resolution.await_count == 42
    artist_playlist_resolver._resolve_missing.assert_not_awaited()


@pytest.mark.parametrize("error_type", [RateLimited, ResourceTemporarilyUnavailable])
async def test_personal_album_selection_stops_after_source_errors(
    collections: LastFMCollections, error_type: type[Exception]
) -> None:
    """Cooldown and source failures must not trigger requests for lower-ranked artists."""
    collections.provider.api.get_items.side_effect = [
        [LastFMItem("Neighbor", similarity_weight=0.5), LastFMItem("Tail", similarity_weight=0)],
        [],
        error_type("Unavailable"),
    ]
    with pytest.raises(error_type):
        await collections.get_personal_albums()
    assert collections.provider.api.get_items.await_count == 3


async def test_personal_genre_artist_ranking_uses_matches_without_leaving_the_genre(
    collections: LastFMCollections,
) -> None:
    """Keep direct seed weights, add combined matches, and never inject outside artists."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed("Seed", weight=1),
        PersonalSeed("Second", weight=0.5),
    ]
    collections.provider.api.get_items.side_effect = [
        [
            LastFMItem("Rank first", similarity_weight=0.1),
            LastFMItem("Shared", similarity_weight=0.9),
            LastFMItem("Outside", similarity_weight=1),
            LastFMItem("Missing"),
            LastFMItem("Zero", similarity_weight=0),
        ],
        [LastFMItem("SHARED", similarity_weight=0.8)],
    ]
    result = await collections._rank_personal_artists(
        [LastFMItem(name) for name in ["Rank first", "Zero", "Seed", "Shared", "Missing"]]
    )
    assert [item.name for item in result] == ["Shared", "Seed", "Missing", "Rank first", "Zero"]
    assert collections.provider.api.get_items.await_count == 2
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=15)


async def test_personal_genre_artist_results_limit_all_profile_seeds_and_fill_target(
    collections: LastFMCollections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile's 15 seeds share the 5-artist quota, including seeds outside the ranking trio."""
    monkeypatch.setattr(recommendations_module, "shuffle", lambda _items: None)
    jazz = genre_for_tag("jazz")
    assert jazz is not None
    collections._genre_profile = GenreProfile((jazz,))
    items = [
        Artist(
            item_id=str(index), provider="library", name=f"Artist {index}", provider_mappings=set()
        )
        for index in range(40)
    ]
    seeds = [PersonalSeed(item.name, identity=item.uri) for item in items[:15]]
    collections.discovery.get_seeds.return_value = seeds
    candidates = [LastFMItem(item.name) for item in items]
    collections.provider.api.get_items.side_effect = lambda method, *_args, **_kwargs: (
        candidates if method == "tag.getTopArtists" else []
    )
    resolver = LastFMDiscovery(collections.provider)
    resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (
            next(item for item in items if item.name == candidate.name),
            False,
        )
    )
    collections.provider.mass.config.get_provider_configs = AsyncMock(return_value=[])
    collections.provider.mass.statistics.get_top_items = AsyncMock(
        return_value=[TopItemResult(item=item, play_count=1) for item in items[:16]]
    )
    collections.discovery.resolve_items = resolver.resolve_items
    result = await collections.get_folder_items(f"user_genre_artists_{jazz.key}")
    assert [item.uri for item in result] == [item.uri for item in [*items[:5], *items[15:35]]]
    collections.provider.mass.statistics.get_top_items.assert_not_awaited()
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=15)


@pytest.mark.parametrize(
    ("prefix", "method", "pool_size"),
    [
        ("user_genre_artists_", "tag.getTopArtists", 75),
        ("global_genre_artists_", "tag.getTopArtists", 75),
        ("global_genre_albums_", "tag.getTopAlbums", 105),
    ],
)
async def test_genre_rows_randomize_the_triple_sized_ranked_pool(
    collections: LastFMCollections,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    method: str,
    pool_size: int,
) -> None:
    """Genre discovery uses a bounded random pool, with personal ranking applied first."""
    monkeypatch.setattr(recommendations_module, "shuffle", lambda items: items.reverse())
    jazz = genre_for_tag("jazz")
    assert jazz is not None
    collections._genre_profile = GenreProfile((jazz,), explore_genres=(jazz,))
    collections._explore_genres = (jazz,)
    candidates = [LastFMItem(f"Candidate {rank}", "Artist") for rank in range(200)]
    collections.provider.api.get_items.return_value = candidates
    collections._rank_personal_artists = AsyncMock(return_value=candidates)
    await collections.get_folder_items(f"{prefix}{jazz.key}")
    collections.provider.api.get_items.assert_awaited_once_with(
        method, {"tag": "jazz"}, limit=pool_size
    )
    assert collections.discovery.resolve_items.await_args.args[0] == candidates[:pool_size][::-1]
    if prefix == "user_genre_artists_":
        collections._rank_personal_artists.assert_awaited_once_with(candidates)
    else:
        collections._rank_personal_artists.assert_not_awaited()
    assert candidates[0].name == "Candidate 0"


@pytest.mark.parametrize(
    ("prefix", "artist_limit"),
    [
        ("global_genre", None),
        ("global_mood", None),
        ("global_decade", None),
        ("user_genre", 10),
    ],
)
async def test_tag_playlists_randomize_one_large_pool_and_respect_family_artist_limits(
    collections: LastFMCollections,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    artist_limit: int | None,
) -> None:
    """Each tag playlist generates from 150 candidates with its own artist diversity limit."""
    monkeypatch.setattr(recommendations_module, "shuffle", lambda items: items.reverse())
    item_id = f"{prefix}_playlist_jazz"
    collections.get_playlist_spec = AsyncMock(
        return_value=Mock(artist="", tag="jazz", personal=prefix.startswith("user_"))
    )
    candidates = [LastFMItem(f"Candidate {rank}", "Artist") for rank in range(200)]
    collections.provider.api.get_items.return_value = candidates
    collections._rank_personal_genre_tracks = AsyncMock(return_value=candidates)
    await collections.get_playlist_tracks(item_id)
    collections.provider.api.get_items.assert_awaited_once_with(
        "tag.getTopTracks", {"tag": "jazz"}, limit=150
    )
    assert collections.discovery.resolve_items.await_args.args[0] == candidates[:150][::-1]
    assert collections.discovery.resolve_items.await_args.kwargs == {
        "max_tracks_per_artist": artist_limit,
    }
    assert candidates[0].name == "Candidate 0"


@pytest.mark.parametrize(
    ("folder_id", "tag"),
    [
        ("global_genre_playlists", "jazz"),
        ("global_mood_playlists", "chill"),
        ("global_decade_playlists", "1980s"),
    ],
)
@pytest.mark.parametrize("missing_count", [0, 20])
async def test_global_tag_playlists_fill_fifty_tracks_without_an_artist_cap(
    collections: LastFMCollections,
    artist_playlist_resolver: LastFMDiscovery,
    monkeypatch: pytest.MonkeyPatch,
    missing_count: int,
    folder_id: str,
    tag: str,
) -> None:
    """A single artist can fill the target; cached misses use reserves without exhausting the pool."""
    monkeypatch.setattr(recommendations_module, "shuffle", lambda _items: None)
    jazz = genre_for_tag("jazz")
    assert jazz is not None
    collections._explore_genres = (jazz,)
    artist = Artist(item_id="artist", provider="library", name="Artist", provider_mappings=set())
    tracks = [
        Track(
            item_id=str(rank),
            provider="library",
            name=f"Track {rank}",
            provider_mappings=set(),
            artists=UniqueList([artist]),
        )
        for rank in range(150)
    ]
    candidates = [LastFMItem(track.name, artist.name) for track in tracks]
    collections.provider.api.get_items.return_value = candidates
    items = {track.name: track for track in tracks[missing_count:]}
    artist_playlist_resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (items.get(candidate.name), True)
    )

    recipes = await collections.get_playlist_specs(folder_id)
    recipe = next(recipe for recipe in recipes if recipe.tag == tag)
    result = await collections.get_playlist_tracks(recipe.item_id)

    assert [track.uri for track in result] == [
        track.uri for track in tracks[missing_count : missing_count + 50]
    ]
    assert [track.position for track in result] == list(range(1, 51))
    assert artist_playlist_resolver._get_cached_resolution.await_count == missing_count + 50
    artist_playlist_resolver._resolve_missing.assert_not_awaited()
    collections.provider.api.get_items.assert_awaited_once_with(
        "tag.getTopTracks", {"tag": tag}, limit=150
    )


@pytest.mark.parametrize("folder_id", ["global_popular_artist_albums", "de_popular_artist_albums"])
async def test_mixed_collections_use_reserve_after_fifty_candidates_and_stop_at_target(
    collections: LastFMCollections,
    monkeypatch: pytest.MonkeyPatch,
    folder_id: str,
) -> None:
    """Ten artist quotas use top-ten pools, replace misses and stop before the final source."""
    monkeypatch.setattr(recommendations_module, "shuffle", lambda _items: None)
    monkeypatch.setattr(collections_module, "shuffle", lambda _items: None)
    collections.discovery.get_seeds.return_value = [PersonalSeed("Artist 0")]
    batches = []
    items: dict[str, Album] = {}
    for artist_index in range(10):
        artist = Artist(
            item_id=str(artist_index),
            provider="library",
            name=f"Artist {artist_index}",
            provider_mappings=set(),
        )
        batch = []
        for rank in range(50):
            item = Album(
                item_id=f"{artist_index}-{rank}",
                provider="library",
                name=f"Item {artist_index}-{rank}",
                provider_mappings=set(),
                artists=UniqueList([artist]),
            )
            items[item.name] = item
            batch.append(LastFMItem(item.name, artist.name))
        batches.append(batch)
    ordered = collections._interleave(batches)
    missing = {candidate.name for candidate in ordered[:50]}

    async def get_items(
        method: str, parameters: dict[str, str] | None = None, **kwargs: object
    ) -> list[LastFMItem]:
        assert kwargs == {"limit": 50}
        if method in {"chart.getTopArtists", "geo.getTopArtists"}:
            return [LastFMItem(f"Artist {index}") for index in range(50)]
        if method == "artist.getSimilar":
            return [LastFMItem("Artist 1"), LastFMItem("Artist 2")]
        assert parameters is not None
        return batches[int(parameters["artist"].split()[-1])]

    collections.provider.api.get_items.side_effect = get_items
    resolver = LastFMDiscovery(collections.provider)
    resolver._get_cached_resolution = AsyncMock(
        side_effect=lambda candidate, _media_type: (
            (None, True) if candidate.name in missing else (items[candidate.name], False)
        )
    )
    resolver._resolve_missing = AsyncMock(
        side_effect=AssertionError("Cached matches and misses require no external search")
    )
    collections.provider.mass.config.get_provider_configs = AsyncMock(return_value=[])
    collections.provider.mass.statistics.get_top_items = AsyncMock(return_value=[])
    collections.discovery.resolve_items = AsyncMock(wraps=resolver.resolve_items)
    result = await collections.get_folder_items(folder_id)
    target = 35
    assert len(ordered) == 500
    assert [item.uri for item in result] == [
        items[candidate.name].uri for batch in batches for candidate in batch[5:9]
    ][:target]
    calls = collections.discovery.resolve_items.await_args_list
    assert [len(call.args[0]) for call in calls] == [10] * 9
    assert [call.kwargs["max_items"] for call in calls] == [4] * 8 + [3]
    assert resolver._get_cached_resolution.await_count == 45 + target
    resolver._resolve_missing.assert_not_awaited()
    assert collections.provider.api.get_items.await_count == 10


@pytest.mark.parametrize("folder_id", ["global_mood_playlists", "global_decade_playlists"])
async def test_public_recipes_have_playable_identities_without_network(
    collections: LastFMCollections, folder_id: str
) -> None:
    """A playlist descriptor is lightweight; contents are generated only on demand."""
    recipes = await collections.get_playlist_specs(folder_id)
    assert recipes
    for recipe in recipes:
        assert await collections.get_playlist_spec(recipe.item_id) == recipe
    collections.provider.api.get_items.assert_not_awaited()


@pytest.mark.parametrize(
    ("key", "tag"),
    [
        ("love", "love"),
        ("motivation", "workout"),
        ("chill", "chill"),
        ("sad", "sad"),
        ("focused", "focus"),
        ("party", "party"),
    ],
)
async def test_flow_inspired_mood_recipes_use_lastfm_without_personal_sources(
    collections: LastFMCollections, key: str, tag: str
) -> None:
    """New moods resolve through the existing Last.fm recipe path without account coupling."""
    recipes = await collections.get_playlist_specs("global_mood_playlists")
    assert len(recipes) == 10
    recipe = await collections.get_playlist_spec(f"global_mood_playlist_{key}")
    assert recipe.tag == tag
    assert not recipe.personal
    await collections.get_playlist_tracks(recipe.item_id)
    collections.provider.api.get_items.assert_awaited_once_with(
        "tag.getTopTracks", {"tag": tag}, limit=150
    )
    collections.discovery.get_seeds.assert_not_awaited()
    assert collections.discovery.resolve_items.await_args.kwargs == {
        "max_tracks_per_artist": None,
    }
    collections.provider.mass.music.get_controller.return_value.get_library_items_by_query.assert_not_awaited()


async def test_playlist_terminates_after_first_page(collections: LastFMCollections) -> None:
    """No remote catalogue paging is hidden behind playlist enumeration."""
    assert await collections.get_playlist_tracks("global_genre_playlist_jazz", page=1) == []
    collections.provider.api.get_items.assert_not_awaited()
    jazz = genre_for_tag("jazz")
    assert jazz is not None
    collections = LastFMCollections(
        collections.discovery, genre_profile=GenreProfile((), explore_genres=(jazz,))
    )
    await collections.get_playlist_tracks(f"global_genre_playlist_{jazz.key}")
    collections.provider.api.get_items.assert_awaited_once_with(
        "tag.getTopTracks", {"tag": "jazz"}, limit=150
    )


async def test_private_recipe_does_not_survive_user_context_change(
    collections: LastFMCollections,
) -> None:
    """A user's artist selection cannot be obtained through another user's recipe lookup."""
    recipes = await collections.get_playlist_specs("user_artist_playlists_monthly")
    collections.discovery.get_seeds.return_value = []
    with pytest.raises(MediaNotFoundError):
        await collections.get_playlist_spec(recipes[0].item_id)


async def test_artist_playlist_identity_survives_a_display_name_change(
    collections: LastFMCollections,
) -> None:
    """Use MA identity rather than a user-visible name whenever that identity exists."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed("Original", identity="artist://library/1")
    ]
    before = await collections.get_playlist_specs("user_artist_playlists_monthly")
    collections.discovery.get_seeds.return_value = [
        PersonalSeed("Renamed", identity="artist://library/1")
    ]
    after = await collections.get_playlist_specs("user_artist_playlists_monthly")
    assert before[0].item_id == after[0].item_id
    assert before[0].name != after[0].name


async def test_personal_and_popular_albums_use_the_same_limit_constants(
    collections: LastFMCollections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both album paths honor changes to the shared seed and random pool limits."""
    monkeypatch.setattr(collections_module, "ALBUM_SEED_LIMIT", 2)
    monkeypatch.setattr(collections_module, "ALBUM_RANDOM_POOL_LIMIT", 4)
    monkeypatch.setattr(collections_module, "shuffle", lambda _items: None)
    collections.discovery.get_seeds.return_value = [
        PersonalSeed("First"),
        PersonalSeed("Second"),
        PersonalSeed("Tail"),
    ]

    async def get_items(
        method: str,
        parameters: dict[str, str],
        *,
        limit: int,
    ) -> list[LastFMItem]:
        assert limit == 50
        if method == "artist.getSimilar":
            return []
        if method == "chart.getTopArtists":
            return [LastFMItem(name) for name in ("First", "Second", "Tail")]
        assert method == "artist.getTopAlbums"
        name = parameters["artist"]
        return [LastFMItem(f"{name} {rank}", name) for rank in range(50)]

    collections.provider.api.get_items.side_effect = get_items
    await collections.get_personal_albums()
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=2)
    await collections.get_folder_items("global_popular_artist_albums")
    calls = collections.provider.api.get_items.await_args_list
    assert [call.args[1]["artist"] for call in calls if call.args[0] == "artist.getTopAlbums"] == [
        "First",
        "Second",
        "First",
        "Second",
    ]
    pools = [call.args[0] for call in collections.discovery.resolve_items.await_args_list]
    assert len(pools) == 4
    assert all(
        [candidate.name for candidate in pool] == [f"{pool[0].artist} {rank}" for rank in range(4)]
        for pool in pools
    )


@pytest.mark.parametrize("folder_id", ["global_popular_artist_albums", "de_popular_artist_albums"])
async def test_popular_albums_never_walk_discographies(
    collections: LastFMCollections, folder_id: str
) -> None:
    """Only ten fixed artist selections are requested, with no follow-up pages."""
    collections.provider.api.get_items.side_effect = [
        [LastFMItem(str(index)) for index in range(50)],
        *([[]] * 10),
    ]
    await collections.get_folder_items(folder_id)
    calls = collections.provider.api.get_items.await_args_list
    assert len(calls) == 11
    assert calls[0].args == (
        ("geo.getTopArtists", {"country": "Germany"})
        if folder_id.startswith("de_")
        else ("chart.getTopArtists", {})
    )
    assert all(
        call.args[0] == "artist.getTopAlbums" and call.kwargs["limit"] == 50 for call in calls[1:]
    )
    assert [call.args[1]["artist"] for call in calls[1:]] == [str(index) for index in range(10)]


@pytest.mark.parametrize("failure", [None, "empty", "missing"])
async def test_personal_genre_playlists_stop_after_ten_successes_in_weight_order(
    collections: LastFMCollections, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    """Rank genres before fetching tracks and only query a reserve after a failed genre."""
    categories = [genre_for_tag(f"Genre {index}") for index in range(15)]
    assert all(category is not None for category in categories)
    scores = [(category, float(15 - index)) for index, category in enumerate(categories)]
    monkeypatch.setattr(collections, "_genre_scores", AsyncMock(side_effect=[scores[::-1], []]))
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    track = Track(
        item_id="t",
        provider="library",
        name="Track",
        provider_mappings=set(),
        artists=UniqueList([artist]),
    )
    collections.discovery.resolve_items.return_value = UniqueList([track])
    requested: list[str] = []

    async def items(method: str, parameters: dict[str, str], **kwargs: object) -> list[LastFMItem]:
        if method != "tag.getTopTracks":
            return []
        assert collections._genre_profile is not None
        assert kwargs == {"limit": 150}
        tag = parameters["tag"]
        requested.append(tag)
        if tag == "genre 0" and failure == "missing":
            raise MediaNotFoundError("Missing genre")
        return [] if tag == "genre 0" and failure == "empty" else [LastFMItem("Track", "Artist")]

    collections.provider.api.get_items.side_effect = items
    collections.discovery.resolve_items.side_effect = lambda candidates, *_args, **_kwargs: (
        UniqueList([track]) if candidates else UniqueList()
    )
    completed = await collections.get_personal_genre_playlists()
    assert len(completed) == 10
    offset = int(failure is not None)
    assert [recipe.tag for recipe, _tracks in completed] == [
        f"genre {index}" for index in range(offset, offset + 10)
    ]
    assert requested == [f"genre {index}" for index in range(10 + offset)]


@pytest.mark.parametrize("error_type", [RateLimited, ResourceTemporarilyUnavailable])
async def test_personal_genre_playlists_do_not_replace_genres_during_source_failures(
    collections: LastFMCollections, error_type: type[Exception]
) -> None:
    """An upstream wait or unavailable source must stop the build, not query reserve genres."""
    jazz = genre_for_tag("jazz")
    rock = genre_for_tag("rock")
    assert jazz is not None
    assert rock is not None
    collections._genre_profile = GenreProfile((jazz, rock))
    collections.provider.api.get_items.side_effect = error_type("Wait")
    with pytest.raises(error_type):
        await collections.get_personal_genre_playlists()
    collections.provider.api.get_items.assert_awaited_once_with(
        "tag.getTopTracks", {"tag": "jazz"}, limit=150
    )


async def test_personal_genres_require_evidence(collections: LastFMCollections) -> None:
    """Local MA genres alone cannot supply a genre recommendation."""
    artist = Artist(item_id="local", provider="library", name="Seed", provider_mappings=set())
    artist.metadata.genres = {"Metal"}
    collections.discovery.get_known_items.return_value = [artist]
    assert await collections.get_genres() == []
    collections.provider.api.get_items.return_value = [LastFMItem("hip hop")]
    fresh = LastFMCollections(collections.discovery)
    assert await fresh.get_genres() == [genre_for_tag("hip-hop")]
    collections.discovery.get_known_items.assert_not_awaited()


async def test_genre_scores_normalize_per_artist_and_aggregate_personal_weights(
    collections: LastFMCollections,
) -> None:
    """One artist distributes its personal influence rather than gaining weight per tag."""
    collections.provider.api.get_items.side_effect = [
        [LastFMItem("Shoegaze", tag_weight=100), LastFMItem("Dream Pop", tag_weight=50)],
        [LastFMItem("Dream Pop", tag_weight=200)],
    ]
    ranked = await collections._genre_scores(
        [PersonalSeed("First", weight=12), PersonalSeed("Second", weight=4)]
    )
    scores = {category.tag: weight for category, weight in ranked}
    assert scores == pytest.approx({"shoegaze": 1, "dream pop": 1})
    assert sum(scores.values()) == pytest.approx(2)
    collections.discovery.get_known_items.assert_not_awaited()


async def test_genre_popularity_ranking_keeps_playlist_ids_stable(
    collections: LastFMCollections,
) -> None:
    """Tag counts determine genre order without changing identities when rankings swap."""

    async def items(
        method: str, _parameters: dict[str, str], **_kwargs: object
    ) -> list[LastFMItem]:
        if method == "artist.getTopTags":
            return [LastFMItem("Shoegaze", tag_weight=25), LastFMItem("Dream Pop", tag_weight=100)]
        return []

    collections.provider.api.get_items.side_effect = items
    before = await collections.get_playlist_specs("user_genre_playlists_monthly")
    assert [recipe.tag for recipe in before] == ["dream pop", "shoegaze"]

    async def changed_items(
        method: str, _parameters: dict[str, str], **_kwargs: object
    ) -> list[LastFMItem]:
        if method == "artist.getTopTags":
            return [LastFMItem("DREAM POP", tag_weight=25), LastFMItem("shoegaze", tag_weight=100)]
        return []

    collections.provider.api.get_items.side_effect = changed_items
    after = await LastFMCollections(collections.discovery).get_playlist_specs(
        "user_genre_playlists_monthly"
    )
    assert [recipe.tag for recipe in after] == ["shoegaze", "dream pop"]
    assert {recipe.tag: recipe.item_id for recipe in before} == {
        recipe.tag: recipe.item_id for recipe in after
    }


async def test_genre_scores_use_source_rank_when_counts_are_missing(
    collections: LastFMCollections,
) -> None:
    """Popularity order remains meaningful when tag weights are absent."""
    collections.provider.api.get_items.return_value = [
        LastFMItem("Shoegaze"),
        LastFMItem("Dream Pop"),
        LastFMItem("Jazz", tag_weight=0),
    ]
    scores = {
        category.tag: weight
        for category, weight in await collections._genre_scores(
            [PersonalSeed("External", weight=9)]
        )
    }
    assert scores == pytest.approx({"shoegaze": 1, "dream pop": 0.5})


async def test_genre_scores_filter_noise_and_deduplicate_aliases(
    collections: LastFMCollections,
) -> None:
    """Alias duplicates and non-genres cannot multiply an artist's influence."""
    collections.provider.api.get_items.return_value = [
        LastFMItem("seen live", tag_weight=100),
        LastFMItem("Hip Hop", tag_weight=80),
        LastFMItem("hip-hop", tag_weight=80),
        LastFMItem("sad", tag_weight=70),
        LastFMItem("1990s", tag_weight=60),
        LastFMItem("Shoegaze", tag_weight=40),
    ]
    scores = {
        category.tag: weight
        for category, weight in await collections._genre_scores(
            [PersonalSeed("External", weight=12)]
        )
    }
    assert scores == pytest.approx({"hip-hop": 1, "shoegaze": 0.5})


async def test_genre_profile_is_bounded_and_shared_by_all_genre_recipes(
    collections: LastFMCollections,
) -> None:
    """Fifteen artist lookups and five tags each bound the ranked genre reserve."""
    collections.discovery.get_seeds.return_value = [PersonalSeed(str(index)) for index in range(25)]

    async def items(method: str, parameters: dict[str, str], **_kwargs: object) -> list[LastFMItem]:
        if method == "artist.getTopTags":
            return [
                LastFMItem(f"Genre {parameters['artist']} {index}", tag_weight=100)
                for index in range(20)
            ]
        return []

    collections.provider.api.get_items.side_effect = items
    profile = await collections.get_genre_profile()
    assert len(profile.genres) == 75
    calls = collections.provider.api.get_items.await_args_list
    assert sum(call.args[0] == "artist.getTopTags" for call in calls) == 15
    assert len(calls) == 15
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=15)
    collections.provider.api.get_items.reset_mock()
    for folder_id, count in [("user_genre_playlists_monthly", 75)]:
        recipes = await collections.get_playlist_specs(folder_id)
        assert len(recipes) == count
        assert [recipe.tag for recipe in recipes] == [
            category.tag for category in profile.genres[:count]
        ]
        for recipe in recipes:
            assert await collections.get_playlist_spec(recipe.item_id) == recipe
    collections.provider.api.get_items.assert_not_awaited()


@pytest.mark.parametrize(
    "prefix", ["global_genre_artists", "global_genre_albums", "user_genre_artists"]
)
async def test_dynamic_genre_folder_routes_only_profile_ids(
    collections: LastFMCollections,
    prefix: str,
) -> None:
    """Unknown genre keys cannot inject an arbitrary tag into Last.fm calls."""
    category = genre_for_tag("Shoegaze")
    assert category is not None
    collections = LastFMCollections(
        collections.discovery, genre_profile=GenreProfile((category,), explore_genres=(category,))
    )
    assert not await collections.get_folder_items(f"{prefix}_unpublished")
    collections.provider.api.get_items.assert_not_awaited()
    await collections.get_folder_items(f"{prefix}_{category.key}")
    assert any(
        call.args[:2]
        == (
            "tag.getTopAlbums" if prefix == "global_genre_albums" else "tag.getTopArtists",
            {"tag": "shoegaze"},
        )
        for call in collections.provider.api.get_items.await_args_list
    )


async def test_global_explore_tags_are_bounded_deduplicated_and_not_personal(
    collections: LastFMCollections,
) -> None:
    """One global list supplies up to twenty Explore categories without personal seeds."""
    collections.discovery.get_seeds.side_effect = AssertionError("Explore must not read seeds")
    collections.provider.api.get_items.return_value = [
        LastFMItem("seen live"),
        LastFMItem("Hip Hop"),
        LastFMItem("hip-hop"),
        LastFMItem("sad"),
        *[LastFMItem(f"Genre {index}") for index in range(40)],
    ]
    categories = await collections.get_explore_genres()
    assert len(categories) == 20
    assert categories[0].tag == "hip-hop"
    assert len({category.key for category in categories}) == 20
    assert await collections.get_explore_genres() == categories
    collections.discovery.get_seeds.assert_not_awaited()
    collections.provider.api.get_items.assert_awaited_once_with("chart.getTopTags", limit=50)


@pytest.mark.parametrize(
    "folder", ["global_genre_playlists", "global_genre_artists", "global_genre_albums"]
)
async def test_explore_folders_never_query_personal_history(
    collections: LastFMCollections,
    folder: str,
) -> None:
    """Global genres control all three Explore paths independently of user favorites."""
    collections.discovery.get_seeds.side_effect = AssertionError("Explore must not read seeds")

    async def items(
        method: str, _parameters: dict[str, str] | None = None, **_kwargs: object
    ) -> list[LastFMItem]:
        return [LastFMItem("Afrobeat")] if method == "chart.getTopTags" else []

    collections.provider.api.get_items.side_effect = items
    category = genre_for_tag("Afrobeat")
    assert category is not None
    if folder == "global_genre_playlists":
        recipes = await collections.get_playlist_specs(folder)
        assert [recipe.tag for recipe in recipes] == ["afrobeat"]
        await collections.get_playlist_tracks(recipes[0].item_id)
    else:
        await collections.get_folder_items(f"{folder}_{category.key}")
    collections.discovery.get_seeds.assert_not_awaited()
    assert [call.args[0] for call in collections.provider.api.get_items.await_args_list] == [
        "chart.getTopTags",
        {
            "global_genre_playlists": "tag.getTopTracks",
            "global_genre_artists": "tag.getTopArtists",
            "global_genre_albums": "tag.getTopAlbums",
        }[folder],
    ]


async def test_global_genre_playlist_ids_use_scope_prefix_without_legacy_aliases(
    collections: LastFMCollections,
) -> None:
    """Global recipes have explicit identities and do not retain the old lookup paths."""
    category = genre_for_tag("Afrobeat")
    assert category is not None
    collections = LastFMCollections(
        collections.discovery, genre_profile=GenreProfile((), explore_genres=(category,))
    )
    recipes = await collections.get_playlist_specs("global_genre_playlists")
    assert [recipe.item_id for recipe in recipes] == [f"global_genre_playlist_{category.key}"]
    assert await collections.get_playlist_spec(recipes[0].item_id) == recipes[0]
    assert await collections.get_playlist_specs("genre_playlists") == []
    assert await collections.get_playlist_specs("top_genre_playlists") == []
    for prefix in ("genre_artists", "genre_albums", "top_genre_artists", "top_genre_albums"):
        assert not await collections.get_folder_items(f"{prefix}_{category.key}")
    with pytest.raises(MediaNotFoundError):
        await collections.get_playlist_spec(f"genre_playlist_{category.key}")
    with pytest.raises(MediaNotFoundError):
        await collections.get_playlist_spec(f"top_genre_playlist_{category.key}")
    collections.provider.api.get_items.assert_not_awaited()
    collections.discovery.get_seeds.assert_not_awaited()


async def test_empty_global_tags_do_not_fall_back_to_personal_genres(
    collections: LastFMCollections,
) -> None:
    """Missing Explore evidence must not turn general discovery into personal discovery."""
    assert await collections.get_playlist_specs("global_genre_playlists") == []
    collections.discovery.get_seeds.assert_not_awaited()


@pytest.mark.parametrize("folder_id", ["user_related_genre_playlists", "user_recommended_album"])
async def test_removed_collections_do_not_load_sources(
    collections: LastFMCollections, folder_id: str
) -> None:
    """Retired folders and playlist identities cannot trigger discovery requests."""
    assert await collections.get_playlist_specs(folder_id) == []
    assert not await collections.get_folder_items(folder_id)
    with pytest.raises(MediaNotFoundError):
        await collections.get_playlist_spec("user_related_genre_playlist_jazz")
    collections.provider.api.get_items.assert_not_awaited()
    collections.discovery.get_seeds.assert_not_awaited()
    collections.discovery.resolve_items.assert_not_awaited()


@pytest.mark.parametrize("artist_order", [("Weak", "Strong"), ("Strong", "Weak")])
async def test_personal_artist_scores_normalize_after_all_seed_contributions(
    collections: LastFMCollections, artist_order: tuple[str, str]
) -> None:
    """Normalize the completed pool so earlier seeds cannot gain an unintended advantage."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed(artist, weight=0.2 if artist == "Weak" else 1) for artist in artist_order
    ]
    collections.provider.api.get_items.return_value = []
    candidates = [LastFMItem("Weak"), LastFMItem("Strong"), LastFMItem("Other")]
    ranked = await collections._rank_personal_artists(candidates)
    assert [item.name for item in ranked] == ["Strong", "Weak", "Other"]


@pytest.mark.parametrize("tail_size", [0, 2])
async def test_personal_genre_tracks_use_track_rank_and_track_favorites(
    collections: LastFMCollections, monkeypatch: pytest.MonkeyPatch, tail_size: int
) -> None:
    """The additive bonus can reorder tracks but does not always put favorites first."""
    monkeypatch.setattr(Track, "available", property(lambda _item: True))
    favorite = Track(
        item_id="f",
        provider="library",
        name="Favorite",
        provider_mappings=set(),
        artists=UniqueList(
            [Artist(item_id="b", provider="library", name="Second", provider_mappings=set())]
        ),
    )
    controller = collections.provider.mass.music.get_controller.return_value
    controller.get_library_items_by_query.return_value = [favorite]
    candidates = [
        LastFMItem("First", "First"),
        LastFMItem("Favorite", "Second"),
        LastFMItem("Other", "Second"),
        LastFMItem("Last", "Last"),
    ] + [LastFMItem(f"Tail {index}", "Last") for index in range(tail_size)]
    ranked = await collections._rank_personal_genre_tracks([*candidates, candidates[0]])
    expected = ["First", "Favorite", "Other", "Last"]
    if tail_size:
        expected[:2] = ["Favorite", "First"]
    assert [item.name for item in ranked] == expected + [
        f"Tail {index}" for index in range(tail_size)
    ]
    assert await collections._rank_personal_genre_tracks(candidates) == ranked
    controller.get_library_items_by_query.assert_awaited_once_with(
        favorite=True, limit=500, offset=0
    )


async def test_personal_genre_playlist_passes_weighted_order_and_fixed_cap_to_resolution(
    collections: LastFMCollections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Personal primary genre playlists use a fixed ten-track artist cap."""
    monkeypatch.setattr(recommendations_module, "shuffle", lambda _items: None)
    jazz = genre_for_tag("jazz")
    assert jazz is not None
    collections = LastFMCollections(collections.discovery, genre_profile=GenreProfile((jazz,)))
    candidates = [LastFMItem("First", "First"), LastFMItem("Last", "Last")]
    collections.provider.api.get_items.return_value = candidates
    await collections.get_playlist_tracks(f"user_genre_playlist_{jazz.key}")
    collections.discovery.resolve_items.assert_awaited_once_with(
        candidates,
        MediaType.TRACK,
        max_tracks_per_artist=10,
    )


async def test_favorite_track_keys_read_later_pages_and_all_artist_credits(
    collections: LastFMCollections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Favorites beyond the first page and secondary credits receive the same bonus."""
    monkeypatch.setattr(Track, "available", property(lambda _item: True))
    artists = UniqueList(
        [
            Artist(
                item_id=str(index),
                provider="library",
                name=f"Artist {index}",
                provider_mappings=set(),
            )
            for index in range(2)
        ]
    )
    first = Track(
        item_id="first", provider="library", name="First", provider_mappings=set(), artists=artists
    )
    last = Track(
        item_id="last", provider="library", name="Last", provider_mappings=set(), artists=artists
    )
    controller = collections.provider.mass.music.get_controller.return_value
    controller.get_library_items_by_query.side_effect = [[first] * 500, [last]]
    keys = await collections._get_favorite_track_keys()
    assert keys == {
        ("first", "artist 0"),
        ("first", "artist 1"),
        ("last", "artist 0"),
        ("last", "artist 1"),
    }
    assert await collections._get_favorite_track_keys() == keys
    assert [
        call.kwargs["offset"] for call in controller.get_library_items_by_query.await_args_list
    ] == [0, 500]


async def test_personal_genre_ranking_handles_empty_candidates(
    collections: LastFMCollections,
) -> None:
    """An empty genre source produces no candidates and does not require genre weights."""
    candidate = LastFMItem("Song", "Artist")
    assert await collections._rank_personal_genre_tracks([candidate]) == [candidate]
    assert await collections._rank_personal_genre_tracks([]) == []


async def test_interleave_deduplicates_names_independently_of_source_url() -> None:
    """Two URLs for the same recording must not consume two positions in a generated list."""
    result = LastFMCollections._interleave(
        [[LastFMItem("Song", "Artist", "first")], [LastFMItem("song", "artist", "second")]]
    )
    assert len(result) == 1
