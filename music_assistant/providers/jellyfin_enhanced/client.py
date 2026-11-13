"""Access Jellyfin endpoints and fields not exposed by aiojellyfin 0.14.1."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Never, cast

from aiohttp import ClientConnectionError, ClientResponseError
from aiojellyfin import NotFound
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
    RateLimited,
)

from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)

if TYPE_CHECKING:
    from aiojellyfin import Connection
    from aiojellyfin.const import ItemFields


class JellyfinClient:
    """Reuse the authenticated aiojellyfin session without decoding away extra fields."""

    def __init__(self, connection: Connection) -> None:
        """Wrap an authenticated connection; the provider retains session ownership."""
        self.connection = connection
        self.logger = logging.getLogger(__name__)
        self.throttler = ThrottlerManager(rate_limit=10, period=1)

    @throttle_with_retries
    async def get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Fetch raw JSON with the connected user's access restrictions."""
        try:
            return await self.connection._session.get_json(
                path, {**params, "userId": self.connection._user_id}
            )
        except NotFound as err:
            raise MediaNotFoundError("Jellyfin resource not found") from err
        except ClientResponseError as err:
            if err.status == 429:
                retry_after = err.headers.get("Retry-After") if err.headers else None
                raise RateLimited(
                    "Jellyfin request rate exceeded", backoff_time=parse_retry_after(retry_after)
                ) from err
            self._raise_http_error(err)
        except (ClientConnectionError, TimeoutError) as err:
            raise ProviderUnavailableError("Unable to reach Jellyfin") from err

    async def request(self, method: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Send a throttled mutation without retrying potentially successful writes."""
        config = self.connection._session_config
        try:
            async with (
                self.throttler.acquire(),
                config.session.request(
                    method,
                    f"{self.connection.base_url}{path}",
                    params={**params, "userId": self.connection._user_id},
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": config.user_agent,
                        "Authorization": config.authentication_header(
                            self.connection._access_token
                        ),
                    },
                    ssl=config.verify_ssl,
                ) as response,
            ):
                response.raise_for_status()
                if response.status == 204 or response.content_type != "application/json":
                    return {}
                return cast("dict[str, Any]", await response.json())
        except ClientResponseError as err:
            self._raise_http_error(err)
        except (ClientConnectionError, TimeoutError) as err:
            raise ProviderUnavailableError("Unable to reach Jellyfin") from err

    async def get_item(
        self, item_id: str, item_type: str, fields: list[ItemFields]
    ) -> dict[str, Any]:
        """Fetch full item metadata while retaining fields missing from the SDK."""
        item = await self.get_json(
            f"/Users/{self.connection._user_id}/Items/{item_id}",
            {"fields": ",".join(field.value for field in fields), "enableUserData": "true"},
        )
        if item.get("Type") != item_type:
            raise MediaNotFoundError(f"Jellyfin item {item_id} is not a {item_type}")
        return item

    async def set_favorite(self, item_id: str, favorite: bool) -> None:
        """Change only the user's favorite marker, never library membership."""
        method = "POST" if favorite else "DELETE"
        try:
            await self.request(method, f"/UserFavoriteItems/{item_id}", {})
        except MediaNotFoundError:
            await self.request(
                method, f"/Users/{self.connection._user_id}/FavoriteItems/{item_id}", {}
            )

    async def can_edit_playlist(self, playlist_id: str) -> bool:
        """Read playlist write permission; legacy servers without this API stay read-only."""
        try:
            permission = await self.get_json(
                f"/Playlists/{playlist_id}/Users/{self.connection._user_id}", {}
            )
        except MediaNotFoundError, ProviderPermissionDenied:
            return False
        return permission.get("CanEdit") is True

    async def get_lyrics(self, track_id: str) -> dict[str, Any] | None:
        """Return existing lyrics, or None when Jellyfin has no lyric resource."""
        try:
            return await self.get_json(f"/Audio/{track_id}/Lyrics", {})
        except MediaNotFoundError:
            return None

    async def get_artist_items(
        self, artist_id: str, item_type: str, fields: list[ItemFields]
    ) -> list[dict[str, Any]]:
        """Fetch every track or album associated with an artist, preserving ratings."""
        return await self.get_items(item_type, fields, {"artistIds": artist_id}, limit=None)

    async def get_items(
        self,
        item_type: str,
        fields: list[ItemFields],
        params: dict[str, str],
        limit: int | None = 50,
        path: str = "/Items",
    ) -> list[dict[str, Any]]:
        """Fetch a bounded listing, or paginate the full listing when limit is None."""
        items: list[dict[str, Any]] = []
        while True:
            response = await self.get_json(
                path,
                {
                    "includeItemTypes": item_type,
                    "recursive": "true",
                    "enableUserData": "true",
                    "fields": ",".join(field.value for field in fields),
                    **(
                        {"sortBy": "SortName", "sortOrder": "Ascending"}
                        if path in ("/Items", "/Genres")
                        else {}
                    ),
                    **params,
                    "startIndex": str(len(items)),
                    "limit": str(min(100, limit - len(items)) if limit is not None else 100),
                },
            )
            page = response["Items"]
            items.extend(page)
            if (
                not page
                or len(items) >= response["TotalRecordCount"]
                or (limit is not None and len(items) >= limit)
            ):
                return items

    def _raise_http_error(self, error: ClientResponseError) -> Never:
        """Map HTTP failures once at the API boundary without exposing credentials."""
        if error.status == 401:
            raise LoginFailed("Jellyfin rejected the authentication token") from error
        if error.status == 403:
            raise ProviderPermissionDenied("Jellyfin denied access to this operation") from error
        if error.status == 404:
            raise MediaNotFoundError("Jellyfin resource not found") from error
        raise ProviderUnavailableError(f"Jellyfin request failed (HTTP {error.status})") from error


def rating_sort_key(item: dict[str, Any]) -> tuple[bool, float, str, str]:
    """Rank rated items first, including zero, and break ties deterministically."""
    rating = item.get("CommunityRating")
    return (
        rating is None,
        -float(rating) if rating is not None else 0.0,
        (item.get("SortName") or item["Name"]).casefold(),
        item["Id"],
    )
