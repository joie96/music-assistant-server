# ListenBrainz Recommendations

The provider resolves ListenBrainz discoveries against connected Music Assistant
music providers. It never adds discoveries to the library automatically. Empty
folders are omitted. Recommendations refresh every 24 hours; the new discovery
API requests use Music Assistant's cache for three hours and are rate limited.

The startup build is queued through the same scheduled task, so it records its
start time and replaces any pending automatic timer. Subsequent automatic runs
are scheduled 24 hours after that start, rather than on a separate startup timer.
Existing schedules are updated to 24 hours while preserving their enabled state.

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
| `user_exploration_weekly` | Latest personal Weekly Exploration playlist, regardless of age |
| `similarusers_jam_weekly` | Latest Weekly Jams playlist from the closest similar listener with a playable Jam |
| `sitewide_top_tracks_weekly` | Global `/1/stats/sitewide/recordings` charts |
| `sitewide_top_albums_weekly` | Global `/1/stats/sitewide/release-groups` charts |
| `sitewide_top_artists_weekly` | Global `/1/stats/sitewide/artists` charts |
| `sitewide_releases_weekly` | Global `/1/explore/fresh-releases/` discoveries |

The weekly charts combine `week` and `this_week`, merge matching identities and
rank by combined listen count. New releases are filtered by release date from
the previous Monday through the end of the current Sunday, including upcoming
releases within the current week. These boundaries use the current UTC date.
Exploration selects the newest playlist by creation time without an age cutoff.
The similar-users Jam sorts `/1/user/<username>/similar-users` by descending
similarity and tries each listener's latest `weekly-jams` playlist until one can
be resolved. September playlists remain eligible in October. A missing or
unresolvable Jam advances to the next listener; an API failure aborts the refresh
instead of replacing the previous snapshot with incomplete data.

New discovery folders contain up to 15 resolved items. New releases search at most
30 candidates, ordered newest first. Discovery searches run in batches of five.
Exploration and Jams resolve all playlist tracks in order and serve them through
the provider's playlist catalogue as `user_exploration_<playlist_mbid>` and
`similarusers_jam_<playlist_mbid>` respectively.

Clients fetch folder descriptors through `music/recommendations` and contents
through `music/recommendations/items`, using the provider instance and folder ID.
The previous folder IDs are not retained as aliases. Recreate the provider setup
when moving from the old configuration; there is no legacy configuration migration.