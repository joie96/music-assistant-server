"""
Helper module for parsing the Youtube Music API.

This helpers file is an async wrapper around the excellent ytmusicapi package.
While the ytmusicapi package does an excellent job at parsing the Youtube Music results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Youtube Music provider logic.
"""

import asyncio
from collections.abc import Callable
from typing import Any, Literal

import ytmusicapi
from music_assistant_models.errors import UnsupportedFeaturedException
from ytmusicapi.exceptions import YTMusicError

from music_assistant.providers.ytmusic_enhanced.constants import YTMRecommendationIcons

# subset of ytmusicapi's accepted search filters that we use
YTMSearchFilter = Literal["artists", "albums", "songs", "playlists", "podcasts"]


async def get_artist(prov_artist_id: str, language: str = "en") -> dict[str, Any]:
    """Async wrapper around the ytmusicapi get_artist function."""

    def _get_artist() -> dict[str, Any]:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        try:
            artist = ytm.get_artist(channelId=prov_artist_id)
            # ChannelId can sometimes be different and original ID is not part of the response
            artist["channelId"] = prov_artist_id
        except KeyError:
            try:
                user = ytm.get_user(channelId=prov_artist_id)
                artist = {"channelId": prov_artist_id, "name": user["name"]}
            except KeyError:
                artist = {"channelId": prov_artist_id, "name": "Unknown"}
        return artist

    return await _run_ytmusic(_get_artist)


async def get_artist_albums(
    channel_id: str,
    params: str,
    language: str = "en",
) -> list[dict[str, Any]]:
    """
    Async wrapper around the ytmusicapi get_artist_albums function.

    :param channel_id: The browseId of an artist section (albums/singles/shows).
    :param params: The params value from that same section, paired with channel_id.
    :param language: Response language.
    """

    def _get_artist_albums() -> list[dict[str, Any]]:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        return ytm.get_artist_albums(channelId=channel_id, params=params, limit=None)

    return await _run_ytmusic(_get_artist_albums)


async def get_album(prov_album_id: str, language: str = "en") -> dict[str, Any]:
    """Async wrapper around the ytmusicapi get_album function."""

    def _get_album() -> dict[str, Any]:
        if prov_album_id.startswith("FEmusic_library_privately_owned_release"):
            raise UnsupportedFeaturedException("Private uploads require a signed-in provider")
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        album = ytm.get_album(browseId=prov_album_id)

        if album.get("audioPlaylistId"):
            # Track id's from album tracks do not match with actual album tracks. E.g. a track
            # points to the videoId of the original version, while we want the album version
            try:
                album_playlist = ytm.get_playlist(playlistId=album["audioPlaylistId"], limit=None)
            except YTMusicError:
                return album

            # Do some basic checks
            if len(album_playlist.get("tracks", [])) != len(album.get("tracks", [])):
                return album
            # Move the correct track info to the album tracks
            playlist_tracks_by_title = {t.get("title"): t for t in album_playlist.get("tracks", [])}
            for album_track in album.get("tracks", []):
                if playlist_track := playlist_tracks_by_title.get(album_track.get("title")):
                    album_track["videoId"] = playlist_track["videoId"]
                    album_track["isAvailable"] = playlist_track.get("isAvailable", True)
        return album

    return await _run_ytmusic(_get_album)


async def get_playlist(
    prov_playlist_id: str,
    language: str = "en",
    limit: int | None = None,
) -> dict[str, Any]:
    """Async wrapper around the ytmusicapi get_playlist function."""

    def _get_playlist() -> dict[str, Any]:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        playlist = ytm.get_playlist(playlistId=prov_playlist_id, limit=limit)
        # Fix missing playlist id in some edge cases
        playlist["id"] = prov_playlist_id if not playlist.get("id") else playlist["id"]
        return playlist

    return await _run_ytmusic(_get_playlist)


async def get_track(prov_track_id: str, language: str = "en") -> dict[str, Any] | None:
    """Async wrapper around the ytmusicapi get_playlist function."""

    def _get_song() -> dict[str, Any] | None:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        track_obj = ytm.get_song(videoId=prov_track_id)
        track: dict[str, Any] = {}
        if "videoDetails" not in track_obj:
            # video that no longer exists
            return None
        track["videoId"] = track_obj["videoDetails"]["videoId"]
        track["title"] = track_obj["videoDetails"]["title"]
        track["artists"] = [
            {
                "channelId": track_obj["videoDetails"]["channelId"],
                "name": track_obj["videoDetails"]["author"],
            }
        ]
        track["duration"] = track_obj["videoDetails"]["lengthSeconds"]
        track["thumbnails"] = track_obj["microformat"]["microformatDataRenderer"]["thumbnail"][
            "thumbnails"
        ]
        if track_thumbs := track_obj["videoDetails"].get("thumbnail", {}).get("thumbnails"):
            track["thumbnails"] = track.get("thumbnails", []) + track_thumbs
        track["isAvailable"] = track_obj["playabilityStatus"]["status"] == "OK"
        return track

    return await _run_ytmusic(_get_song)


