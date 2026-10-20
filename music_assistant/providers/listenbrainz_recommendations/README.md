# ListenBrainz Recommendations

The provider resolves ListenBrainz discoveries against connected Music Assistant
music providers. It never adds discoveries to the library automatically. Empty
folders are omitted. Recommendations refresh every six hours; the new discovery
API requests use Music Assistant's cache for three hours and are rate limited.

Refreshes build a complete replacement privately and publish folders and playlist
tracks together only after every build step succeeds. Requests continue to read
the previous snapshot during a refresh. API failures, unavailable statistics
(HTTP 204), search failures, and cancellation leave that snapshot untouched.
Refreshes run serially. Cover filenames include a content hash, so constructing
a replacement cannot overwrite an image used by the published snapshot.

## Recommendation Folder IDs

| ID | Contents |
| --- | --- |
| `user_top_artists_monthly` | Personal top artists from the previous and current month |
| `user_top_tracks_monthly` | Personal top tracks from the previous and current month |
| `user_recent_artists` | Artists from the latest 100 listens, without a date filter |
| `user_recommendations` | LB-Radio `recs:<username>::unlistened`, without a weekly filter |
| `user_genre_playlists_monthly` | Existing genre-based LB-Radio playlists |
| `user_artist_playlists_monthly` | Existing artist-based LB-Radio playlists |
| `user_genre_artists_<genre>_monthly` | Existing genre-based artist discoveries |
| `user_similar_artists_<seed>_monthly` | Existing similar-artist discoveries |
| `user_exploration_weekly` | Personal Weekly Exploration playlists from both weeks |
| `sitewide_top_tracks_weekly` | Global `/1/stats/sitewide/recordings` charts |
| `sitewide_top_albums_weekly` | Global `/1/stats/sitewide/release-groups` charts |
| `sitewide_top_artists_weekly` | Global `/1/stats/sitewide/artists` charts |
| `sitewide_releases_weekly` | Global `/1/explore/fresh-releases/` discoveries |

The weekly charts combine `week` and `this_week`, merge matching identities and
rank by combined listen count. New releases are filtered by release date from
the previous Monday through the end of the current Sunday, including upcoming
releases within the current week. These boundaries use the current UTC date.
Exploration uses each playlist's creation date in its recorded timezone and
excludes playlists outside those two calendar weeks; older playlists are not
substituted when current playlists are unavailable. Weekly Jams is not included.

New discovery folders contain up to 15 resolved items. New releases search at most
30 candidates, ordered newest first. Discovery searches run in batches of five.
Exploration resolves all playlist tracks in order and serves them through the
provider's playlist catalogue as `user_exploration_<playlist_mbid>`.

Clients fetch folder descriptors through `music/recommendations` and contents
through `music/recommendations/items`, using the provider instance and folder ID.
The previous folder IDs are not retained as aliases. Recreate the provider setup
when moving from the old configuration; there is no legacy configuration migration.