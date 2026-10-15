# Mashin player provider

Connects Mashin clients to Music Assistant (MA) through authenticated Control and Audio
WebSockets. Each device has its own MA player and queue, with a stable identity across
reconnects and server restarts. One provider instance serves multiple independent devices.

This provider is under development and is not yet production-ready. The
[V1 contract](doc/contract.md) defines the protocol and required acceptance tests.

## Requirements

- A complete MA Linux runtime with Python 3.14+, FFmpeg 7+ and libopus.
- A client implementing the Mashin V1 protocol.
- TLS on MA's main webserver, directly or through a trusted reverse proxy.
- An MA bearer token with `players.read`, `players.control` and access to the device's
  player. Recovery seeks and live audio-profile changes also require `queues.control`.

## Setup

1. Deploy this directory as `music_assistant/providers/mashin` in the MA installation
   and restart MA for discovery. No core changes or external plugin installer are needed.
2. Add a Mashin provider instance through MA's setup flow.
3. Connect the client to `GET /mashin/v1/control` and `GET /mashin/v1/audio` on MA's main
   webserver using WebSocket upgrades and `Authorization: Bearer <token>`.
4. Send a Control `hello` with a stable `device_id`, client name and explicit
   `audio_profile`. The authenticated MA user becomes the device owner. Bind Audio
   using the binding returned in `welcome`.

Do not put tokens or bindings in URLs. A device ID cannot be reclaimed by another user.

### Provider settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `tls_proxy_ip` | Unset | Exact directly connected TLS proxy IP, if used. |
| `max_registered_devices` | 32 | Provider-wide device registration limit. |
| `max_devices_per_user` | 8 | Device registration limit per MA user. |
| `max_websocket_connections` | 128 | Simultaneous sockets, including handshakes and retiring connections. |

Lowering a device quota does not prevent already registered devices from reconnecting.

When using a reverse proxy, configure `tls_proxy_ip` with its exact peer IP. The proxy
must enforce TLS, overwrite `X-Forwarded-Proto` with `https`, forward Authorization
and WebSocket upgrades, and prevent untrusted access to its backend connection.

## Playback and reconnects

- Clients choose `flac`, `opus_96k`, `opus_128k`, `opus_192k` or `opus_256k` in `hello`.
  A live `set_audio_profile` change restarts playback at the validated whole-second
  position and preserves pause.
- Delivery uses bounded chunk buffering, client capacity reports and receipt ACKs.
  MA retains control of source buffering, DSP, normalization and service stream limits.
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
summaries. Failures and prolonged sends produce warnings. Tokens, bindings, source
URLs and audio payloads are not logged.

## Verification

Run from the repository root in the MA development environment:

```text
python -m pytest --noconftest -o addopts= tests/providers/mashin
python -m ruff check music_assistant/providers/mashin tests/providers/mashin
```

Native provider and codec tests require the complete Linux MA/FFmpeg runtime; Windows
cannot validate that pipeline. Unit tests do not replace the contract's acceptance
gates for codecs, source lifecycle, authorization, reconnect races, multi-device
isolation and end-to-end client playback. These gates remain required before release.