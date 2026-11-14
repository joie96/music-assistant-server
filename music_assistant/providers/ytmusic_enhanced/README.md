# YouTube Music Enhanced

An independent variant of the bundled YouTube Music provider. Public search,
artists, albums, playlists, podcasts and related tracks still use **ytmusicapi**
and Music Assistant's metadata cache. Audio extraction and downloads use
**yt-dlp**, with a persistent file cache inspired by
[music-assistant-yt-dlp](https://github.com/joie96/music-assistant-yt-dlp).

## Setup

Add **YouTube Music Enhanced** in Music Assistant. No username, browser cookies,
Google Cloud API key or PO-token server is configured or required at setup.
yt-dlp chooses its current anonymous YouTube player clients. The provider does
not replace an existing official YouTube Music instance.

Only publicly accessible content is available. Personal library synchronization,
private playlists, private uploads, account mixes and YouTube likes/playlist
editing are not supported. Home recommendations are anonymous, not personalized.
You can still manage public items in Music Assistant's own library.

Anonymous playback is not guaranteed for every video or network: YouTube may
require sign-in, age verification or additional verification. Such restrictions
are reported as playback errors, not bypassed using account credentials.

## Cache Settings

Configure the provider's options after adding it:

| Setting | Default | Meaning |
| --- | --- | --- |
| Enable audio file cache | On | Download complete audio once and reuse it. |
| Audio cache directory | `ytmusic_enhanced` under MA's cache directory | Writable server-side root; each instance uses its own subdirectory. |
| Maximum audio cache size (MB) | 1024 | Retained audio limit per instance in MiB; 0 means unlimited. |

Use a persistent writable volume for a cache that should survive container
recreation. Changing the directory does not move or delete files in the old
directory. Disabling the cache stops using it but does not delete existing files.
An unusable directory at initialization produces a setup error.

Completed downloads retain their original container without transcoding. Format
metadata is stored beside each file. Entries are validated before reuse, and
the least recently used files are evicted when necessary. Incomplete downloads
are never published as cache hits. The limit covers retained audio bytes, not
sidecar metadata or temporary downloads; allow additional free disk space.

## Streaming

On a cache miss, yt-dlp downloads the selected audio into a temporary directory
using 10 MiB HTTP chunks. Only complete files become visible in the cache.
Playback then uses Music Assistant's native local-file decoder with seeking.
First playback waits for the full download unless already prefetched; subsequent playback reuses it,
including after a provider restart, without resolving another audio URL.

While playing, the provider scans up to three upcoming queue entries and caches
tracks belonging to the same provider instance in the background. Existing cache
entries are reused; live streams and other providers are skipped. Prefetch errors
do not interrupt playback, and disabling the cache also disables prefetch.

With caching disabled, a cache error or audio larger than the configured limit,
the provider returns the selected HTTP/HLS source to Music Assistant's native
decoder, including yt-dlp's required HTTP headers. Live streams are never cached.

Audio selection prefers format IDs 141, 251, 140, 171, 250, 249 and 139, in that
order, prioritizing higher-quality audio rather than every Opus format first.
This is a codec/bitrate quality heuristic, not an exact cross-codec quality ranking.
It then falls back to yt-dlp's `m4a/bestaudio/best` selector. Failed extractions
are retried up to three total attempts, waiting one and then two seconds.

The provider honors yt-dlp's `available_at` timestamp, waiting at most 45 seconds.
Disk I/O and yt-dlp run outside the event loop. Parallel downloads for the same
instance are serialized and reuse completed files; unload and cancellation wait
for outstanding writers. yt-dlp remains dynamically installed as in the original
provider, and no PO-token plugin is installed.

## Logging

The provider's `DEBUG` log shows cache configuration, cache hits/misses, selected
audio formats, download completion, size-limit decisions, invalid cache entries,
LRU evictions and queue prefetch activity. `VERBOSE` additionally includes
yt-dlp's internal debug output; per-chunk download progress is disabled.

Extraction retries and cache failures that fall back to streaming are logged as
warnings. Background prefetch failures stay at debug level because they do not
interrupt playback. Terminal playback failures use MA's normal error handling.
yt-dlp output and logged extraction/cache error text use MA's shared sanitization
helper to redact credentials and URL query strings before logging.