"""Youtube Music support for MusicAssistant."""

from __future__ import annotations

import asyncio
import gettext
import importlib
import logging
import math
import time
from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, unquote, urlparse

import ytmusicapi
from duration_parser import parse as parse_str_duration
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    SetupFailedError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from ytmusicapi.constants import SUPPORTED_LANGUAGES
from ytmusicapi.parsers.podcasts import Description

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    DEFAULT_AUDIOBOOK_PODCAST_GENRE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.diagnostics import sanitize_text
from music_assistant.helpers.util import (
    import_module_in_thread,
    infer_album_type,
    install_package,
    parse_title_and_version,
)
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from .file_cache import CachedStreamInfo, FileCache
from .helpers import (
    YTMSearchFilter,
    determine_recommendation_icon,
    get_album,
    get_artist,
    get_artist_albums,
    get_home,
    get_playlist,
    get_podcast,
    get_podcast_episode,
    get_song_radio_tracks,
    get_track,
    search,
)

if TYPE_CHECKING:
    from types import ModuleType

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_CACHE_ENABLED = "cache_enabled"
CONF_CACHE_DIR = "cache_dir"
CONF_CACHE_MAX_SIZE_MB = "cache_max_size_mb"

YTM_DOMAIN = "https://music.youtube.com"
VARIOUS_ARTISTS_YTM_ID = "UCUTXlgdcKU5vfzFqHOWIvkA"
PODCAST_EPISODE_SPLITTER = "|"
PACKAGES_TO_INSTALL = ("yt-dlp[default]",)
DEFAULT_STREAM_URL_EXPIRATION = 3600  # 1 hour
PREFERRED_AUDIO_FORMAT_IDS = ("141", "251", "140", "171", "250", "249", "139")
EXTRACT_ATTEMPTS = 3
EXTRACT_RETRY_DELAY = 1.0
PREFETCH_AHEAD = 3

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.ALBUM_VERSIONS,
}


# ruff: noqa: PLW2901


