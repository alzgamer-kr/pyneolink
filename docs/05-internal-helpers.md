# Internal Helpers

`pyneolink/internal` contains helper modules for public, user-facing modules.

The intent is to keep files like `camera.py`, `battery.py`, `voice.py`,
`sd_card.py`, and `stream_server.py` focused on public behavior while protocol
parsing, value normalization, and small reusable helpers live in focused
internal modules.

These modules are not part of the public API. Code outside the package should
use `pyneolink.Camera`, `Camera.sd_card()`, `Camera.battery()`,
`Camera.motion()`, `Camera.voice()`, and stream server helpers instead of
importing from `pyneolink.internal`.

## `pyneolink/internal/battery.py`

Battery response helpers.

Responsible for:

- parsing `BatteryInfo` XML into a normalized dict;
- converting integer-like fields safely;
- mapping `adapterStatus` to normalized charge type values;
- validating battery polling mode: `reconnect` or `online`.

Used by `pyneolink/battery.py`.

## `pyneolink/internal/camera.py`

Camera-level helpers.

Responsible for:

- parsing `host:port` camera addresses;
- keeping an online lease counter for long-running actions;
- mapping stream aliases like `high` and `low` to Reolink stream names;
- redacting sensitive fields from camera info output.

Used by `pyneolink/camera.py`.

## `pyneolink/internal/snapshot.py`

Snapshot response helpers.

Responsible for:

- parsing snapshot XML metadata;
- reading camera-provided file names;
- validating expected snapshot size;
- choosing the output path when `Camera.snapshot(out=...)` receives either a
  file path or a directory path.

Used by `pyneolink/camera.py`.

## `pyneolink/internal/voice.py`

Voice/talk helpers.

Responsible for:

- parsing `<TalkAbility>` XML into `TalkConfig`;
- validating and converting audio files through `ffprobe` and `ffmpeg`;
- reading microphone input through `sounddevice`;
- generating a test tone;
- converting PCM audio into IMA ADPCM blocks;
- serializing ADPCM blocks into BCMedia talk packets.

Used by `pyneolink/voice.py`.

## Next Candidates

`sd_card.py` is the next large cleanup target, but it should be split carefully
because its download path is protocol-sensitive and already covered by tests.

Recommended split:

- `internal/sd_card_parse.py`: XML parsing, file identity, date/time coercion;
- `internal/sd_card_queries.py`: list, handle, day, and download query builders;
- `internal/sd_card_download.py`: download response handling, progress, file
  finalization, transport snapshots.

This keeps the stable public API in `SdCard` while making the large private
function set easier to navigate.
