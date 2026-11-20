"""Bounded generated collections and dynamic playlist contracts."""

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Track, UniqueList

from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMItem
from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    GenreProfile,
    genre_for_tag,
)
from music_assistant.providers.lastfm_recommendations_enhanced.collections import LastFMCollections
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import PersonalSeed


@pytest.fixture
def collections() -> LastFMCollections:
    """Keep the generator real and mock only its resolver and source boundaries."""
    discovery = Mock()
    discovery.get_seeds = AsyncMock(return_value=[PersonalSeed("Seed", weight=10)])
    discovery.get_known_items = AsyncMock(return_value=[])
    discovery.resolve_items = AsyncMock(return_value=UniqueList())
    discovery.exclude_rotation = AsyncMock(side_effect=lambda items, _media_type: items)
    discovery.provider.api.get_items = AsyncMock(return_value=[])
    return LastFMCollections(discovery)


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
        "tag.getTopTracks", {"tag": tag}, limit=50
    )
    collections.discovery.get_seeds.assert_not_awaited()
    collections.discovery.exclude_rotation.assert_not_awaited()


async def test_playlist_terminates_after_first_page(collections: LastFMCollections) -> None:
    """No remote catalogue paging is hidden behind playlist enumeration."""
    assert await collections.get_playlist_tracks("global_genre_playlist_jazz", page=1) == []
    collections.provider.api.get_items.assert_not_awaited()
    jazz = genre_for_tag("jazz")
    assert jazz is not None
    collections = LastFMCollections(
        collections.discovery, genre_profile=GenreProfile((), (), (jazz,))
    )
    await collections.get_playlist_tracks(f"global_genre_playlist_{jazz.key}")
    collections.provider.api.get_items.assert_awaited_once_with(
        "tag.getTopTracks", {"tag": "jazz"}, limit=50
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


@pytest.mark.parametrize("folder_id", ["global_popular_artist_albums", "de_popular_artist_albums"])
async def test_popular_albums_never_walk_discographies(
    collections: LastFMCollections, folder_id: str
) -> None:
    """Only three fixed artist selections are requested, with no follow-up pages."""
    collections.provider.api.get_items.side_effect = [
        [LastFMItem(str(index)) for index in range(3)],
        [],
        [],
        [],
    ]
    await collections.get_folder_items(folder_id)
    calls = collections.provider.api.get_items.await_args_list
    assert len(calls) == 4
    assert calls[0].args == (
        ("geo.getTopArtists", {"country": "Germany"})
        if folder_id.startswith("de_")
        else ("chart.getTopArtists", {})
    )
    assert all(
        call.args[0] == "artist.getTopAlbums" and call.kwargs["limit"] == 20 for call in calls[1:]
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
    assert scores == pytest.approx({"shoegaze": 8, "dream pop": 8})
    assert sum(scores.values()) == pytest.approx(16)
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
    assert scores == pytest.approx({"shoegaze": 6, "dream pop": 3})


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
    assert scores == pytest.approx({"hip-hop": 8, "shoegaze": 4})


async def test_genre_profile_is_bounded_and_shared_by_all_genre_recipes(
    collections: LastFMCollections,
) -> None:
    """Ten artist lookups and five tags each bound the six-genre generation."""
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
    assert len(profile.genres) == 6
    calls = collections.provider.api.get_items.await_args_list
    assert sum(call.args[0] == "artist.getTopTags" for call in calls) == 10
    collections.discovery.get_seeds.assert_awaited_once_with(MediaType.ARTIST, limit=10)
    collections.provider.api.get_items.reset_mock()
    for folder_id, count in [("user_genre_playlists_monthly", 3)]:
        recipes = await collections.get_playlist_specs(folder_id)
        assert len(recipes) == count
        assert [recipe.tag for recipe in recipes] == [
            category.tag for category in profile.genres[:count]
        ]
        for recipe in recipes:
            assert await collections.get_playlist_spec(recipe.item_id) == recipe
    collections.provider.api.get_items.assert_not_awaited()


async def test_related_genres_preserve_personal_artist_weight_and_exclude_primary(
    collections: LastFMCollections,
) -> None:
    """Neighbor genres inherit personal importance and cannot repeat the main profile."""
    collections.discovery.get_seeds.return_value = [
        PersonalSeed("First", weight=100),
        PersonalSeed("Second", weight=1),
    ]

    async def items(method: str, parameters: dict[str, str], **_kwargs: object) -> list[LastFMItem]:
        artist = parameters["artist"]
        if method == "artist.getSimilar":
            return [LastFMItem(f"{artist} Neighbor")]
        return {
            "First": [LastFMItem("Shoegaze", tag_weight=100)],
            "Second": [LastFMItem("Shoegaze", tag_weight=100)],
            "First Neighbor": [LastFMItem("Doom Metal", tag_weight=100)],
            "Second Neighbor": [
                LastFMItem("Dream Pop", tag_weight=100),
                LastFMItem("Shoegaze", tag_weight=100),
            ],
        }[artist]

    collections.provider.api.get_items.side_effect = items
    assert [category.tag for category in await collections.get_genres()] == ["shoegaze"]
    assert [category.tag for category in await collections.get_genres(related=True)] == [
        "doom metal",
        "dream pop",
    ]


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
        collections.discovery, genre_profile=GenreProfile((category,), (), (category,))
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
async def test_explore_folders_never_query_personal_history_or_exclude_rotation(
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
    collections.discovery.exclude_rotation.assert_not_awaited()
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
        collections.discovery, genre_profile=GenreProfile((), (), (category,))
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


async def test_album_spotlight_contains_one_album_and_its_track_list(
    collections: LastFMCollections,
) -> None:
    """Keep the chosen album and ordered playable tracks in the highlighted row."""
    artist = Artist(item_id="a", provider="library", name="Artist", provider_mappings=set())
    album = Album(
        item_id="b",
        provider="library",
        name="Album",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    track = Track(
        item_id="c",
        provider="library",
        name="Track",
        artists=UniqueList([artist]),
        provider_mappings=set(),
    )
    collections.provider.api.get_items.return_value = [LastFMItem("Track", "Artist")]
    collections.discovery.resolve_items.side_effect = [UniqueList([album]), UniqueList([track])]
    result = await collections.get_folder_items("user_recommended_album")
    assert list(result) == [album, track]
    calls = collections.discovery.resolve_items.await_args_list
    assert len(calls[0].args[0]) == 1
    assert calls[0].args[1] == MediaType.ALBUM
    assert calls[1].args[1] == MediaType.TRACK
    assert collections.provider.api.get_items.await_args_list[-1].args == (
        "album.getInfo",
        {"artist": "Artist", "album": "Album"},
    )


async def test_interleave_deduplicates_names_independently_of_source_url() -> None:
    """Two URLs for the same recording must not consume two positions in a generated list."""
    result = LastFMCollections._interleave(
        [[LastFMItem("Song", "Artist", "first")], [LastFMItem("song", "artist", "second")]]
    )
    assert len(result) == 1
