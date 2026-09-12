"""ListenBrainz recommendations metadata provider for Music Assistant."""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.providers.listenbrainz_recommendations.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz_recommendations.cover_art import build_split_cover_png
from music_assistant.providers.listenbrainz_recommendations.constants import (
    ARTIST_PLAYLISTS_FEATURED_ARTISTS_LIMIT,
    ARTIST_PLAYLISTS_LIMIT,
    CONF_API_BASE_URL,
    CONF_AUTH_TOKEN,
    CONF_LB_RADIO_MODE,
    CONF_LB_RADIO_POP_BEGIN,
    CONF_LB_RADIO_POP_END,
    CONF_USERNAME,
    DEFAULT_API_BASE_URL,
    GENRE_ARTISTS_GENRE_LIMIT,
    GENRE_ARTISTS_PER_GENRE_LIMIT,
    GENRE_PLAYLISTS_LIMIT,
    LB_RADIO_TRACK_LIMIT,
    REFRESH_INTERVAL_HOURS,
    REFRESH_TASK_ID,
    SIMILAR_ARTISTS_PER_SEED_LIMIT,
    SIMILAR_ARTISTS_SEED_LIMIT,
    STATS_RANGE,
    SUPPORTED_FEATURES,
    TOP_ARTISTS_LIMIT,
    TOP_TRACKS_LIMIT,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ListenBrainzRecommendationsProvider:
    """Initialize provider(instance) with given configuration."""
    return ListenBrainzRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    values = values or {}
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="ListenBrainz Username",
            required=True,
            value=values.get(CONF_USERNAME),
            description="Needed to build your personalized ListenBrainz recommendations.",
        ),
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="ListenBrainz User Token",
            required=True,
            value=values.get(CONF_AUTH_TOKEN),
            description=(
                "ListenBrainz API token used for authenticated endpoints such as LB-Radio."
            ),
        ),
        ConfigEntry(
            key=CONF_API_BASE_URL,
            type=ConfigEntryType.STRING,
            label="Base URL",
            required=False,
            value=values.get(CONF_API_BASE_URL) or DEFAULT_API_BASE_URL,
            description="ListenBrainz API base URL.",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_LB_RADIO_POP_BEGIN,
            type=ConfigEntryType.INTEGER,
            label="LB-Radio Pop Begin",
            required=True,
            default_value=75,
            value=values.get(CONF_LB_RADIO_POP_BEGIN),
            description="Lower popularity bound for LB-Radio tag requests (0-100).",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_LB_RADIO_POP_END,
            type=ConfigEntryType.INTEGER,
            label="LB-Radio Pop End",
            required=True,
            default_value=100,
            value=values.get(CONF_LB_RADIO_POP_END),
            description="Upper popularity bound for LB-Radio tag requests (0-100).",
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_LB_RADIO_MODE,
            type=ConfigEntryType.STRING,
            label="LB-Radio Mode",
            required=True,
            default_value="easy",
            value=values.get(CONF_LB_RADIO_MODE),
            options=[
                ConfigValueOption("Easy", "easy"),
                ConfigValueOption("Medium", "medium"),
                ConfigValueOption("Hard", "hard"),
            ],
            description="Difficulty mode used for LB-Radio artist recommendations. Easy is likely going to create a playlist with familiar music, and a hard playlist may expose you to less familiar music.",
            advanced=True,
        ),
    )


