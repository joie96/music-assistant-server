# Mashin Stream player provider

Connects Mashin clients to Music Assistant (MA) through token-free LAN Control and Audio
WebSockets. Each device has its own MA player and queue, with a stable identity across
reconnects and server restarts. One provider instance serves multiple independent devices.

This provider is under development and is not yet production-ready. The
[V3 contract](doc/contract.md) defines the protocol and required acceptance tests.

## Requirements

- A complete MA Linux runtime with Python 3.14+, FFmpeg 7+ and libopus.
- A client implementing the Mashin Stream V3 protocol.
- Trusted network access to MA's main webserver. Both `ws://` over HTTP and `wss://`
  over HTTPS are supported, without an MA token or user account.

## Setup

1. Deploy this directory as `music_assistant/providers/mashin_stream` in the MA installation
   and restart MA for discovery. No core changes or external plugin installer are needed.
2. Add a Mashin Stream provider instance through MA's setup flow.
3. Connect the client to `GET /mashin-stream/v3/control` and
  `GET /mashin-stream/v3/audio` on MA's main webserver using WebSocket upgrades.
  Use `ws://<ma-host>:<port>/mashin-stream/v3/control` for HTTP, or
  `wss://<ma-host>:<port>/mashin-stream/v3/control` for HTTPS, and the matching
  `/mashin-stream/v3/audio` route. No Authorization header is required.
4. Send a Control `hello` with a stable `device_id`, client name and explicit
  `audio_profile`. Bind Audio using the one-time binding returned in `welcome`.

Device IDs identify players but do not authenticate devices. Anyone who can reach these
routes can register a player or claim an existing device ID. Keep them on a trusted LAN;
do not expose them through port forwarding or a public reverse proxy. WSS encrypts the
transport but does not add access control. Do not put Audio bindings in URLs.

WSS requires TLS on the webserver or a reverse proxy that forwards WebSocket upgrades.
No provider-specific proxy IP or forwarded-protocol configuration is needed. The provider
does not create certificates or enable HTTPS on MA itself.

The provider domain is `mashin_stream`; the client routes are
`/mashin-stream/v3/control` and `/mashin-stream/v3/audio`. Server and clients must
be updated together; the old routes are not registered as aliases.
Existing installations of the unreleased `mashin`
provider must remove it and configure Mashin Stream again. Provider configuration and
device bindings are not migrated automatically.

### Provider settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `max_registered_devices` | 32 | Provider-wide device registration limit. |
| `max_websocket_connections` | 128 | Simultaneous sockets, including handshakes and retiring connections. |

Lowering a device quota does not prevent already registered devices from reconnecting.

## Playback and reconnects

- Clients choose `flac`, `opus_96k`, `opus_128k`, `opus_192k` or `opus_256k` in `hello`.
  A live `set_audio_profile` change restarts playback at the validated whole-second
  position and preserves pause.
- Delivery uses bounded chunk buffering, client capacity reports and receipt ACKs.
  MA retains control of source buffering, DSP, normalization and service stream limits.
  Recovery seeks and codec changes target only the device's own queue and require no
  client user scopes. MA's regular API authentication remains unchanged.
- Reconnects reconcile retained streams and receipt cursors. Replaced connections lose
  authority immediately; their Close handshake does not delay the new connection.
  Transport loss alone does not release the source or clear MA's PCM buffer.
- Source interruption triggers bounded, best-effort PCM repair with a pause-safe MA seek
  fallback. An unsuccessful fallback reports `source_failed` to the client instead of
  silently advancing the queue.

Groups, multiroom, crossfade, flow mode and overlays are not supported. PCM matching
can fail with DSP, normalization or resampling; a successful match does not guarantee
an inaudible transition.

## Logging

Use MA's standard provider log-level setting. DEBUG covers connection and playback
diagnostics; VERBOSE (level 5) adds command/ACK identifiers and rate-limited transport
summaries, next-track handover stages, and rejected queue-context fields (including
stream-details presence, session match, duration and playback restrictions).
Failures and prolonged sends produce warnings. Tokens, bindings, source
URLs and audio payloads are not logged.

## Verification

Run from the repository root in the MA development environment:

```text
python -m pytest --noconftest -o addopts= tests/providers/mashin_stream
python -m ruff check music_assistant/providers/mashin_stream tests/providers/mashin_stream
```

Native provider and codec tests require the complete Linux MA/FFmpeg runtime; Windows
cannot validate that pipeline. Unit tests do not replace the contract's acceptance
gates for codecs, source lifecycle, session binding, reconnect races, multi-device
isolation and end-to-end client playback. These gates remain required before release.