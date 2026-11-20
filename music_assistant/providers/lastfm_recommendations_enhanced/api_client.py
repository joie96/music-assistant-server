"""Throttled Last.fm access using only the explicitly configured API key."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import ceil, isfinite
from time import perf_counter, time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import orjson
from aiohttp import ClientError, ClientResponseError
from music_assistant_models.errors import (
    AuthenticationFailed,
    InvalidDataError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.throttle_retry import ThrottlerManager, parse_retry_after

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aiohttp import ClientSession

    from music_assistant.controllers.cache import CacheController

ITEM_METHODS: dict[str, tuple[str, str, int]] = {
    "chart.getTopArtists": ("artists", "artist", 21600),
    "chart.getTopTracks": ("tracks", "track", 21600),
    "chart.getTopTags": ("tags", "tag", 21600),
    "geo.getTopArtists": ("topartists", "artist", 21600),
    "geo.getTopTracks": ("tracks", "track", 21600),
    "artist.getSimilar": ("similarartists", "artist", 604800),
    "track.getSimilar": ("similartracks", "track", 604800),
    "artist.getTopAlbums": ("topalbums", "album", 86400),
    "artist.getTopTracks": ("toptracks", "track", 86400),
    "artist.getTopTags": ("toptags", "tag", 604800),
    "tag.getTopTracks": ("tracks", "track", 86400),
    "tag.getTopArtists": ("topartists", "artist", 86400),
    "tag.getTopAlbums": ("albums", "album", 86400),
    "album.getInfo": ("album", "tracks", 86400),
}
CACHE_NAMESPACE = "lastfm_recommendations_enhanced"
CACHE_SLOTS = 128
MAX_CACHE_ENTRY_BYTES = 256 * 1024


@dataclass(frozen=True)
class LastFMItem:
    """An external discovery candidate, never a playable Last.fm stream."""

    name: str
    artist: str = ""
    url: str = ""
    mbid: str = ""
    tag_weight: float | None = None


class LastFMAPIClient:
    """Read-only client without bundled credentials or Last.fm account authentication."""

    BASE_URL = "https://ws.audioscrobbler.com/2.0/"
    throttler = ThrottlerManager(rate_limit=5, period=1)

    def __init__(
        self,
        http_session: ClientSession,
        api_key: str,
        logger: logging.Logger | None = None,
        *,
        cache: CacheController | None = None,
    ) -> None:
        """
        Create a client with the shared Music Assistant HTTP session.

        :param http_session: The server-owned session; this client never closes it.
        :param api_key: The mandatory user-supplied Last.fm API key.
        :param logger: The provider instance logger, including its configured log level.
        :param cache: The shared MA cache for public discovery responses.
        """
        if not api_key.strip():
            raise AuthenticationFailed("A personal Last.fm API key is required")
        self._http_session = http_session
        self._api_key = api_key.strip()
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self._cache = cache

    async def validate_api_key(self) -> None:
        """Reject invalid credentials with a minimal read-only Last.fm request."""
        self.logger.debug("Last.fm API key validation start method=chart.getTopArtists limit=1")
        await self._request("chart.getTopArtists", {"limit": 1}, use_cache=False)
        self.logger.debug("Last.fm API key validation successful")

    async def get_items(
        self, method: str, parameters: dict[str, str] | None = None, limit: int = 50
    ) -> list[LastFMItem]:
        """
        Return at most 50 candidates from one documented, read-only discovery endpoint.

        :param method: An explicitly supported Last.fm discovery method.
        :param parameters: Artist, track, album, tag or country; no credential overrides.
        :param limit: Maximum candidate count; capped at 50 and never paginated.
        """
        if method not in ITEM_METHODS or set(parameters or {}) - {
            "artist",
            "track",
            "album",
            "tag",
            "country",
        }:
            raise InvalidDataError("Unsupported Last.fm discovery request")
        if method == "chart.getTopTags" and parameters:
            raise InvalidDataError("Global Last.fm tags do not support country or artist filters")
        limit = min(max(limit, 1), 50)
        request_parameters: dict[str, str | int] = dict(parameters or {})
        if method != "artist.getTopTags":
            request_parameters["limit"] = limit
        data = await self._request(method, request_parameters)
        root, item_key, _ttl = ITEM_METHODS[method]
        rows = data[root].get(item_key, [])
        if method == "album.getInfo" and isinstance(rows, dict):
            rows = rows.get("track", [])
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise InvalidDataError("Last.fm returned an invalid item list")
        result = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            artist = row.get("artist", "")
            if not artist and method in {
                "album.getInfo",
                "artist.getTopAlbums",
                "artist.getTopTracks",
            }:
                artist = (parameters or {}).get("artist", "")
            if isinstance(artist, dict):
                artist = artist.get("name", "")
            url = row.get("url", "")
            mbid = row.get("mbid", "")
            result.append(
                LastFMItem(
                    row["name"],
                    artist if isinstance(artist, str) else "",
                    self._catalogue_url(url),
                    mbid if isinstance(mbid, str) else "",
                    tag_weight=(
                        self._tag_weight(row.get("count"))
                        if method == "artist.getTopTags"
                        else None
                    ),
                )
            )
        return result

    @staticmethod
    def _tag_weight(value: object) -> float | None:
        """Retain valid nonnegative Last.fm tag weights without assuming a fixed scale."""
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            return None
        try:
            weight = float(value)
        except ValueError, OverflowError:
            return None
        return weight if isfinite(weight) and weight >= 0 else None

    async def _request(
        self, method: str, parameters: dict[str, str | int], *, use_cache: bool = True
    ) -> dict[str, Any]:
        """Fetch one response through the shared rate gate and bounded MA cache."""
        fingerprint = sha256(orjson.dumps([method, sorted(parameters.items())])).hexdigest()
        slot = f"response_{int(fingerprint, 16) % CACHE_SLOTS}"
        if use_cache and self._cache is not None:
            cached = await self._cache.get(slot, provider=CACHE_NAMESPACE)
            if isinstance(cached, dict) and cached.get("request") == fingerprint:
                if isinstance(cached.get("data"), dict):
                    self.logger.debug("Last.fm cache hit method=%s", method)
                    return cached["data"]
        self.logger.debug("Last.fm cache miss method=%s", method)
        started = perf_counter()
        retry_after = 0
        ttl = ITEM_METHODS[method][2]
        try:
            self._check_cooldown()
            async with self.throttler.acquire():
                self._check_cooldown()
                async with self._http_session.get(
                    self.BASE_URL,
                    params={
                        **parameters,
                        "method": method,
                        "api_key": self._api_key,
                        "format": "json",
                    },
                ) as response:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    ttl = self._cache_ttl(response.headers, ttl)
                    self.logger.debug(
                        "Last.fm HTTP GET response method=%s status=%s",
                        method,
                        response.status,
                    )
                    response.raise_for_status()
                    data = await response.json()
        except ClientResponseError as err:
            self.logger.debug("Last.fm HTTP GET failed method=%s status=%s", method, err.status)
            if err.status == 429:
                self._raise_rate_limited(retry_after)
            raise ResourceTemporarilyUnavailable("Last.fm request failed") from None
        except (ClientError, TimeoutError) as err:
            self.logger.debug(
                "Last.fm HTTP GET failed method=%s error_type=%s",
                method,
                type(err).__name__,
            )
            raise ResourceTemporarilyUnavailable("Last.fm request failed") from None
        except ValueError:
            raise InvalidDataError("Last.fm returned invalid JSON") from None
        finally:
            self.logger.debug(
                "Last.fm HTTP GET finished method=%s elapsed=%.3fs (includes throttle wait)",
                method,
                perf_counter() - started,
            )

        self._validate_response(data, method, retry_after)
        if use_cache and self._cache is not None and ttl > 0:
            entry = {"request": fingerprint, "data": data}
            if len(orjson.dumps(entry)) <= MAX_CACHE_ENTRY_BYTES:
                await self._cache.set(slot, entry, expiration=ttl, provider=CACHE_NAMESPACE)
                self.logger.debug("Last.fm cache stored method=%s ttl=%ds", method, ttl)
            else:
                self.logger.debug("Last.fm cache skipped method=%s reason=size_limit", method)
        return data

    def _validate_response(self, data: Any, method: str, retry_after: int) -> None:
        """Reject API errors and malformed envelopes before they can enter the cache."""
        if not isinstance(data, dict):
            self.logger.debug("Last.fm API key validation failed reason=invalid_response")
            raise InvalidDataError("Last.fm returned an invalid response")
        if "error" in data:
            error_code = str(data["error"])
            if error_code == "29":
                self._raise_rate_limited(retry_after)
            if error_code in {"4", "10", "26"}:
                self.logger.debug(
                    "Last.fm API key validation failed reason=authentication error_code=%s",
                    error_code,
                )
                raise AuthenticationFailed("Last.fm rejected the configured API key")
            if error_code in {"11", "16"}:
                self.logger.debug(
                    "Last.fm API key validation failed reason=service_unavailable error_code=%s",
                    error_code,
                )
                raise ResourceTemporarilyUnavailable("Last.fm is temporarily unavailable")
            self.logger.debug("Last.fm API key validation failed reason=unknown_api_error")
            raise InvalidDataError("Last.fm could not validate the API key")
        if not isinstance(data.get(ITEM_METHODS[method][0]), dict):
            self.logger.debug("Last.fm API key validation failed reason=invalid_chart_response")
            raise InvalidDataError("Last.fm returned an invalid chart response")
        root, item_key, _ttl = ITEM_METHODS[method]
        if not isinstance(data[root].get(item_key, []), list | dict):
            raise InvalidDataError("Last.fm returned an invalid item list")

    @staticmethod
    def _catalogue_url(value: object) -> str:
        """Only expose credential-free Last.fm catalogue links over HTTPS."""
        if not isinstance(value, str):
            return ""
        try:
            parsed = urlparse(value)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"last.fm", "www.last.fm"}
                or parsed.username
                or parsed.password
                or not parsed.path.startswith("/music/")
            ):
                return ""
        except ValueError:
            return ""
        return parsed._replace(scheme="https", netloc="www.last.fm", query="", fragment="").geturl()

    @staticmethod
    def _cache_ttl(headers: Mapping[str, str], default: int) -> int:
        """Only shorten the method TTL; never cache responses that prohibit reuse."""
        control = headers.get("Cache-Control", "")
        if not isinstance(control, str):
            return 0
        directives = {part.strip().lower().split("=", 1)[0] for part in control.split(",")}
        if directives & {"no-store", "no-cache", "private"}:
            return 0
        try:
            age = max(0, int(headers.get("Age", "0")))
            for part in control.lower().split(","):
                key, separator, value = part.strip().partition("=")
                if key == "max-age" and separator:
                    return max(0, min(default, int(value.strip('"')) - age))
            expires = headers.get("Expires")
            if isinstance(expires, str):
                return max(
                    0, min(default, int(parsedate_to_datetime(expires).timestamp() - time()))
                )
        except ValueError, TypeError, OverflowError:
            return 0
        return default

    def _raise_rate_limited(self, retry_after: int) -> None:
        """Stop all Enhanced instances for at least the server's requested wait."""
        cooldown = max(retry_after, 60)
        self.throttler.set_cooldown(cooldown)
        self.logger.debug("Last.fm rate limited cooldown=%ds", cooldown)
        raise RateLimited("Last.fm rate limit reached", backoff_time=cooldown) from None

    def _check_cooldown(self) -> None:
        """Reject an outbound call while the shared server-imposed cooldown remains active."""
        if remaining := self.throttler.cooldown_remaining:
            raise RateLimited("Last.fm rate limit cooldown is active", backoff_time=ceil(remaining))
