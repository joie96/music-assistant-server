"""Last.fm recommendations based on Music Assistant listening and favorites."""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from functools import partial
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING

from aiohttp import ClientError
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    AuthenticationFailed,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)
from music_assistant_models.media_items.metadata import MediaItemImage, MediaItemLink

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_current_user,
    impersonated_user,
)
from music_assistant.helpers.images import get_image_thumb
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.throttle_retry import RequestPriority, with_request_priority
from music_assistant.models.media_capabilities import MediaCatalogMixin
from music_assistant.models.metadata_provider import MetadataProvider
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMAPIClient
from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    EXTRA_FOLDERS,
    GENRE_FOLDER_FAMILIES,
    GenreProfile,
    PlaylistSpec,
)
from music_assistant.providers.lastfm_recommendations_enhanced.collections import (
    PLAYLIST_FAMILIES,
    LastFMCollections,
)
from music_assistant.providers.lastfm_recommendations_enhanced.constants import (
    CONF_API_KEY,
    CONF_RECOMMENDATION_USER_ID,
    REFRESH_INTERVAL_HOURS,
    REFRESH_TASK_ID,
)
from music_assistant.providers.lastfm_recommendations_enhanced.cover_art import (
    build_split_cover_png,
)
from music_assistant.providers.lastfm_recommendations_enhanced.recommendations import (
    DISCOVERY_FOLDERS,
    RESOLUTION_RETRY_DELAY,
    LastFMDiscovery,
    ResolutionState,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import (
        BrowseFolder,
        ItemMapping,
        MediaItemType,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES = {ProviderFeature.RECOMMENDATIONS}
TOP_ITEMS_FOLDERS: tuple[tuple[str, MediaType, str], ...] = (
    ("user_top_artists_monthly", MediaType.ARTIST, "Your most played artists"),
    ("user_top_tracks_monthly", MediaType.TRACK, "Your most played tracks"),
    ("user_top_albums_monthly", MediaType.ALBUM, "Your most played albums"),
)
TOP_ITEMS_LIMIT = 50
PLAYLIST_COVER_LABELS = {
    "user_genre_playlists_monthly": "Genre",
    "global_genre_playlists": "Genre",
    "user_related_genre_playlists": "Genre",
    "user_artist_playlists_monthly": "K\u00fcnstler",
    "global_artist_radio_playlists": "Radio",
    "global_mood_playlists": "Stimmung",
    "global_decade_playlists": "Jahrzehnt",
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> LastFMRecommendationsEnhancedProvider:
    """Create the independent Last.fm Recommendations Enhanced provider."""
    return LastFMRecommendationsEnhancedProvider(mass, manifest, config, SUPPORTED_FEATURES)


class LastFMRecommendationsEnhancedProvider(MetadataProvider, MediaCatalogMixin):
    """Personal and general recommendations without a Last.fm account."""

    async def handle_async_init(self) -> None:
        """Validate the configured API key before exposing the provider."""
        self.logger.debug("Last.fm Enhanced initialization start instance=%s", self.instance_id)
        api_key = self.get_setup_value(CONF_API_KEY)
        if not isinstance(api_key, str) or not api_key.strip():
            self.logger.debug("Last.fm Enhanced initialization failed reason=missing_api_key")
            raise AuthenticationFailed("A personal Last.fm API key is required")
        user_id = self.get_setup_value(CONF_RECOMMENDATION_USER_ID)
        if not isinstance(user_id, str) or not user_id.strip():
            raise AuthenticationFailed("Select a Music Assistant user for recommendations")
        user = await self.mass.webserver.auth.get_user(user_id)
        if user is None or not user.enabled:
            raise AuthenticationFailed("The selected Music Assistant user is not available")
        self.api = LastFMAPIClient(
            self.mass.http_session, api_key, self.logger, cache=self.mass.cache
        )
        await self.api.validate_api_key()
        self._recommendation_folders: list[RecommendationFolder] = []
        self._resolved_virtual_playlists: dict[str, tuple[Playlist, list[Track]]] = {}
        self._refresh_lock = asyncio.Lock()
        self._is_unloading = False
        self._recommendation_user_id = user_id
        task_id = f"{REFRESH_TASK_ID}_{self.instance_id}"
        schedule = TaskSchedule.hourly(every=REFRESH_INTERVAL_HOURS)
        task = self.mass.tasks.register_scheduled_task(
            task_id=task_id,
            name="Refresh Last.fm Enhanced recommendations",
            handler=self._refresh_recommendations,
            schedule=schedule,
        )
        if task.schedule is not None:
            schedule.enabled = task.schedule.enabled
            if task.schedule != schedule:
                self.mass.tasks.update_task_schedule(task_id, schedule)
        self.mass.call_later(
            20,
            partial(self.mass.tasks.run_task, task_id),
            task_id=f"{REFRESH_TASK_ID}_initial_{self.instance_id}",
        )
        self.logger.debug("Last.fm Enhanced initialized instance=%s", self.instance_id)

    async def unload(self, is_removed: bool = False) -> None:
        """Cancel startup and stop scheduled work before releasing prepared recommendations."""
        self._is_unloading = True
        self.mass.cancel_timer(f"{REFRESH_TASK_ID}_initial_{self.instance_id}")
        self.mass.cancel_timer(f"{REFRESH_TASK_ID}_retry_{self.instance_id}")
        stopped = await self.mass.tasks.unregister_scheduled_task_and_wait(
            f"{REFRESH_TASK_ID}_{self.instance_id}", clear_persisted_state=is_removed
        )
        if stopped:
            self._recommendation_folders.clear()
            self._resolved_virtual_playlists.clear()

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """List prepared folders for the selected MA user without their contents."""
        if not self._is_visible_to_current_user():
            return []
        return [replace(folder, items=UniqueList()) for folder in self._recommendation_folders]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Return prepared folder contents without statistics, searches or Last.fm requests."""
        if not self._is_visible_to_current_user():
            return UniqueList()
        for folder in self._recommendation_folders:
            if folder.item_id == item_id:
                return deepcopy(folder.items)
        return UniqueList()

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a prepared read-only playlist belonging to the selected MA user."""
        if not self._is_visible_to_current_user(allow_internal=True):
            raise MediaNotFoundError("The generated playlist is not available")
        if payload := self._resolved_virtual_playlists.get(prov_playlist_id):
            return deepcopy(payload[0])
        raise MediaNotFoundError("The generated playlist is not available")

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return pre-resolved playlist tracks; subsequent pages are empty."""
        if not self._is_visible_to_current_user(allow_internal=True):
            raise MediaNotFoundError("The generated playlist is not available")
        if page != 0:
            return []
        if payload := self._resolved_virtual_playlists.get(prov_playlist_id):
            return deepcopy(payload[1])
        raise MediaNotFoundError("The generated playlist is not available")

    async def resolve_image(self, path: str) -> bytes:
        """Resolve only this instance's published covers, subject to user visibility."""
        if not self._is_visible_to_current_user(allow_internal=True):
            raise MediaNotFoundError("The generated image is not available")
        if not re.fullmatch(r"[a-f0-9]{64}\.png", path):
            raise MediaNotFoundError("The generated image is not available")

        def read_cover() -> bytes:
            directory = self._cover_directory().resolve()
            image_path = (directory / path).resolve()
            if not is_safe_path(str(image_path), str(directory)):
                raise MediaNotFoundError("The generated image is not available")
            try:
                return image_path.read_bytes()
            except FileNotFoundError as err:
                raise MediaNotFoundError("The generated image is not available") from err

        return await asyncio.to_thread(read_cover)

    def _is_visible_to_current_user(self, *, allow_internal: bool = False) -> bool:
        """Restrict prepared results to their MA user, allowing internal playlist playback."""
        if self._is_unloading:
            return False
        user_id = self.get_setup_value(CONF_RECOMMENDATION_USER_ID)
        if not isinstance(user_id, str) or user_id != self._recommendation_user_id:
            return False
        user = get_current_user()
        return allow_internal if user is None else user.user_id == user_id

    async def _get_recommendation_folders(
        self, *, genre_profile: GenreProfile | None = None
    ) -> list[RecommendationFolder]:
        """Describe all supported rows for the next background generation."""
        self.logger.debug("Listing Last.fm recommendation folders")
        if genre_profile is None:
            collections = LastFMCollections(LastFMDiscovery(self))
            genre_profile = await collections.get_genre_profile()
            genre_profile = replace(
                genre_profile, explore_genres=await collections.get_explore_genres()
            )
        folders = [
            RecommendationFolder(
                item_id=folder_id,
                provider=self.instance_id,
                name=name,
                translation_key=folder_id,
            )
            for folder_id, _media_type, name in TOP_ITEMS_FOLDERS
        ]
        folders.extend(
            RecommendationFolder(
                item_id=folder.item_id,
                provider=self.instance_id,
                name=folder.name,
                translation_key=folder.item_id,
                subtitle="Powered by Last.fm | https://www.last.fm/" if folder.method else None,
            )
            for folder in DISCOVERY_FOLDERS
        )
        folders.extend(
            RecommendationFolder(
                item_id=folder_id,
                provider=self.instance_id,
                name=name,
                translation_key=folder_id,
                subtitle="Powered by Last.fm | https://www.last.fm/",
            )
            for folder_id, name in EXTRA_FOLDERS
        )
        folders.extend(
            RecommendationFolder(
                item_id=f"{prefix}_{category.key}",
                provider=self.instance_id,
                name=f"{category.name}: {label}",
                translation_key=prefix,
                translation_params=[category.name],
                subtitle="Powered by Last.fm | https://www.last.fm/",
            )
            for prefix, label in GENRE_FOLDER_FAMILIES
            for category in (
                genre_profile.genres[:3]
                if prefix == "user_genre_artists"
                else genre_profile.explore_genres
            )
        )
        self.logger.debug("Last.fm recommendation folders ready count=%d", len(folders))
        return folders

    async def _build_recommendation_items(
        self,
        item_id: str,
        *,
        genre_profile: GenreProfile | None = None,
        discovery: LastFMDiscovery | None = None,
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Build one statistics or discovery folder within the selected user's context.

        :param item_id: The folder identifier returned by get_recommendations.
        """
        discovery = discovery if discovery is not None else LastFMDiscovery(self)
        collections = LastFMCollections(discovery, genre_profile=genre_profile)
        if item_id == "user_album_recommendations":
            return UniqueList(await collections.get_personal_albums())
        if item_id in PLAYLIST_FAMILIES.values():
            return UniqueList(
                self._playlist(recipe) for recipe in await collections.get_playlist_specs(item_id)
            )
        if item_id in {folder_id for folder_id, _name in EXTRA_FOLDERS} or item_id.startswith(
            tuple(f"{prefix}_" for prefix, _label in GENRE_FOLDER_FAMILIES)
        ):
            return UniqueList(await collections.get_folder_items(item_id))
        for folder in DISCOVERY_FOLDERS:
            if folder.item_id == item_id:
                return UniqueList(await discovery.build_items(folder))
        for folder_id, media_type, _name in TOP_ITEMS_FOLDERS:
            if item_id != folder_id:
                continue
            started = perf_counter()
            self.logger.debug(
                "Building Last.fm folder=%s source=ma_statistics media_type=%s period=month limit=%d",
                folder_id,
                media_type.value,
                TOP_ITEMS_LIMIT,
            )
            try:
                rows = await self.mass.statistics.get_top_items(
                    media_type=media_type, period="month", limit=TOP_ITEMS_LIMIT
                )
            except MusicAssistantError as err:
                self.logger.debug(
                    "Last.fm folder build failed folder=%s error_type=%s elapsed=%.3fs",
                    folder_id,
                    type(err).__name__,
                    perf_counter() - started,
                )
                raise
            self.logger.debug(
                "Last.fm folder source received folder=%s rows=%d", folder_id, len(rows)
            )
            items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList(
                entry.item for entry in rows if entry.item.available
            )
            unavailable = sum(not entry.item.available for entry in rows)
            self.logger.debug(
                "Last.fm folder build completed folder=%s received=%d unavailable=%d "
                "duplicates=%d items=%d reason=%s elapsed=%.3fs",
                folder_id,
                len(rows),
                unavailable,
                len(rows) - unavailable - len(items),
                len(items),
                "ready" if items else "no_available_items" if rows else "no_history",
                perf_counter() - started,
            )
            return items
        self.logger.debug("Last.fm folder build skipped reason=unknown_folder")
        return UniqueList()

    @with_request_priority(RequestPriority.LOW)
    async def _refresh_recommendations(self) -> None:
        """Rebuild all folders and playlists using the selected MA user's listening data."""
        async with self._refresh_lock:
            if self._is_unloading:
                return
            self.mass.cancel_timer(f"{REFRESH_TASK_ID}_retry_{self.instance_id}")
            user_id = self.get_setup_value(CONF_RECOMMENDATION_USER_ID)
            if not isinstance(user_id, str) or not user_id.strip():
                raise AuthenticationFailed("Select a Music Assistant user for recommendations")
            user = await self.mass.webserver.auth.get_user(user_id)
            if user is None or not user.enabled:
                raise AuthenticationFailed("The selected Music Assistant user is not available")
            started = perf_counter()
            resolution_state = ResolutionState()
            self.logger.debug("Building Last.fm recommendations user_id=%s", user_id)
            try:
                with _recommendation_user_context(user):
                    collections = LastFMCollections(LastFMDiscovery(self, state=resolution_state))
                    genre_profile = await collections.get_genre_profile()
                    genre_profile = replace(
                        genre_profile, explore_genres=await collections.get_explore_genres()
                    )
                    folders = await self._get_recommendation_folders(genre_profile=genre_profile)
                    playlists: dict[str, tuple[Playlist, list[Track]]] = {}
                    for folder in folders:
                        folder.items = await self._build_recommendation_items(
                            folder.item_id,
                            genre_profile=genre_profile,
                            discovery=LastFMDiscovery(self, state=resolution_state),
                        )
                        for item in folder.items:
                            if isinstance(item, Playlist) and item.item_id not in playlists:
                                tracks = await LastFMCollections(
                                    LastFMDiscovery(
                                        self,
                                        state=resolution_state,
                                    ),
                                    genre_profile=genre_profile,
                                ).get_playlist_tracks(item.item_id)
                                await self._set_playlist_cover(item, tracks, folder.item_id)
                                playlists[item.item_id] = (item, tracks)
                        self.logger.debug(
                            "Last.fm refresh folder=%s items=%d", folder.item_id, len(folder.items)
                        )
            except MusicAssistantError as err:
                self.logger.warning(
                    "Last.fm recommendations refresh failed error_type=%s elapsed=%.3fs",
                    type(err).__name__,
                    perf_counter() - started,
                )
                if isinstance(err, RateLimited | ResourceTemporarilyUnavailable):
                    self._schedule_refresh_retry(
                        err.backoff_time if isinstance(err, RateLimited) else RESOLUTION_RETRY_DELAY
                    )
                raise
            if resolution_state.retry_after:
                self._schedule_refresh_retry(resolution_state.retry_after)
            if resolution_state.has_incomplete_searches:
                self.logger.warning("Last.fm refresh deferred: streaming resolution incomplete")
                return
            if self._is_unloading:
                return
            retained_covers = {
                image.path
                for playlist, _tracks in self._resolved_virtual_playlists.values()
                for image in playlist.metadata.images or ()
            }
            retained_covers.update(
                image.path
                for playlist, _tracks in playlists.values()
                for image in playlist.metadata.images or ()
            )
            await self._remove_obsolete_covers(retained_covers)
            if self._is_unloading:
                return
            self._recommendation_user_id = user_id
            self._recommendation_folders = folders
            self._resolved_virtual_playlists = playlists
            self.logger.info(
                "Last.fm recommendations built folders=%d playlists=%d elapsed=%.3fs",
                len(folders),
                len(playlists),
                perf_counter() - started,
            )

    def _schedule_refresh_retry(self, delay: float) -> None:
        """Schedule one delayed retry without bypassing an upstream wait or unloading."""
        if self._is_unloading:
            return
        delay = max(RESOLUTION_RETRY_DELAY, delay)
        self.mass.call_later(
            delay,
            partial(self.mass.tasks.run_task, f"{REFRESH_TASK_ID}_{self.instance_id}"),
            task_id=f"{REFRESH_TASK_ID}_retry_{self.instance_id}",
        )
        self.logger.debug("Last.fm refresh retry scheduled delay=%.0fs", delay)

    async def _set_playlist_cover(
        self, playlist: Playlist, tracks: list[Track], folder_id: str
    ) -> None:
        """Attach artwork from prepared tracks without querying music catalogues."""
        images: list[bytes] = []
        seen_sources: set[tuple[str, str]] = set()
        for track in tracks:
            image = track.image
            if image is None or (image.provider, image.path) in seen_sources:
                continue
            if len(seen_sources) >= 8:
                break
            seen_sources.add((image.provider, image.path))
            try:
                data = await get_image_thumb(
                    self.mass, image.path, 600, image.provider, image_format="PNG"
                )
            except (OSError, ClientError, MediaNotFoundError, ProviderUnavailableError) as err:
                self.logger.debug(
                    "Last.fm cover source unavailable error_type=%s", type(err).__name__
                )
                continue
            if data and data not in images:
                images.append(data)
            if len(images) == 4:
                break
        right_title = playlist.name
        if folder_id == "user_artist_playlists_monthly":
            right_title = right_title.removeprefix("Featuring ")
        elif folder_id == "global_artist_radio_playlists":
            right_title = right_title.removesuffix(" & similar artists")
        payload = await asyncio.to_thread(
            build_split_cover_png, PLAYLIST_COVER_LABELS[folder_id], images, right_title
        )
        filename = sha256(payload).hexdigest() + ".png"

        def write_cover() -> None:
            directory = self._cover_directory()
            directory.mkdir(parents=True, exist_ok=True)
            image_path = directory / filename
            if not image_path.exists():
                with TemporaryDirectory(dir=directory) as temporary:
                    temporary_path = Path(temporary) / filename
                    temporary_path.write_bytes(payload)
                    temporary_path.replace(image_path)

        try:
            await asyncio.to_thread(write_cover)
        except OSError as err:
            raise ResourceTemporarilyUnavailable("The playlist cover could not be stored") from err
        playlist.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=filename, provider=self.instance_id)]
        )

    async def _remove_obsolete_covers(self, retained_covers: set[str]) -> None:
        """Keep current and previous generation artwork without unbounded daily growth."""

        def remove_covers() -> None:
            for image_path in self._cover_directory().glob("*.png"):
                if image_path.name not in retained_covers:
                    image_path.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(remove_covers)
        except OSError as err:
            self.logger.warning("Last.fm cover cleanup failed error_type=%s", type(err).__name__)

    def _cover_directory(self) -> Path:
        """Return the instance-isolated directory inside the MA image cache."""
        return (
            Path(self.mass.cache_path)
            / "collage_images"
            / "lastfm_recommendations_enhanced"
            / sha256(self.instance_id.encode()).hexdigest()
        )

    def _playlist(self, recipe: PlaylistSpec) -> Playlist:
        """Expose a read-only recipe without changing favorites, history or the library."""
        playlist = Playlist(
            item_id=recipe.item_id,
            provider=self.instance_id,
            name=recipe.name,
            provider_mappings={
                ProviderMapping(
                    item_id=recipe.item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            owner="Last.fm Recommendations Enhanced",
            is_editable=False,
            is_dynamic=True,
        )
        playlist.metadata.links = {MediaItemLink(type=LinkType.LASTFM, url="https://www.last.fm/")}
        return playlist


async def get_recommendation_user_entry(mass: MusicAssistant) -> ConfigEntry:
    """Offer enabled MA users for setup and the provider's configuration page."""
    users = await mass.webserver.auth.list_users()
    return ConfigEntry(
        key=CONF_RECOMMENDATION_USER_ID,
        type=ConfigEntryType.STRING,
        required=True,
        options=[
            ConfigValueOption(user.user_id, title=user.display_name or user.username)
            for user in users
            if user.enabled
        ],
    )


@contextmanager
def _recommendation_user_context(user: User | None) -> Iterator[None]:
    """Scope recommendation work to its MA user and restore any requesting-user context."""
    user_token = current_user.set(user)
    impersonation_token = impersonated_user.set(None)
    try:
        yield
    finally:
        impersonated_user.reset(impersonation_token)
        current_user.reset(user_token)