async def get_podcast(prov_podcast_id: str, language: str = "en") -> dict[str, Any]:
    """Async wrapper around the get_podcast function."""

    def _get_podcast() -> dict[str, Any]:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        podcast_obj = ytm.get_podcast(playlistId=prov_podcast_id)
        if "podcastId" not in podcast_obj:
            podcast_obj["podcastId"] = prov_podcast_id
        return podcast_obj

    return await _run_ytmusic(_get_podcast)


async def get_podcast_episode(prov_episode_id: str, language: str = "en") -> dict[str, Any]:
    """Async wrapper around the podcast episode function."""

    def _get_podcast_episode() -> dict[str, Any]:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        episode = ytm.get_episode(videoId=prov_episode_id)
        if "videoId" not in episode:
            episode["videoId"] = prov_episode_id
        return episode

    return await _run_ytmusic(_get_podcast_episode)


async def get_song_radio_tracks(prov_item_id: str, limit: int = 25) -> dict[str, Any]:
    """Async wrapper around the ytmusicapi radio function."""

    def _get_song_radio_tracks() -> dict[str, Any]:
        ytm = ytmusicapi.YTMusic(auth=None)
        playlist_id = f"RDAMVM{prov_item_id}"
        result = ytm.get_watch_playlist(
            videoId=prov_item_id, playlistId=playlist_id, limit=limit, radio=True
        )
        # Replace inconsistensies for easier parsing
        tracks = result.get("tracks")
        if isinstance(tracks, list):
            for track in tracks:
                if track.get("thumbnail"):
                    track["thumbnails"] = track["thumbnail"]
                    del track["thumbnail"]
                if track.get("length"):
                    track["duration"] = get_sec(track["length"])
        return result

    return await _run_ytmusic(_get_song_radio_tracks)


async def search(
    query: str,
    ytm_filter: YTMSearchFilter | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Async wrapper around the ytmusicapi search function."""

    def _search() -> list[dict[str, Any]]:
        # Always search in English: ytmusicapi (1.12.3) matches the result shelf title,
        # which YouTube returns translated, against the English filter name, so a filtered
        # search silently returns nothing in most other languages. Revisit once ytmusicapi
        # compares against the translated title.
        ytm = ytmusicapi.YTMusic(language="en")
        results = ytm.search(query=query, filter=ytm_filter, limit=limit)
        # Sync result properties with uniformal objects
        for result in results:
            if result["resultType"] == "artist":
                if "artists" in result and len(result["artists"]) > 0:
                    result["id"] = result["artists"][0]["id"]
                    result["name"] = result["artists"][0]["name"]
                    del result["artists"]
                else:
                    result["id"] = result["browseId"]
                    result["name"] = result["artist"]
                    del result["browseId"]
                    del result["artist"]
            elif result["resultType"] == "playlist":
                if "playlistId" in result:
                    result["id"] = result["playlistId"]
                    del result["playlistId"]
                elif "browseId" in result:
                    result["id"] = result["browseId"]
                    del result["browseId"]
        return results[:limit]

    return await _run_ytmusic(_search)


def get_sec(time_str: str) -> int:
    """Get seconds from time."""
    parts = time_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


async def get_home(language: str = "en", limit: int = 3) -> list[dict[str, Any]]:
    """Get the recommendations from the home page."""

    def _get_home() -> list[dict[str, Any]]:
        ytm = ytmusicapi.YTMusic(auth=None, language=language)
        return ytm.get_home(limit=limit)

    return await _run_ytmusic(_get_home)


def determine_recommendation_icon(name: str) -> str:
    """Determine the icon for a recommendation based on its name."""
    query = name.lower()

    if "new" in query:
        return YTMRecommendationIcons.NEW_RELEASES
    if "recommended" in query:
        return YTMRecommendationIcons.RECOMMENDED
    return YTMRecommendationIcons.DEFAULT


async def _run_ytmusic[T](func: Callable[[], T]) -> T:
    """Run blocking ytmusicapi calls without blocking the server's event loop."""
    return await asyncio.to_thread(func)
