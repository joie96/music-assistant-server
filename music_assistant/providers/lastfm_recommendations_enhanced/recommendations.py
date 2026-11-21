"""User-scoped discovery and streaming resolution through the shared MA cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from random import shuffle
from time import perf_counter
from typing import TYPE_CHECKING
from urllib.parse import quote

from music_assistant_models.enums import (
    ExternalID,
    LinkType,
    MediaType,
    ProviderFeature,
    ProviderStatus,
    ProviderType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, Artist, ItemMapping, Track, UniqueList
from music_assistant_models.media_items.metadata import MediaItemLink

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version

from .api_client import LastFMItem
from .constants import CONF_RECOMMENDATION_USER_ID, FAVORITE_BONUS

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from . import LastFMRecommendationsEnhancedProvider

RESULT_LIMIT = 50
SEED_LIMIT = 3
TRACK_SEED_LIMIT = 15
ARTIST_SEED_LIMIT = 15
RESOLUTION_TARGET_LIMITS = {MediaType.TRACK: 50, MediaType.ALBUM: 35, MediaType.ARTIST: 25}
SEED_RESULT_PERCENT = 20
SOURCE_SEARCH_RESULT_LIMIT = 2
SEARCH_CONCURRENCY_LIMIT = 5
_SEARCH_SEMAPHORE = asyncio.Semaphore(SEARCH_CONCURRENCY_LIMIT)
CACHE_CATEGORY_RESOLUTION = 1
RESOLUTION_CACHE_TTL = 90 * 24 * 60 * 60
RESOLUTION_MISS_CACHE_TTL = 24 * 60 * 60
RESOLUTION_RETRY_DELAY = 5 * 60


@dataclass
class ResolutionState:
    """Share search results, unavailable sources and retry timing within one user refresh."""

    source_items: dict[MediaType, UniqueList[Artist | Album | Track]] = field(default_factory=dict)
    source_queries: set[tuple[MediaType, str, str]] = field(default_factory=set)
    unavailable_sources: set[str] = field(default_factory=set)
    has_blocked_mbid_lookup: bool = False
    has_incomplete_searches: bool = False
    retry_after: float = 0


@dataclass(frozen=True)
class PersonalSeed:
    """A weighted listening or favorite signal, limited to the requesting user."""

    artist: str
    track: str = ""
    weight: float = 1
    identity: str = ""


@dataclass(frozen=True)
class DiscoveryFolder:
    """An implemented discovery row and its bounded candidate source."""

    item_id: str
    name: str
    media_type: MediaType
    method: str = ""
    country: str = ""
    seed_type: MediaType | None = None
    max_seeds: int = SEED_LIMIT


DISCOVERY_FOLDERS = (
    DiscoveryFolder(
        "global_top_tracks", "Worldwide track charts", MediaType.TRACK, "chart.getTopTracks"
    ),
    DiscoveryFolder(
        "de_top_tracks", "Track charts for Germany", MediaType.TRACK, "geo.getTopTracks", "Germany"
    ),
    DiscoveryFolder(
        "global_top_artists", "Worldwide artist charts", MediaType.ARTIST, "chart.getTopArtists"
    ),
    DiscoveryFolder(
        "de_top_artists",
        "Artist charts for Germany",
        MediaType.ARTIST,
        "geo.getTopArtists",
        "Germany",
    ),
    DiscoveryFolder(
        "user_recommendations",
        "Track recommendations",
        MediaType.TRACK,
        "track.getSimilar",
        seed_type=MediaType.TRACK,
        max_seeds=TRACK_SEED_LIMIT,
    ),
    DiscoveryFolder(
        "user_artist_recommendations",
        "Artist recommendations",
        MediaType.ARTIST,
        "artist.getSimilar",
        seed_type=MediaType.ARTIST,
        max_seeds=10,
    ),
    DiscoveryFolder(
        "user_album_recommendations",
        "Album recommendations",
        MediaType.ALBUM,
        "artist.getTopAlbums",
        seed_type=MediaType.ARTIST,
    ),
    DiscoveryFolder("user_rediscovery_tracks", "Not heard in a while", MediaType.TRACK),
)


def normalize_scores[ScoreKey](scores: dict[ScoreKey, float]) -> dict[ScoreKey, float]:
    """Normalize nonnegative scores to the strongest score, preserving empty and zero pools."""
    maximum = max(scores.values(), default=0)
    return {key: max(score, 0) / maximum if maximum > 0 else 0 for key, score in scores.items()}


def similarity_weights(items: list[LastFMItem], *, normalize_matches: bool = False) -> list[float]:
    """Use explicit match scores, optionally normalized per response, with rank fallback."""
    scores = {
        rank: item.similarity_weight
        for rank, item in enumerate(items, 1)
        if item.similarity_weight is not None
    }
    if normalize_matches:
        scores = normalize_scores(scores)
    return [scores.get(rank, 1 / rank) for rank in range(1, len(items) + 1)]


def random_candidate_pool(items: list[LastFMItem], max_items: int) -> list[LastFMItem]:
    """Shuffle a copy of the strongest candidates, capped at three times the result target."""
    candidates = items[: 3 * max_items]
    shuffle(candidates)
    return candidates


class LastFMDiscovery:
    """Build per-request results without retaining a user's seeds or resolved media."""

    def __init__(
        self,
        provider: LastFMRecommendationsEnhancedProvider,
        *,
        state: ResolutionState | None = None,
    ) -> None:
        """Use the active provider's API, logger and user-scoped controller calls."""
        self.provider = provider
        self._source_search_count = 0
        self.state = state if state is not None else ResolutionState()
        self._has_loading_sources = False

    async def build_items(
        self, folder: DiscoveryFolder
    ) -> UniqueList[Artist | Album | Track | ItemMapping]:
        """Build one row, preserving chart order or combining a fixed number of personal seeds."""
        started = perf_counter()
        logger = self.provider.logger
        logger.debug(
            "Building Last.fm discovery folder=%s limit=%d",
            folder.item_id,
            RESOLUTION_TARGET_LIMITS[folder.media_type] if folder.method else RESULT_LIMIT,
        )
        result: UniqueList[Artist | Album | Track | ItemMapping] = UniqueList()
        try:
            if not folder.method:
                result.extend(await self._rediscovery())
            else:
                seeds = (
                    await self.get_seeds(
                        folder.seed_type,
                        limit=folder.max_seeds,
                    )
                    if folder.seed_type is not None
                    else []
                )
                candidates = await self._candidates(folder, seeds=seeds)
                if folder.seed_type is not None:
                    candidates = random_candidate_pool(
                        candidates, RESOLUTION_TARGET_LIMITS[folder.media_type]
                    )
                logger.debug(
                    "Last.fm folder=%s candidates=%d phase=resolve", folder.item_id, len(candidates)
                )
                resolved = await self.resolve_items(
                    candidates,
                    folder.media_type,
                    seeds=seeds,
                )
                result.extend(resolved)
        except MusicAssistantError as err:
            logger.debug(
                "Last.fm discovery failed folder=%s error_type=%s elapsed=%.3fs",
                folder.item_id,
                type(err).__name__,
                perf_counter() - started,
            )
            raise
        logger.debug(
            "Last.fm discovery completed folder=%s items=%d elapsed=%.3fs",
            folder.item_id,
            len(result),
            perf_counter() - started,
        )
        return result

    async def get_seeds(
        self, media_type: MediaType, *, limit: int = SEED_LIMIT
    ) -> list[PersonalSeed]:
        """Combine personal listening and favorites into bounded, weighted seeds."""
        limit = min(
            max(limit, 1), TRACK_SEED_LIMIT if media_type == MediaType.TRACK else ARTIST_SEED_LIMIT
        )
        weights: dict[str, float] = {}
        history = {}
        for period, factor in (("month", 1.0), ("week", 2.0), ("all_time", 0.1)):
            rows = await self.provider.mass.statistics.get_top_items(
                media_type=media_type, period=period, limit=50
            )
            for row in rows:
                if row.item.available:
                    history[row.item.uri] = row.item
                    weights[row.item.uri] = weights.get(row.item.uri, 0) + row.play_count * factor
        controller = self.provider.mass.music.get_controller(media_type)
        by_uri: dict[str, Artist | Album | Track] = {}
        favorites = await controller.get_library_items_by_query(favorite=True, limit=50)
        for item in favorites:
            if item.available:
                history[item.uri] = item
                by_uri[item.uri] = item
                weights.setdefault(item.uri, 0)
        weights = {
            uri: score + (FAVORITE_BONUS if uri in by_uri else 0)
            for uri, score in normalize_scores(weights).items()
        }
        result: list[PersonalSeed] = []
        seen: set[tuple[str, str]] = set()
        metadata_lookup_limit = limit if media_type == MediaType.TRACK else SEED_LIMIT
        metadata_lookups_remaining = metadata_lookup_limit
        for uri in sorted(weights, key=lambda uri: weights[uri], reverse=True):
            item = by_uri.get(uri, history[uri])
            if (
                media_type == MediaType.TRACK
                and not isinstance(item, Track)
                and metadata_lookups_remaining
            ):
                metadata_lookups_remaining -= 1
                try:
                    item = await controller.get(
                        item.item_id, item.provider, allow_update_metadata=False
                    )
                except MediaNotFoundError:
                    self.provider.logger.debug("Last.fm seed skipped reason=track_not_found")
                    continue
            if media_type == MediaType.ARTIST:
                seed = PersonalSeed(item.name, weight=weights[uri], identity=uri)
            elif isinstance(item, Track) and item.available and item.artists:
                seed = PersonalSeed(item.artists[0].name, item.name, weights[uri], identity=uri)
            else:
                continue
            key = (seed.artist.casefold(), seed.track.casefold())
            if key not in seen:
                result.append(seed)
                seen.add(key)
            if len(result) == limit:
                break
        self.provider.logger.debug(
            "Last.fm seeds media_type=%s periods=month:30d,week:7d,all_time "
            "history=%d selected=%d metadata_lookups=%d",
            media_type.value,
            len(history),
            len(result),
            metadata_lookup_limit - metadata_lookups_remaining,
        )
        return result

    async def resolve_items(
        self,
        candidates: list[LastFMItem],
        media_type: MediaType,
        *,
        max_tracks_per_artist: int | None = None,
        seeds: Sequence[PersonalSeed] = (),
        max_items: int | None = None,
        excluded_uris: set[str] | None = None,
    ) -> UniqueList[Artist | Album | Track]:
        """
        Fill the media target with at most 20% seeds, respecting artist diversity.

        :param excluded_uris: Previously accepted identities that do not consume the target.
        """
        target_limit = (
            min(RESOLUTION_TARGET_LIMITS[media_type], max_items)
            if max_items is not None
            else RESOLUTION_TARGET_LIMITS[media_type]
        )
        self._has_loading_sources = any(
            config.status == ProviderStatus.LOADING
            for config in await self.provider.mass.config.get_provider_configs(
                provider_type=ProviderType.MUSIC
            )
        )
        misses = 0
        duplicates = 0
        attempted = 0
        cached_hits = 0
        searches_before = self._source_search_count
        found: dict[int, Artist | Album | Track] = {}
        for index, candidate in enumerate(candidates):
            selected = self._select_resolution_indices(
                found, max_tracks_per_artist, seeds, target_limit
            )
            if len(selected) >= target_limit:
                break
            attempted += 1
            item, missed = await self._get_cached_resolution(candidate, media_type)
            if item is not None:
                cached_hits += 1
            elif missed:
                misses += 1
                continue
            else:
                item = self._find_match(
                    candidate, self.state.source_items.get(media_type, ()), require_exact_title=True
                )
                if item is None:
                    try:
                        item = await self._resolve_missing(candidate, media_type)
                    except ResourceTemporarilyUnavailable:
                        self.state.has_incomplete_searches = True
                        self.state.retry_after = max(self.state.retry_after, RESOLUTION_RETRY_DELAY)
                        misses += 1
                        continue
                cache_key = self._resolution_cache_key(candidate, media_type)
                await self.provider.mass.cache.set(
                    key=cache_key if item is not None else f"miss_{cache_key}",
                    data=item.to_dict() if item is not None else True,
                    category=CACHE_CATEGORY_RESOLUTION,
                    provider=self.provider.instance_id,
                    expiration=RESOLUTION_CACHE_TTL
                    if item is not None
                    else RESOLUTION_MISS_CACHE_TTL,
                )
            if item is None:
                misses += 1
            elif excluded_uris is not None and item.uri in excluded_uris:
                misses += 1
                duplicates += 1
            elif item.uri not in {value.uri for value in found.values()}:
                found[index] = item
            else:
                misses += 1
                duplicates += 1
        selected = self._select_resolution_indices(
            found, max_tracks_per_artist, seeds, target_limit
        )
        result = UniqueList(
            self._with_attribution(found[index], candidates[index], media_type)
            for index in selected[:target_limit]
        )
        self.provider.logger.debug(
            "Last.fm resolution candidates=%d cached=%d resolved=%d "
            "searches=%d misses=%d limit=%d "
            "missing_artist=%d deferred=%d duplicates=%d filtered=%d",
            len(candidates),
            cached_hits,
            len(result),
            self._source_search_count - searches_before,
            misses,
            target_limit,
            sum(not item.artist for item in candidates) if media_type != MediaType.ARTIST else 0,
            len(candidates) - attempted,
            duplicates,
            len(found) - len(selected),
        )
        return result

    @staticmethod
    def _select_resolution_indices(
        found: dict[int, Artist | Album | Track],
        max_tracks_per_artist: int | None,
        seeds: Sequence[PersonalSeed] = (),
        target_limit: int = RESULT_LIMIT,
    ) -> list[int]:
        """Select eligible matches in candidate order before applying the media target."""
        selected: list[int] = []
        artist_counts: dict[str, int] = {}
        seed_count = 0
        seed_limit = target_limit * SEED_RESULT_PERCENT // 100
        for index in sorted(found):
            item = found[index]
            is_seed = LastFMDiscovery._is_seed(item, seeds)
            if is_seed and seed_count >= seed_limit:
                continue
            if max_tracks_per_artist is not None:
                if not isinstance(item, Track) or not item.artists:
                    continue
                artist = item.artists[0].name.casefold()
                if artist_counts.get(artist, 0) >= max_tracks_per_artist:
                    continue
                artist_counts[artist] = artist_counts.get(artist, 0) + 1
            seed_count += is_seed
            selected.append(index)
        return selected

    @staticmethod
    def _is_seed(item: Artist | Album | Track, seeds: Sequence[PersonalSeed]) -> bool:
        """Recognize the same seed across MA sources without conflating artists and their releases."""
        return any(
            item.uri == seed.identity
            or (
                isinstance(item, Artist)
                and not seed.track
                and item.name.casefold() == seed.artist.casefold()
            )
            or (
                isinstance(item, Track)
                and bool(seed.track)
                and item.name.casefold() == seed.track.casefold()
                and any(artist.name.casefold() == seed.artist.casefold() for artist in item.artists)
            )
            for seed in seeds
        )

    async def _candidates(
        self, folder: DiscoveryFolder, *, seeds: Sequence[PersonalSeed] | None = None
    ) -> list[LastFMItem]:
        """Fetch one bounded page per seed, combine weights and retain the strongest candidates."""
        if folder.seed_type is None:
            params = {"country": folder.country} if folder.country else {}
            return await self.provider.api.get_items(folder.method, params, limit=RESULT_LIMIT)
        if seeds is None:
            seeds = await self.get_seeds(
                folder.seed_type,
                limit=folder.max_seeds,
            )
        self.provider.logger.debug(
            "Last.fm folder=%s seeds=%d phase=fetch", folder.item_id, len(seeds)
        )
        scores: dict[tuple[str, str], float] = {}
        candidates: dict[tuple[str, str], LastFMItem] = {}
        for index, seed in enumerate(seeds, 1):
            params = {"artist": seed.artist}
            if seed.track:
                params["track"] = seed.track
            items = await self.provider.api.get_items(
                folder.method,
                params,
                limit=RESULT_LIMIT,
            )
            weights = similarity_weights(
                items, normalize_matches=folder.method == "track.getSimilar"
            )
            for item, weight in zip(items, weights, strict=True):
                key = (item.name.casefold(), item.artist.casefold())
                candidates.setdefault(key, item)
                scores[key] = scores.get(key, 0) + seed.weight * weight
            self.provider.logger.debug(
                "Last.fm folder=%s seed=%d/%d received=%d",
                folder.item_id,
                index,
                len(seeds),
                len(items),
            )
        scores = normalize_scores(scores)
        return [
            candidates[key] for key in sorted(scores, key=lambda key: scores[key], reverse=True)
        ]

    async def _resolve_missing(
        self, candidate: LastFMItem, media_type: MediaType
    ) -> Artist | Album | Track | None:
        """Search each eligible streaming source until it returns a verified match."""
        controller = self.provider.mass.music.get_controller(media_type)
        if media_type != MediaType.ARTIST and not candidate.artist:
            return None
        providers = [
            source
            for source in self.provider.mass.music.providers
            if source.instance_id != self.provider.instance_id
            and source.available
            and source.is_streaming_provider
            and ProviderFeature.SEARCH in source.supported_features
        ]
        linked_item, lookup_failed = await self._resolve_via_musicbrainz(candidate, media_type)
        if linked_item is not None:
            return linked_item
        failed = not providers or self._has_loading_sources or lookup_failed
        title, _version = parse_title_and_version(candidate.name, strip_for_search=True)
        query = (
            f"{candidate.artist} - {title}"
            if candidate.artist and media_type != MediaType.ARTIST
            else title
        )
        for source in providers:
            query_key = (media_type, source.instance_id, query.casefold())
            if source.instance_id in self.state.unavailable_sources:
                failed = True
                continue
            if query_key in self.state.source_queries:
                continue
            async with _SEARCH_SEMAPHORE:
                if not source.available:
                    failed = True
                    continue
                self._source_search_count += 1
                self.provider.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Last.fm source search started media_type=%s source=%s query=%s",
                    media_type.value,
                    source.instance_id,
                    query,
                )
                try:
                    results = await controller.search(
                        query, source.instance_id, limit=SOURCE_SEARCH_RESULT_LIMIT
                    )
                except MusicAssistantError as error:
                    failed = True
                    self.state.unavailable_sources.add(source.instance_id)
                    if isinstance(error, RateLimited):
                        self.state.has_blocked_mbid_lookup = True
                    self.state.retry_after = max(
                        self.state.retry_after,
                        error.backoff_time
                        if isinstance(error, RateLimited)
                        else RESOLUTION_RETRY_DELAY,
                    )
                    self.provider.logger.debug(
                        "Last.fm source resolution failed media_type=%s error_type=%s",
                        media_type.value,
                        type(error).__name__,
                    )
                    continue
                if not source.available:
                    failed = True
                    continue
                self.state.source_queries.add(query_key)
                reusable = self.state.source_items.setdefault(media_type, UniqueList())
                reusable.extend(
                    item
                    for item in results
                    if isinstance(item, Artist | Album | Track)
                    and item.media_type == media_type
                    and item.available
                )
                self.provider.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Last.fm source search completed media_type=%s source=%s query=%s "
                    "results=%d reusable=%d",
                    media_type.value,
                    source.instance_id,
                    query,
                    len(results),
                    len(reusable),
                )
                if item := self._find_match(
                    candidate,
                    (
                        value
                        for value in results
                        if isinstance(value, Artist | Album | Track)
                        and value.media_type == media_type
                    ),
                ):
                    return item
        if failed:
            raise ResourceTemporarilyUnavailable(
                "Music sources were unavailable during recommendation resolution"
            )
        return None

    async def _resolve_via_musicbrainz(
        self, candidate: LastFMItem, media_type: MediaType
    ) -> tuple[Artist | Album | Track | None, bool]:
        """Resolve a MusicBrainz identity through MA, marking unavailable lookups as incomplete."""
        if not candidate.mbid:
            return None, False
        if self.state.has_blocked_mbid_lookup:
            return None, True
        external_id_type = {
            MediaType.ARTIST: ExternalID.MB_ARTIST,
            MediaType.ALBUM: ExternalID.MB_ALBUM,
            MediaType.TRACK: ExternalID.MB_RECORDING,
        }[media_type]
        controller = self.provider.mass.music.get_controller(media_type)
        try:
            async with _SEARCH_SEMAPHORE:
                linked_item = await controller.get_item_by_external_id(
                    candidate.mbid, external_id_type
                )
        except MediaNotFoundError:
            return None, False
        except MusicAssistantError as error:
            self.provider.logger.debug(
                "Last.fm MusicBrainz resolution failed media_type=%s error_type=%s",
                media_type.value,
                type(error).__name__,
            )
            if isinstance(error, RateLimited):
                self.state.has_blocked_mbid_lookup = True
                self.state.unavailable_sources.update(
                    source.instance_id
                    for source in self.provider.mass.music.providers
                    if source.is_streaming_provider
                )
                self.state.retry_after = max(self.state.retry_after, error.backoff_time)
            return None, True
        if (
            isinstance(linked_item, Artist | Album | Track)
            and linked_item.media_type == media_type
            and self._matches(candidate, linked_item)
        ):
            return linked_item, False
        return None, False

    async def _get_cached_resolution(
        self, candidate: LastFMItem, media_type: MediaType
    ) -> tuple[Artist | Album | Track | None, bool]:
        """Return a still-visible cached match or a confirmed, unexpired streaming miss."""
        cache_key = self._resolution_cache_key(candidate, media_type)
        base_class = {MediaType.ARTIST: Artist, MediaType.ALBUM: Album, MediaType.TRACK: Track}[
            media_type
        ]
        cached = await self.provider.mass.cache.get(
            key=cache_key,
            category=CACHE_CATEGORY_RESOLUTION,
            provider=self.provider.instance_id,
            base_class=base_class,
        )
        if isinstance(cached, base_class) and self._matches(candidate, cached):
            if any(
                source.available
                and source.is_streaming_provider
                and (
                    source.instance_id == cached.provider
                    or (
                        cached.provider == "library"
                        and any(
                            mapping.available and mapping.provider_instance == source.instance_id
                            for mapping in cached.provider_mappings
                        )
                    )
                )
                for source in self.provider.mass.music.providers
            ):
                return cached, False
        missed = await self.provider.mass.cache.get(
            key=f"miss_{cache_key}",
            category=CACHE_CATEGORY_RESOLUTION,
            provider=self.provider.instance_id,
        )
        return None, bool(missed) and not self._has_loading_sources

    def _resolution_cache_key(self, candidate: LastFMItem, media_type: MediaType) -> str:
        """Scope one media identity to the selected user's ordered visible music sources."""
        identity = (
            self.provider.get_setup_value(CONF_RECOMMENDATION_USER_ID),
            [
                (source.instance_id, source.available)
                for source in self.provider.mass.music.providers
                if source.is_streaming_provider
            ],
            media_type.value,
            candidate.mbid or (candidate.artist.casefold(), candidate.name.casefold()),
        )
        return "resolution_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    @classmethod
    def _find_match(
        cls,
        candidate: LastFMItem,
        items: Iterable[Artist | Album | Track],
        *,
        require_exact_title: bool = False,
    ) -> Artist | Album | Track | None:
        """Prefer exact normalized titles over compatible fuzzy matches."""
        title, _version = parse_title_and_version(candidate.name, strip_for_search=True)
        matches = [item for item in items if cls._matches(candidate, item)]
        return next(
            (item for item in matches if compare_strings(title, item.name)),
            matches[0] if matches and not require_exact_title else None,
        )

    @staticmethod
    def _matches(candidate: LastFMItem, item: Artist | Album | Track) -> bool:
        """Verify the title and artist, and reject contradictory recording identities."""
        if not item.available:
            return False
        title, _version = parse_title_and_version(candidate.name, strip_for_search=True)
        credited_title = f"{candidate.artist} - {title}" if candidate.artist else title
        if not compare_strings(title, item.name, strict=False) and not compare_strings(
            credited_title, item.name, strict=False
        ):
            return False
        if isinstance(item, Track | Album) and (
            not candidate.artist
            or not item.artists
            or not any(
                compare_strings(candidate.artist, artist.name, strict=False)
                for artist in item.artists
            )
        ):
            return False
        external_id_type = (
            ExternalID.MB_RECORDING
            if isinstance(item, Track)
            else ExternalID.MB_ALBUM
            if isinstance(item, Album)
            else ExternalID.MB_ARTIST
        )
        mbids = {value for key, value in item.external_ids if key == external_id_type}
        return not candidate.mbid or not mbids or candidate.mbid in mbids

    @staticmethod
    def _with_attribution(
        item: Artist | Album | Track, candidate: LastFMItem, media_type: MediaType
    ) -> Artist | Album | Track:
        """Attach the source link to a copy, never to library or shared search metadata."""
        resolved = deepcopy(item)
        if candidate.url:
            url = candidate.url
        elif media_type == MediaType.ARTIST:
            url = "https://www.last.fm/music/" + quote(candidate.name, safe="")
        else:
            url = "https://www.last.fm/music/" + quote(candidate.artist, safe="")
            url += "/_/" if media_type == MediaType.TRACK else "/"
            url += quote(candidate.name, safe="")
        resolved.metadata.links = {
            *(resolved.metadata.links or ()),
            MediaItemLink(type=LinkType.LASTFM, url=url),
        }
        return resolved

    async def _rediscovery(self) -> UniqueList[ItemMapping]:
        """Subtract the complete current rotation, or fail closed when its bound is reached."""
        statistics = self.provider.mass.statistics
        recent = await statistics.get_top_items(
            media_type=MediaType.TRACK, period="month", limit=500
        )
        if len(recent) == 500:
            self.provider.logger.debug("Last.fm rediscovery skipped reason=recent_history_limit")
            return UniqueList()
        previous = await statistics.get_top_items(
            media_type=MediaType.TRACK, period="all_time", limit=500
        )
        recent_uris = {row.item.uri for row in recent}
        candidates = [
            row.item for row in previous if row.item.available and row.item.uri not in recent_uris
        ]
        candidates = list(UniqueList(candidates))[: 3 * RESULT_LIMIT]
        self.provider.logger.debug(
            "Last.fm rediscovery recent=%d historical=%d candidates=%d",
            len(recent),
            len(previous),
            len(candidates),
        )
        shuffle(candidates)
        return UniqueList(candidates[:RESULT_LIMIT])
