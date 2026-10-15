# Mashin player provider

Development implementation of the [V1 contract](doc/contract.md).
It is not yet V1-approved or production-ready. The contract remains the authority;
passing the focused Python tests does not replace its blocking integration gates.

## Configuration

Configure one provider instance through MA's setup flow with an optional trusted TLS
proxy IP. Multiple authorized clients connect to its shared
routes. Each client's stable `device_id` registers one independent MA player and queue;
the first Control `hello` supplies its initial name. A reconnect or server restart keeps
that player's identity. There are no groups or multiroom sessions.
Each client must request its codec/quality through `hello.audio_profile`: `flac`,
`opus_96k`, `opus_128k`, `opus_192k` or `opus_256k`. There is no provider-level profile
setting or implicit handshake default. The previous development `audio_profile` value
is deliberately ignored. Retained reconnect streams keep their declared format.

Later, send `set_audio_profile` with `sequence`, `request_id` and `audio_profile`.
The provider confirms with `audio_profile_changed`. While playing, it restarts through
MA's normal seek at the validated whole-second position using a fresh stream and codec;
pause is preserved without autoplay. This requires `queues.control`. An idle selection
needs no seek. Unvalidated positions, pending commands or missing rights reject the
change. Decoder and capacity barriers still apply; no existing chunk changes codec.

Device ownership is persisted against the authenticated MA user, not a bearer token.
A different user cannot reclaim the same device ID. Audio cannot register a device or
change its owner. The previous development configuration's `device_id`, `user_id` and
`device_name` are deliberately not imported; clients register through Control again.
V1 connection, buffer and repair limits apply independently to each device. In particular,
`max_devices=1` limits one client session, not the provider's total number of clients.
MA's provider-wide entitlement and concurrent-stream limits still apply.

Advanced admission settings default to 32 registered devices, 8 devices per MA user
and 128 simultaneous WebSockets, including handshakes. Increase them for larger
deployments. These are provider-wide protection budgets, not V1 per-player wire
limits. Existing persisted identities may reconnect above a subsequently lowered
device quota; only new identities are rejected. Socket slots are reserved before
the upgrade, and quota rejection does not create ownership records.

The development-session clarification permits MA's own internal next-item audio
prebuffering. The one-reader/producer boundary applies to Mashin's adapter: it opens
and consumes B only after the final A receipt. MA may prepare source buffers earlier,
within its existing source-slot, entitlement, pacing and memory rules. The provider
does not disable those core mechanisms, alter their budgets or bypass service limits.

Transport loss, a full client cache or a full ten-chunk delivery buffer does not close
Mashin's source or clear MA's PCM buffer. MA's independent raw-PCM producer may keep
filling until its own capacity, source limits, EOF or lifecycle cleanup; a full MA
buffer applies backpressure without requiring a Mashin restart. Mashin retains its
reader/DSP and committed chunks, without a provider-owned disconnect cleanup timer.
The legacy `disconnected_source_retention_seconds=30` welcome field is reserved for
wire compatibility, not an active source-retention deadline. Client reconnect grace
and transport handshake/ACK deadlines are unchanged.

Replacing Control or Audio immediately fences the old connection and closes its
delivery gates. Graceful WebSocket close runs in a referenced task, independently
of the new `welcome` or `audio_ready`, with a five-second cleanup budget followed
by transport closure. Retiring sockets still count toward connection admission.
Old report workers, pingers, send locks and deadline callbacks cannot acquire
authority over the replacement connection. Stream sources and receipt cursors stay
subject to the normal reconnect reconciliation.

If MA actually releases the PCM buffer, the next allowed read treats this as source
loss, not normal EOF, and uses the existing bounded repair/fallback path. A completed
healthy MA buffer remains readable after its upstream producer ends. No polling or
keepalive reads prolong MA's buffer lifetime. Explicit stop, replacement, revoked
authority and shutdown still release resources. An actual MA queue-pause command can
also trigger MA's own 30-second pause watcher and stop; client output pause alone is
not forwarded as a queue-pause command by this provider.

