# Jellyfin discovery and library controls

Artist top tracks and top albums are ranked by `CommunityRating`, not listening
popularity. Rated items sort before unrated items, including a rating of zero.
Equal ratings sort by name and item ID. Rankings cover all matching artist items,
with paginated queries rather than a fixed library-size limit.

Recommendations offer recently added albums, highly rated albums and tracks,
most-played tracks, recently played tracks, and music genres. These are derived
from the connected user's accessible Jellyfin library, not an external catalog.
Genre browsing includes albums, tracks, and Jellyfin instant mixes.

Favorites change only Jellyfin's user-specific favorite markers. Audio playlists
can be created and edited when Jellyfin explicitly grants the user write access.
Servers without the playlist permission endpoint remain read-only. Removing a
playlist occurrence uses its `PlaylistItemId`, preserving other occurrences of
the same track. Mutations are not automatically retried.

Existing plain and synchronized lyrics are loaded only for full track details.
Jellyfin's tick timestamps are converted to Music Assistant's existing LRC field.

## aiojellyfin 0.14.1 gaps

The pinned SDK does not model `CommunityRating`, `Genres`, `PlaylistItemId`, or
playback counters and timestamps in `UserData`. Its decoder drops fields not in
its TypedDicts. The query builder also lacks artist-ID and sort filters, and the
client lacks similar-artist, genre, lyric, favorite-write, and playlist-write APIs.

`client.py` isolates these gaps using raw JSON and the existing authenticated
connection, shared HTTP session, SSL policy, and user identity. It does not modify
the installed SDK, create another session, or change Music Assistant's API models.
Read requests are throttled, discovery results use Music Assistant's cache, and
successful mutations invalidate only this provider's cache.
