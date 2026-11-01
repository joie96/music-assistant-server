"""Jellyfin support for MusicAssistant."""

from __future__ import annotations

import hashlib
import socket
from asyncio import TaskGroup
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from aiojellyfin import ItemFields, NotFound, authenticate_by_name
from aiojellyfin import MediaLibrary as JellyMediaLibrary
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import UNKNOWN_ARTIST, UNKNOWN_ARTIST_ID_MBID
from music_assistant.controllers.cache import use_cache
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.jellyfin.parsers import (
    audio_format,
    parse_album,
    parse_artist,
    parse_lyrics,
    parse_playlist,
    parse_track,
)

from .client import JellyfinClient, rating_sort_key
from .const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    COLLECTION_TYPE_MUSIC,
    COLLECTION_TYPE_PLAYLISTS,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_MEDIA_TYPE,
    ITEM_KEY_NAME,
    ITEM_KEY_RUNTIME_TICKS,
    MEDIA_TYPE_AUDIO,
    RECOMMENDATION_ROWS,
    SUPPORTED_CONTAINER_FORMATS,
    TRACK_FIELDS,
    USER_APP_NAME,
)

if TYPE_CHECKING:
    from aiojellyfin import Album as JellyAlbum
    from aiojellyfin import Artist as JellyArtist
    from aiojellyfin import Track as JellyTrack
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.FAVORITE_ARTISTS_EDIT,
    ProviderFeature.FAVORITE_ALBUMS_EDIT,
    ProviderFeature.FAVORITE_TRACKS_EDIT,
    ProviderFeature.FAVORITE_PLAYLISTS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return JellyfinProvider(mass, manifest, config, SUPPORTED_FEATURES)