Initial source preparation uses MA's own source-slot and PCM-buffer readiness
budgets. The normal ten-second PCM/encoding watchdog begins after that preparation;
recovery instead keeps its shared thirty-second active-work budget. The source
identity is pinned after MA's initial provider selection and checked strictly on
repair. Each stream shares one cumulative PCM pacing ledger across reopens and
recovery fallback loads, charging discarded overlap as well as new audio.

A failed recovery seek or replacement source is a terminal `source_failed` error
sent through Control, just like an unavailable source. The producer remains alive
for later explicit MA loads; failed recovery does not silently advance the queue.

Unload immediately seals playback and cancels device workers. It waits at most five
seconds for cleanup. MA's process teardown can exceed this deadline, so the remaining
cleanup task stays referenced and supervised until sockets, processes and player
registration have been released; it is not abandoned or reported as audible EOF.

Control receipt ordering is retained even when command ACKs bypass the report queue:
only a capacity snapshot received after the accepted load ACK can open production.
Winning B promotion resolves a pending revocation without closing its Audio socket.
Failures from replaced senders cannot invalidate a newly bound connection.

The client upgrades `GET /mashin/v1/control` and `GET /mashin/v1/audio` on MA's main
webserver, using TLS and `Authorization: Bearer <token>`. No token or binding belongs
in a URL. The token must provide both `players.read` and `players.control`, with
access to the client-specific player. Its user becomes the device owner on registration.
Recovery seeks and live codec restarts additionally require `queues.control`; neither
can bypass missing or revoked queue authority.

For TLS termination, configure `tls_proxy_ip` only with the exact directly connected
proxy IP. That peer alone may attest HTTPS through `X-Forwarded-Proto: https`.
The proxy must overwrite that header, enforce TLS externally, forward Authorization
and WebSocket upgrades, and prevent untrusted access to its backend connection.
An unconfigured or mismatched proxy is rejected. This does not expose MA's separate
LAN streams server.

## Logging

