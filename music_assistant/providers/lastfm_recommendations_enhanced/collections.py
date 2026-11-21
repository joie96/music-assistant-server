"""Request-scoped genre, album and playlist generation without catalogue traversal."""

from __future__ import annotations

from hashlib import sha256
from itertools import zip_longest
from math import ceil
from random import shuffle
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
from .constants import FAVORITE_BONUS
from .recommendations import (
    RESOLUTION_TARGET_LIMITS,
    RESULT_LIMIT,
    PersonalSeed,
    normalize_scores,
    random_candidate_pool,
    similarity_weights,
)

if TYPE_CHECKING:
    from .recommendations import LastFMDiscovery

PLAYLIST_FAMILIES = {
    "global_genre_playlist_": "global_genre_playlists",
    "user_genre_playlist_": "user_genre_playlists_monthly",
    "global_mood_playlist_": "global_mood_playlists",
    "global_decade_playlist_": "global_decade_playlists",
    "user_artist_playlist_": "user_artist_playlists_monthly",
    "global_artist_radio_playlist_": "global_artist_radio_playlists",
}
GENRE_ARTIST_LIMIT = 15
PERSONAL_GENRE_PLAYLIST_LIMIT = 10
EXPLORE_GENRE_LIMIT = 20
TAGS_PER_ARTIST = 5
PERSONAL_GENRE_ARTIST_TRACK_LIMIT = 10
PERSONAL_ARTIST_PLAYLIST_LIMIT = 10
GLOBAL_ARTIST_RADIO_PLAYLIST_LIMIT = 15
ALBUM_SEED_LIMIT = 10
ALBUM_RANDOM_POOL_LIMIT = 10
ARTIST_PLAYLIST_RANDOM_POOL_LIMIT = 25


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
        self._favorite_track_keys: set[tuple[str, str]] | None = None
        self._genre_seeds: list[PersonalSeed] | None = None

    async def get_folder_items(self, folder_id: str) -> UniqueList[Artist | Album | Track]:
        """Build an album or genre row; playlist descriptors use get_playlist_specs."""
        started = perf_counter()
        self.provider.logger.debug("Building Last.fm collection folder=%s", folder_id)
        if folder_id in {"global_popular_artist_albums", "de_popular_artist_albums"}:
            is_germany = folder_id == "de_popular_artist_albums"
            artists = await self.provider.api.get_items(
                "geo.getTopArtists" if is_germany else "chart.getTopArtists",
                {"country": "Germany"} if is_germany else {},
                limit=50,
            )
            result = await self._albums([artist.name for artist in artists])
        else:
            result = await self._genre_items(folder_id)
        self.provider.logger.debug(
            "Last.fm collection completed folder=%s items=%d elapsed=%.3fs",
            folder_id,
            len(result),
            perf_counter() - started,
        )
        return result

    async def get_personal_albums(self) -> UniqueList[Artist | Album | Track]:
        """Recommend random top-ten albums with similarity-weighted artist contributions."""
        seeds = (await self.discovery.get_seeds(MediaType.ARTIST, limit=ALBUM_SEED_LIMIT))[
            :ALBUM_SEED_LIMIT
        ]
        if not seeds:
            return UniqueList()
        artists: dict[str, str] = {}
        scores: dict[str, float] = {}
        for seed in seeds:
            seed_key = seed.artist.casefold()
            contributions = {seed_key: (seed.artist, 1.0)}
            candidates = await self.provider.api.get_items(
                "artist.getSimilar", {"artist": seed.artist}, limit=50
            )
            for item, weight in zip(candidates, similarity_weights(candidates), strict=True):
                key = item.name.casefold()
                if key != seed_key and weight > contributions.get(key, ("", -1))[1]:
                    contributions[key] = (item.name, weight)
            for key, (artist, weight) in contributions.items():
                artists.setdefault(key, artist)
                scores[key] = scores.get(key, 0) + seed.weight * weight
        weight_sum = sum(scores.values())
        if not weight_sum:
            return UniqueList()
        target_limit = RESOLUTION_TARGET_LIMITS[MediaType.ALBUM]
        result: UniqueList[Artist | Album | Track] = UniqueList()
        seen: set[str] = set()
        for key in sorted(scores, key=lambda key: scores[key], reverse=True):
            if len(result) >= target_limit:
                break
            quota = max(1, ceil(target_limit * scores[key] / weight_sum))
            source_started = perf_counter()
            self.provider.logger.debug(
                "Last.fm album source build started artist=%s personal=True quota=%d",
                artists[key],
                quota,
            )
            try:
                top_albums = await self.provider.api.get_items(
                    "artist.getTopAlbums", {"artist": artists[key]}, limit=50
                )
            except MediaNotFoundError:
                self.provider.logger.debug(
                    "Last.fm album source build completed artist=%s personal=True "
                    "added=0 reason=not_found elapsed=%.3fs",
                    artists[key],
                    perf_counter() - source_started,
                )
                continue
            candidates = top_albums[: min(3 * quota, ALBUM_RANDOM_POOL_LIMIT)]
            shuffle(candidates)
            resolved = await self.discovery.resolve_items(
                candidates,
                MediaType.ALBUM,
                max_items=min(quota, target_limit - len(result)),
                excluded_uris=seen,
            )
            previous_count = len(result)
            result.extend(item for item in resolved if isinstance(item, Album))
            seen.update(item.uri for item in result if item.uri is not None)
            self.provider.logger.debug(
                "Last.fm album source build completed artist=%s personal=True "
                "quota=%d candidates=%d added=%d total=%d elapsed=%.3fs",
                artists[key],
                quota,
                len(candidates),
                len(result) - previous_count,
                len(result),
                perf_counter() - source_started,
            )
        shuffle(result)
        return result

    async def get_personal_genre_playlists(self) -> list[tuple[PlaylistSpec, list[Track]]]:
        """Prepare the strongest playable genre playlists, replacing empty or missing genres."""
        recipes = await self.get_playlist_specs("user_genre_playlists_monthly")
        result: list[tuple[PlaylistSpec, list[Track]]] = []
        for recipe in recipes:
            try:
                tracks = await self.get_playlist_tracks(recipe.item_id)
            except MediaNotFoundError:
                self.provider.logger.debug("Last.fm genre playlist missing tag=%s", recipe.tag)
                continue
            if not tracks:
                self.provider.logger.debug("Last.fm genre playlist empty tag=%s", recipe.tag)
                continue
            result.append((recipe, tracks))
            if len(result) == PERSONAL_GENRE_PLAYLIST_LIMIT:
                break
        return result

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
        if folder_id == "user_genre_playlists_monthly":
            categories = (await self.get_genre_profile()).genres
            return [
                PlaylistSpec(
                    f"user_genre_playlist_{item.key}", item.name, tag=item.tag, personal=True
                )
                for item in categories
            ]
        if folder_id == "user_artist_playlists_monthly":
            seeds = await self.discovery.get_seeds(
                MediaType.ARTIST, limit=PERSONAL_ARTIST_PLAYLIST_LIMIT
            )
            return [
                self._artist_spec(seed.artist, personal=True, identity=seed.identity)
                for seed in seeds[:PERSONAL_ARTIST_PLAYLIST_LIMIT]
            ]
        if folder_id == "global_artist_radio_playlists":
            artists = await self.provider.api.get_items("chart.getTopArtists", limit=50)
            return [
                self._artist_spec(artist.name, identity=artist.mbid)
                for artist in artists[:GLOBAL_ARTIST_RADIO_PLAYLIST_LIMIT]
            ]
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
        """Return one bounded batch with the recipe's artist limits; later pages are empty."""
        if page != 0:
            return []
        recipe = await self.get_playlist_spec(item_id)
        started = perf_counter()
        self.provider.logger.debug(
            "Last.fm playlist build started playlist=%s personal=%s",
            item_id,
            recipe.personal,
        )
        if recipe.artist:
            result = await self._artist_playlist_tracks(recipe.artist)
        else:
            candidates = await self.provider.api.get_items(
                "tag.getTopTracks", {"tag": recipe.tag}, limit=3 * RESULT_LIMIT
            )
            if item_id.startswith("user_genre_playlist_"):
                candidates = await self._rank_personal_genre_tracks(candidates)
            candidates = random_candidate_pool(self._interleave([candidates]), RESULT_LIMIT)
            resolved = await self.discovery.resolve_items(
                candidates,
                MediaType.TRACK,
                max_tracks_per_artist=(
                    PERSONAL_GENRE_ARTIST_TRACK_LIMIT
                    if item_id.startswith("user_genre_playlist_")
                    else None
                ),
            )
            result = []
            for item in resolved:
                if not isinstance(item, Track) or not item.artists:
                    continue
                item.position = len(result) + 1
                result.append(item)
        self.provider.logger.debug(
            "Last.fm playlist build completed playlist=%s tracks=%d elapsed=%.3fs",
            item_id,
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

    async def get_genres(self) -> list[Category]:
        """Return the strongest personal genres from the generation's profile."""
        profile = await self.get_genre_profile()
        return list(profile.genres[:3])

    async def get_genre_profile(self) -> GenreProfile:
        """Prepare bounded personal genre categories solely from weighted Last.fm artist tags."""
        if self._genre_profile is not None:
            return self._genre_profile
        seeds = await self._get_genre_seeds()
        scores = await self._genre_scores(seeds)
        genres = tuple(
            category
            for category, _weight in sorted(scores, key=lambda entry: (-entry[1], entry[0].key))
        )
        self._genre_profile = GenreProfile(genres)
        return self._genre_profile

    async def _rank_personal_genre_tracks(self, candidates: list[LastFMItem]) -> list[LastFMItem]:
        """Rank unique genre tracks by Last.fm rank and the selected user's track favorites."""
        favorites = await self._get_favorite_track_keys()
        candidates = self._interleave([candidates])
        scores = normalize_scores(
            {rank: float(len(candidates) - rank) for rank in range(len(candidates))}
        )
        weighted = [
            (
                candidate,
                scores[rank]
                + (
                    FAVORITE_BONUS
                    if (candidate.name.casefold(), candidate.artist.casefold()) in favorites
                    else 0
                ),
            )
            for rank, candidate in enumerate(candidates)
        ]
        self.provider.logger.debug(
            "Last.fm personal genre playlist candidates=%d max_artist_tracks=%d",
            len(candidates),
            PERSONAL_GENRE_ARTIST_TRACK_LIMIT,
        )
        return [candidate for candidate, _score in sorted(weighted, key=lambda entry: -entry[1])]

    async def _get_favorite_track_keys(self) -> set[tuple[str, str]]:
        """Read the selected user's track favorites once per collection, preserving artist credits."""
        if self._favorite_track_keys is not None:
            return self._favorite_track_keys
        controller = self.provider.mass.music.get_controller(MediaType.TRACK)
        keys: set[tuple[str, str]] = set()
        offset = 0
        while True:
            items = await controller.get_library_items_by_query(
                favorite=True, limit=500, offset=offset
            )
            keys.update(
                (item.name.casefold(), artist.name.casefold())
                for item in items
                if isinstance(item, Track) and item.available
                for artist in item.artists
            )
            if len(items) < 500:
                break
            offset += len(items)
        self._favorite_track_keys = keys
        return keys

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
                "artist.getTopTags", {"artist": seed.artist}, limit=50
            )
            selected: dict[str, tuple[Category, float | None, int]] = {}
            for rank, tag in enumerate(tags[:50], 1):
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
        return [(categories[key], weight) for key, weight in normalize_scores(scores).items()]

    async def _artist_playlist_tracks(self, seed_artist: str) -> list[Track]:
        """Build a randomized playlist with similarity-weighted contributions per artist."""
        similar = await self.provider.api.get_items(
            "artist.getSimilar", {"artist": seed_artist}, limit=50
        )
        seed_key = seed_artist.casefold()
        artists: dict[str, tuple[str, float]] = {seed_key: (seed_artist, 1.0)}
        for item, weight in zip(similar, similarity_weights(similar), strict=True):
            key = item.name.casefold()
            if item.similarity_weight is None:
                self.provider.logger.debug("Last.fm similar artist has no valid score")
                continue
            if key != seed_key and weight > artists.get(key, ("", -1))[1]:
                artists[key] = (item.name, weight)
        ranked = sorted(artists.values(), key=lambda entry: entry[1], reverse=True)
        weight_sum = sum(weight for _artist, weight in ranked)
        result: list[Track] = []
        seen: set[str] = set()
        for artist, weight in ranked:
            if len(result) >= RESULT_LIMIT:
                break
            quota = max(1, ceil(RESULT_LIMIT * weight / weight_sum))
            source_started = perf_counter()
            self.provider.logger.debug(
                "Last.fm artist tracks build started seed=%s artist=%s quota=%d",
                seed_artist,
                artist,
                quota,
            )
            try:
                top_tracks = await self.provider.api.get_items(
                    "artist.getTopTracks", {"artist": artist}, limit=50
                )
            except MediaNotFoundError:
                self.provider.logger.debug(
                    "Last.fm artist tracks build completed seed=%s artist=%s "
                    "added=0 reason=not_found elapsed=%.3fs",
                    seed_artist,
                    artist,
                    perf_counter() - source_started,
                )
                continue
            candidates = top_tracks[: min(3 * quota, ARTIST_PLAYLIST_RANDOM_POOL_LIMIT)]
            shuffle(candidates)
            resolved = await self.discovery.resolve_items(
                candidates,
                MediaType.TRACK,
                max_tracks_per_artist=quota,
                max_items=min(quota, RESULT_LIMIT - len(result)),
                excluded_uris=seen,
            )
            previous_count = len(result)
            result.extend(item for item in resolved if isinstance(item, Track) and item.artists)
            seen.update(item.uri for item in result if item.uri is not None)
            self.provider.logger.debug(
                "Last.fm artist tracks build completed seed=%s artist=%s "
                "quota=%d candidates=%d added=%d total=%d elapsed=%.3fs",
                seed_artist,
                artist,
                quota,
                len(candidates),
                len(result) - previous_count,
                len(result),
                perf_counter() - source_started,
            )
        shuffle(result)
        for position, track in enumerate(result, 1):
            track.position = position
        return result

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
            candidates = await self.provider.api.get_items(
                method, {"tag": category.tag}, limit=3 * RESOLUTION_TARGET_LIMITS[media_type]
            )
            seeds = []
            if prefix == "user_genre_artists_":
                seeds = await self._get_genre_seeds()
                candidates = await self._rank_personal_artists(candidates)
            candidates = random_candidate_pool(
                self._interleave([candidates]), RESOLUTION_TARGET_LIMITS[media_type]
            )
            return await self.discovery.resolve_items(
                candidates,
                media_type,
                seeds=seeds,
            )
        return UniqueList()

    async def _get_genre_seeds(self) -> list[PersonalSeed]:
        """Reuse the genre profile's MA artist seeds for ranking and the final seed quota."""
        if self._genre_seeds is None:
            self._genre_seeds = await self.discovery.get_seeds(
                MediaType.ARTIST, limit=GENRE_ARTIST_LIMIT
            )
        return self._genre_seeds

    async def _rank_personal_artists(self, candidates: list[LastFMItem]) -> list[LastFMItem]:
        """Promote preferred and similar artists within the genre, never outside its tag list."""
        seeds = await self._get_genre_seeds()
        weights: dict[str, float] = {}
        for seed in seeds[:3]:
            weights[seed.artist.casefold()] = weights.get(seed.artist.casefold(), 0) + seed.weight
            similar = await self.provider.api.get_items(
                "artist.getSimilar", {"artist": seed.artist}, limit=50
            )
            for item, weight in zip(similar, similarity_weights(similar), strict=True):
                key = item.name.casefold()
                weights[key] = weights.get(key, 0) + seed.weight * weight
        weights = normalize_scores(weights)
        return sorted(
            candidates, key=lambda item: weights.get(item.name.casefold(), 0), reverse=True
        )

    async def _albums(
        self, artists: list[str], *, max_items: int | None = None
    ) -> UniqueList[Artist | Album | Track]:
        """Randomly select popular albums using the shared artist and candidate pool limits."""
        artists = list(dict.fromkeys(artists))[:ALBUM_SEED_LIMIT]
        result: UniqueList[Artist | Album | Track] = UniqueList()
        if not artists:
            return result
        target_limit = RESOLUTION_TARGET_LIMITS[MediaType.ALBUM]
        if max_items is not None:
            target_limit = min(target_limit, max_items)
        quota = ceil(target_limit / len(artists))
        seen: set[str] = set()
        for index, artist in enumerate(artists, 1):
            if len(result) >= target_limit:
                break
            source_started = perf_counter()
            self.provider.logger.debug(
                "Last.fm album source build started artist=%s personal=False quota=%d",
                artist,
                quota,
            )
            top_albums = await self.provider.api.get_items(
                "artist.getTopAlbums", {"artist": artist}, limit=50
            )
            candidates = self._interleave([top_albums])[: min(3 * quota, ALBUM_RANDOM_POOL_LIMIT)]
            shuffle(candidates)
            resolved = await self.discovery.resolve_items(
                candidates,
                MediaType.ALBUM,
                max_items=min(quota, target_limit - len(result)),
                excluded_uris=seen,
            )
            previous_count = len(result)
            result.extend(item for item in resolved if isinstance(item, Album))
            seen.update(item.uri for item in result if item.uri is not None)
            self.provider.logger.debug(
                "Last.fm album source build completed artist=%s personal=False "
                "source=%d/%d quota=%d candidates=%d added=%d total=%d elapsed=%.3fs",
                artist,
                index,
                len(artists),
                quota,
                len(candidates),
                len(result) - previous_count,
                len(result),
                perf_counter() - source_started,
            )
        shuffle(result)
        return result

    @staticmethod
    def _artist_spec(artist: str, *, personal: bool = False, identity: str = "") -> PlaylistSpec:
        """Use a stable artist identity rather than a changing chart rank in playlist ids."""
        key = sha256((identity or artist.casefold()).encode()).hexdigest()[:16]
        prefix = "user_artist_playlist" if personal else "global_artist_radio_playlist"
        name = f"Featuring {artist}"
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
        return result