class JellyfinProvider(MusicProvider):
    """Provider for a jellyfin music library."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        username = str(self.get_setup_value(CONF_USERNAME))

        # Device ID should be stable between reboots
        # Otherwise every time the provider starts we "leak" a new device
        # entry in the Jellyfin backend, which creates devices and entities
        # in HA if they also use the Jellyfin integration there.

        # We follow a suggestion a Jellyfin dev gave to HA and use an ID
        # that is stable even if provider is removed and re-added.
        # They said mix in username in case the same device/app has 2
        # connections to the same servers

        # Neither of these are secrets (username is handed over to mint a
        # token and server_id is used in zeroconf) but hash them anyway as its meant
        # to be an opaque identifier

        device_id = hashlib.sha256(f"{self.mass.server_id}+{username}".encode()).hexdigest()
        verify_ssl = bool(self.get_setup_value(CONF_VERIFY_SSL))
        http_session = self.mass.http_session if verify_ssl else self.mass.http_session_no_ssl

        session_config = SessionConfiguration(
            session=http_session,
            url=str(self.get_setup_value(CONF_URL)),
            verify_ssl=verify_ssl,
            app_name=USER_APP_NAME,
            app_version=self.mass.version,
            device_name=socket.gethostname(),
            device_id=device_id,
        )

        try:
            self._client = await authenticate_by_name(
                session_config,
                username,
                str(self.get_setup_value(CONF_PASSWORD) or ""),
            )
        except Exception as err:
            raise LoginFailed(f"Authentication failed: {err}") from err

        self._api = JellyfinClient(self._client)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Return an accessible Jellyfin artwork URL.

        :param path: Artwork URL or provider path to resolve.
        """
        return _normalize_jellyfin_media_url(path)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """
        Perform search on the Jellyfin library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        artists = None
        albums = None
        tracks = None
        playlists = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(self._search_artist(search_query, limit))
            if MediaType.ALBUM in media_types:
                albums = tg.create_task(self._search_album(search_query, limit))
            if MediaType.TRACK in media_types:
                tracks = tg.create_task(self._search_track(search_query, limit))
            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(self._search_playlist(search_query, limit))

        search_results = SearchResults()

        if artists:
            search_results.artists = artists.result()
        if albums:
            search_results.albums = albums.result()
        if tracks:
            search_results.tracks = tracks.result()
        if playlists:
            search_results.playlists = playlists.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve all library artists from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            stream = (
                self._client.artists.parent(jellyfin_library[ITEM_KEY_ID])
                .enable_userdata()
                .fields(*ARTIST_FIELDS)
                .stream(100)
            )
            async for artist in stream:
                yield parse_artist(self.logger, self.instance_id, self._client, artist)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve all library albums from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            stream = (
                self._client.albums.parent(jellyfin_library[ITEM_KEY_ID])
                .enable_userdata()
                .fields(*ALBUM_FIELDS)
                .stream(100)
            )
            async for album in stream:
                yield parse_album(self.logger, self.instance_id, self._client, album)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            stream = (
                self._client.tracks.parent(jellyfin_library[ITEM_KEY_ID])
                .enable_userdata()
                .fields(*TRACK_FIELDS)
                .stream(100)
            )
            async for track in stream:
                if not len(track[ITEM_KEY_MEDIA_STREAMS]):
                    self.logger.warning(
                        "Invalid track %s: Does not have any media streams", track[ITEM_KEY_NAME]
                    )
                    continue
                yield parse_track(self.logger, self.instance_id, self._client, track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve all library playlists from the provider."""
        playlist_libraries = await self._get_playlists()
        for playlist_library in playlist_libraries:
            stream = (
                self._client.playlists.parent(playlist_library[ITEM_KEY_ID])
                .enable_userdata()
                .stream(100)
            )
            async for playlist in stream:
                if ITEM_KEY_MEDIA_TYPE in playlist:  # Only jellyfin has this property
                    if playlist[ITEM_KEY_MEDIA_TYPE] == MEDIA_TYPE_AUDIO:
                        yield parse_playlist(self.instance_id, self._client, playlist)
                else:  # emby playlists are only audio type
                    yield parse_playlist(self.instance_id, self._client, playlist)

    @use_cache(900)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album = await self._api.get_item(prov_album_id, "MusicAlbum", ALBUM_FIELDS)
        return parse_album(self.logger, self.instance_id, self._client, cast("JellyAlbum", album))

    @use_cache(3600)  # Cache for 1 hour
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        jellyfin_album_tracks = (
            await self._client.tracks.parent(prov_album_id)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .request()
        )
        return [
            parse_track(self.logger, self.instance_id, self._client, jellyfin_album_track)
            for jellyfin_album_track in jellyfin_album_tracks["Items"]
        ]

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id == UNKNOWN_ARTIST:
            artist = Artist(
                item_id=UNKNOWN_ARTIST,
                name=UNKNOWN_ARTIST,
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            artist.mbid = UNKNOWN_ARTIST_ID_MBID
            return artist

        jellyfin_artist = await self._api.get_item(prov_artist_id, "MusicArtist", ARTIST_FIELDS)
        return parse_artist(
            self.logger, self.instance_id, self._client, cast("JellyArtist", jellyfin_artist)
        )

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self._api.get_item(prov_track_id, "Audio", TRACK_FIELDS)
        result = parse_track(self.logger, self.instance_id, self._client, cast("JellyTrack", track))
        if lyrics := await self._api.get_lyrics(prov_track_id):
            result.metadata.lyrics, result.metadata.lrc_lyrics = parse_lyrics(lyrics)
        return result

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        try:
            playlist = await self._client.get_playlist(prov_playlist_id)
        except NotFound:
            raise MediaNotFoundError(f"Item {prov_playlist_id} not found")
        result = parse_playlist(self.instance_id, self._client, playlist)
        result.is_editable = await self._api.can_edit_playlist(prov_playlist_id)
        return result

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        if page < 0:
            raise InvalidDataError("Playlist page must not be negative")
        result: list[Track] = []
        playlist_items = await self._get_playlist_items(prov_playlist_id)
        for index, jellyfin_track in enumerate(playlist_items[page * 100 : (page + 1) * 100], 1):
            pos = (page * 100) + index
            try:
                if track := parse_track(
                    self.logger, self.instance_id, self._client, cast("JellyTrack", jellyfin_track)
                ):
                    track.position = pos
                    result.append(track)
            except (KeyError, ValueError) as err:
                self.logger.error(
                    "Skipping track %s: %s", jellyfin_track.get(ITEM_KEY_NAME, index), str(err)
                )
        return result

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """Update a Jellyfin favorite without adding or deleting library items."""
        if media_type not in (
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
        ):
            raise UnsupportedFeaturedException(f"Jellyfin favorites do not support {media_type}")
        await self._api.set_favorite(prov_item_id, favorite)
        await self.mass.cache.delete(None, provider=self.instance_id)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create an audio playlist owned by the connected Jellyfin user."""
        if media_types != {MediaType.TRACK}:
            raise UnsupportedFeaturedException("Jellyfin playlists support tracks only")
        if not name.strip():
            raise InvalidDataError("Playlist name must not be empty")
        response = await self._api.request(
            "POST", "/Playlists", {"name": name, "mediaType": "Audio"}
        )
        await self.mass.cache.delete(None, provider=self.instance_id)
        return await self.get_playlist(response["Id"])

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Append tracks to a writable Jellyfin playlist, preserving duplicates."""
        if not prov_track_ids:
            return
        await self._assert_playlist_editable(prov_playlist_id)
        await self._api.request(
            "POST", f"/Playlists/{prov_playlist_id}/Items", {"ids": ",".join(prov_track_ids)}
        )
        await self.mass.cache.delete(None, provider=self.instance_id)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove specific playlist entries by one-based position, not track identity."""
        if not positions_to_remove:
            return
        await self._assert_playlist_editable(prov_playlist_id)
        items = await self._get_playlist_items(prov_playlist_id)
        if any(position < 1 or position > len(items) for position in positions_to_remove):
            raise InvalidDataError("Playlist position is out of range")
        entry_ids = [
            items[position - 1]["PlaylistItemId"] for position in sorted(set(positions_to_remove))
        ]
        await self._api.request(
            "DELETE", f"/Playlists/{prov_playlist_id}/Items", {"entryIds": ",".join(entry_ids)}
        )
        await self.mass.cache.delete(None, provider=self.instance_id)

    @use_cache(3600)  # Cache for 1 hour
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return []
        albums = await self._get_artist_items(prov_artist_id, "MusicAlbum", ALBUM_FIELDS)
        return [
            parse_album(self.logger, self.instance_id, self._client, cast("JellyAlbum", album))
            for album in albums
        ]

    @use_cache(3600)
    async def get_artist_topalbums(self, prov_artist_id: str) -> list[Album]:
        """Get an artist's albums ranked by Jellyfin community rating."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return []
        albums = await self._get_artist_items(prov_artist_id, "MusicAlbum", ALBUM_FIELDS)
        return [
            parse_album(self.logger, self.instance_id, self._client, cast("JellyAlbum", album))
            for album in sorted(albums, key=rating_sort_key)
        ]

    @use_cache(3600)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get an artist's tracks ranked by Jellyfin community rating."""
        if prov_artist_id == UNKNOWN_ARTIST:
            return []
        tracks = await self._get_artist_items(prov_artist_id, "Audio", TRACK_FIELDS)
        return [
            parse_track(self.logger, self.instance_id, self._client, cast("JellyTrack", track))
            for track in sorted(tracks, key=rating_sort_key)
        ]

    @use_cache(3600)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Get related artists in Jellyfin's similarity order."""
        if limit <= 0 or prov_artist_id == UNKNOWN_ARTIST:
            return []
        response = await self._api.get_json(
            f"/Artists/{prov_artist_id}/Similar",
            {"limit": str(limit + 1), "fields": ",".join(field.value for field in ARTIST_FIELDS)},
        )
        artists: list[Artist] = []
        seen_ids = {prov_artist_id}
        for artist in response["Items"]:
            if artist["Id"] in seen_ids or artist.get("Type") != "MusicArtist":
                continue
            seen_ids.add(artist["Id"])
            artists.append(parse_artist(self.logger, self.instance_id, self._client, artist))
            if len(artists) == limit:
                break
        return artists

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        try:
            jellyfin_track = await self._client.get_track(item_id)
        except NotFound:
            raise MediaNotFoundError(f"Item {item_id} not found")
        audio_url = self._client.audio_url(
            jellyfin_track[ITEM_KEY_ID], container=SUPPORTED_CONTAINER_FORMATS
        )
        url = _normalize_jellyfin_media_url(audio_url)
        runtime_ticks = jellyfin_track.get(ITEM_KEY_RUNTIME_TICKS)
        return StreamDetails(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            audio_format=audio_format(jellyfin_track),
            stream_type=StreamType.HTTP,
            duration=(
                int(runtime_ticks / 10000000)  # 10000000 ticks per second
                if runtime_ticks is not None
                else None
            ),
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    @use_cache(3600)  # Cache for 1 hour
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        if limit <= 0:
            return []
        resp = await self._api.get_json(
            f"/Items/{prov_track_id}/Similar",
            {
                "limit": str(limit + 1),
                "enableUserData": "true",
                "fields": ",".join(field.value for field in TRACK_FIELDS),
            },
        )
        tracks: list[Track] = []
        seen_ids = {prov_track_id}
        for track in resp["Items"]:
            if track["Id"] in seen_ids or track.get("Type") != "Audio":
                continue
            seen_ids.add(track["Id"])
            tracks.append(parse_track(self.logger, self.instance_id, self._client, track))
            if len(tracks) == limit:
                break
        return tracks

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return static library discovery rows without making backend calls."""
        rows = [
            RecommendationFolder(
                item_id=item_id,
                provider=self.instance_id,
                name=definition[0],
                translation_key=item_id,
                is_playable=True,
            )
            for item_id, definition in RECOMMENDATION_ROWS.items()
        ]
        rows.append(
            RecommendationFolder(
                item_id="genres", provider=self.instance_id, name="Genres", translation_key="genres"
            )
        )
        return rows

    @use_cache(900)
    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Fetch one recommendation row from the connected user's music library."""
        if item_id == "genres":
            return UniqueList(await self._get_genre_folders())
        if item_id not in RECOMMENDATION_ROWS:
            raise MediaNotFoundError(f"Unknown Jellyfin recommendation row: {item_id}")
        _, item_type, sort_by = RECOMMENDATION_ROWS[item_id]
        params = {"sortBy": f"{sort_by},SortName", "sortOrder": "Descending,Ascending"}
        if sort_by == "CommunityRating":
            params["minCommunityRating"] = "0"
        elif sort_by in ("PlayCount", "DatePlayed"):
            params["isPlayed"] = "true"
        fields = ALBUM_FIELDS if item_type == "MusicAlbum" else TRACK_FIELDS
        items = await self._api.get_items(item_type, fields, params)
        if item_type == "MusicAlbum":
            return UniqueList(
                parse_album(self.logger, self.instance_id, self._client, cast("JellyAlbum", item))
                for item in items
            )
        return UniqueList(
            parse_track(self.logger, self.instance_id, self._client, cast("JellyTrack", item))
            for item in items
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse standard library folders and genre-specific music or instant mixes."""
        prefix = f"{self.instance_id}://"
        if not path.startswith(prefix):
            raise MediaNotFoundError("Invalid Jellyfin browse path")
        parts = path[len(prefix) :].strip("/").split("/")
        if parts[0] != "genres":
            folders = list(await super().browse(path))
            if parts == [""]:
                folders.append(
                    BrowseFolder(
                        item_id="genres",
                        provider=self.instance_id,
                        name="Genres",
                        translation_key="genres",
                        path=f"{prefix}genres",
                    )
                )
            return folders
        if len(parts) == 1:
            return await self._get_genre_folders()
        if len(parts) == 2:
            return [
                BrowseFolder(
                    item_id=f"{parts[1]}_{section}",
                    provider=self.instance_id,
                    name=name,
                    translation_key=section,
                    is_playable=True,
                    path=f"{prefix}genres/{parts[1]}/{section}",
                )
                for section, name in (
                    ("albums", "Albums"),
                    ("tracks", "Tracks"),
                    ("mix", "Instant mix"),
                )
            ]
        if len(parts) != 3 or parts[2] not in ("albums", "tracks", "mix"):
            raise MediaNotFoundError("Invalid Jellyfin genre path")
        return await self._get_genre_items(parts[1], parts[2])

    async def _search_track(self, search_query: str, limit: int) -> list[Track]:
        resultset = (
            await self._client.tracks.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .request()
        )
        tracks = []
        for item in resultset["Items"]:
            tracks.append(parse_track(self.logger, self.instance_id, self._client, item))
        return tracks

    async def _search_album(self, search_query: str, limit: int) -> list[Album]:
        # an "Artist - Album" style query: search on the album part only
        albumname = search_query.split(" - ", 1)[1] if " - " in search_query else search_query
        resultset = (
            await self._client.albums.search_term(albumname)
            .limit(limit)
            .enable_userdata()
            .fields(*ALBUM_FIELDS)
            .request()
        )
        albums = []
        for item in resultset["Items"]:
            albums.append(parse_album(self.logger, self.instance_id, self._client, item))
        return albums

    async def _search_artist(self, search_query: str, limit: int) -> list[Artist]:
        resultset = (
            await self._client.artists.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .fields(*ARTIST_FIELDS)
            .request()
        )
        artists = []
        for item in resultset["Items"]:
            artists.append(parse_artist(self.logger, self.instance_id, self._client, item))
        return artists

    async def _search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        resultset = (
            await self._client.playlists.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .request()
        )
        playlists = []
        for item in resultset["Items"]:
            playlists.append(parse_playlist(self.instance_id, self._client, item))
        return playlists

    @use_cache(3600)
    async def _get_artist_items(
        self, artist_id: str, item_type: str, fields: list[ItemFields]
    ) -> list[dict[str, Any]]:
        """Share raw rated artist listings between album and top-list requests."""
        return await self._api.get_artist_items(artist_id, item_type, fields)

    @use_cache(900)
    async def _get_playlist_items(self, playlist_id: str) -> list[dict[str, Any]]:
        """Preserve playlist order and entry IDs across every page."""
        return await self._api.get_items(
            "Audio", TRACK_FIELDS, {}, limit=None, path=f"/Playlists/{playlist_id}/Items"
        )

    async def _assert_playlist_editable(self, playlist_id: str) -> None:
        """Reject writes to shared or legacy playlists without explicit write permission."""
        if not await self._api.can_edit_playlist(playlist_id):
            raise ProviderPermissionDenied("Jellyfin playlist is read-only for this user")

    @use_cache(900)
    async def _get_genres(self) -> list[dict[str, Any]]:
        """Get every genre associated with accessible music, rather than video genres."""
        return await self._api.get_items("Audio,MusicAlbum", [], {}, limit=None, path="/Genres")

    async def _get_genre_folders(self) -> list[BrowseFolder]:
        """Map music genres to provider-owned browse folders."""
        return [
            BrowseFolder(
                item_id=genre["Id"],
                provider=self.instance_id,
                name=genre["Name"],
                path=f"{self.instance_id}://genres/{genre['Id']}",
            )
            for genre in await self._get_genres()
        ]

    @use_cache(900)
    async def _get_genre_items(self, genre_id: str, section: str) -> list[Album] | list[Track]:
        """Fetch a complete genre listing or a bounded Jellyfin instant mix."""
        genre = next((item for item in await self._get_genres() if item["Id"] == genre_id), None)
        if genre is None:
            raise MediaNotFoundError(f"Unknown Jellyfin genre: {genre_id}")
        if section == "mix":
            response = await self._api.get_json(
                f"/MusicGenres/{quote(genre['Name'], safe='')}/InstantMix",
                {
                    "limit": "50",
                    "enableUserData": "true",
                    "fields": ",".join(f.value for f in TRACK_FIELDS),
                },
            )
            return [
                parse_track(self.logger, self.instance_id, self._client, item)
                for item in response["Items"]
                if item.get("Type") == "Audio"
            ]
        item_type = "MusicAlbum" if section == "albums" else "Audio"
        fields = ALBUM_FIELDS if section == "albums" else TRACK_FIELDS
        items = await self._api.get_items(item_type, fields, {"genreIds": genre_id}, limit=None)
        if section == "albums":
            return [
                parse_album(self.logger, self.instance_id, self._client, cast("JellyAlbum", item))
                for item in items
            ]
        return [
            parse_track(self.logger, self.instance_id, self._client, cast("JellyTrack", item))
            for item in items
        ]

    async def _get_music_libraries(self) -> list[JellyMediaLibrary]:
        """Return all supported libraries a user has access to."""
        response = await self._client.get_media_folders()
        libraries = response["Items"]
        result = []
        for library in libraries:
            if library.get(ITEM_KEY_COLLECTION_TYPE) == COLLECTION_TYPE_MUSIC:
                result.append(library)
        return result

    async def _get_playlists(self) -> list[JellyMediaLibrary]:
        """Return all supported libraries a user has access to."""
        response = await self._client.get_media_folders()
        libraries = response["Items"]
        result = []
        for library in libraries:
            if library.get(ITEM_KEY_COLLECTION_TYPE) == COLLECTION_TYPE_PLAYLISTS:
                result.append(library)
        return result


def _normalize_jellyfin_media_url(url: str) -> str:
    """Return a media URL using Jellyfin's supported auth parameter."""
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.lower() == "api_key" for key, _ in query):
        return url

    query = [("apiKey" if key.lower() == "api_key" else key, value) for key, value in query]
    return urlunsplit(parsed._replace(query=urlencode(query)))
