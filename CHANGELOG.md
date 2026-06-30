# Changelog

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