class _YtDlpLogger:
    """Route sanitized library output to MA; the provider owns failure severity."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, message: str) -> None:
        level = VERBOSE_LOG_LEVEL if message.startswith("[debug]") else logging.DEBUG
        self._logger.log(level, "yt-dlp: %s", sanitize_text(message))

    def warning(self, message: str) -> None:
        self._logger.log(logging.WARNING, "yt-dlp: %s", sanitize_text(message))

    def error(self, message: str) -> None:
        self._logger.log(logging.DEBUG, "yt-dlp: %s", sanitize_text(message))


def _artist_is_resolvable(artist_obj: dict[str, Any]) -> bool:
    """Check if a YTM artist object can be mapped to an artist item."""
    return bool(
        artist_obj.get("id")
        or artist_obj.get("channelId")
        or artist_obj.get("name") == "Various Artists"
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YoutubeMusicEnhancedProvider(mass, manifest, config, SUPPORTED_FEATURES)


class YoutubeMusicEnhancedProvider(RecommendationPayloadMixin, MusicProvider):
    """Public ytmusicapi metadata with anonymous yt-dlp audio and a disk cache."""

    _yt_dlp_module: ModuleType | None = None
    _file_cache: FileCache | None = None

    @property
    def max_concurrent_streams(self) -> int:
        """Bound parallel audio requests to avoid excessive YouTube traffic."""
        return 3

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_CACHE_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_CACHE_DIR,
                type=ConfigEntryType.STRING,
                default_value=str(Path(self.mass.cache_path) / "ytmusic_enhanced"),
            ),
            ConfigEntry(
                key=CONF_CACHE_MAX_SIZE_MB,
                type=ConfigEntryType.INTEGER,
                default_value=1024,
                range=(0, 1048576),
            ),
        )

    async def handle_async_init(self) -> None:
        """Set up the YTMusic provider."""
        self._downloads: set[asyncio.Task[CachedStreamInfo | None]] = set()
        self._prefetch_tasks: dict[str, asyncio.Task[None]] = {}
        self._is_unloading = False
        await self._install_packages()
        self._file_cache = None
        if self.get_config_value(CONF_CACHE_ENABLED, True):
            cache_dir = self.get_config_value(
                CONF_CACHE_DIR,
                str(Path(self.mass.cache_path) / "ytmusic_enhanced"),
                return_type=str,
            )
            max_size_mb = self.get_config_value(CONF_CACHE_MAX_SIZE_MB, 1024, return_type=int)
            if not cache_dir.strip() or max_size_mb < 0:
                raise SetupFailedError("Provide a cache directory and a nonnegative cache size")
            self._file_cache = FileCache(
                Path(cache_dir).expanduser().resolve() / self.instance_id,
                max_size_mb * 1024 * 1024,
                logger=self.logger,
            )
            try:
                await asyncio.to_thread(self._file_cache.ensure_ready)
            except OSError as err:
                raise SetupFailedError(
                    "Cannot use the configured YouTube audio cache directory"
                ) from err
            self.logger.debug(
                "Audio cache enabled: directory=%s, limit=%s bytes (0 = unlimited)",
                self._file_cache.cache_dir,
                self._file_cache.max_size_bytes,
            )
        else:
            self.logger.debug("Audio cache and queue prefetch disabled")
        # get default language (that is supported by YTM)
        mass_locale = self.mass.metadata.locale
        for lang_code in SUPPORTED_LANGUAGES:
            if lang_code in (mass_locale, mass_locale.split("_")[0]):
                self.language = lang_code
                break
        else:
            self.language = "en"

    async def unload(self, is_removed: bool = False) -> None:
        """Wait for disk writers before unloading; cached audio persists across restarts."""
        self._is_unloading = True
        prefetch_tasks = tuple(self._prefetch_tasks.values())
        self.logger.debug(
            "Stopping %s prefetch tasks and waiting for %s audio cache writers",
            len(prefetch_tasks),
            len(self._downloads),
        )
        for task in prefetch_tasks:
            task.cancel()
        await asyncio.gather(*prefetch_tasks, return_exceptions=True)
        if self._downloads:
            await asyncio.gather(*self._downloads, return_exceptions=True)
        await super().unload(is_removed)

    # the checksum invalidates entries cached before the search was pinned to English
    @use_cache(3600 * 24 * 7, cache_checksum="english_search_v1")  # Cache for 7 days
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        parsed_results = SearchResults()
        ytm_filter: YTMSearchFilter | None = None
        if len(media_types) == 1:
            # YTM does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.ARTIST:
                ytm_filter = "artists"
            if media_types[0] == MediaType.ALBUM:
                ytm_filter = "albums"
            if media_types[0] == MediaType.TRACK:
                ytm_filter = "songs"
            if media_types[0] == MediaType.PLAYLIST:
                ytm_filter = "playlists"
            if media_types[0] == MediaType.PODCAST:
                ytm_filter = "podcasts"
            if media_types[0] == MediaType.RADIO:
                # bit of an edge case but still good to handle
                return parsed_results
        results = await search(
            query=search_query,
            ytm_filter=ytm_filter,
            limit=limit,
        )
        artists: list[Artist | ItemMapping] = []
        albums: list[Album | ItemMapping] = []
        playlists: list[Playlist | ItemMapping] = []
        tracks: list[Track | ItemMapping] = []
        podcasts: list[Podcast | ItemMapping] = []
        for result in results:
            try:
                if result["resultType"] == "artist" and MediaType.ARTIST in media_types:
                    artists.append(self._parse_artist(result))
                elif result["resultType"] == "album" and MediaType.ALBUM in media_types:
                    albums.append(self._parse_album(result))
                elif result["resultType"] == "playlist" and MediaType.PLAYLIST in media_types:
                    playlists.append(self._parse_playlist(result))
                elif result["resultType"] == "podcast" and MediaType.PODCAST in media_types:
                    if podcast := self._parse_browse_podcast(result):
                        podcasts.append(podcast)
                elif (
                    result["resultType"] in ("song", "video")
                    and MediaType.TRACK in media_types
                    and (track := self._parse_track(result))
                ):
                    tracks.append(track)
            except InvalidDataError:
                pass  # ignore invalid item
        parsed_results.artists = artists
        parsed_results.albums = albums
        parsed_results.playlists = playlists
        parsed_results.tracks = tracks
        parsed_results.podcasts = podcasts
        return parsed_results

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if album_obj := await get_album(
            prov_album_id=prov_album_id,
            language=self.language,
        ):
            return self._parse_album(album_obj=album_obj, album_id=prov_album_id)
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)  # Cache for 30 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album_obj = await get_album(
            prov_album_id=prov_album_id,
            language=self.language,
        )
        if not album_obj.get("tracks"):
            return []
        album_artists = [
            artist for artist in album_obj.get("artists") or [] if _artist_is_resolvable(artist)
        ]
        tracks = []
        for track_number, track_obj in enumerate(album_obj["tracks"], 1):
            # YTM omits the artist id on some album tracks, which drops them below.
            # Credit those to the album artist, like YTM's own UI does.
            if album_artists and not any(
                _artist_is_resolvable(artist) for artist in track_obj.get("artists") or []
            ):
                track_obj = {**track_obj, "artists": album_artists}
            try:
                track = self._parse_track(track_obj=track_obj, track_number=track_number)
            except InvalidDataError:
                continue
            tracks.append(track)
        return tracks

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_album_versions(self, prov_album_id: str) -> list[Album]:
        """
        Get albums that YTM has indicated as alternate versions to the given album.

        YTM won't surface these variants via search, so we must explicitly grab
        them out of the other_versions field.
        """
        if album_obj := await get_album(
            prov_album_id=prov_album_id,
            language=self.language,
        ):
            return [
                self._parse_album(album_obj=ov, album_id=ov["browseId"])
                for ov in album_obj.get("other_versions", [])
            ]
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if artist_obj := await get_artist(prov_artist_id=prov_artist_id, language=self.language):
            return self._parse_artist(artist_obj=artist_obj)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if track_obj := await get_track(
            prov_track_id=prov_track_id,
            language=self.language,
        ):
            return self._parse_track(track_obj)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if playlist_obj := await get_playlist(
            prov_playlist_id=prov_playlist_id,
            language=self.language,
        ):
            return self._parse_playlist(playlist_obj)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 3, allow_expired_cache=True)  # Cache for 3 hours
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return playlist tracks for the given provider playlist id."""
        if page > 0:
            # paging not supported, we always return the whole list at once
            return []
        # Add a try to prevent MA from stopping syncing whenever we fail a single playlist
        try:
            playlist_obj = await get_playlist(
                prov_playlist_id=prov_playlist_id,
                language=self.language,
            )
        except KeyError as ke:
            self.logger.warning("Could not load playlist: %s: %s", prov_playlist_id, ke)
            return []
        if "tracks" not in playlist_obj:
            return []
        result = []
        # TODO: figure out how to handle paging in YTM
        for index, track_obj in enumerate(playlist_obj["tracks"], 1):
            if track_obj["isAvailable"]:
                # Playlist tracks sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    if track := self._parse_track(track_obj):
                        track.position = index
                        result.append(track)
                except InvalidDataError:
                    if track := await self.get_track(track_obj["videoId"]):
                        track.position = index
                        result.append(track)
        # YTM doesn't seem to support paging so we ignore offset and limit
        return result

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)  # Cache for 7 days
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)
        artists = []

        for similar_artist in artist_obj.get("related", {}).get("results", []):
            # ytmusicapi returns related artists as if they were tracks,
            # reshape into something _parse_artist() will understand.
            fake_artist = {
                "channelId": similar_artist["browseId"],
                "name": similar_artist["title"],
                "thumbnails": similar_artist["thumbnails"],
            }
            artists.append(self._parse_artist(fake_artist))

        return artists[:limit]

    @use_cache(3600 * 24 * 7, cache_checksum="v1", allow_expired_cache=True)  # Cache for 7 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)

        # get_artist() only embeds ~10 item previews, so fetch each section in full.
        # "singles" covers singles and EPs, "shows" covers radio shows and audio dramas.
        sections = [
            (key, section)
            for key in ("albums", "singles", "shows")
            if (section := artist_obj.get(key))
        ]
        to_hydrate = [
            (key, section)
            for key, section in sections
            if section.get("browseId") and section.get("params")
        ]
        hydrated_sections = await asyncio.gather(
            *(
                get_artist_albums(
                    channel_id=section["browseId"],
                    params=section["params"],
                    language=self.language,
                )
                for _key, section in to_hydrate
            ),
            return_exceptions=True,
        )
        sections_by_key = dict(
            zip((key for key, _section in to_hydrate), hydrated_sections, strict=True)
        )

        seen: set[str] = set()
        albums: list[Album] = []
        for key, section in sections:
            result = sections_by_key.get(key)
            if isinstance(result, BaseException):
                if isinstance(result, (KeyError, IndexError, TypeError)):
                    # ytmusicapi fails to parse some empty sections; keep the preview instead.
                    self.logger.warning(
                        "Failed to hydrate YouTube Music artist %s section %r, "
                        "using the inline preview instead",
                        artist_obj.get("name", prov_artist_id),
                        key,
                        exc_info=result,
                    )
                    items = section.get("results", [])
                else:
                    # Network/auth errors should not be cached as a complete discography.
                    raise result
            else:
                items = result if result is not None else section.get("results", [])

            for album_obj in items:
                browse_id = album_obj.get("browseId")
                if not browse_id or browse_id in seen:
                    continue
                seen.add(browse_id)
                if not album_obj.get("artists"):
                    album_obj["artists"] = [
                        {"id": artist_obj["channelId"], "name": artist_obj["name"]}
                    ]
                albums.append(self._parse_album(album_obj, browse_id))

        return albums

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)  # Cache for 7 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 25 most popular tracks for the given artist."""
        artist_obj = await get_artist(prov_artist_id=prov_artist_id)
        if artist_obj.get("songs") and artist_obj["songs"].get("browseId"):
            prov_playlist_id = artist_obj["songs"]["browseId"]
            playlist_tracks = await self.get_playlist_tracks(prov_playlist_id)
            return playlist_tracks[:25]
        return []

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get the full details of a Podcast."""
        podcast_obj = await get_podcast(prov_podcast_id)
        return self._parse_podcast(podcast_obj)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes from a podcast."""
        podcast_obj = await get_podcast(prov_podcast_id)
        podcast_obj["podcastId"] = prov_podcast_id
        podcast = self._parse_podcast(podcast_obj)
        episodes = podcast_obj.get("episodes", [])
        total = len(episodes)
        # API lists newest-first; number down so bigger position = newer
        for idx, episode_obj in enumerate(episodes):
            episode = self._parse_podcast_episode(episode_obj, podcast)
            episode.position = total - idx
            yield episode

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single Podcast Episode."""
        podcast_id, episode_id = prov_episode_id.split(PODCAST_EPISODE_SPLITTER)
        podcast = await self.get_podcast(podcast_id)
        episode_obj = await get_podcast_episode(episode_id)
        episode = self._parse_podcast_episode(episode_obj, podcast)
        episode.position = 0
        return episode

    @use_cache(3600 * 24, allow_expired_cache=True)  # Cache for 1 day
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        result = await get_song_radio_tracks(prov_item_id=prov_track_id, limit=limit)
        if "tracks" in result:
            tracks = []
            for track in result["tracks"]:
                # Playlist tracks sometimes do not have a valid artist id
                # In that case, call the API for track details based on track id
                try:
                    track = self._parse_track(track)
                    if track:
                        tracks.append(track)
                except InvalidDataError:
                    if track := await self.get_track(track["videoId"]):
                        tracks.append(track)
            return tracks
        return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        video_id = item_id
        if media_type == MediaType.PODCAST_EPISODE:
            video_id = item_id.split(PODCAST_EPISODE_SPLITTER)[1]
        if self._file_cache:
            cached = await asyncio.to_thread(self._file_cache.get_hit, video_id)
            if cached:
                self.logger.debug("Audio cache hit for %s", video_id)
                self._schedule_prefetch(video_id)
                return self._cached_stream_details(item_id, media_type, cached)
            self.logger.debug("Audio cache miss for %s", video_id)
        stream_format = await self._get_stream_format(item_id=video_id)
        if self._file_cache and not stream_format.get("is_live"):
            try:
                cached = await self._cache_stream(video_id, stream_format)
                if cached:
                    self._schedule_prefetch(video_id)
                    return self._cached_stream_details(item_id, media_type, cached)
            except (OSError, ValueError, UnplayableMediaError) as err:
                self.logger.warning(
                    "Audio cache failed for %s; streaming instead (%s): %s",
                    video_id,
                    type(err).__name__,
                    sanitize_text(str(err)),
                )
        url = stream_format["url"]
        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if parsed := parse_qs(urlparse(url).query):
            if expire_ts := parsed.get("expire", [None])[0]:
                with suppress(ValueError):
                    expiration = max(0, int(expire_ts) - int(time.time()))
        headers = dict(stream_format.get("http_headers") or {})
        if any(
            "\r" in str(value) or "\n" in str(value) for pair in headers.items() for value in pair
        ):
            raise UnplayableMediaError("Invalid YouTube audio headers")
        header_args = (
            ["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())]
            if headers
            else []
        )
        is_hls = stream_format.get("protocol") in ("m3u8", "m3u8_native")
        self.logger.debug(
            "Using %s stream for %s (live=%s)",
            "HLS" if is_hls else "HTTP",
            video_id,
            bool(stream_format.get("is_live")),
        )
        stream_details = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=media_type,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(
                    stream_format.get("audio_ext") or stream_format.get("ext", "")
                ),
            ),
            stream_type=StreamType.HLS if is_hls else StreamType.HTTP,
            path=url,
            extra_input_args=header_args,
            can_seek=not stream_format.get("is_live", False),
            allow_seek=not stream_format.get("is_live", False),
            expiration=expiration,
            is_realtime=bool(stream_format.get("is_live")),
        )
        if (audio_channels := stream_format.get("audio_channels")) and str(
            audio_channels
        ).isdigit():
            stream_details.audio_format.channels = int(audio_channels)
        if asr := stream_format.get("asr"):
            stream_details.audio_format.sample_rate = int(asr)
        if bitrate := stream_format.get("abr"):
            stream_details.audio_format.bit_rate = int(bitrate)
        if not stream_details.is_realtime:
            self._schedule_prefetch(video_id)
        return stream_details

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return await self._recommendation_rows_from_payload()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        return await self._recommendation_items_from_payload(item_id)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the home feed and parse its sections into recommendation folders with items."""
        recommendations = await get_home(self.language)

        def _parse_sections() -> list[RecommendationFolder]:
            # building model objects from the raw payload is CPU-bound; run off the event loop
            folders: list[RecommendationFolder] = []
            for section in recommendations:
                folder = RecommendationFolder(
                    name=section["title"],
                    item_id=f"{self.instance_id}_{section['title']}",
                    provider=self.instance_id,
                    icon=determine_recommendation_icon(section["title"]),
                )
                for recommended_item in section.get("contents", []):
                    if not recommended_item:
                        continue  # yeah this seems to happen sometimes ?!
                    if recommended_item.get("videoId"):
                        # Probably a track
                        try:
                            track = self._parse_track(recommended_item)
                            folder.items.append(track)
                        except InvalidDataError:
                            self.logger.debug(
                                "Invalid track in recommendations: %s", recommended_item
                            )
                            continue
                    elif recommended_item.get("playlistId"):
                        # Probably a playlist
                        recommended_item["id"] = recommended_item["playlistId"]
                        del recommended_item["playlistId"]
                        folder.items.append(self._parse_playlist(recommended_item))
                    elif recommended_item.get("subscribers"):
                        # Probably artist, but it's in that same weird album-like format
                        # that you see for the get_similar_artists payload
                        fake_artist = {
                            "channelId": recommended_item["browseId"],
                            "name": recommended_item["title"],
                            "thumbnails": recommended_item["thumbnails"],
                        }
                        folder.items.append(self._parse_artist(fake_artist))
                    elif recommended_item.get("browseId"):
                        if podcast := self._parse_browse_podcast(recommended_item):
                            folder.items.append(podcast)
                        else:
                            # Probably an album
                            folder.items.append(self._parse_album(recommended_item))
                    elif recommended_item.get("videoType") == "MUSIC_VIDEO_TYPE_PODCAST_EPISODE":
                        # Podcast episodes show up here without a videoId/browseId,
                        # so there is no playable item to build from them
                        self.logger.debug(
                            "Skipping podcast episode in recommendation folder: %s",
                            recommended_item.get("title"),
                        )
                        continue
                    else:
                        self.logger.warning(
                            "Unknown item type in recommendation folder: %s",
                            recommended_item,
                        )
                        continue
                folders.append(folder)
            return folders

        return await asyncio.to_thread(_parse_sections)

    def _parse_album(self, album_obj: dict[str, Any], album_id: str | None = None) -> Album:
        """Parse a YT Album response to an Album model object."""
        album_id = album_id or album_obj.get("id") or album_obj.get("browseId")

        if not album_id:
            raise InvalidDataError("Album ID is required but not found")

        if "title" in album_obj:
            name, version = parse_title_and_version(album_obj["title"])
        elif "name" in album_obj:
            name, version = parse_title_and_version(album_obj["name"])
        else:
            name, version = "", ""
        album = Album(
            item_id=album_id,
            name=name,
            version=version,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={album_obj.get('audioPlaylistId')}",
                )
            },
        )
        if album_obj.get("year") and album_obj["year"].isdigit():
            album.year = album_obj["year"]
        if "thumbnails" in album_obj:
            album.metadata.images = UniqueList(self._parse_thumbnails(album_obj["thumbnails"]))
        if description := album_obj.get("description"):
            album.metadata.description = unquote(description)
        if "isExplicit" in album_obj:
            album.metadata.explicit = album_obj["isExplicit"]
        if "artists" in album_obj:
            album.artists = UniqueList(
                [
                    self._get_artist_item_mapping(artist)
                    for artist in album_obj["artists"]
                    if _artist_is_resolvable(artist)
                ]
            )
        if "type" in album_obj:
            album.album_type = _album_type_labels(self.language).get(
                album_obj["type"].casefold(), AlbumType.UNKNOWN
            )

        # Try inference - override if it finds something more specific
        inferred_type = infer_album_type(name, version)
        if inferred_type in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            album.album_type = inferred_type

        return album

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse a YT Artist response to Artist model object."""
        artist_id = None
        if "channelId" in artist_obj:
            artist_id = artist_obj["channelId"]
        elif artist_obj.get("id"):
            artist_id = artist_obj["id"]
        elif artist_obj["name"] == "Various Artists":
            artist_id = VARIOUS_ARTISTS_YTM_ID
        if not artist_id:
            msg = "Artist does not have a valid ID"
            raise InvalidDataError(msg)
        artist = Artist(
            item_id=artist_id,
            name=artist_obj["name"],
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/channel/{artist_id}",
                )
            },
        )
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        if artist_obj.get("thumbnails"):
            artist.metadata.images = self._parse_thumbnails(artist_obj["thumbnails"])
        return artist

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse a YT Playlist response to a Playlist object."""
        playlist_id = playlist_obj["id"]
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=playlist_obj["title"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={playlist_id}",
                )
            },
            is_editable=False,
        )
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]
        if playlist_obj.get("thumbnails"):
            playlist.metadata.images = self._parse_thumbnails(playlist_obj["thumbnails"])

        if authors := playlist_obj.get("author"):
            if isinstance(authors, str):
                playlist.owner = authors
            elif isinstance(authors, list):
                playlist.owner = authors[0]["name"]
            else:
                playlist.owner = authors["name"]
        else:
            playlist.owner = self.name
        return playlist

    def _parse_track(self, track_obj: dict[str, Any], track_number: int = 0) -> Track:
        """Parse a YT Track response to a Track model object."""
        if not track_obj.get("videoId"):
            msg = "Track is missing videoId"
            raise InvalidDataError(msg)
        track_id = str(track_obj["videoId"])
        name, version = parse_title_and_version(track_obj["title"])
        track = Track(
            item_id=track_id,
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=track_obj.get("isAvailable", True),
                    url=f"{YTM_DOMAIN}/watch?v={track_id}",
                    audio_format=AudioFormat(
                        content_type=ContentType.M4A,
                    ),
                )
            },
            # Disc info is not available in YTM, assume a single disc
            disc_number=1,
            # Track number is "sometimes" available in the track object, otherwise approach
            # by counting album tracks when fetching full album details
            track_number=track_obj.get("trackNumber") or track_number or 0,
        )

        if track_obj.get("artists"):
            track.artists = UniqueList(
                self._get_artist_item_mapping(artist)
                for artist in track_obj["artists"]
                if _artist_is_resolvable(artist)
            )
        # guard that track has valid artists
        if not track.artists:
            msg = "Track is missing artists"
            raise InvalidDataError(msg)
        if track_obj.get("thumbnails"):
            track.metadata.images = self._parse_thumbnails(track_obj["thumbnails"])
        if (
            track_obj.get("album")
            and isinstance(track_obj.get("album"), dict)
            and track_obj["album"].get("id")
        ):
            album = track_obj["album"]
            track.album = self._get_item_mapping(MediaType.ALBUM, album["id"], album["name"])
        if "isExplicit" in track_obj:
            track.metadata.explicit = track_obj["isExplicit"]
        if "duration" in track_obj and str(track_obj["duration"]).isdigit():
            track.duration = int(track_obj["duration"])
        elif "duration_seconds" in track_obj and str(track_obj["duration_seconds"]).isdigit():
            track.duration = int(track_obj["duration_seconds"])
        return track

    def _parse_podcast(self, podcast_obj: dict[str, Any]) -> Podcast:
        """Parse a YTM Podcast into a MA Podcast."""
        podcast = Podcast(
            item_id=podcast_obj["podcastId"],
            name=podcast_obj["title"],
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_obj["podcastId"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if description := podcast_obj.get("description"):
            podcast.metadata.description = description
        if author := podcast_obj.get("author") or podcast_obj.get("channel"):
            podcast.publisher = author["name"]
        if thumbnails := podcast_obj.get("thumbnails"):
            podcast.metadata.images = self._parse_thumbnails(thumbnails)
        podcast.metadata.genres = {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
        return podcast

    def _parse_browse_podcast(self, item_obj: dict[str, Any]) -> Podcast | None:
        """Parse a podcast from a browse or search item."""
        browse_id: str = item_obj.get("browseId") or ""
        if not browse_id.startswith("MPSP"):
            return None
        podcast_obj = dict(item_obj)
        # Match the unprefixed IDs used by library podcasts.
        podcast_obj["podcastId"] = item_obj.get("podcastId") or browse_id.removeprefix("MPSP")
        return self._parse_podcast(podcast_obj)

    def _parse_podcast_episode(
        self, episode_obj: dict[str, Any], podcast: Podcast
    ) -> PodcastEpisode:
        """Parse a raw episode into a PodcastEpisode."""
        episode_id = episode_obj.get("videoId")
        if not episode_id:
            msg = "Podcast episode is missing videoId"
            raise InvalidDataError(msg)
        item_id = f"{podcast.item_id}{PODCAST_EPISODE_SPLITTER}{episode_id}"
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=episode_obj.get("title", ""),
            position=0,
            podcast=podcast,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.M4A,
                    ),
                    url=f"{YTM_DOMAIN}/watch?v={episode_id}",
                )
            },
        )
        if duration := episode_obj.get("duration"):
            duration_sec = parse_str_duration(duration)
            episode.duration = int(duration_sec)
        if description := episode_obj.get("description"):
            # the single episode endpoint returns a Description object instead of a string
            episode.metadata.description = (
                description.text if isinstance(description, Description) else description
            )
        if thumbnails := episode_obj.get("thumbnails"):
            episode.metadata.images = self._parse_thumbnails(thumbnails)
        if release_date := episode_obj.get("date"):
            with suppress(ValueError):
                episode.metadata.release_date = datetime.fromisoformat(release_date)
        return episode

    def _schedule_prefetch(self, current_item_id: str) -> None:
        """Keep at most one background queue scan per currently requested track."""
        if not self._file_cache or self._is_unloading or current_item_id in self._prefetch_tasks:
            return
        task = asyncio.create_task(self._prefetch_upcoming(current_item_id))
        self._prefetch_tasks[current_item_id] = task
        task.add_done_callback(lambda _task: self._prefetch_tasks.pop(current_item_id, None))

    def _video_id_for_instance(self, media_item: MediaItemType | ItemMapping | None) -> str | None:
        """Resolve queue tracks only when they belong to this provider instance."""
        if media_item is None or media_item.media_type != MediaType.TRACK:
            return None
        if isinstance(media_item, ItemMapping):
            return media_item.item_id if media_item.provider == self.instance_id else None
        return next(
            (
                mapping.item_id
                for mapping in media_item.provider_mappings
                if mapping.provider_instance == self.instance_id
            ),
            None,
        )

    async def _prefetch_upcoming(self, current_item_id: str) -> None:
        """Cache up to three upcoming queue entries without blocking playback."""
        assert self._file_cache is not None
        video_ids: list[str] = []
        for queue in self.mass.player_queues.all():
            if (
                queue.current_item is None
                or queue.current_index is None
                or self._video_id_for_instance(queue.current_item.media_item) != current_item_id
            ):
                continue
            index: int | str = queue.current_index
            for _ in range(PREFETCH_AHEAD):
                next_item = self.mass.player_queues.get_next_item(queue.queue_id, index)
                if next_item is None:
                    break
                index = next_item.queue_item_id
                video_id = self._video_id_for_instance(next_item.media_item)
                if video_id and video_id != current_item_id and video_id not in video_ids:
                    video_ids.append(video_id)
                if len(video_ids) == PREFETCH_AHEAD:
                    break
            if len(video_ids) == PREFETCH_AHEAD:
                break
        if video_ids:
            self.logger.debug(
                "Prefetching %s upcoming audio cache entries after %s",
                len(video_ids),
                current_item_id,
            )
        for video_id in video_ids:
            try:
                if await asyncio.to_thread(self._file_cache.get_hit, video_id):
                    self.logger.debug("Prefetch cache hit for %s", video_id)
                    continue
                stream_format = await self._get_stream_format(video_id)
                if not stream_format.get("is_live"):
                    await self._cache_stream(video_id, stream_format)
                else:
                    self.logger.debug("Skipping live stream %s during prefetch", video_id)
            except Exception as err:
                self.logger.debug(
                    "Prefetch cache failed for %s (%s): %s",
                    video_id,
                    type(err).__name__,
                    sanitize_text(str(err)),
                )

    async def _cache_stream(
        self, video_id: str, stream_format: dict[str, Any]
    ) -> CachedStreamInfo | None:
        """Keep disk writers alive until completion even if their caller is cancelled."""
        task = asyncio.create_task(self._download_and_cache(video_id, stream_format))
        self._downloads.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._downloads.discard(task)

    def _cached_stream_details(
        self, item_id: str, media_type: MediaType, cached: CachedStreamInfo
    ) -> StreamDetails:
        """Expose a completed original-container download to MA's file decoder."""
        metadata = cached.metadata
        audio_format = AudioFormat(
            content_type=ContentType.try_parse(metadata.get("audio_ext") or "")
        )
        if metadata.get("asr"):
            audio_format.sample_rate = int(metadata["asr"])
        if metadata.get("audio_channels"):
            audio_format.channels = int(metadata["audio_channels"])
        if metadata.get("abr"):
            audio_format.bit_rate = int(metadata["abr"])
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=media_type,
            audio_format=audio_format,
            stream_type=StreamType.LOCAL_FILE,
            path=str(cached.audio_path),
            can_seek=True,
            allow_seek=True,
            duration=int(metadata["duration"]) if metadata.get("duration") else None,
            expiration=0,
        )

    async def _download_and_cache(
        self, video_id: str, stream_format: dict[str, Any]
    ) -> CachedStreamInfo | None:
        """Download the selected audio with yt-dlp outside the event loop."""
        assert self._file_cache is not None
        if (
            self._file_cache.max_size_bytes
            and stream_format.get("filesize")
            and stream_format["filesize"] > self._file_cache.max_size_bytes
        ):
            self.logger.debug(
                "Skipping audio cache for %s: %s bytes exceed the %s byte limit",
                video_id,
                stream_format["filesize"],
                self._file_cache.max_size_bytes,
            )
            return None

        def download(directory: Path) -> CachedStreamInfo:
            assert self._yt_dlp_module is not None
            self.logger.debug(
                "Downloading audio for %s (format %s)", video_id, stream_format["format_id"]
            )
            yt_dlp = self._yt_dlp_module
            options = {
                **self._ydl_options(),
                "outtmpl": str(directory / "download.%(ext)s"),
                "http_headers": dict(stream_format.get("http_headers") or {}),
                "http_chunk_size": 10 * 1024 * 1024,
            }
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    downloader.process_info({**stream_format, "id": video_id, "title": video_id})
            except yt_dlp.utils.DownloadError as err:
                raise UnplayableMediaError("YouTube audio download failed") from err
            files = [
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix not in (".part", ".ytdl")
            ]
            if len(files) != 1:
                raise UnplayableMediaError(
                    "YouTube download did not produce one complete audio file"
                )
            metadata = {
                key: stream_format.get(key)
                for key in ("format_id", "asr", "audio_channels", "abr", "duration", "filesize")
            }
            metadata["audio_ext"] = stream_format.get("audio_ext") or stream_format.get("ext")
            return CachedStreamInfo(files[0], metadata)

        cached = await asyncio.to_thread(self._file_cache.get_or_download, video_id, download)
        if cached:
            self.logger.debug(
                "Audio cache ready for %s (format %s, %s bytes)",
                video_id,
                cached.metadata.get("format_id"),
                cached.metadata.get("size"),
            )
        else:
            self.logger.debug("Audio for %s exceeds the cache size limit; not retained", video_id)
        return cached

    def _ydl_options(self) -> dict[str, Any]:
        """Let yt-dlp choose its current anonymous player clients."""
        return {
            "logger": _YtDlpLogger(self.logger),
            "quiet": self.logger.level > logging.DEBUG,
            "verbose": self.logger.level == VERBOSE_LOG_LEVEL,
            "noprogress": True,
            "format": "bestaudio/best",
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_args": {"youtube": {"skip": ["translated_subs"]}},
        }

    async def _get_stream_format(self, item_id: str) -> dict[str, Any]:
        """Resolve preferred audio formats, retrying complete failed extractions."""

        def _extract_best_stream_url_format() -> dict[str, Any]:
            if self._yt_dlp_module is None:
                self._yt_dlp_module = importlib.import_module("yt_dlp")
            yt_dlp = self._yt_dlp_module
            url = f"{YTM_DOMAIN}/watch?v={item_id}"
            with yt_dlp.YoutubeDL(self._ydl_options()) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError as err:
                    raise UnplayableMediaError(err) from err
                if not info or not info.get("formats"):
                    raise UnplayableMediaError("No stream formats found")
                formats_by_id = {
                    str(audio_format.get("format_id", "")).split("-", maxsplit=1)[0]: audio_format
                    for audio_format in info["formats"]
                    if audio_format.get("url")
                }
                stream_format: dict[str, Any] | None = next(
                    (
                        formats_by_id[format_id]
                        for format_id in PREFERRED_AUDIO_FORMAT_IDS
                        if format_id in formats_by_id
                    ),
                    None,
                )
                if stream_format is None:
                    format_selector = ydl.build_format_selector("m4a/bestaudio/best")
                    stream_format = next(format_selector({"formats": info["formats"]}), None)
                if not stream_format or not stream_format.get("url"):
                    raise UnplayableMediaError("No stream formats found")
                return {
                    **stream_format,
                    "duration": info.get("duration"),
                    "is_live": info.get("is_live", False),
                    "http_headers": stream_format.get("http_headers")
                    or info.get("http_headers")
                    or {},
                }

        for attempt in range(EXTRACT_ATTEMPTS):
            try:
                self.logger.debug(
                    "Extracting audio for %s (attempt %s/%s)",
                    item_id,
                    attempt + 1,
                    EXTRACT_ATTEMPTS,
                )
                stream_format = await asyncio.to_thread(_extract_best_stream_url_format)
                break
            except Exception as err:
                if attempt == EXTRACT_ATTEMPTS - 1:
                    raise UnplayableMediaError(str(err)) from err
                self.logger.warning(
                    "Stream extraction attempt %s/%s failed for %s (%s): %s; retrying in %.1fs",
                    attempt + 1,
                    EXTRACT_ATTEMPTS,
                    item_id,
                    type(err).__name__,
                    sanitize_text(str(err)),
                    EXTRACT_RETRY_DELAY * (attempt + 1),
                )
                await asyncio.sleep(EXTRACT_RETRY_DELAY * (attempt + 1))
        else:
            raise UnplayableMediaError("No stream formats found")
        self.logger.debug(
            "Resolved audio format %s for %s (codec=%s, bitrate=%s kbit/s)",
            stream_format["format_id"],
            item_id,
            stream_format.get("acodec"),
            stream_format.get("abr"),
        )
        try:
            available_at = float(stream_format.get("available_at") or 0)
        except (TypeError, ValueError, OverflowError) as err:
            raise UnplayableMediaError("Invalid YouTube stream availability timestamp") from err
        if not math.isfinite(available_at):
            raise UnplayableMediaError("Invalid YouTube stream availability timestamp")
        delay = available_at - time.time()
        if delay > 45:
            raise UnplayableMediaError("YouTube stream is not available within 45 seconds")
        if delay > 0:
            self.logger.debug("Waiting %.1fs for stream availability", delay)
            await asyncio.sleep(delay)
        return stream_format

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    def _get_artist_item_mapping(self, artist_obj: dict[str, Any]) -> ItemMapping:
        artist_id = artist_obj.get("id") or artist_obj.get("channelId")
        if not artist_id and artist_obj["name"] == "Various Artists":
            artist_id = VARIOUS_ARTISTS_YTM_ID
        # callers only pass artists with a valid id
        return self._get_item_mapping(
            MediaType.ARTIST, cast("str", artist_id), artist_obj.get("name", "")
        )

    def _parse_thumbnails(self, thumbnails_obj: list[dict[str, Any]]) -> UniqueList[MediaItemImage]:
        """Parse and YTM thumbnails to MediaItemImage."""
        result: UniqueList[MediaItemImage] = UniqueList()
        processed_images = set()
        for img in sorted(thumbnails_obj, key=lambda w: w.get("width", 0), reverse=True):
            url: str = img["url"]
            url_base = url.split("=w", maxsplit=1)[0]
            width: int = img.get("width") or 0
            height: int = img.get("height") or 0
            if not width or not height:
                continue
            image_ratio: float = width / height
            image_type = (
                ImageType.LANDSCAPE
                if "maxresdefault" in url or image_ratio > 2.0
                else ImageType.THUMB
            )
            if "=w" not in url and width < 400:
                continue
            # if the size is in the url, we can actually request a higher thumb
            if "=w" in url and width < 600:
                url = f"{url_base}=w600-h600-p"
                image_type = ImageType.THUMB
            if (url_base, image_type) in processed_images:
                continue
            processed_images.add((url_base, image_type))
            result.append(
                MediaItemImage(
                    type=image_type,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return result

    async def _install_packages(self) -> None:
        """Install frequently changing packages dynamically."""
        # NOTE: Google breaks things quite often which requires us to update
        # some packages very frequently. Installing them dynamically prevents
        # us from having to update MA to ensure this provider works.
        for package_name in PACKAGES_TO_INSTALL:
            await install_package(package_name)
        # verify if the yt_dlp package is usable
        try:
            await import_module_in_thread("yt_dlp")
        except ImportError:
            raise SetupFailedError("Package yt_dlp failed to install")


@lru_cache
def _album_type_labels(language: str) -> dict[str, AlbumType]:
    """
    Return the (casefolded) album type labels YouTube Music uses for the given language.

    :param language: The YouTube Music language code.
    """
    translation = gettext.translation(
        "base",
        localedir=Path(ytmusicapi.__file__).parent / "locales",
        languages=[language],
        fallback=True,
    )
    labels = {"album": AlbumType.ALBUM, "ep": AlbumType.EP, "single": AlbumType.SINGLE}
    translated = {label: translation.gettext(label).casefold() for label in labels}
    # a label shared by several types (e.g. Spanish) can't tell them apart, so it stays unknown
    counts = Counter(translated.values())
    return labels | {text: labels[label] for label, text in translated.items() if counts[text] == 1}
