# Changelog

## Unreleased

### Added

- Shared `storage_dir` in Camera, StreamServer, serve_streams, and PushClient,
  plus global CLI `--storage-dir` / `--storage_dir`. Connection and push state
  share the per-camera SQLite file; explicit legacy state overrides still work.
- Typed `PushEvent` classification helpers and properties for notification text,
  camera metadata, language, UTC server time, and decoded options/customConfig.
  Full-response getters return independent copies and preserve unknown fields.
- Notification `title` in SDK events and default CLI push read/watch output,
  without requiring `--full-response`.
- Documented `LPC` as a physically confirmed low-battery push warning on
  Reolink Argus Eco; the exact charge threshold remains unverified.
- Full decrypted SDK event responses persisted in SQLite, opt-in CLI
  `push read/watch --full-response`, and raw JSONL capture during `push read`
  synchronization. Summary output excludes full notification bodies and URLs.
- Opt-in `push watch --raw-output PATH.jsonl` / `PushClient.watch(raw_output=...)`
  for full decrypted notification diagnostics before filtering. Appends flushed
  records across reconnects without an in-memory archive; disabled by default.
- Optional cloud push API: `PushClient`, `PushWatch`, `PushEvent`, `PushResult`,
  and sanitized push exceptions. FCM reception requires the `[push]` extra;
  ordinary SDK imports and camera commands do not.
- Main CLI `push init/subscribe/status/unsubscribe/watch/read`,
  with compact output, optional JSON, and sanitized debug diagnostics.
- Reusable PC receiver identities, command 524 registration, command 526 status,
  HTTP unsubscribe scoped to the PC client, and verification/reconciliation of
  timed-out registration and completed removal.
- Unlimited watch by default, optional `duration`, configurable reconnect delay
  with bounded exponential backoff, and `skip_previous=True` based on server time.
- Context-managed reception with a bounded 128-event queue, bounded 2048-ID
  deduplication history, and cancellation that retains the cloud subscription.
- Finite `read` of unread events with bounded FCM synchronization, an offline
  SQLite-only option, atomic batch claiming, and a configurable result limit.
- Per-camera `~/pyneolink/{camera}.db` storage via SQLAlchemy Core, automatic
  transactional schema migrations, identity checks, and receiver leases.
- Bundled public Firebase application profile: `push init` / `PushClient.initialize()`
  no longer require an APK export or external config file. Explicit overrides
  and saved settings take priority; existing receiver identities are preserved.

### Changed

- Unified push read/watch formatting: identical text event lines and JSONL
  (one object per event) in JSON/full-response mode. Empty reads emit no JSON
  lines; lifecycle notices use stderr. SDK read still returns a list.
- Push watch shutdown waits for SQLite lease release before returning, deferring
  repeated Ctrl+C during bounded cleanup so an immediate restart is not blocked.
- Increased the optional receiver's GCM registration budget from two to five
  attempts within the same upstream call/check-in/subtype, still bounded by the
  existing 60-second registration timeout. No automatic new-identity loop.
- Push initialization errors include allowlisted registration stages and Google
  error codes without requiring debug logs or exposing raw response bodies.
- Clarified failed-initialization errors: an existing receiver is not replaced;
  this does not imply that a first registration saved usable credentials.
- Consolidated push research examples into `examples/push.py`; protocol logic
  now lives in the SDK, without imports from examples or changes to the existing
  camera dispatcher/session architecture.
- Moved SDK-managed connection and push state from JSON to SQLite. Matching
  legacy files are imported once and retained; user configs, logs and media are
  unchanged. Existing receiver identities are reused, not registered again.
- Push events are persisted before filtering/delivery. Watch consumes live
  events; repeated read returns only unread events from the current subscription.
- One-time initialization persists application configuration so deployed
  receivers need no JADX export. SQLAlchemy is now a core dependency; the FCM
  dependency remains optional.

### Validation

- User-run physical tests confirmed subscription lifecycle, independent receivers
  on multiple cameras, unread-only read, prolonged watch, and reconnect after
  network loss. Full queued LPC payload confirmed the low-battery meaning.
  The final typed helpers and unified CLI formatting have offline regression
  coverage; a final manual smoke check is still recommended before release.
- Version remains `0.5.0`; these changes are not yet released.

## 0.5.0

Reliable SD-card downloads and batch download API.

### Added

- Added list-compatible `SDFileCollection` results from `SdCard.files()` with
  `count()`, `names()`, metadata `info()`, local sorting, total `size`, and
  `limit_size()` for volume-limited download selections.
- Added sequential `SDFileCollection.download()` with structured progress,
  compact `DownloadBatchResult` metadata, reusable completed/skipped/failed
  collections, JSON-ready reporting, and optional best-effort desktop
  notification on completion.
- Added regular and delayed reconnect tiers with independent retry jitter for
  long-running downloads from multiple cameras.