MA adds the standard provider log-level setting automatically; Mashin does not duplicate
it. Connection, registration, handshake, stream and seek diagnostics use DEBUG. Successful
codec changes and device access revocation use INFO. Stream failures and lost Control
liveness use WARNING. PCM repair start and successful alignment use INFO, individual
attempts and safe failure codes use DEBUG, and failed repair or fallback uses WARNING.
MA-seek fallback start/completion and next-item re-preparation are logged separately
from a successful PCM match. A reopen before the first chunk explicitly reports that
no PCM match was needed. Repair reopens with active PCM-altering processing log an INFO
snapshot of output-filter count, normalization mode, fade-in, resampling and channel
conversion, noting that best-effort matching may fail. Raw filter expressions are omitted.
VERBOSE (level 5, MA's TRACE equivalent) adds command/ACK IDs
and actual output-state transitions, not every progress report. Active transport
events produce at most one summary per five seconds per device: sent chunks/bytes,
resolved ACKs, status reports, buffer slots/bytes, capacity, pending ACK, retry blocking
and maximum send duration. Collection is disabled outside VERBOSE; no polling task
or periodic idle output is added. Counts include accepted and rejected resolved chunk ACKs.

Completed Audio sends taking at least 500 ms emit WARNING, independently of VERBOSE.
Repeated stalls are reported at most every 30 seconds per device with the number of
occurrences since the previous warning. Normal capacity backpressure or waiting for
an ACK is not itself a warning. Existing terminal-error warnings remain unchanged.
No individual chunk payloads, tokens, bindings, source URLs or raw client payloads
are logged at any level. Logs identify the MA player and safe protocol error code.

Model field/type schemas are cached rather than reflected on every report. Encoder
input feeds history and PCM separately and retains only the required history suffix.
Encoding still uses one FFmpeg process per independent mini-stream: startup cost and
Opus compression-level changes require measurements on the pinned Linux runtime,
not an unverified persistent-codec replacement.

## Runtime and deployment

The contract references MA `1611ea10b`; this implementation was developed against
workspace commit `80d8592e598772a1712e3f01e247aab54c6de6d2` and models `1.1.221`.
Do not assume compatibility with another server revision, including the contract's
reference revision, until the adapter integration gates have passed there.
`welcome.server_revision` reports the official image's build revision, or the actual
MA package version for a development checkout; it does not invent a tested revision.

Use an official MA image pinned by digest, with Python 3.14+, FFmpeg 7+ and libopus.
Add only this provider directory under the image's installed
`music_assistant/providers/mashin` package directory, then restart MA for discovery.
Do not modify core controllers or use an external plugin installer. The manifest pins
SciPy `1.18.1`; the tested matcher used NumPy `2.3.5` and Mutagen `1.48.1` from MA's
environment. Record the exact image digest, MA/model versions and FFmpeg/libopus
build in the acceptance results. No image digest or codec build has been certified yet.

## Verification

Focused, dependency-light tests:

```text
python -m unittest discover -s tests/providers/mashin -v
python -m ruff check music_assistant/providers/mashin tests/providers/mashin
```

The real codec tests use synthetic PCM only and require the complete MA environment
and FFmpeg. Without FFmpeg they are explicitly skipped, not counted as decoder proof.
The Windows development environment cannot verify the Linux playback pipeline.

Before V1 acceptance, execute every blocking gate in contract section 13, including:

- Real FLAC subframe bounds and decoded frame counts; all Opus profiles, independent
  decoder resets, preroll alignment, CRCs, granules and final single-frame chunks.
- MA normalized PCM/output plans, source ownership, one Mashin reader/producer, cumulative
  pacing across reopens, source/codec backpressure and exactly-once history callbacks.
- Real Control/Audio TLS, separate scope checks, player ACL and token/user revocation;
  reconnect deadlines, native ping timing and socket/task teardown under races.
- Concurrent independent clients, stable per-device players and ownership across restart;
  one client's replacement, command, queue, capacity or revocation cannot affect another.
- Terminal errors, pending commands, promotion/revocation and stale producer callbacks
  against real MA queue changes, seeks, shuffle/repeat and finite track completion.
- PCM repair against actual codecs/providers and supported deterministic DSP;
  ambiguity rejection, budgets, cumulative source pacing and pause-safe MA fallback.
- Interoperability with the Rust client's cache/decoder/device clocks and platform
  tests. No client implementation or acceptance evidence is present in this repository.

PCM repair is best-effort: DSP filters, rate/channel conversion, fade-in and normalization
do not preempt an attempt. MA applies its normal processing; the matcher compares the
actual encoder-input PCM without inspecting or restoring filter state. Crossfade, flow
mode and overlays remain unsupported playback modes, not repair-time DSP checks.

Matching requires a complete three-second window in at most twelve seconds of replacement
PCM, searching only within one second of the expected endpoint. Whole-window correlation
must be at least 0.995, with a 0.005 margin over alternative peaks outside five adjacent
frames on either side. Every half-second block in every channel must also have correlation
at least 0.995, centered signal energy at least 1e-7 and normalized mean-square error at
most 0.01 (10% relative RMS error, not 1% sample-amplitude error). Limits never relax on
retry. Silence, periodic ambiguity, drift, unrelated recordings and excessive local
differences reject the match; the existing bounded, pause-safe MA-seek fallback applies.

An accepted match estimates alignment; it does not prove identical samples, restored DSP
state or an inaudible transition. Thresholds are provisional and covered by synthetic
positive/negative tests; real codec/provider/DSP acceptance evidence is still required.
Do not enable production use merely because a handshake or synthetic unit test passes.