"""Async API client for ListenBrainz statistics endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.listenbrainz_recommendations.constants import (
    CONF_AUTH_TOKEN,
    DEFAULT_API_BASE_URL,
)

if TYPE_CHECKING:
    from music_assistant.providers.listenbrainz_recommendations import (
        ListenBrainzRecommendationsProvider,
    )


class ListenBrainzAPIClient:
    """Lightweight client for ListenBrainz top statistics endpoints."""

    def __init__(self, provider: ListenBrainzRecommendationsProvider) -> None:
        """Initialize API client."""
        self.provider = provider
        base_url_value = provider.config.get_value("api_base_url")
        self.base_url = (base_url_value or DEFAULT_API_BASE_URL).rstrip("/")

    async def get_user_top_artists(
        self,
        username: str,
        stats_ranges: list[str],
        count: int,
    ) -> list[dict[str, Any]]:
        """Get user top artists aggregated across the given stats ranges."""
        quoted_username = quote(username, safe="")
        aggregated_by_key: dict[str, dict[str, Any]] = {}

        for stats_range in stats_ranges:
            data = await self._request(
                path=f"/1/stats/user/{quoted_username}/artists",
                params={"range": stats_range, "count": count, "offset": 0},
            )
            payload = data.get("payload", {})
            artists = payload.get("artists", [])
            if not isinstance(artists, list):
                continue

            for artist in artists:
                if not isinstance(artist, dict):
                    continue
                artist_mbid = artist.get("artist_mbid")
                artist_name = artist.get("artist_name")
                listen_count = artist.get("listen_count", 0)
                if not isinstance(listen_count, int):
                    continue
                if isinstance(artist_mbid, str) and artist_mbid:
                    key = f"mbid:{artist_mbid}"
                elif isinstance(artist_name, str) and artist_name.strip():
                    key = f"name:{artist_name.casefold().strip()}"
                else:
                    continue

                if key not in aggregated_by_key:
                    aggregated_by_key[key] = {
                        "artist_mbid": artist_mbid,
                        "artist_name": artist_name,
                        "listen_count": listen_count,
                    }
                    continue

                aggregated_by_key[key]["listen_count"] += listen_count

        return sorted(
            aggregated_by_key.values(),
            key=lambda item: item.get("listen_count", 0),
            reverse=True,
        )

    async def get_user_top_tracks(
        self,
        username: str,
        stats_ranges: list[str],
        count: int,
    ) -> list[dict[str, Any]]:
        """Get user top tracks aggregated across the given stats ranges."""
        quoted_username = quote(username, safe="")
        aggregated_by_key: dict[str, dict[str, Any]] = {}

        for stats_range in stats_ranges:
            data = await self._request(
                path=f"/1/stats/user/{quoted_username}/recordings",
                params={"range": stats_range, "count": count, "offset": 0},
            )
            payload = data.get("payload", {})
            tracks = payload.get("recordings", [])
            if not isinstance(tracks, list):
                continue

            for track in tracks:
                if not isinstance(track, dict):
                    continue

                recording_mbid = track.get("recording_mbid")
                track_name = track.get("track_name")
                if not isinstance(track_name, str) or not track_name.strip():
                    continue
                track_name = track_name.strip()
                artist_name = track.get("artist_name")
                if not isinstance(artist_name, str) or not artist_name.strip():
                    artists = track.get("artists")
                    if isinstance(artists, list):
                        artist_credit_names: list[str] = []
                        for artist in artists:
                            if not isinstance(artist, dict):
                                continue
                            artist_credit_name = artist.get("artist_credit_name")
                            if isinstance(artist_credit_name, str) and artist_credit_name.strip():
                                artist_credit_names.append(artist_credit_name.strip())
                        if artist_credit_names:
                            artist_name = " ".join(artist_credit_names)

                if isinstance(artist_name, str):
                    artist_name = artist_name.strip()
                listen_count = track.get("listen_count", 0)
                if not isinstance(listen_count, int):
                    continue

                if isinstance(recording_mbid, str) and recording_mbid.strip():
                    recording_mbid = recording_mbid.strip()
                    key = f"mbid:{recording_mbid}"
                else:
                    normalized_artist = ""
                    if isinstance(artist_name, str) and artist_name.strip():
                        normalized_artist = artist_name.casefold().strip()
                    key = f"name:{normalized_artist}::{track_name.casefold()}"

                if key not in aggregated_by_key:
                    aggregated_by_key[key] = {
                        "recording_mbid": recording_mbid,
                        "track_name": track_name,
                        "artist_name": artist_name,
                        "listen_count": listen_count,
                    }
                    continue

                aggregated_by_key[key]["listen_count"] += listen_count

        return sorted(
            aggregated_by_key.values(),
            key=lambda item: item.get("listen_count", 0),
            reverse=True,
        )

    async def get_user_listens(self, username: str, count: int = 100) -> list[dict[str, Any]]:
        """Get the most recent listens for a user in API-provided order."""
        quoted_username = quote(username, safe="")
        data = await self._request(
            path=f"/1/user/{quoted_username}/listens",
            params={"count": count},
        )
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            return []
        listens = payload.get("listens", [])
        if not isinstance(listens, list):
            return []
        return [listen for listen in listens if isinstance(listen, dict)]

    async def get_user_genre_activity(
        self,
        username: str,
        stats_ranges: list[str],
    ) -> list[dict[str, Any]]:
        """Get user genre activity sorted by listen_count (not aggregated by genre)."""
        quoted_username = quote(username, safe="")
        aggregated_by_key: dict[tuple[str, int], dict[str, Any]] = {}

        for stats_range in stats_ranges:
            data = await self._request(
                path=f"/1/stats/user/{quoted_username}/genre-activity",
                params={"range": stats_range},
            )
            payload = data.get("payload", {})
            if not isinstance(payload, dict):
                continue
            genre_activity = payload.get("genre_activity", [])
            if not isinstance(genre_activity, list):
                continue

            for entry in genre_activity:
                if not isinstance(entry, dict):
                    continue
                genre = entry.get("genre")
                hour = entry.get("hour")
                listen_count = entry.get("listen_count", 0)
                if not isinstance(genre, str) or not genre.strip():
                    continue
                if not isinstance(hour, int):
                    continue
                if not isinstance(listen_count, int):
                    continue

                normalized_genre = genre.strip().casefold()
                key = (normalized_genre, hour)
                if key not in aggregated_by_key:
                    aggregated_by_key[key] = {
                        "genre": normalized_genre,
                        "hour": hour,
                        "listen_count": listen_count,
                    }
                    continue

                aggregated_by_key[key]["listen_count"] += listen_count

        return sorted(
            aggregated_by_key.values(),
            key=lambda item: item.get("listen_count", 0),
            reverse=True,
        )

    async def get_explore_lb_radio(self, prompt: str, mode: str) -> list[dict[str, Any]]:
        """Generate LB-Radio tracks using a prompt and mode."""
        try:
            data = await self._request(
                path="/1/explore/lb-radio",
                params={"prompt": prompt, "mode": mode},
            )
        except MusicAssistantError as err:
            self.provider.logger.debug("LB-Radio prompt yielded no results, skipping: %s", err)
            return []
        payload = data.get("payload", data)
        jspf = payload.get("jspf", {})
        playlist = jspf.get("playlist", {})
        return playlist.get("track", [])

    async def get_lb_radio_artist(
        self,
        seed_artist_mbid: str,
        mode: str,
        max_similar_artists: int,
        max_recordings_per_artist: int,
        pop_begin: int,
        pop_end: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get recordings for use in LB radio with the given seed artist. The endpoint returns a dict of all the similar artists, including the seed artist."""
        try:
            data = await self._request(
                path=f"/1/lb-radio/artist/{quote(seed_artist_mbid, safe='')}",
                params={
                    "mode": mode,
                    "max_similar_artists": max_similar_artists,
                    "max_recordings_per_artist": max_recordings_per_artist,
                    "pop_begin": pop_begin,
                    "pop_end": pop_end,
                },
            )
        except MusicAssistantError as err:
            self.provider.logger.debug("LB-Radio artist request yielded no results, skipping: %s", err)
            return {}

        payload = data.get("payload", data)
        if not isinstance(payload, dict):
            return {}

        result: dict[str, list[dict[str, Any]]] = {}
        for key, values in payload.items():
            if not isinstance(values, list):
                continue
            parsed_values = [item for item in values if isinstance(item, dict)]
            if parsed_values:
                result[str(key)] = parsed_values
        return result

    async def get_lb_radio_tags(
        self,
        tags: list[str],
        pop_begin: int,
        pop_end: int,
        count: int = 25,
        operator: str | None = None,
    ) -> list[Any]:
        """Get LB-Radio recordings for one or more MusicBrainz tags."""
        cleaned_tags = [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()]
        if not cleaned_tags:
            return []

        params: dict[str, Any] = {
            "tag": cleaned_tags,
            "pop_begin": pop_begin,
            "pop_end": pop_end,
            "count": count,
        }
        if isinstance(operator, str) and operator.strip():
            params["operator"] = operator.strip().upper()

        try:
            data = await self._request(path="/1/lb-radio/tags", params=params)
        except MusicAssistantError as err:
            self.provider.logger.debug("LB-Radio tags yielded no results, skipping: %s", err)
            return []

        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []

        payload = data.get("payload", data)
        if not isinstance(payload, list):
            return []
        return payload

    async def get_recording_metadata(
        self,
        recording_mbids: list[str],
        inc: str = "",
    ) -> dict[str, Any]:
        """Get recording metadata for a list of recording MBIDs."""
        cleaned_mbids = [mbid.strip() for mbid in recording_mbids if isinstance(mbid, str) and mbid.strip()]
        if not cleaned_mbids:
            return {}

        data = await self._request(
            path="/1/metadata/recording/",
            params={
                "recording_mbids": ",".join(cleaned_mbids),
                "inc": inc,
            },
        )
        payload = data.get("payload", data)
        if not isinstance(payload, dict):
            return {}

        return {str(key): value for key, value in payload.items()}

    async def get_artist_metadata(
        self,
        artist_mbids: list[str],
        inc: str = "artist tag release",
    ) -> list[Any]:
        """Get artist metadata for a list of artist MBIDs."""
        cleaned_mbids = [mbid.strip() for mbid in artist_mbids if isinstance(mbid, str) and mbid.strip()]
        if not cleaned_mbids:
            return []

        data = await self._request(
            path="/1/metadata/artist/",
            params={
                "artist_mbids": ",".join(cleaned_mbids),
                "inc": inc,
            },
        )
        payload = data.get("payload", data)
        if not isinstance(payload, list):
            return []

        return payload

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a GET request and return JSON body or empty dict."""
        url = f"{self.base_url}{path}"
        auth_token = self.provider.config.get_value(CONF_AUTH_TOKEN)
        headers = (
            {"Authorization": f"Token {auth_token}"}
            if isinstance(auth_token, str) and auth_token.strip()
            else {}
        )
        try:
            async with self.provider.mass.http_session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 204:
                    return {}
                if response.status >= 400:
                    body = await response.text()
                    msg = f"ListenBrainz request failed ({response.status}) for {path}: {body}"
                    raise MusicAssistantError(msg)
                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    body = await response.text()
                    msg = f"ListenBrainz returned invalid JSON for {path}: {body[:300]}"
                    raise MusicAssistantError(msg) from err
        except MusicAssistantError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"ListenBrainz transport error for {path}: {type(err).__name__}: {err}"
            raise MusicAssistantError(msg) from err
