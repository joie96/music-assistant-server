"""Request-scoped discovery; only public Last.fm responses enter the shared MA cache."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from music_assistant_models.enums import ExternalID, LinkType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    MusicAssistantError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, Artist, Track, UniqueList
from music_assistant_models.media_items.metadata import MediaItemLink

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version

from .api_client import LastFMItem

if TYPE_CHECKING:
    from . import LastFMRecommendationsEnhancedProvider

RESULT_LIMIT = 50
SEED_LIMIT = 3
SOURCE_SEARCH_BUDGET = 6
IDENTITY_LOOKUP_BUDGET = 6


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
    ),
    DiscoveryFolder(
        "user_artist_recommendations",
        "Artist recommendations",
        MediaType.ARTIST,
        "artist.getSimilar",
        seed_type=MediaType.ARTIST,
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


class LastFMDiscovery:
    """Build per-request results without retaining a user's seeds or resolved media."""

    def __init__(self, provider: LastFMRecommendationsEnhancedProvider) -> None:
        """Use the active provider's API, logger and user-scoped controller calls."""
        self.provider = provider
        self._source_searches_remaining = SOURCE_SEARCH_BUDGET
        self._identity_lookups_remaining = IDENTITY_LOOKUP_BUDGET

    async def build_items(self, folder: DiscoveryFolder) -> UniqueList[Artist | Album | Track]:
        """Build one row, preserving chart order or combining a fixed number of personal seeds."""
        started = perf_counter()
        logger = self.provider.logger
        logger.debug("Building Last.fm discovery folder=%s limit=%d", folder.item_id, RESULT_LIMIT)
        try:
            if not folder.method:
                result = await self._rediscovery()
            else:
                candidates = await self._candidates(folder)
                logger.debug(
                    "Last.fm folder=%s candidates=%d phase=resolve", folder.item_id, len(candidates)
                )
                result = await self.resolve_items(candidates, folder.media_type)
                if folder.seed_type is not None:
                    result = await self.exclude_rotation(result, folder.media_type)
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
        """Combine personal listening and favorites into at most ten weighted seeds."""
        limit = min(max(limit, 1), 10)
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
        known = (
            await self.get_known_items(
                [LastFMItem(item.name) for item in history.values()], media_type
            )
            if media_type == MediaType.TRACK
            else []
        )
        by_uri = {item.uri: item for item in known}
        favorites = await controller.get_library_items_by_query(favorite=True, limit=50)
        for item in favorites:
            if item.available:
                history[item.uri] = item
                by_uri[item.uri] = item
                weights[item.uri] = weights.get(item.uri, 0) + 5
        result: list[PersonalSeed] = []
        seen: set[tuple[str, str]] = set()
        for uri in sorted(weights, key=lambda uri: weights[uri], reverse=True):
            item = by_uri.get(uri, history[uri])
            if media_type == MediaType.ARTIST:
                seed = PersonalSeed(item.name, weight=weights[uri], identity=uri)
            elif isinstance(item, Track) and item.artists:
                seed = PersonalSeed(item.artists[0].name, item.name, weights[uri], identity=uri)
            else:
                continue
            key = (seed.artist.casefold(), seed.track.casefold())
            if key not in seen:
                result.append(seed)
                seen.add(key)
            if len(result) == limit:
                break
        return result

    async def exclude_rotation(
        self, items: UniqueList[Artist | Album | Track], media_type: MediaType
    ) -> UniqueList[Artist | Album | Track]:
        """Omit known identities from the requesting user's bounded current rotation."""
        recent = await self.provider.mass.statistics.get_top_items(
            media_type=media_type, period="month", limit=500
        )
        recent_uris = {row.item.uri for row in recent}
        return UniqueList(item for item in items if item.uri not in recent_uris)

    async def get_known_items(
        self, candidates: list[LastFMItem], media_type: MediaType
    ) -> list[Artist | Album | Track]:
        """Resolve names in one bounded controller query, never by walking external catalogues."""
        names = sorted({item.name.lower() for item in candidates})
        if not names:
            return []
        controller = self.provider.mass.music.get_controller(media_type)
        params = {f"candidate_{index}": name for index, name in enumerate(names)}
        placeholders = ", ".join(f":{key}" for key in params)
        items = await controller.get_library_items_by_query(
            extra_query_parts=[f"WHERE LOWER({controller.db_table}.name) IN ({placeholders})"],
            extra_query_params=params,
            limit=500,
        )
        return cast("list[Artist | Album | Track]", items)

    async def resolve_items(
        self, candidates: list[LastFMItem], media_type: MediaType
    ) -> UniqueList[Artist | Album | Track]:
        """Prefer the library, then resolve missing items on visible streaming music sources."""
        known = await self.get_known_items(candidates, media_type)
        result: UniqueList[Artist | Album | Track] = UniqueList()
        for candidate in candidates:
            item = next((item for item in known if self._matches(candidate, item)), None)
            if item is None:
                item = await self._resolve_missing(candidate, media_type)
            if item is not None:
                result.append(self._with_attribution(item, candidate, media_type))
        self.provider.logger.debug(
            "Last.fm resolution candidates=%d known=%d resolved=%d",
            len(candidates),
            len(known),
            len(result),
        )
        return result

    async def _candidates(self, folder: DiscoveryFolder) -> list[LastFMItem]:
        """Limit every source to one page; personal discovery uses at most three seeds."""
        if folder.seed_type is None:
            params = {"country": folder.country} if folder.country else {}
            return await self.provider.api.get_items(folder.method, params, limit=RESULT_LIMIT)
        seeds = await self.get_seeds(folder.seed_type)
        self.provider.logger.debug(
            "Last.fm folder=%s seeds=%d phase=fetch", folder.item_id, len(seeds)
        )
        scores: dict[tuple[str, str], float] = {}
        candidates: dict[tuple[str, str], LastFMItem] = {}
        for index, seed in enumerate(seeds, 1):
            params = {"artist": seed.artist}
            if seed.track:
                params["track"] = seed.track
            items = await self.provider.api.get_items(folder.method, params, limit=20)
            for rank, item in enumerate(items, 1):
                key = (item.name.casefold(), item.artist.casefold())
                candidates.setdefault(key, item)
                scores[key] = scores.get(key, 0) + max(seed.weight, 1) / rank
            self.provider.logger.debug(
                "Last.fm folder=%s seed=%d/%d received=%d",
                folder.item_id,
                index,
                len(seeds),
                len(items),
            )
        seed_names = {
            (seed.artist if folder.media_type == MediaType.ARTIST else seed.track).casefold()
            for seed in seeds
        }
        return [
            candidates[key]
            for key in sorted(scores, key=lambda key: scores[key], reverse=True)
            if candidates[key].name.casefold() not in seed_names
        ][:RESULT_LIMIT]

    async def _resolve_missing(
        self, candidate: LastFMItem, media_type: MediaType
    ) -> Artist | Album | Track | None:
        """Spend a fixed identity and source budget rather than resolving a complete catalogue."""
        controller = self.provider.mass.music.get_controller(media_type)
        external_id_type = {
            MediaType.ARTIST: ExternalID.MB_ARTIST,
            MediaType.ALBUM: ExternalID.MB_ALBUM,
            MediaType.TRACK: ExternalID.MB_RECORDING,
        }[media_type]
        if candidate.mbid and self._identity_lookups_remaining:
            self._identity_lookups_remaining -= 1
            library = await controller.get_library_item_by_external_ids(
                {(external_id_type, candidate.mbid)}
            )
            if isinstance(library, Artist | Album | Track) and library.available:
                return library
        if media_type != MediaType.ARTIST and not candidate.artist:
            return None
        feature = {
            MediaType.ARTIST: ProviderFeature.LIBRARY_ARTISTS,
            MediaType.ALBUM: ProviderFeature.LIBRARY_ALBUMS,
            MediaType.TRACK: ProviderFeature.LIBRARY_TRACKS,
        }[media_type]
        providers = [
            source
            for source in self.provider.mass.music.providers
            if source.instance_id != self.provider.instance_id
            and source.available
            and source.is_streaming_provider
            and feature in source.supported_features
            and ProviderFeature.SEARCH in source.supported_features
        ]
        failed = False
        title, _version = parse_title_and_version(candidate.name, strip_for_search=True)
        query = (
            f"{candidate.artist} - {title}"
            if candidate.artist and media_type != MediaType.ARTIST
            else title
        )
        for source in providers:
            if not self._source_searches_remaining:
                break
            self._source_searches_remaining -= 1
            try:
                results = await controller.search(query, source.instance_id, limit=2)
            except RateLimited:
                raise
            except MusicAssistantError as error:
                failed = True
                self.provider.logger.debug(
                    "Last.fm source resolution failed media_type=%s error_type=%s",
                    media_type.value,
                    type(error).__name__,
                )
                continue
            if not source.available:
                failed = True
                continue
            for item in results:
                if isinstance(item, Artist | Album | Track) and self._matches(candidate, item):
                    return await self._prefer_library(item, media_type)
        if failed:
            raise ResourceTemporarilyUnavailable(
                "Music sources were unavailable during recommendation resolution"
            )
        return None

    async def _prefer_library(
        self, item: Artist | Album | Track, media_type: MediaType
    ) -> Artist | Album | Track:
        """Prefer an existing visible library identity revealed by the source's external IDs."""
        if item.external_ids and self._identity_lookups_remaining:
            self._identity_lookups_remaining -= 1
            controller = self.provider.mass.music.get_controller(media_type)
            library = await controller.get_library_item_by_external_ids(item.external_ids)
            if isinstance(library, Artist | Album | Track) and library.available:
                return library
        return item

    @staticmethod
    def _matches(candidate: LastFMItem, item: Artist | Album | Track) -> bool:
        """Verify the title and artist, and reject contradictory recording identities."""
        if not item.available:
            return False
        title, _version = parse_title_and_version(candidate.name, strip_for_search=True)
        if not compare_strings(title, item.name, strict=False):
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

    async def _rediscovery(self) -> UniqueList[Artist | Album | Track]:
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
        ][:RESULT_LIMIT]
        self.provider.logger.debug(
            "Last.fm rediscovery recent=%d historical=%d candidates=%d",
            len(recent),
            len(previous),
            len(candidates),
        )
        items = await self.get_known_items(
            [LastFMItem(item.name) for item in candidates], MediaType.TRACK
        )
        by_uri = {item.uri: item for item in items if item.available}
        return UniqueList(by_uri[item.uri] for item in candidates if item.uri in by_uri)