- Added download packet tracing to `examples/download_test.py` for controlled
  physical diagnostics, including local and UTC timestamps, message headers,
  payload sizes, and readable XML events.
- Added a static official-SDK Baichuan message catalog and documented the
  evidence level for each mapping.

### Changed

- Made the reliable UDP transport drain and reorder incoming datagrams in a
  dedicated receiver worker, with independent ACK/heartbeat maintenance and a
  larger requested socket receive buffer.
- Send the measured receive rate in UDP ACK packets and expose receive,
  buffering, ACK, resend, and receiver-health counters through
  `debug_snapshot()`.
- SD-card download progress now includes file index, camera name, transferred
  size, rate, ETA, chunk count, and visible packet-gap diagnostics.

### Fixed

- Treat the camera's explicit successful terminal response (`300`) as the
  authoritative SD-card download completion signal instead of guessing from
  listed file size or a quiet timeout.
- Keep outbound UDP acknowledgement and receive processing active while media
  bytes are being decoded or written, preventing remote transfers from stalling
  behind a full receive socket.
- Preserve incomplete recordings as `*.part` files and restart them only after
  reconnecting; finalized files are not silently replaced by partial data.
- Avoid synchronized P2P reconnect bursts when several remote camera workers
  encounter the same temporary transport failure.
- Wait for the initial unsolicited `BatteryList` status event after login on
  `wifi_solo_ipc` battery cameras, preventing ordinary commands from racing an
  unready session and receiving an initial `400` response.

### Validation

- A four-camera physical download run completed all 1,050 selected recordings
  with no failed files or leftover `*.part` files. Twelve interrupted transfers
  and sixteen reconnects recovered successfully; all twenty LAN discovery
  misses fell back to successful UID/P2P registration.
- The fixes were refined against an additional remote battery-camera model and
  verified without changing that camera's configuration or behavior.

---

## 0.4.4

Single-session streaming and recovery patch.

### Added

- Added dispatcher-managed request contexts for regular commands, multipart
  replies, live streams, motion events, snapshots, voice, and SD-card access.
- Added experimental `Camera.continue_preview()` support for the Reolink
  `LongTimePreview` command and runtime diagnostics for session continuation.
- Expanded the battery/runtime examples with per-second motion polling, stream
  payload and UDP statistics, maximum-duration runs, and reconnect reporting.

### Changed

- Use one managed transport session per `Camera`; concurrent helpers now share
  its dispatcher instead of opening isolated camera connections.
- Move stream recording and HTTP serving onto dispatcher-managed payload
  readers so status commands can run while video is active.
- Simplified snapshot capture to one standard image request because the tested
  snapshot stream types return the same image quality.
- Removed the undocumented `raw-stream` CLI command. Use `record` for a local
  MPEG-TS file or `serve` for live playback; low-level stream payload access
  remains available through the SDK.
- Updated public class and method annotations and the physical-test examples
  for the managed-session API.

### Fixed

- Route replies by message ID, message number, and scoped multipart matchers so
  concurrent stream, motion, battery, and command operations cannot consume
  each other's packets.
- Restart the dispatcher and active stream after a stale or closed camera
  session reconnects, including the observed battery-camera stream cutoff near
  30 minutes.
- Keep UDP acknowledgements and heartbeats running in the background while
  media packets arrive continuously, without clearing unrelated pending data.
- Detect stalled media separately from ordinary receive timeouts and recover
  the stream through a fresh P2P registration when needed.
- Give the MPEG-TS recorder test enough time to start on slower Python 3.11
  Windows runners, removing a timing-dependent empty-file failure.

### Notes

- A real-camera high-stream test completed 35 minutes with battery polling,
  detected the session cutoff at 30 minutes, reconnected in about six seconds,
  and completed normally.
- `LongTimePreview` does not bypass the observed camera-side 30-minute stream
  limit; automatic reconnect remains the recovery mechanism.

---

## 0.4.3

Session dispatcher and release workflow preparation.

### Added

- Stored PTZ preset list and recall through `camera.ptz()`, with documentation
  for the Argus PT Ultra same-session snapshot prerequisite.
- Documented Conda-based local development setup and the tested Python versions.
- Added long-running battery/runtime, stream-session, and dual-camera
  stream-session probe examples for real camera diagnostics.
- Added GitHub Actions test coverage across CPython 3.11, 3.12, 3.13, and 3.14
  on Windows and Linux.
- Stream-session probe examples now detect stalled media payloads separately
  from active UDP keepalive traffic.

### Changed

- `with Camera(...) as camera:` now starts an internal message dispatcher
  automatically, so SDK users can use one camera session for stream reads,
  motion events, and regular commands without managing the socket reader.
- The stream server can be used as a context manager for tests and embedded SDK
  use.

### Fixed

