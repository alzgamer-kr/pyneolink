# Changelog

## Unreleased

No changes yet.

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
