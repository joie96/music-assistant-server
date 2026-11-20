"""Request-scoped genre, album and playlist generation without catalogue traversal."""

from __future__ import annotations

from hashlib import sha256
from itertools import zip_longest
from time import perf_counter
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Track, UniqueList

from .api_client import LastFMItem
from .catalog import (
    DECADES,
    MOODS,
    Category,
    GenreProfile,
    PlaylistSpec,
    genre_for_tag,
)
from .recommendations import PersonalSeed

if TYPE_CHECKING:
    from .recommendations import LastFMDiscovery

PLAYLIST_FAMILIES = {
    "global_genre_playlist_": "global_genre_playlists",
    "user_genre_playlist_": "user_genre_playlists_monthly",
    "user_related_genre_playlist_": "user_related_genre_playlists",
    "global_mood_playlist_": "global_mood_playlists",
    "global_decade_playlist_": "global_decade_playlists",
    "user_artist_playlist_": "user_artist_playlists_monthly",
    "global_artist_radio_playlist_": "global_artist_radio_playlists",
}
GENRE_ARTIST_LIMIT = 10
GENRE_LIMIT = 6
EXPLORE_GENRE_LIMIT = 20
TAGS_PER_ARTIST = 5


class LastFMCollections:
    """Generate bounded collections using the active request's listening context."""

    def __init__(
        self, discovery: LastFMDiscovery, *, genre_profile: GenreProfile | None = None
    ) -> None:
        """Reuse the provider's resolver, API and user-scoped statistics."""
        self.discovery = discovery
        self.provider = discovery.provider
        self._genre_profile = genre_profile
        self._explore_genres = genre_profile.explore_genres if genre_profile is not None else None

    async def get_folder_items(self, folder_id: str) -> UniqueList[Artist | Album | Track]:
        """Build an album or genre row; playlist descriptors use get_playlist_specs."""
        started = perf_counter()
        self.provider.logger.debug("Building Last.fm collection folder=%s", folder_id)
        if folder_id in {"global_popular_artist_albums", "de_popular_artist_albums"}:
            is_germany = folder_id == "de_popular_artist_albums"
            artists = await self.provider.api.get_items(
                "geo.getTopArtists" if is_germany else "chart.getTopArtists",
                {"country": "Germany"} if is_germany else {},
                limit=3,
            )
            result = await self._albums([artist.name for artist in artists])
        elif folder_id == "user_recommended_album":
            albums = await self.get_personal_albums(spotlight=True)
            result = UniqueList(albums[:1])
            if albums and isinstance(album := albums[0], Album) and album.artists:
                candidates = await self.provider.api.get_items(
                    "album.getInfo",
                    {"artist": album.artists[0].name, "album": album.name},
                    limit=50,
                )
                result.extend(await self.discovery.resolve_items(candidates, MediaType.TRACK))
        else:
            result = await self._genre_items(folder_id)
        self.provider.logger.debug(
            "Last.fm collection completed folder=%s items=%d elapsed=%.3fs",
            folder_id,
            len(result),
            perf_counter() - started,
        )
        return result

    async def get_personal_albums(
        self, *, spotlight: bool = False
    ) -> UniqueList[Artist | Album | Track]:
        """Recommend albums from preferred and similar artists, excluding current rotation."""
        seeds = await self.discovery.get_seeds(MediaType.ARTIST)
        if not seeds:
            return UniqueList()
        similar: list[str] = []
        for seed in seeds[:3]:
            candidates = await self.provider.api.get_items(
                "artist.getSimilar", {"artist": seed.artist}, limit=3
            )
            similar.extend(item.name for item in candidates)
        artists = list(
            dict.fromkeys([seeds[0].artist, *similar, *(seed.artist for seed in seeds)])
        )[:3]
        return await self.discovery.exclude_rotation(
            await self._albums(artists, limit=1 if spotlight else 50), MediaType.ALBUM
        )

    async def get_playlist_specs(self, folder_id: str) -> list[PlaylistSpec]:
        """List recipes without building their track lists or persisting private selections."""
        if folder_id == "global_genre_playlists":
            return [
                PlaylistSpec(f"global_genre_playlist_{item.key}", item.name, tag=item.tag)
                for item in await self.get_explore_genres()
            ]
        if folder_id == "global_mood_playlists":
            return [
                PlaylistSpec(f"global_mood_playlist_{item.key}", item.name, tag=item.tag)
                for item in MOODS
            ]
        if folder_id == "global_decade_playlists":
            return [
                PlaylistSpec(f"global_decade_playlist_{item.key}", item.name, tag=item.tag)
                for item in DECADES
            ]
        if folder_id in {"user_genre_playlists_monthly", "user_related_genre_playlists"}:
            related = folder_id == "user_related_genre_playlists"
            categories = await self.get_genres(related=related)
            prefix = "user_related_genre_playlist" if related else "user_genre_playlist"
            return [
                PlaylistSpec(f"{prefix}_{item.key}", item.name, tag=item.tag, personal=True)
                for item in categories
            ]
        if folder_id == "user_artist_playlists_monthly":
            seeds = await self.discovery.get_seeds(MediaType.ARTIST)
            return [
                self._artist_spec(seed.artist, personal=True, identity=seed.identity)
                for seed in seeds[:3]
            ]
        if folder_id == "global_artist_radio_playlists":
            artists = await self.provider.api.get_items("chart.getTopArtists", limit=3)
            return [self._artist_spec(artist.name, identity=artist.mbid) for artist in artists]
        return []

    async def get_playlist_spec(self, item_id: str) -> PlaylistSpec:
        """Authorize dynamic personal identities against the current user's recipe list."""
        for prefix, folder_id in PLAYLIST_FAMILIES.items():
            if not item_id.startswith(prefix):
                continue
            recipes = await self.get_playlist_specs(folder_id)
            if recipe := next((recipe for recipe in recipes if recipe.item_id == item_id), None):
                return recipe
            break
        raise MediaNotFoundError(
            "The generated playlist is not available in this listening context"
        )

    async def get_playlist_tracks(self, item_id: str, page: int = 0) -> list[Track]:
        """Return one bounded, artist-diverse batch; all subsequent pages are empty."""
        if page != 0:
            return []
        recipe = await self.get_playlist_spec(item_id)
        started = perf_counter()
        self.provider.logger.debug("Building Last.fm playlist personal=%s", recipe.personal)
        if recipe.artist:
            similar = await self.provider.api.get_items(
                "artist.getSimilar", {"artist": recipe.artist}, limit=2
            )
            artists = list(dict.fromkeys([recipe.artist, *(item.name for item in similar)]))[:3]
            batches = []
            for index, artist in enumerate(artists, 1):
                batches.append(
                    await self.provider.api.get_items(
                        "artist.getTopTracks", {"artist": artist}, limit=20
                    )
                )
                self.provider.logger.debug(
                    "Last.fm playlist source=%d/%d received=%d",
                    index,
                    len(artists),
                    len(batches[-1]),
                )
            candidates = self._interleave(batches)
        else:
            candidates = await self.provider.api.get_items(
                "tag.getTopTracks", {"tag": recipe.tag}, limit=50
            )
        resolved = await self.discovery.resolve_items(candidates, MediaType.TRACK)
        if recipe.personal:
            resolved = await self.discovery.exclude_rotation(resolved, MediaType.TRACK)
        counts: dict[str, int] = {}
        result = []
        for item in resolved:
            if not isinstance(item, Track) or not item.artists:
                continue
            artist = item.artists[0].name.casefold()
            if counts.get(artist, 0) >= (20 if recipe.artist else 5):
                continue
            counts[artist] = counts.get(artist, 0) + 1
            item.position = len(result) + 1
            result.append(item)
        self.provider.logger.debug(
            "Last.fm playlist completed candidates=%d tracks=%d elapsed=%.3fs",
            len(candidates),
            len(result),
            perf_counter() - started,
        )
        return result

    async def get_explore_genres(self) -> tuple[Category, ...]:
        """Prepare global Explore categories without consulting user history or favorites."""
        if self._explore_genres is not None:
            return self._explore_genres
        tags = await self.provider.api.get_items("chart.getTopTags", limit=50)
        categories: dict[str, Category] = {}
        for tag in tags[:50]:
            if (category := genre_for_tag(tag.name)) is None:
                continue
            categories.setdefault(category.key, category)
            if len(categories) == EXPLORE_GENRE_LIMIT:
                break
        self._explore_genres = tuple(categories.values())
        return self._explore_genres

    async def get_genres(self, *, related: bool = False) -> list[Category]:
        """Return the strongest personal or neighboring genres from the generation's profile."""
        profile = await self.get_genre_profile()
        return list(profile.related_genres if related else profile.genres[:3])

    async def get_genre_profile(self) -> GenreProfile:
        """Prepare bounded personal genre categories solely from weighted Last.fm artist tags."""
        if self._genre_profile is not None:
            return self._genre_profile
        seeds = await self.discovery.get_seeds(MediaType.ARTIST, limit=GENRE_ARTIST_LIMIT)
        scores = await self._genre_scores(seeds)
        genres = tuple(
            category
            for category, _weight in sorted(scores, key=lambda entry: (-entry[1], entry[0].key))[
                :GENRE_LIMIT
            ]
        )
        neighbors: list[PersonalSeed] = []
        if genres:
            for seed in seeds[:3]:
                similar = await self.provider.api.get_items(
                    "artist.getSimilar", {"artist": seed.artist}, limit=1
                )
                neighbors.extend(PersonalSeed(item.name, weight=seed.weight) for item in similar)
        related_scores = await self._genre_scores(neighbors[:3])
        primary_keys = {category.key for category in genres}
        related = tuple(
            category
            for category, _weight in sorted(
                related_scores, key=lambda entry: (-entry[1], entry[0].key)
            )
            if category.key not in primary_keys
        )[:3]
        self._genre_profile = GenreProfile(genres, related)
        return self._genre_profile

    async def _genre_scores(self, seeds: list[PersonalSeed]) -> list[tuple[Category, float]]:
        """Weight community genre candidates by personal importance, never by MA genre mappings."""
        scores: dict[str, float] = {}
        categories: dict[str, Category] = {}
        artists: dict[str, PersonalSeed] = {}
        for seed in seeds[:GENRE_ARTIST_LIMIT]:
            key = seed.artist.casefold()
            if seed.weight <= 0 or not key.strip():
                continue
            previous = artists.get(key)
            artists[key] = PersonalSeed(
                seed.artist, weight=seed.weight + (previous.weight if previous else 0)
            )
        for seed in artists.values():
            tags = await self.provider.api.get_items(
                "artist.getTopTags", {"artist": seed.artist}, limit=20
            )
            selected: dict[str, tuple[Category, float | None, int]] = {}
            for rank, tag in enumerate(tags[:20], 1):
                if (category := genre_for_tag(tag.name)) is None:
                    continue
                if category.key not in selected:
                    selected[category.key] = (category, tag.tag_weight, rank)
                if len(selected) == TAGS_PER_ARTIST:
                    break
            maximum = max(
                (weight or 0 for _category, weight, _rank in selected.values()), default=0
            )
            weighted: list[tuple[Category, float]] = []
            for category, tag_weight, rank in selected.values():
                if tag_weight is None:
                    weight = 1 / rank
                elif maximum:
                    weight = tag_weight / maximum
                else:
                    continue
                weighted.append((category, weight))
            total = sum(weight for _category, weight in weighted)
            if not total:
                continue
            for category, weight in weighted:
                if weight <= 0:
                    continue
                categories.setdefault(category.key, category)
                scores[category.key] = scores.get(category.key, 0) + seed.weight * weight / total
        self.provider.logger.debug(
            "Last.fm genre profile seeds=%d genres=%d", len(seeds), len(scores)
        )
        return [(categories[key], weight) for key, weight in scores.items()]

    async def _genre_items(self, folder_id: str) -> UniqueList[Artist | Album | Track]:
        """Build only advertised genre keys, with personal rows gated by the genre profile."""
        for prefix, method, media_type in (
            ("user_genre_artists_", "tag.getTopArtists", MediaType.ARTIST),
            ("global_genre_artists_", "tag.getTopArtists", MediaType.ARTIST),
            ("global_genre_albums_", "tag.getTopAlbums", MediaType.ALBUM),
        ):
            if not folder_id.startswith(prefix):
                continue
            categories = (
                await self.get_genres()
                if prefix == "user_genre_artists_"
                else await self.get_explore_genres()
            )
            category = next(
                (item for item in categories if item.key == folder_id.removeprefix(prefix)),
                None,
            )
            if category is None:
                break
            candidates = await self.provider.api.get_items(method, {"tag": category.tag}, limit=50)
            if prefix == "user_genre_artists_":
                candidates = await self._rank_personal_artists(candidates)
            result = await self.discovery.resolve_items(candidates, media_type)
            return (
                await self.discovery.exclude_rotation(result, media_type)
                if prefix == "user_genre_artists_"
                else result
            )
        return UniqueList()

    async def _rank_personal_artists(self, candidates: list[LastFMItem]) -> list[LastFMItem]:
        """Promote preferred and similar artists within the genre, never outside its tag list."""
        seeds = await self.discovery.get_seeds(MediaType.ARTIST)
        weights: dict[str, float] = {}
        for seed in seeds[:3]:
            weights[seed.artist.casefold()] = weights.get(seed.artist.casefold(), 0) + seed.weight
            similar = await self.provider.api.get_items(
                "artist.getSimilar", {"artist": seed.artist}, limit=20
            )
            for rank, item in enumerate(similar, 1):
                key = item.name.casefold()
                weights[key] = weights.get(key, 0) + seed.weight / rank
        return sorted(
            candidates, key=lambda item: weights.get(item.name.casefold(), 0), reverse=True
        )

    async def _albums(
        self, artists: list[str], *, limit: int = 50
    ) -> UniqueList[Artist | Album | Track]:
        """Select top albums for at most three artists, never their complete discographies."""
        batches = []
        for index, artist in enumerate(list(dict.fromkeys(artists))[:3], 1):
            batches.append(
                await self.provider.api.get_items(
                    "artist.getTopAlbums", {"artist": artist}, limit=20
                )
            )
            self.provider.logger.debug(
                "Last.fm album source=%d/%d received=%d",
                index,
                min(len(artists), 3),
                len(batches[-1]),
            )
        return await self.discovery.resolve_items(
            self._interleave(batches)[:limit], MediaType.ALBUM
        )

    @staticmethod
    def _artist_spec(artist: str, *, personal: bool = False, identity: str = "") -> PlaylistSpec:
        """Use a stable artist identity rather than a changing chart rank in playlist ids."""
        key = sha256((identity or artist.casefold()).encode()).hexdigest()[:16]
        prefix = "user_artist_playlist" if personal else "global_artist_radio_playlist"
        name = f"Featuring {artist}" if personal else f"{artist} & similar artists"
        return PlaylistSpec(f"{prefix}_{key}", name, artist=artist, personal=personal)

    @staticmethod
    def _interleave(batches: list[list[LastFMItem]]) -> list[LastFMItem]:
        """Mix source ranks without allowing the first artist to occupy the complete row."""
        result = []
        seen: set[tuple[str, str]] = set()
        for group in zip_longest(*batches):
            for item in group:
                if item is None:
                    continue
                key = (item.name.casefold(), item.artist.casefold())
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result[:50]