- Keep UDP relay maintenance running even while media data is continuously
  arriving, which prevents long live streams from losing their P2P session.
- Route live stream, motion, and command replies by Baichuan message id and
  message number when the dispatcher is active, avoiding packet mix-ups between
  stream payloads and status requests such as battery polling.

### Notes

- PTZ preset recall is currently documented as experimental: on the tested
  Argus PT Ultra it must be called after a `Camera.snapshot()` exchange in the
  same authenticated session.
- The new runtime examples are intended for real-camera diagnostics and may be
  updated as more camera models are tested.
- The test suite was verified with Conda on CPython 3.11.15, 3.12.13, 3.13.15,
  and 3.14.7.

---

## 0.4.2

ADPCM compatibility and code style patch.

### Changed

- Added `ruff` development configuration for PEP8 checks and formatting with a
  project line length of 120 characters.
- Reformatted Python sources with `ruff` while keeping dense protocol tables and
  XML payload constants readable.

### Fixed

- Pack pure-Python voice ADPCM sample pairs low-nibble-first to match DVI/IMA
  ADPCM as produced by ffmpeg/GStreamer and the Rust Neolink talk pipeline,
  based on protocol-review work from [kklemon](https://github.com/kklemon/).
- Avoid shadowing the imported protocol message constants inside
  `Camera._recv()`.

---

## 0.4.1

Voice/talk reliability patch.

### Changed

- Kept `Camera.snapshot()` and the `snapshot` CLI on explicit `stream_type` selection instead of adding snapshot `quality` aliases.
- Snapshot payloads now keep `<fullFrame>0</fullFrame>` for both main and sub snapshot streams.
- Clarified that `Camera.record()` and the `record` CLI perform local
  MPEG-TS live-stream recording, not camera-side SD-card recording control.

### Fixed

- Reconnect once and retry when short camera commands hit a stale UDP session timeout.
- Reconnect once and retry the whole snapshot request when snapshot metadata or data waits time out.
- Reused the shared command retry path for repeated voice/talk and siren commands.
- Treat Tree360-style `MSG.FILE_PLAYBACK` response `331` as a scoped playback
  continuation so SD-card playback downloads can continue to their normal
  completion response, based on fork testing from
  [megablocks](https://github.com/megablocks).
- Hardened config parsing, snapshot output paths, connection-state writes, UDP
  packet decoding, and global CLI `--camera` parsing based on external fork
  review from [kklemon](https://github.com/kklemon/).

---

## 0.4.0

SD-card file API and preview playback work.

### Added

- Added `SDFile` wrappers for SD-card recordings with `info()`, `download()`, and `preview()`.
- Added `SdCard.files()` and `SdCard.file(...)` helpers.
- Added cached SD-card preview playback with an HTTP stream helper for players such as VLC.
- Updated `examples/sd_card_example.py` with list, download, preview, remove, and format examples.

### Changed

- Moved public recording downloads from `SdCard.download(file, ...)` to `SDFile.download(...)`.
- Updated README, docs, and examples to use the new SD-card file API.
- Use camera `file_name` plus the media extension for finalized download filenames.

### Fixed

- Treat camera `400` responses after partial SD-card download data as interrupted downloads so reconnect/retry handling can recover.

---

## 0.3.2

Downloader reliability improvements.

### Added

- Added `CameraConnectionError` for unrecoverable camera reconnect failures.
- Added `reconnect_retries` to `SdCard.download()` for interrupted long downloads.
- Added `rewrite_exists` to `SdCard.download()` to skip already finalized local files.
- Added IDE-friendly docstrings for SDK classes, CLI helpers, and core protocol components.

### Changed

- Treat existing non-empty `.mp4` files as complete when `rewrite_exists=False`.
- Remove stale `.part` files for a recording when the finalized `.mp4` is skipped.
- Translated internal documentation to English for publication.

---

## 0.3.1

PyPI metadata and README link update.

### Changed

- Updated package metadata and installation links for the first PyPI publication.

---

## 0.3.0

Initial public alpha preparation.

### Added

- UID/P2P camera connection with local, relay, and cached address paths.
- Baichuan login, command framing, BC XOR, and AES-CFB support.
- Camera info, UID, reboot, snapshot, LED/IR compatibility commands.
- SD-card listing with pagination and high/low recording download.
- Battery status with reconnect and online polling modes.
- Live MPEG-TS stream server and HLS timeshift buffer.
- Local MPEG-TS recording from live streams.
- Motion status and motion event watch mode.
- Voice/talk from microphone, audio file, or generated tone.
- Built-in siren trigger.
- Settings facade with PIR and IR light controls.
- CLI and SDK examples.

### Notes

- This release is experimental and reverse engineered.
- Tested on a limited number of Reolink cameras.
- API compatibility is not guaranteed before `1.0.0`.