class ListenBrainzRecommendationsProvider(MetadataProvider):
    """ListenBrainz recommendations provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.api_client = ListenBrainzAPIClient(self)
        self._recommendation_folders: list[RecommendationFolder] = []
        self._resolved_virtual_playlists: dict[str, tuple[Playlist, list[Track]]] = {}
        self._artist_cache: dict[str, Artist] = {}

        self.mass.tasks.register_scheduled_task(
            task_id=f"{REFRESH_TASK_ID}_{self.instance_id}",
            name="Refresh ListenBrainz recommendations",
            handler=self._refresh_recommendations,
            schedule=TaskSchedule.hourly(every=REFRESH_INTERVAL_HOURS),
        )

        # Populate on startup so recommendations are available before the first interval tick.
        self.mass.call_later(
            20,
            self._refresh_recommendations,
            task_id=f"{REFRESH_TASK_ID}_initial_{self.instance_id}",
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        self.mass.tasks.unregister_scheduled_task(
            f"{REFRESH_TASK_ID}_{self.instance_id}",
            clear_persisted_state=is_removed,
        )

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's recommendations organized into folders."""
        return self._recommendation_folders

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return prebuilt playlist object for virtual playlists."""
        payload = self._resolved_virtual_playlists.get(prov_playlist_id)
        if payload is None:
            raise MediaNotFoundError(f"Unknown ListenBrainz playlist: {prov_playlist_id}")
        playlist, _tracks = payload
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return pre-resolved tracks for virtual playlists."""
        if page > 0:
            return []
        payload = self._resolved_virtual_playlists.get(prov_playlist_id)
        if payload is None:
            raise MediaNotFoundError(f"Unknown ListenBrainz playlist: {prov_playlist_id}")
        _playlist, tracks = payload
        return tracks

    def _get_lb_radio_pop_range(self) -> tuple[int, int]:
        """Return LB-Radio popularity bounds from provider configuration."""
        pop_begin = self.config.get_value(CONF_LB_RADIO_POP_BEGIN)
        pop_end = self.config.get_value(CONF_LB_RADIO_POP_END)

        if not isinstance(pop_begin, int) or not isinstance(pop_end, int):
            raise MusicAssistantError("LB-Radio pop range must be configured as integers")
        if pop_begin < 0 or pop_begin > 100 or pop_end < 0 or pop_end > 100:
            raise MusicAssistantError("LB-Radio pop range must be within 0-100")
        if pop_begin > pop_end:
            raise MusicAssistantError("LB-Radio pop begin must be less than or equal to pop end")
        return pop_begin, pop_end

    def _get_lb_radio_mode(self) -> str:
        """Return LB-Radio mode from provider configuration."""
        mode = self.config.get_value(CONF_LB_RADIO_MODE)
        if not isinstance(mode, str):
            raise MusicAssistantError("LB-Radio mode must be configured as a string")
        normalized_mode = mode.strip().casefold()
        if normalized_mode not in {"easy", "medium", "hard"}:
            raise MusicAssistantError("LB-Radio mode must be one of: easy, medium, hard")
        return normalized_mode

    async def _refresh_recommendations(self, initial_delay: int = 0) -> None:
        """Refresh all recommendation folders."""
        if initial_delay:
            self.logger.debug("ListenBrainz refresh delayed by %ss", initial_delay)
            await asyncio.sleep(initial_delay)

        try:
            self.logger.info("Building ListenBrainz recommendations")
            folders, resolved_virtual_playlists = await self._build_recommendations()
            self._recommendation_folders = folders
            self._resolved_virtual_playlists = resolved_virtual_playlists
            self.logger.debug(
                "Build completed: folders=%d virtual_playlists=%d",
                len(self._recommendation_folders),
                len(self._resolved_virtual_playlists),
            )
            self.logger.info(
                "Built %d ListenBrainz recommendation folders",
                len(self._recommendation_folders),
            )
        except MusicAssistantError as err:
            self.logger.warning("Failed to refresh ListenBrainz recommendations: %s", err)
        except Exception as err:  # noqa: BLE001
            self.logger.exception("Unexpected error while refreshing ListenBrainz recommendations: %s", err)

    async def _build_recommendations(
        self,
    ) -> tuple[list[RecommendationFolder], dict[str, tuple[Playlist, list[Track]]]]:
        """Build all requested recommendation folders."""
        folders: list[RecommendationFolder] = []
        resolved_virtual_playlists: dict[str, tuple[Playlist, list[Track]]] = {}

        top_artists_folder = await self._run_build_step(
            "top artists",
            self._build_folder_top_artists,
            RecommendationFolder(
                item_id="top_artists",
                provider=self.instance_id,
                name="Top Artists",
                items=UniqueList([]),
            ),
        )
        if top_artists_folder.items:
            folders.append(top_artists_folder)

        top_tracks_folder = await self._run_build_step(
            "top tracks",
            self._build_folder_top_tracks,
            RecommendationFolder(
                item_id="top_tracks",
                provider=self.instance_id,
                name="Top Tracks",
                items=UniqueList([]),
            ),
        )
        if top_tracks_folder.items:
            folders.append(top_tracks_folder)

        recent_listens_folder = await self._run_build_step(
            "recent listens",
            self._build_folder_recent_listens,
            RecommendationFolder(
                item_id="recent_listens",
                provider=self.instance_id,
                name="Recent Listens",
                items=UniqueList([]),
            ),
        )
        if recent_listens_folder.items:
            folders.append(recent_listens_folder)

        folders.extend(
            await self._run_build_step(
                "similar artists",
                self._build_folder_similar_artists,
                [],
            )
        )

        recommendations_folder = await self._run_build_step(
            "recommendations",
            self._build_folder_recommendations,
            RecommendationFolder(
                item_id="recommendations",
                provider=self.instance_id,
                name="Recommendations",
                items=UniqueList([]),
            ),
        )
        if recommendations_folder.items:
            folders.append(recommendations_folder)

        folders.extend(
            await self._run_build_step(
                "genre artists",
                self._build_folder_genre_artists,
                [],
            )
        )

        genre_playlists_folder, genre_playlists = await self._run_build_step(
            "genre playlists",
            self._build_folder_genre_playlists,
            (
                RecommendationFolder(
                    item_id="genre_playlists",
                    provider=self.instance_id,
                    name="Genre Playlists",
                    items=UniqueList([]),
                ),
                {},
            ),
        )
        if genre_playlists_folder.items:
            folders.append(genre_playlists_folder)
        resolved_virtual_playlists.update(genre_playlists)

        artist_playlists_folder, artist_playlists = await self._run_build_step(
            "artist playlists",
            self._build_folder_artist_playlists,
            (
                RecommendationFolder(
                    item_id="artist_playlists",
                    provider=self.instance_id,
                    name="Artist Playlists",
                    items=UniqueList([]),
                ),
                {},
            ),
        )
        if artist_playlists_folder.items:
            folders.append(artist_playlists_folder)
        resolved_virtual_playlists.update(artist_playlists)

        return folders, resolved_virtual_playlists

    async def _run_build_step[T](
        self,
        step_name: str,
        builder: Any,
        default_value: T,
    ) -> T:
        """Run a single build step and continue on errors."""
        try:
            return await builder()
        except MusicAssistantError as err:
            self.logger.warning("Skipping ListenBrainz %s step: %s", step_name, err)
        except Exception as err:  # noqa: BLE001
            self.logger.exception(
                "Unexpected error in ListenBrainz %s step: %s", step_name, err
            )
        return default_value

    async def _build_folder_genre_artists(
        self,
    ) -> list[RecommendationFolder]:
        """Build a top-level artists folder for each genre."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return []

        self.logger.debug(
            "Building genre artists folders for user=%s (genre_limit=%d, artists_per_genre=%d)",
            username.strip(),
            GENRE_ARTISTS_GENRE_LIMIT,
            GENRE_ARTISTS_PER_GENRE_LIMIT,
        )
        genre_activity = await self.api_client.get_user_genre_activity(
            username=username.strip(),
            stats_ranges=STATS_RANGE,
        )
        if not isinstance(genre_activity, list):
            return []

        self.logger.debug("Genre activity rows received: %d", len(genre_activity))

        top_genres = [
            entry.get("genre")
            for entry in genre_activity[:GENRE_ARTISTS_GENRE_LIMIT]
            if isinstance(entry, dict) and isinstance(entry.get("genre"), str)
        ]

        result: list[RecommendationFolder] = []
        pop_begin, pop_end = self._get_lb_radio_pop_range()
        for genre in top_genres:
            try:
                self.logger.debug("Building genre artists folder for genre='%s'", genre)
                tag_rows = await self.api_client.get_lb_radio_tags(
                    tags=[genre],
                    pop_begin=pop_begin,
                    pop_end=pop_end,
                    count=LB_RADIO_TRACK_LIMIT,
                )
                if not tag_rows:
                    continue

                recording_mbids = [
                    row.get("recording_mbid")
                    for row in tag_rows[:LB_RADIO_TRACK_LIMIT]
                    if isinstance(row, dict) and isinstance(row.get("recording_mbid"), str)
                ]
                recording_mbids = [mbid.strip() for mbid in recording_mbids if mbid and mbid.strip()]
                if not recording_mbids:
                    continue

                self.logger.debug(
                    "Genre '%s': using %d tag recordings for artist extraction",
                    genre,
                    len(recording_mbids),
                )

                # Keep insertion order from tag recordings while deduplicating artists.
                ordered_artist_names: list[str] = []
                seen_artist_keys: set[str] = set()

                for start_idx in range(0, len(recording_mbids), 10):
                    recording_batch = recording_mbids[start_idx : start_idx + 10]
                    recording_metadata = await self.api_client.get_recording_metadata(
                        recording_batch,
                        "artist",
                    )
                    if not recording_metadata:
                        continue

                    for recording_mbid in recording_batch:
                        metadata_row = recording_metadata.get(recording_mbid)
                        if not isinstance(metadata_row, dict):
                            continue
                        artist_data = metadata_row.get("artist")
                        if not isinstance(artist_data, dict):
                            continue

                        artists = artist_data.get("artists")
                        if isinstance(artists, list):
                            for artist in artists:
                                if not isinstance(artist, dict):
                                    continue
                                artist_name = artist.get("name")
                                if not isinstance(artist_name, str) or not artist_name.strip():
                                    continue
                                artist_mbid = artist.get("artist_mbid")
                                if isinstance(artist_mbid, str) and artist_mbid.strip():
                                    artist_key = f"mbid:{artist_mbid.strip()}"
                                else:
                                    artist_key = f"name:{self._normalize_text(artist_name)}"
                                if artist_key in seen_artist_keys:
                                    continue
                                seen_artist_keys.add(artist_key)
                                ordered_artist_names.append(artist_name.strip())
                            continue

                        artist_name = artist_data.get("name")
                        if not isinstance(artist_name, str) or not artist_name.strip():
                            continue
                        artist_key = f"name:{self._normalize_text(artist_name)}"
                        if artist_key in seen_artist_keys:
                            continue
                        seen_artist_keys.add(artist_key)
                        ordered_artist_names.append(artist_name.strip())

                if not ordered_artist_names:
                    continue

                self.logger.debug(
                    "Genre '%s': extracted %d unique artists from tag recordings",
                    genre,
                    len(ordered_artist_names),
                )

                resolved_artists: UniqueList[Artist] = UniqueList()
                for artist_name in ordered_artist_names:
                    resolved_artist = await self.resolve_provider_artist_by_name(artist_name)
                    if resolved_artist is not None:
                        resolved_artists.append(resolved_artist)
                    if len(resolved_artists) >= GENRE_ARTISTS_PER_GENRE_LIMIT:
                        break

                if not resolved_artists:
                    continue

                folder_slug = re.sub(r"[^a-z0-9]+", "_", genre.casefold()).strip("_") or "genre"
                result.append(
                    RecommendationFolder(
                        item_id=f"genre_artists_{folder_slug}",
                        provider=self.instance_id,
                        name=f"{genre.title()} Artists",
                        items=UniqueList(resolved_artists),
                    )
                )
                self.logger.debug(
                    "Genre '%s': created folder with %d resolved artists",
                    genre,
                    len(resolved_artists),
                )
            except MusicAssistantError as err:
                self.logger.warning("Skipping genre artists for '%s': %s", genre, err)
            except Exception as err:  # noqa: BLE001
                self.logger.exception(
                    "Unexpected error while building genre artists for '%s': %s", genre, err
                )

        self.logger.debug("Genre artists build completed: %d folders created", len(result))
        return result

    async def _build_folder_genre_playlists(
        self,
    ) -> tuple[RecommendationFolder, dict[str, tuple[Playlist, list[Track]]]]:
        """Build one playlist per genre and pre-resolve its tracks."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return (
                RecommendationFolder(
                    item_id="genre_playlists",
                    provider=self.instance_id,
                    name="Genre Playlists",
                    items=UniqueList([]),
                ),
                {},
            )

        genre_activity = await self.api_client.get_user_genre_activity(
            username=username.strip(),
            stats_ranges=STATS_RANGE,
        )
        if not isinstance(genre_activity, list):
            return (
                RecommendationFolder(
                    item_id="genre_playlists",
                    provider=self.instance_id,
                    name="Genre Playlists",
                    items=UniqueList([]),
                ),
                {},
            )

        self.logger.debug("Genre activity rows received: %d", len(genre_activity))

        top_genres = [
            entry.get("genre")
            for entry in genre_activity[:GENRE_PLAYLISTS_LIMIT]
            if isinstance(entry, dict) and isinstance(entry.get("genre"), str)
        ]

        self.logger.debug("Building genre playlists folder for %d genres", len(top_genres))

        playlist_items: UniqueList[Playlist] = UniqueList()
        resolved_virtual_playlists: dict[str, tuple[Playlist, list[Track]]] = {}
        pop_begin, pop_end = self._get_lb_radio_pop_range()

        for genre in top_genres:
            tag_rows = await self.api_client.get_lb_radio_tags(
                tags=[genre],
                pop_begin=pop_begin,
                pop_end=pop_end,
                count=LB_RADIO_TRACK_LIMIT,
            )
            if not tag_rows:
                continue

            recording_mbids = [
                row.get("recording_mbid")
                for row in tag_rows[:LB_RADIO_TRACK_LIMIT]
                if isinstance(row, dict) and isinstance(row.get("recording_mbid"), str)
            ]
            recording_mbids = [mbid.strip() for mbid in recording_mbids if mbid and mbid.strip()]
            if not recording_mbids:
                continue

            self.logger.debug(
                "Genre '%s': building playlist from %d tag recordings",
                genre,
                len(recording_mbids),
            )

            resolved_tracks: UniqueList[Track] = UniqueList()
            for start_idx in range(0, len(recording_mbids), 10):
                recording_batch = recording_mbids[start_idx : start_idx + 10]
                recording_metadata = await self.api_client.get_recording_metadata(
                    recording_batch,
                    "artist",
                )
                if not recording_metadata:
                    continue

                for recording_mbid in recording_batch:
                    metadata_row = recording_metadata.get(recording_mbid)
                    if not isinstance(metadata_row, dict):
                        continue

                    recording_data = metadata_row.get("recording")
                    if not isinstance(recording_data, dict):
                        continue
                    track_name = recording_data.get("name")
                    if not isinstance(track_name, str) or not track_name.strip():
                        continue

                    artist_name = ""
                    artist_data = metadata_row.get("artist")
                    if isinstance(artist_data, dict):
                        artists = artist_data.get("artists")
                        if isinstance(artists, list):
                            for artist in artists:
                                if not isinstance(artist, dict):
                                    continue
                                candidate_name = artist.get("name")
                                if isinstance(candidate_name, str) and candidate_name.strip():
                                    artist_name = candidate_name.strip()
                                    break
                        if not artist_name:
                            candidate_name = artist_data.get("name")
                            if isinstance(candidate_name, str) and candidate_name.strip():
                                artist_name = candidate_name.strip()

                    resolved_track = await self.resolve_provider_track_by_name(
                        artist_name,
                        track_name.strip(),
                    )
                    if resolved_track is not None:
                        resolved_tracks.append(resolved_track)

            if not resolved_tracks:
                continue

            cover_images: list[bytes] = []
            for resolved_track in resolved_tracks:
                track_image = getattr(resolved_track, "image", None)
                if not track_image or not isinstance(track_image.path, str) or not track_image.path:
                    continue
                image_bytes = await self.mass.metadata.get_thumbnail(
                    path=track_image.path,
                    provider=track_image.provider,
                    size=600,
                    base64=False,
                )
                if not isinstance(image_bytes, bytes):
                    continue
                if not image_bytes:
                    continue
                if image_bytes in cover_images:
                    continue
                cover_images.append(image_bytes)
                if len(cover_images) >= 4:
                    break

            cover_image_png = None
            if cover_images:
                cover_image_png = build_split_cover_png(
                    text_left="Radio",
                    text_right=f"{genre.title()}",
                    cover_images=cover_images,
                )

            folder_slug = re.sub(r"[^a-z0-9]+", "_", genre.casefold()).strip("_") or "genre"
            playlist_item_id = f"genre_playlist_{folder_slug}"
            playlist = await self._create_virtual_playlist(
                item_id=playlist_item_id,
                name=f"Radio: {genre.title()}",
                cover_image_png=cover_image_png,
            )
            playlist_items.append(playlist)
            resolved_virtual_playlists[playlist_item_id] = (playlist, list(resolved_tracks))

            self.logger.debug(
                "Genre '%s': created playlist with %d resolved tracks",
                genre,
                len(resolved_tracks),
            )

        return (
            RecommendationFolder(
                item_id="genre_playlists",
                provider=self.instance_id,
                name="Genre Playlists",
                items=playlist_items,
            ),
            resolved_virtual_playlists,
        )

    async def _build_folder_artist_playlists(
        self,
    ) -> tuple[RecommendationFolder, dict[str, tuple[Playlist, list[Track]]]]:
        """Build one playlist per top artist and pre-resolve its tracks."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return (
                RecommendationFolder(
                    item_id="artist_playlists",
                    provider=self.instance_id,
                    name="Artist Playlists",
                    items=UniqueList([]),
                ),
                {},
            )

        top_artist_rows = await self.api_client.get_user_top_artists(
            username=username.strip(),
            stats_ranges=STATS_RANGE,
            count=ARTIST_PLAYLISTS_LIMIT,
        )
        if not isinstance(top_artist_rows, list):
            return (
                RecommendationFolder(
                    item_id="artist_playlists",
                    provider=self.instance_id,
                    name="Artist Playlists",
                    items=UniqueList([]),
                ),
                {},
            )

        self.logger.debug("Top artists rows received: %d", len(top_artist_rows))

        top_artists = [
            row
            for row in top_artist_rows[:ARTIST_PLAYLISTS_LIMIT]
            if isinstance(row, dict)
        ]
        self.logger.debug("Building artist playlists folder for %d artists", len(top_artists))

        playlist_items: UniqueList[Playlist] = UniqueList()
        resolved_virtual_playlists: dict[str, tuple[Playlist, list[Track]]] = {}
        pop_begin, pop_end = self._get_lb_radio_pop_range()
        lb_radio_mode = self._get_lb_radio_mode()
        featured_artists_limit = ARTIST_PLAYLISTS_FEATURED_ARTISTS_LIMIT
        if featured_artists_limit < 1:
            raise MusicAssistantError("ARTIST_PLAYLISTS_FEATURED_ARTISTS_LIMIT must be >= 1")
        max_similar_artists = max(0, featured_artists_limit)
        max_recordings_per_artist = max(1, LB_RADIO_TRACK_LIMIT // featured_artists_limit)

        for artist_row in top_artists:
            seed_artist_mbid = artist_row.get("artist_mbid")
            if not isinstance(seed_artist_mbid, str) or not seed_artist_mbid.strip():
                continue
            seed_artist_mbid = seed_artist_mbid.strip()

            artist_name_value = artist_row.get("artist_name")
            artist_name = artist_name_value.strip() if isinstance(artist_name_value, str) else ""
            if not artist_name:
                artist_name = "Artist"

            artist_payload = await self.api_client.get_lb_radio_artist(
                seed_artist_mbid=seed_artist_mbid,
                mode=lb_radio_mode,
                max_similar_artists=max_similar_artists,
                max_recordings_per_artist=max_recordings_per_artist,
                pop_begin=pop_begin,
                pop_end=pop_end,
            )
            artist_tracks: list[dict[str, Any]] = []
            for values in artist_payload.values():
                artist_tracks.extend(values)
            if not artist_tracks:
                continue

            recording_mbids = [
                row.get("recording_mbid")
                for row in artist_tracks[:LB_RADIO_TRACK_LIMIT]
                if isinstance(row, dict) and isinstance(row.get("recording_mbid"), str)
            ]
            recording_mbids = [mbid.strip() for mbid in recording_mbids if mbid and mbid.strip()]
            if not recording_mbids:
                continue

            self.logger.debug(
                "Artist '%s': building playlist from %d LB-Radio recordings",
                artist_name,
                len(recording_mbids),
            )

            resolved_tracks: UniqueList[Track] = UniqueList()
            for start_idx in range(0, len(recording_mbids), 10):
                recording_batch = recording_mbids[start_idx : start_idx + 10]
                recording_metadata = await self.api_client.get_recording_metadata(
                    recording_batch,
                    "artist",
                )
                if not recording_metadata:
                    continue

                for recording_mbid in recording_batch:
                    metadata_row = recording_metadata.get(recording_mbid)
                    if not isinstance(metadata_row, dict):
                        continue

                    recording_data = metadata_row.get("recording")
                    if not isinstance(recording_data, dict):
                        continue
                    track_name = recording_data.get("name")
                    if not isinstance(track_name, str) or not track_name.strip():
                        continue

                    resolved_track = await self.resolve_provider_track_by_name(
                        artist_name,
                        track_name.strip(),
                    )
                    if resolved_track is not None:
                        resolved_tracks.append(resolved_track)

            if not resolved_tracks:
                continue

            artist_names_for_cover: list[str] = [artist_name]
            seen_artist_names = {self._normalize_text(artist_name)}
            for featured_artist_mbid, featured_rows in artist_payload.items():
                if featured_artist_mbid == seed_artist_mbid:
                    continue
                if not isinstance(featured_rows, list) or not featured_rows:
                    continue
                featured_name: str | None = None
                first_row = featured_rows[0]
                if isinstance(first_row, dict):
                    candidate_name = first_row.get("similar_artist_name")
                    if isinstance(candidate_name, str) and candidate_name.strip():
                        featured_name = candidate_name.strip()
                if not featured_name:
                    continue
                normalized_featured_name = self._normalize_text(featured_name)
                if not normalized_featured_name or normalized_featured_name in seen_artist_names:
                    continue
                seen_artist_names.add(normalized_featured_name)
                artist_names_for_cover.append(featured_name)
                if len(artist_names_for_cover) >= 4:
                    break

            cover_images: list[bytes] = []
            for cover_artist_name in artist_names_for_cover:
                resolved_cover_artist = await self.resolve_provider_artist_by_name(cover_artist_name)
                if resolved_cover_artist is None:
                    continue
                artist_image = getattr(resolved_cover_artist, "image", None)
                if not artist_image or not isinstance(artist_image.path, str) or not artist_image.path:
                    continue
                image_bytes = await self.mass.metadata.get_thumbnail(
                    path=artist_image.path,
                    provider=artist_image.provider,
                    size=600,
                    base64=False,
                )
                if not isinstance(image_bytes, bytes) or not image_bytes:
                    continue
                if image_bytes in cover_images:
                    continue
                cover_images.append(image_bytes)

            cover_image_png = None
            if cover_images:
                cover_image_png = build_split_cover_png(
                    text_left="FEATURING",
                    text_right=f"{artist_name.title()}",
                    cover_images=cover_images,
                )

            folder_slug = re.sub(r"[^a-z0-9]+", "_", artist_name.casefold()).strip("_") or "artist"
            playlist_item_id = f"artist_playlist_{folder_slug}"
            playlist = await self._create_virtual_playlist(
                item_id=playlist_item_id,
                name=f"Featuring {', '.join(artist_names_for_cover[:3])}",
                cover_image_png=cover_image_png,
            )
            playlist_items.append(playlist)
            resolved_virtual_playlists[playlist_item_id] = (playlist, list(resolved_tracks))

            self.logger.debug(
                "Artist '%s': created playlist with %d resolved tracks",
                artist_name,
                len(resolved_tracks),
            )

        return (
            RecommendationFolder(
                item_id="artist_playlists",
                provider=self.instance_id,
                name="Artist Playlists",
                items=playlist_items,
            ),
            resolved_virtual_playlists,
        )

    async def _build_folder_top_artists(
        self,
    ) -> RecommendationFolder:
        """Build the top artists recommendation folder."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return RecommendationFolder(
                item_id="top_artists",
                provider=self.instance_id,
                name="Top Artists",
                items=UniqueList([]),
            )

        top_artist_rows = await self.api_client.get_user_top_artists(
            username=username.strip(),
            stats_ranges=STATS_RANGE,
            count=TOP_ARTISTS_LIMIT,
        )
        if not isinstance(top_artist_rows, list):
            return RecommendationFolder(
                item_id="top_artists",
                provider=self.instance_id,
                name="Top Artists",
                items=UniqueList([]),
            )

        resolved_artists: UniqueList[Artist] = UniqueList()
        for artist_row in top_artist_rows[:TOP_ARTISTS_LIMIT]:
            if not isinstance(artist_row, dict):
                continue
            artist_name = artist_row.get("artist_name")
            if not isinstance(artist_name, str) or not artist_name.strip():
                continue
            resolved_artist = await self.resolve_provider_artist_by_name(artist_name)
            if resolved_artist is not None:
                resolved_artists.append(resolved_artist)

        self.logger.debug(
            "Top artists build completed: %d artists resolved",
            len(resolved_artists),
        )

        return RecommendationFolder(
            item_id="top_artists",
            provider=self.instance_id,
            name="Top Artists",
            items=resolved_artists,
        )

    async def _build_folder_recommendations(self) -> RecommendationFolder:
        """Build recommendations from LB-Radio recs prompt."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return RecommendationFolder(
                item_id="recommendations",
                provider=self.instance_id,
                name="Recommendations",
                items=UniqueList([]),
            )

        prompt = f"recs:{username.strip()}::unlistened"
        explore_tracks = await self.api_client.get_explore_lb_radio(
            prompt=prompt,
            mode=self._get_lb_radio_mode(),
        )
        if not isinstance(explore_tracks, list) or not explore_tracks:
            return RecommendationFolder(
                item_id="recommendations",
                provider=self.instance_id,
                name="Recommendations",
                items=UniqueList([]),
            )

        resolved_tracks: UniqueList[Track] = UniqueList()
        seen_track_keys: set[str] = set()

        for track_row in explore_tracks[:LB_RADIO_TRACK_LIMIT]:
            if not isinstance(track_row, dict):
                continue

            track_name = track_row.get("title")
            artist_name = track_row.get("creator")
            if not isinstance(track_name, str) or not track_name.strip():
                continue
            if not isinstance(artist_name, str):
                artist_name = ""

            track_key = (
                f"{self._normalize_text(artist_name)}::{self._normalize_text(track_name)}"
            )
            if not track_key or track_key in seen_track_keys:
                continue
            seen_track_keys.add(track_key)

            resolved_track = await self.resolve_provider_track_by_name(
                artist_name,
                track_name,
            )
            if resolved_track is not None:
                resolved_tracks.append(resolved_track)

        self.logger.debug(
            "Recommendations build completed (prompt=%s): %d tracks resolved",
            prompt,
            len(resolved_tracks),
        )

        return RecommendationFolder(
            item_id="recommendations",
            provider=self.instance_id,
            name="Recommendations",
            items=resolved_tracks,
        )

    async def _build_folder_top_tracks(self) -> RecommendationFolder:
        """Build the top tracks recommendation folder."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return RecommendationFolder(
                item_id="top_tracks",
                provider=self.instance_id,
                name="Top Tracks",
                items=UniqueList([]),
            )

        top_track_rows = await self.api_client.get_user_top_tracks(
            username=username.strip(),
            stats_ranges=STATS_RANGE,
            count=TOP_TRACKS_LIMIT,
        )
        if not isinstance(top_track_rows, list):
            return RecommendationFolder(
                item_id="top_tracks",
                provider=self.instance_id,
                name="Top Tracks",
                items=UniqueList([]),
            )

        resolved_tracks: UniqueList[Track] = UniqueList()
        for track_row in top_track_rows[:TOP_TRACKS_LIMIT]:
            if not isinstance(track_row, dict):
                continue

            track_name = track_row.get("track_name")
            if not isinstance(track_name, str) or not track_name.strip():
                continue

            artist_name = track_row.get("artist_name")
            if not isinstance(artist_name, str):
                artist_name = ""

            resolved_track = await self.resolve_provider_track_by_name(
                artist_name,
                track_name.strip(),
            )
            if resolved_track is not None:
                resolved_tracks.append(resolved_track)

        self.logger.debug(
            "Top tracks build completed: %d tracks resolved",
            len(resolved_tracks),
        )

        return RecommendationFolder(
            item_id="top_tracks",
            provider=self.instance_id,
            name="Top Tracks",
            items=resolved_tracks,
        )

    async def _build_folder_recent_listens(self) -> RecommendationFolder:
        """Build artists from the user's most recent listens."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return RecommendationFolder(
                item_id="recent_listens",
                provider=self.instance_id,
                name="Recent Listens",
                items=UniqueList([]),
            )

        self.logger.debug(
            "Building recent listens folder for user=%s (count=%d)",
            username.strip(),
            100,
        )

        recent_listens = await self.api_client.get_user_listens(
            username=username.strip(),
            count=100,
        )
        if not isinstance(recent_listens, list) or not recent_listens:
            return RecommendationFolder(
                item_id="recent_listens",
                provider=self.instance_id,
                name="Recent Listens",
                items=UniqueList([]),
            )

        self.logger.debug("Recent listens rows received: %d", len(recent_listens))

        ordered_artist_names: list[str] = []
        seen_artist_keys: set[str] = set()

        for listen in recent_listens:
            if not isinstance(listen, dict):
                continue
            track_metadata = listen.get("track_metadata")
            if not isinstance(track_metadata, dict):
                continue

            artist_name = track_metadata.get("artist_name")
            mbid_mapping = track_metadata.get("mbid_mapping")

            artist_mbid: str | None = None
            if isinstance(mbid_mapping, dict):
                artist_mbids = mbid_mapping.get("artist_mbids")
                if isinstance(artist_mbids, list):
                    for candidate_mbid in artist_mbids:
                        if isinstance(candidate_mbid, str) and candidate_mbid.strip():
                            artist_mbid = candidate_mbid.strip()
                            break
                if (not isinstance(artist_name, str) or not artist_name.strip()) and isinstance(
                    mbid_mapping.get("artists"), list
                ):
                    for artist in mbid_mapping["artists"]:
                        if not isinstance(artist, dict):
                            continue
                        candidate_name = artist.get("artist_credit_name")
                        if isinstance(candidate_name, str) and candidate_name.strip():
                            artist_name = candidate_name.strip()
                            break

            if not isinstance(artist_name, str) or not artist_name.strip():
                continue
            artist_name = artist_name.strip()

            if artist_mbid:
                artist_key = f"mbid:{artist_mbid}"
            else:
                artist_key = f"name:{self._normalize_text(artist_name)}"
            if artist_key in seen_artist_keys:
                continue

            seen_artist_keys.add(artist_key)
            ordered_artist_names.append(artist_name)

        self.logger.debug(
            "Recent listens: extracted %d unique artists in listen order",
            len(ordered_artist_names),
        )

        resolved_artists: UniqueList[Artist] = UniqueList()
        for artist_name in ordered_artist_names:
            resolved_artist = await self.resolve_provider_artist_by_name(artist_name)
            if resolved_artist is not None:
                resolved_artists.append(resolved_artist)

        self.logger.debug(
            "Recent listens build completed: %d artist candidates, %d artists resolved",
            len(ordered_artist_names),
            len(resolved_artists),
        )

        return RecommendationFolder(
            item_id="recent_listens",
            provider=self.instance_id,
            name="Recent Listens",
            items=resolved_artists,
        )

    async def _build_folder_similar_artists(
        self,
    ) -> list[RecommendationFolder]:
        """Build a top-level similar-artists folder for each seed artist."""
        username = self.config.get_value(CONF_USERNAME)
        if not isinstance(username, str) or not username.strip():
            return []

        seed_artist_rows = await self.api_client.get_user_top_artists(
            username=username.strip(),
            stats_ranges=STATS_RANGE,
            count=SIMILAR_ARTISTS_SEED_LIMIT,
        )
        if not isinstance(seed_artist_rows, list):
            return []

        seed_artists = [
            row
            for row in seed_artist_rows[:SIMILAR_ARTISTS_SEED_LIMIT]
            if isinstance(row, dict)
        ]
        if not seed_artists:
            return []

        self.logger.debug(
            "Building similar artists folders for %d seed artists (limit=%d)",
            len(seed_artists),
            SIMILAR_ARTISTS_SEED_LIMIT,
        )

        pop_begin, pop_end = self._get_lb_radio_pop_range()
        lb_radio_mode = self._get_lb_radio_mode()
        max_similar_artists = max(0, SIMILAR_ARTISTS_PER_SEED_LIMIT + 1)
        max_recordings_per_artist = 1

        result: list[RecommendationFolder] = []
        for seed_artist in seed_artists:
            seed_artist_mbid = seed_artist.get("artist_mbid")
            if not isinstance(seed_artist_mbid, str) or not seed_artist_mbid.strip():
                continue
            seed_artist_mbid = seed_artist_mbid.strip()

            seed_artist_name_value = seed_artist.get("artist_name")
            seed_artist_name = (
                seed_artist_name_value.strip()
                if isinstance(seed_artist_name_value, str) and seed_artist_name_value.strip()
                else "Artist"
            )
            normalized_seed_artist_name = self._normalize_text(seed_artist_name)

            artist_payload = await self.api_client.get_lb_radio_artist(
                seed_artist_mbid=seed_artist_mbid,
                mode=lb_radio_mode,
                max_similar_artists=max_similar_artists,
                max_recordings_per_artist=max_recordings_per_artist,
                pop_begin=pop_begin,
                pop_end=pop_end,
            )
            if not artist_payload:
                continue

            ordered_artist_names: list[str] = []
            seen_artist_names: set[str] = set()

            for similar_artist_mbid, rows in artist_payload.items():
                if similar_artist_mbid.strip() == seed_artist_mbid:
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    candidate_mbid = row.get("similar_artist_mbid")
                    if isinstance(candidate_mbid, str) and candidate_mbid.strip() == seed_artist_mbid:
                        continue
                    candidate_name = row.get("similar_artist_name")
                    if not isinstance(candidate_name, str) or not candidate_name.strip():
                        continue
                    normalized_candidate_name = self._normalize_text(candidate_name)
                    if (
                        not normalized_candidate_name
                        or normalized_candidate_name == normalized_seed_artist_name
                        or normalized_candidate_name in seen_artist_names
                    ):
                        continue
                    seen_artist_names.add(normalized_candidate_name)
                    ordered_artist_names.append(candidate_name.strip())
                    if len(ordered_artist_names) >= SIMILAR_ARTISTS_PER_SEED_LIMIT:
                        break
                if len(ordered_artist_names) >= SIMILAR_ARTISTS_PER_SEED_LIMIT:
                    break

            if not ordered_artist_names:
                continue

            resolved_artists: UniqueList[Artist] = UniqueList()
            for artist_name in ordered_artist_names:
                resolved_artist = await self.resolve_provider_artist_by_name(artist_name)
                if resolved_artist is not None:
                    resolved_artists.append(resolved_artist)
                if len(resolved_artists) >= SIMILAR_ARTISTS_PER_SEED_LIMIT:
                    break

            if not resolved_artists:
                continue

            folder_slug = (
                re.sub(r"[^a-z0-9]+", "_", seed_artist_name.casefold()).strip("_") or "artist"
            )
            result.append(
                RecommendationFolder(
                    item_id=f"similar_artists_{folder_slug}",
                    provider=self.instance_id,
                    name=f"Similar Artists for {seed_artist_name}",
                    items=UniqueList(resolved_artists),
                )
            )
            self.logger.debug(
                "Seed artist '%s': created similar artists folder with %d artists",
                seed_artist_name,
                len(resolved_artists),
            )

        self.logger.debug("Similar artists build completed: %d folders created", len(result))
        return result

    async def _create_virtual_playlist(
        self,
        item_id: str,
        name: str,
        cover_image_png: bytes | None = None,
    ) -> Playlist:
        """Create a minimal virtual playlist item.

        The cover PNG (if provided) is saved to the server's collage_images
        directory so it is served as a regular HTTP imageproxy URL.
        """
        playlist_kwargs: dict[str, Any] = {}
        if cover_image_png:
            collage_dir = os.path.join(self.mass.cache_path, "collage_images")
            await asyncio.to_thread(os.makedirs, collage_dir, exist_ok=True)
            filename = f"lb_{item_id}.png"
            file_path = os.path.join(collage_dir, filename)
            await asyncio.to_thread(_write_bytes, file_path, cover_image_png)
            playlist_kwargs["metadata"] = MediaItemMetadata(
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"/collage/{filename}",
                            provider="builtin",
                            remotely_accessible=False,
                        )
                    ]
                )
            )
        return Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            is_editable=False,
            owner="ListenBrainz",
            **playlist_kwargs,
        )

    async def resolve_provider_artist_by_name(self, artist_name: str) -> Artist | None:
        """Resolve an artist by name."""
        artist_name = self._normalize_text(artist_name)
        cache_key = artist_name
        if not cache_key:
            return None
        if cache_key in self._artist_cache:
            return self._artist_cache[cache_key]

        try:
            search_result = await self.mass.music.search(
                search_query=artist_name,
                media_types=[MediaType.ARTIST],
                limit=10,
            )
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Artist resolve failed for '%s': %s", artist_name, err)
            return None
        if not search_result.artists:
            return None

        # Prefer an exact name match first, then fall back to first hit.
        selected_artist = search_result.artists[0]
        for artist in search_result.artists:
            if self._normalize_text(artist.name) == cache_key:
                selected_artist = artist
                break

        self._artist_cache[cache_key] = selected_artist
        return selected_artist

    async def resolve_provider_track_by_name(
        self,
        artist_name: str,
        track_name: str,
    ) -> Track | None:
        """Resolve a track by artist and track name."""
        track_name = self._normalize_text(track_name)
        artist_name = self._normalize_text(artist_name)
        if not track_name:
            return None

        normalized_track = track_name
        normalized_artist = artist_name
        search_query = f"{artist_name} - {track_name}" if artist_name else track_name
        try:
            search_result = await self.mass.music.search(
                search_query=search_query,
                media_types=[MediaType.TRACK],
                limit=10,
            )
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Track resolve failed for query '%s': %s", search_query, err)
            return None
        if not search_result.tracks:
            return None

        # Best match: exact track title + exact artist name.
        if normalized_artist:
            for track in search_result.tracks:
                if self._normalize_text(track.name) != normalized_track:
                    continue
                artists = getattr(track, "artists", None)
                artist_names: list[str] = []
                if artists:
                    for artist in artists:
                        name = getattr(artist, "name", None)
                        if isinstance(name, str) and name.strip():
                            artist_names.append(self._normalize_text(name))
                if normalized_artist in artist_names:
                    return track

        # Second best: exact track title.
        for track in search_result.tracks:
            if self._normalize_text(track.name) == normalized_track:
                return track

        return search_result.tracks[0]

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """Normalize text for case-insensitive matching and search input cleanup."""
        if not isinstance(value, str):
            return ""
        value = unicodedata.normalize("NFKD", value)
        value = "".join(char for char in value if not unicodedata.combining(char))
        value = value.casefold().strip()
        value = re.sub(r"\s+", " ", value)
        return value


def _write_bytes(path: str, data: bytes) -> None:
    """Write bytes to a file (synchronous, intended for asyncio.to_thread)."""
    with open(path, "wb") as fh:
        fh.write(data)
