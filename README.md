# PyNeolink

[![Downloads](https://img.shields.io/pypi/dm/pyneolink?style=for-the-badge&label=downloads&labelColor=006dad&color=005d94&cacheSeconds=86400)](https://pypistats.org/packages/pyneolink)
[![Release](https://img.shields.io/github/v/release/alzgamer-kr/pyneolink?style=for-the-badge&label=release&labelColor=006dad&color=005d94&cacheSeconds=7200)](https://github.com/alzgamer-kr/pyneolink/releases)
[![GitHub](https://img.shields.io/github/forks/alzgamer-kr/pyneolink?style=for-the-badge&label=forks&labelColor=006dad&color=005d94&cacheSeconds=7200)](https://github.com/alzgamer-kr/pyneolink)
[![GitHub](https://img.shields.io/github/stars/alzgamer-kr/pyneolink?style=for-the-badge&label=stars&labelColor=006dad&color=005d94&cacheSeconds=7200)](https://github.com/alzgamer-kr/pyneolink)

PyNeolink is a Python client for Reolink/Neolink-style Baichuan cameras. It focuses on UID/P2P access, camera information, SD-card recordings, live viewing, snapshots, local recording, motion events, battery status, voice/talk, and siren control.

Version: `0.5.0` alpha.

Cloud push support described below is **Unreleased**, available from this checkout,
not the published `0.5.0` package.

This project was developed with OpenAI Codex as an AI-assisted implementation effort. It is a Python port inspired by and based on protocol knowledge from the Rust `neolink` project, especially `QuantumEntangledAndy/neolink` and `surfzoid/neolink`. The reverse-engineering foundation belongs to the Neolink contributors. The goal is not to replace Neolink, but to make a working Python implementation available for people who want to study, adapt, or extend this protocol without working in Rust.

Neolink and Reolink protocol support are reverse engineered. This project is not affiliated with Reolink.

## Status

PyNeolink is experimental alpha software. It works against a limited set of real cameras, but Reolink firmware and model behavior can differ. APIs may change before `1.0.0`.

The test suite is currently verified with Conda on CPython 3.11.15, 3.12.13, 3.13.15, and 3.14.7.

## What Works

- JSON camera configuration with `address` or `uid`
- Reolink UID/P2P lookup, registration, UDP relay, and local UDP connection
- Baichuan login and command framing
- BC XOR encryption and AES-CFB support through `cryptography`
- Camera information, UID, LED command, and reboot command
- Battery status, including reconnect and online polling modes
- SD-card recording list with pagination, filtering, metadata sorting, and size limits
- Resilient SD-card recording download with high/low quality selection, batch results, retryable failed collections, progress, and optional completion notifications
- SD-card preview playback cache with an HTTP stream helper for players such as VLC
- Snapshot download to bytes or JPEG file
- Local MPEG-TS recording from the live stream with `Camera.record()` or the
  `record` CLI command
- Live HTTP MPEG-TS viewing with H264/H265 video and AAC audio
- HLS timeshift viewing with an in-memory sliding buffer
- Concurrent SDK stream, motion, battery, and command handling inside one
  `with Camera(...)` session
- Battery-camera sessions wait for their initial status event before accepting
  regular commands
- Automatic dispatcher and live-stream recovery when a camera session closes
- Motion status and motion event watch mode
- Two-way voice/talk from microphone, audio file, or generated test tone
- Camera siren trigger
- PIR status and PIR on/off settings
- IR light status and IR on/off/auto settings
- Stored PTZ preset listing and recall by ID or exact preset name

## Current Limits

- This is reverse engineered and tested against a small number of real cameras, so behavior may differ between models and firmware versions.
- Voice file playback uses `ffmpeg`/`ffprobe` for format validation and conversion. Install FFmpeg and make sure both commands are available in `PATH`.
- Microphone voice input needs the Python `sounddevice` package and a working local input device.
- Local stream recording writes MPEG-TS (`.ts`) files, not MP4. It records the
  live stream on the client machine; it does not start or stop the camera's own
  SD-card recording schedule.
- SD-card `remove()` and `format()` exist, but are intentionally guarded.
- Some tested battery cameras end a continuous stream near 30 minutes. The SDK
  reconnects and restarts the stream, which can cause a short playback gap.
- Arbitrary directional PTZ movement, image settings, alarm schedules, floodlight settings, and Web UI are not implemented yet.

## Install

From PyPI:

```powershell
python -m pip install pyneolink==0.5.0
```

With microphone voice input support:

```powershell
python -m pip install "pyneolink[voice]==0.5.0"
```

For local development from a checkout:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install -e ".[dev,voice]"
```

With Conda from a checkout:

```powershell
conda create -n pyneolink-dev python=3.14 pip
conda activate pyneolink-dev
python -m pip install -r requirements.txt
python -m pip install -e ".[dev,voice]"
python -m pytest
```

The `cryptography` package is required for AES-encrypted cameras. It is installed automatically when installing the package.

## Cloud Push (Unreleased)

Cloud push is separate from `Camera.motion()`: the receiver listens to FCM, not
the camera stream. Subscribe/status use brief camera sessions; unsubscribe uses
the cloud and, by default, a brief verification session. Watch/read never opens
a camera connection and does not enable or disable subscriptions implicitly.
This is reverse-engineered support, physically tested on one standalone camera.
Other firmware, NVR channels and multi-lens devices are not yet validated;
nonzero channels are currently rejected.

Install optional receiver dependencies from the checkout:

```powershell
python -m pip install -e ".[push]"
```

Normal camera features and push status/subscription operations do not require
the FCM dependency. Initial receiver registration, watch and online read do.
SQLite-only `read(sync=False)` does not. SQLAlchemy is a core dependency.

### Receiver Setup

State is stored in `~/pyneolink/{camera}.db`, separately for each camera.
Do not copy a phone's FCM credentials. Stop old receiver processes before the
first run: legacy research JSON is imported once, without deleting originals
or replacing existing SQLite state. One-time initialization uses the bundled
Firebase application profile, reuses an existing receiver, or registers one if
none exists. No APK, JADX export, phone credentials or personal Google project
is needed:

```powershell
python -m pyneolink.cli --camera "Home-Back" push init
```

Initialization is idempotent: call `PushClient.initialize()` on every startup
without first checking whether the receiver exists. With valid saved state and
matching application settings, it returns `ok=True`, `status="receiver-ready"`,
and `result.info()["reused"] == True`, without network registration or camera
connections. This also works with a new client object using the same database.
It does not subscribe again or change an existing listener. Corrupt/mismatched
state, storage errors and concurrent receiver operations still raise errors;
idempotence does not hide those failures. Initialize before entering watch.

The bundled profile contains public client-side configuration from Reolink
Android `4.64.3.20261023`, including the Firebase API key and APK signer
fingerprint. It does not contain user tokens, service-account keys, camera
passwords or a shared receiver identity. Each camera database gets its own
receiver credentials. The profile was used in physical delivery tests, but is
not an official integration contract: changes by Reolink/Google may require a
package update.

For research or a changed application profile, optionally use
`--android-strings path/to/strings.xml` or `--firebase-config firebase-config.json`, containing
`project_id`, `app_id`, `api_key`, `messaging_sender_id`, and `bundle_id` for the
Reolink application. An unrelated Google project cannot receive Reolink pushes.
Explicit parameters take precedence over the saved SQLite configuration, which
takes precedence over the bundled default. Existing incompatible receiver
identities are rejected, never silently replaced. Config overrides do not
change the bundled Android signing fingerprint; a different signer requires a
protocol update. Use `--legacy-state-dir .tmp/push` to explicitly import old
research state; it is not required for new users.

Fresh Google check-ins can receive `PHONE_REGISTRATION_ERROR` during GCM
registration, even with HTTP 200. Init allows up to five attempts within the
same upstream GCM call (same check-in identity and subtype), instead of the
dependency's default two; its one-second retry interval is unchanged. Complete
network registration is capped at 60 seconds. Failure remains visible; no
unbounded registration or identity-reset loop runs. This retry budget is a
mitigation, not a guarantee that Google will accept registration. It does not
affect camera subscriptions or an already initialized receiver.
The database contains authentication material, not encrypted at rest: keep it
out of version control and shared backups. Newly created POSIX databases are
owner-only; Windows files inherit directory ACLs. Protect the parent directory.

### CLI Cycle

```powershell
python -m pyneolink.cli --config config.json --camera "Home-Back" push subscribe
python -m pyneolink.cli --config config.json --camera "Home-Back" push status
python -m pyneolink.cli --camera "Home-Back" push read --json
python -m pyneolink.cli --camera "Home-Back" push read --no-sync --json
python -m pyneolink.cli --camera "Home-Back" push watch --duration 1800
python -m pyneolink.cli --config config.json --camera "Home-Back" push unsubscribe
python -m pyneolink.cli --config config.json --camera "Home-Back" push status
python -m pyneolink.cli --config config.json --camera "Home-Back" push subscribe
```

Stop watch with Ctrl+C before changing the subscription or running online read
(a database lease prevents concurrent receivers for the same identity).
`read` is a finite batch operation, not an alias of `watch`. No `--confirm` is needed
in the main CLI: the subscribe/unsubscribe command itself requests the change.
Repeated subscribe checks 526 and avoids duplicate creation if already active.
Timeouts are reconciled by a state query, not a blind registration retry.
Unsubscribe only targets this PC client; it does not switch global camera push
off or remove the phone's registration. `--no-verify` omits the post-removal
camera check; use `--retry-rejected` only for a deliberate retry of a known
rejected HTTP request. Unknown outcomes are not automatically repeated.

Normal output is compact. Add `--json` for structured results/events or `--debug`
for sanitized diagnostics on stderr. Debug does not print tokens or raw bodies.

#### Observed Push Event Types

The following `event_type` values have been observed in physical tests:

| Value | Meaning |
| --- | --- |
| `PEOPLE` | Person detection. |
| `MD` | General motion without a specific object category; triggers may include insects, animals, moving trees, or other scene changes. This value does not identify which of these caused the event. |
| `VEHICLE` | Vehicle detection. |
| `TEST` | Test notification requested from the official application. |
| `LPC` | Low-battery warning, confirmed from the full push payload on Reolink Argus Eco. The exact battery threshold and availability on other models remain unverified. |

These are observed values, not an exhaustive enum. For supported push envelope
versions, the SDK preserves other `event_type` strings rather than filtering them
out. Applications should keep a fallback for unfamiliar event types.

On 2026-11-06, a full `LPC` payload from Reolink Argus Eco confirmed the meaning:
`title="LOW BATTERY WARNING"` and `alert="Low battery on ... Please recharge."`.
The server timestamp was 13:50:28.451 UTC; `read` received the queued message at
13:51:08.283 UTC. The payload contains no battery percentage, so it does not
establish an exact trigger threshold. This confirms queued delivery in this test,
not guaranteed retention during an arbitrarily long offline period.

On 2026-11-05, `LPC` was received at 16:59:07 UTC from Home-Front.
The battery was estimated, not measured, at roughly 15%; this is not a documented
notification threshold. Five more `MD` events followed, the last about 6 minutes
25 seconds later, so `LPC` did not indicate immediate camera shutdown.
The inspected Android app handles `LPC` as `OnlyTip` and takes notification text
from the received payload. SDK consumers can inspect it in `event.response`;
CLI users can include it with `--full-response`.
Use `watch --json` to capture the reported type without polling the camera's
battery or holding a camera session open. Stopping watch with Ctrl+C retains
the subscription; starting watch for another camera uses that camera's own
initialized receiver and subscription.

#### Watching and Reading Events

`read` and `watch` use the same event output: text lines contain server time
(falling back to reception time), event type, camera UID, and title. With
`--json` or `--full-response`, both print one object per line (JSONL), not an
array. An empty read prints no JSON lines. Lifecycle notices go to stderr.
The SDK's `read()` still returns a list of `PushEvent` objects.

`watch` and `read` accept `--full-response` to include a `response` field with
the complete decrypted notification in JSON console output. Without this flag,
the compact/JSON summary includes `title` but omits the full response. SDK events expose this
data as `event.response` or `event.info(full_response=True)`. New incoming events
retain it in SQLite; older events without a captured response return `null`.
Full responses contain private data, including notification text and URLs.

To collect notifications queued while the PC was off:

```shell
python -m pyneolink.cli --camera "Home-Front" push read --sync-timeout 30 --idle-timeout 5 --full-response --raw-output .tmp/logs/push/front-read-raw.jsonl --json
```

`read(raw_output=...)` captures incoming messages during synchronization, including
messages outside the returned unread batch. It cannot be combined with `--no-sync`.
`--raw-output` always preserves the complete incoming envelope, independently of
the console's `--full-response` setting. Cloud retention limits still apply.

For local diagnostics, `watch --raw-output PATH.jsonl` appends the complete
decrypted notification envelope supplied by the FCM library before normalization,
deduplication, or old-event filtering:

```shell
python -m pyneolink.cli --camera "Home-Front" push watch --raw-output .tmp/logs/push/front-raw.jsonl --json
```

The SDK equivalent is `push.watch(raw_output=".tmp/logs/push/front-raw.jsonl")`.
Each JSONL record contains `received_at` (UTC), `persistent_id`, and `notification`.
For v2 events, inspect `notification.data.ALMTYPE`, `alert`, `title`, and `ALMNAME`
when present. Unknown fields and duplicate deliveries are preserved. This is
the decrypted application message, not a packet capture of FCM transport frames.

**Sensitive diagnostic output:** full messages may contain device identifiers,
private text, and image URLs. Receiver credentials, registration tokens and
decryption keys are not deliberately exported, but the notification contents
are not redacted. Do not publish the file without reviewing it. Capture is off
by default and independent of `--debug`; normal console output remains unchanged.
Use a separate file per camera. Parent directories are created automatically;
existing files are appended to across restarts/reconnects. New files use owner-only
permissions on POSIX; on Windows, directory ACLs apply. Records are flushed after
each notification without keeping an in-memory archive. Files grow on disk without
automatic rotation, so use this option for bounded tests (for example with
`--duration 1800`). Stop and restart watch to enable it; no resubscription is needed.
Previously discarded fields cannot be recovered from the normalized SQLite inbox.

Watch defaults to **unlimited duration**, with `skip_previous=True`. Use
`--duration 1800` for a 30-minute test, not as a reconnect timeout.
`--reconnect-delay 30` controls the initial retry pause for transient network
failures; exponential backoff caps at 300 seconds. Authentication/configuration
and consumer errors surface instead of retrying forever. This is not a delivery
guarantee: FCM retention and upstream behavior still apply.

Old events are filtered by cloud server time relative to watch start, with the
same cutoff retained across reconnects. Synchronize the PC clock. Events with
missing/invalid server time are delivered with an age marker, not silently lost.
`--no-skip-previous` also delivers backlog still available from FCM. Previously
stored unread events can be obtained with `read`, including old events skipped
by watch. Events yielded by watch are marked read and excluded from later read.

`read` briefly synchronizes with FCM, then returns **only unread events** from
the current subscription, oldest received first, and marks that batch read.
The default limit is 1000 events (maximum 10000); remaining events stay unread.
`--sync-timeout 10` caps synchronization, `--idle-timeout 2` ends it after an idle
period once connected. FCM provides no end-of-queue marker, so this is a bounded
catch-up, not proof that its remote queue is empty. Increase the timeouts on slow
connections or repeat read. Cleanup may take additional time. Events with server
time after read began remain unread for the next invocation. Events without
server time use local receipt time and may therefore appear on the next read.
`--no-sync` reads SQLite only, without network access. Successful return marks
events as consumed, not confirmation that the caller's bot or other side effect
succeeded. A crash after claiming a batch can prevent it from being returned
again; this is not transactional delivery to external applications.

RAM is bounded by a 128-event application queue and a 2048-entry deduplication
history; no complete event archive is kept in memory. If the consumer cannot
keep up, a full queue raises `PushError`. Debug logs written by the caller can
still grow on disk, so configure log rotation for unattended operation. The
SQLite inbox also grows on disk; no unread events are automatically deleted.

### SQLite State and Upgrades

Connection endpoints, push credentials, subscription status, Firebase settings,
delivery IDs and received events use SQLAlchemy Core with per-camera SQLite.
Camera network sessions and the dispatcher architecture are unchanged. User
configuration JSON, downloaded media and diagnostic logs remain ordinary files.
Research-only internal compatibility helpers can still read/write old probe
JSON, but the public SDK and CLI use SQLite.

Tables are checked on opening a database. Ordered, versioned migrations run in
an explicit transaction automatically, with rollback on failure. No Alembic
command or manual schema setup is required. A newer/unknown or damaged schema
is rejected instead of reset; downgrading the package cannot silently erase it.
Stop SDK processes before upgrading. Separate process leases prevent duplicate
FCM receivers; a lease abandoned by a crash expires after 120 seconds. Each
operation uses short database transactions, not a connection per event held open.

Matching legacy `.tmp/push` and `~/.pyneolink/push` state is detected automatically;
use `--legacy-state-dir` for another location. The original files are retained,
not kept in sync: do not run old JSON-based receivers alongside the new SDK.
Earlier events already acknowledged by the research receiver cannot be recovered
retroactively. `.pyneolink_state.json` connection entries are imported lazily.
An explicit legacy `Camera(state_path="cache.json")` imports to `cache.db`;
`state_path=None` still disables the connection cache. The shared `storage_dir`
SDK argument and global CLI `--storage-dir` / `--storage_dir` select the SQLite
directory for camera connection state, streaming, and push. Use the same directory
and camera name to keep all of that state in the same database. Unsafe camera
filename characters are replaced with a stable hash suffix; a DB identity check
prevents reuse for another camera. Back up databases only while SDK processes
are stopped, or use SQLite's online backup API.

To keep all SDK state inside a project, pass a custom directory on every command:

```shell
python -m pyneolink.cli --camera "Home-Front" push read --storage_dir ./storage --json
python -m pyneolink.cli --camera "Home-Front" push watch --storage_dir ./storage --json
python -m pyneolink.cli --storage_dir ./storage --camera "Home-Front" info
python -m pyneolink.cli serve --storage_dir ./storage --config config.json
```

SDK equivalents (all use the same per-camera SQLite files):

```python
camera = Camera(config, storage_dir="./storage")
push = PushClient(config, storage_dir="./storage")
server = StreamServer("config.json", storage_dir="./storage")
```

Read/watch without camera credentials also supports
`PushClient(camera_name="Home-Front", storage_dir="./storage")`.
Paths may be absolute or relative
to the working directory; `~` expands to the current user's home. Missing
directories are created. The default remains `~/pyneolink`.

Changing the path does **not** move an existing receiver or subscription. Reuse
the same directory for init/subscribe/status/read/watch/unsubscribe; do not run
two copied receiver databases concurrently. An explicit `--database` takes
precedence for push. For backward compatibility, a nondefault explicit
`state_path` / `--state` overrides the connection-cache location, and
`state_path=None` disables that cache even when storage_dir is supplied.
Keep project-local storage out of version control: it contains receiver
credentials and private notifications.

Normal SDK operations write persistent state only to SQLite. Legacy JSON files
are import sources, not active state stores. User configuration files, downloaded
media, and explicitly requested logs/raw JSONL captures remain ordinary files;
they are not internal state and are not redirected by storage_dir.

### Python API

```python
from pyneolink import PushClient, load_config

config = load_config("config.json").camera("Home-Back")
push = PushClient(config)
result = push.subscribe()
print(result.status, result.ok)
print(push.status().info())

for event in push.read(limit=1000):
    print(event.event_type, event.title, event.server_time)
# Repeated read returns only the remaining/new unread events.

with push.watch(skip_previous=True, reconnect_delay=30) as events:
    for event in events:
        if event.low_battery():
            print(event.camera_name, event.title, event.body)
        elif event.is_people():
            print("Person detected", event.camera_uid)
        elif event.is_motion():
            print("Motion detected", event.event_type)
        # Forward to your own queue, bot, or other application here.

# Explicitly call push.unsubscribe() when the subscription should be removed.
```

Use `PushClient(camera_name="Home-Back")` without camera config for read/watch-only
programs. `PushEvent.info()` includes summary fields and the notification title;
`PushResult.info()` returns sanitized operation details.

`PushEvent` provides:

- `is_people()`, `is_vehicle()`, and `is_test()` for their exact event codes.
- `is_motion()` for `MD`, `PEOPLE`, or `VEHICLE`; `is_general_motion()` for `MD`
  alone. Unrecognized codes are preserved, not guessed to be motion.
- `low_battery()` for `LPC` (confirmed on Argus Eco) and `LP` (mapped to
  `LowBatteryTip` in the official Android app, not physically verified here).
  Neither supplies a battery percentage or promises a universal threshold.
- Properties `title`, `body`, `language`, `camera_name`, `channel_name`, and
  `device_model`; missing values are `None`. Device/channel names are taken from
  the message, not from the local config alias.
- `server_time` as a timezone-aware UTC datetime, or `None` when invalid/missing.
  Original strings remain in `server_time_raw` and `device_time_raw`.
- `options` and `custom_config` as decoded dictionaries, or `None` when absent
  or malformed. Unknown fields remain in the complete response.
- `get_response()` or `info(full_response=True)` for a deep copy of all decrypted
  envelope fields, including unknown ones. `response` is also available directly;
  treat it as read-only. Older rows without captured envelopes return `None`.

Raw responses are stored on disk in SQLite, not an unbounded RAM list. They can
contain private text and URLs; protect database files and diagnostic exports.
The unread inbox/history and raw JSONL files currently have no automatic retention
limit. `read(limit=...)` bounds the returned batch; consume it before requesting
another rather than accumulating all batches in memory.

`with` closes the receiver, not the subscription. The synchronous iterator uses
one receiver thread plus a lease-heartbeat thread and a bounded queue. SDK construction is offline
and importing `pyneolink` does not import optional FCM packages.
See [examples/push.py](examples/push.py) for separate initialization, subscription,
status, unsubscribe, read and watch functions. It also accepts the same subcommands as
`pyneolink push`. The old `push_*_probe.py` examples have been consolidated there.

## FFmpeg

FFmpeg is a system dependency for audio-file voice playback and some media conversion paths. PyNeolink expects both `ffmpeg` and `ffprobe` to be available in `PATH`.

Official FFmpeg download page:

- Windows: https://ffmpeg.org/download.html
- Linux: https://ffmpeg.org/download.html
- macOS: https://ffmpeg.org/download.html

On Linux and macOS, using the OS package manager is usually the simplest path when available. On Windows, install one of the builds linked from the official FFmpeg download page and add its `bin` directory to `PATH`.

## Configuration

Create a local `config.json`:

```json
{
  "bind": "0.0.0.0",
  "bind_port": 8554,
  "cameras": [
    {
      "name": "Home-Front",
      "username": "admin",
      "password": "password",
      "uid": "ABCDEF0123456789",
      "discovery": "relay"
    }
  ]
}
```

`config.json` is ignored by Git because it can contain camera credentials.

## CLI

Camera info:

```powershell
python pyneolink/cli.py info --camera "Home-Front" --config config.json
python pyneolink/cli.py --info --camera "Home-Front"
```

Battery:

```powershell
python pyneolink/cli.py battery --camera "Home-Front"
python pyneolink/cli.py battery --camera "Home-Front" --watch --interval 60
python pyneolink/cli.py battery --camera "Home-Front" --watch --interval 60 --mode online
```

Snapshots and local recording:

```powershell
python pyneolink/cli.py snapshot --camera "Home-Front" --out snapshots/
python pyneolink/cli.py record --camera "Home-Front" --out recordings/ --duration 30 --quality high
python pyneolink/cli.py record --camera "Home-Front" --out recordings/live.ts --quality low
```

`record` opens the selected live stream and saves it locally as MPEG-TS. Omit
`--duration` to record until Ctrl+C. This is not a command to force the camera
to start writing a new SD-card recording.

Motion:

```powershell
python pyneolink/cli.py motion --camera "Home-Front"
python pyneolink/cli.py motion --camera "Home-Front" --watch --duration 30
```

PIR:

```powershell
python pyneolink/cli.py pir --config config.json --camera "Home-Front" status
python pyneolink/cli.py pir --config config.json --camera "Home-Front" on
python pyneolink/cli.py pir --config config.json --camera "Home-Front" off
```

IR light:

```powershell
python pyneolink/cli.py ir --config config.json --camera "Home-Front" status
python pyneolink/cli.py ir --config config.json --camera "Home-Front" on
python pyneolink/cli.py ir --config config.json --camera "Home-Front" off
python pyneolink/cli.py ir --config config.json --camera "Home-Front" auto
python pyneolink/cli.py led --config config.json --camera "Home-Front" auto
```

PTZ presets:

```powershell
python pyneolink/cli.py ptz --config config.json --camera "Home-Front" presets
python pyneolink/cli.py ptz --config config.json --camera "Home-Front" preset 3
python pyneolink/cli.py ptz --config config.json --camera "Home-Front" preset garage
```

The tested Argus PT Ultra requires a snapshot exchange after login before it
accepts PTZ preset recall. The SDK caller must perform that snapshot and recall
in the same `Camera` session; `goto_preset()` does not do it automatically.
On that model, the standalone `ptz preset` CLI command may still return `400`
because it does not currently perform that snapshot warm-up automatically.

For battery cameras reporting device type `wifi_solo_ipc`, entering
`with Camera(...)` also waits for the first unsolicited `BatteryList` status
event after login. This prevents an ordinary command from racing the camera's
session initialization. It is separate from the Argus PT Ultra snapshot
prerequisite above.

Voice and siren:

```powershell
python pyneolink/cli.py voice --camera "Home-Front" --file alert.mp3
python pyneolink/cli.py voice --camera "Home-Front" --microphone --seconds 10
python pyneolink/cli.py voice --camera "Home-Front" --tone 1000 --seconds 3
python pyneolink/cli.py voice --camera "Home-Front" --siren
```

Live view server:

```powershell
python pyneolink/cli.py serve --config config.json
```

Open direct MPEG-TS in VLC/ffplay:

```text
http://127.0.0.1:8554/Home-Front/high
http://127.0.0.1:8554/Home-Front/low
```

Open HLS timeshift in VLC/ffplay:

```text
http://127.0.0.1:8554/Home-Front/high/hls.m3u8
http://127.0.0.1:8554/Home-Front/low/hls.m3u8
```

HLS keeps a sliding in-memory buffer. By default it stores up to 100 MB and cuts segments around 2 seconds:

```powershell
python pyneolink/cli.py serve --config config.json --hls-buffer-mb 100 --hls-segment-seconds 2
```

When binding to `0.0.0.0`, do not open `0.0.0.0` in VLC. Use `127.0.0.1` on the same PC or the PC's LAN IP from another device.

## Library Use

`with Camera(...) as camera:` opens the camera session and starts the internal
message dispatcher automatically. This lets one `Camera` object handle live
stream reads, motion events, and ordinary status/config commands without the
caller managing the socket reader manually.

Camera information:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    info = camera.info()
    print(info)
```

Battery:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    battery = camera.battery()

    with battery.info() as info:
        print(info["level_percent"], info["is_charging"], info["adapter_status"])

    with battery.info(interval=60, mode="reconnect", count=3) as updates:
        for update in updates:
            print(update["level_percent"], update["adapter_status"])
```

SD-card download:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    sd = camera.sd_card()
    videos = sd.files(start="2026-06-03", end="2026-06-03", name=".mp4")
    print(videos.count(), videos.names(), videos.size)

    videos.sort(key="size", reverse=True)
    videos = videos.limit_size(2 * 1024**3)

    result = videos.download(
        "downloads",
        quality="high",
        rewrite_exists=False,
        progress=True,
        delayed_reconnect_retries=2,
        delayed_reconnect_delay=120,
        notify_on_finish=True,
    )
    print(result.info())

    failed = result.failed_list()
    if failed:
        failed.download("downloads", quality="high", rewrite_exists=False)
```

`SdCard.files()` returns a list-compatible `SDFileCollection`: existing
iteration, indexing, and slicing continue to work. Its batch `download()`
stores only result metadata, so completed media is never retained in RAM.

Motion:

```python
from pyneolink import Camera, EVENTS

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    motion = camera.motion()
    print(motion.status())

    with motion.watch(duration=30) as events:
        for event in events:
            if event == EVENTS.human and event.active:
                print("human detected")
```

Snapshot:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    camera.snapshot(out="snapshots")
    image = camera.snapshot()
```

Local recording from the SDK:

```python
import time

from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    path = camera.record(out="recordings", duration=30, stream="mainStream")
    print(f"saved to {path}")

    with camera.record(out="recordings/live.ts", stream="subStream") as recorder:
        time.sleep(10)
    print(f"saved to {recorder.path}")
```

`Camera.record()` records the live stream on the client machine and writes
MPEG-TS (`.ts`). With `duration`, it waits and returns the final `Path`. Without
`duration`, it returns a running `StreamRecorder`; use it as a context manager
or call `stop()` to finalize the file. This does not enable or disable the
camera's own SD-card recording.

Voice and siren:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    voice = camera.voice()
    voice.play("alert.mp3")
    voice.siren()
```

Settings and PIR:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    settings = camera.settings()
    print(settings.pir.status())
    settings.pir.on()
    settings.pir.off()
    print(settings.ir.status())
    settings.ir.on()
    settings.ir.off()
    settings.ir.auto()
```

PTZ presets:

```python
from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    print(camera.ptz().presets())
    # Required by the tested Argus PT Ultra before preset recall.
    camera.snapshot()
    camera.ptz().goto_preset(3)
```

The tested Argus PT Ultra requires a snapshot exchange after login before it
accepts PTZ preset recall. The caller must perform and discard that snapshot in
the same session; `goto_preset()` intentionally has no model-specific side
effects and does not start a continuous video stream.

Live stream server from a dict:

```python
from pyneolink import StreamServer

config = {
    "bind": "0.0.0.0",
    "bind_port": 8554,
    "cameras": [
        {
            "name": "Home-Front",
            "username": "admin",
            "password": "password",
            "uid": "ABCDEF0123456789",
            "discovery": "relay",
        }
    ],
}

StreamServer(config, buffer_seconds=1.5, hls_buffer_mb=100, hls_segment_seconds=2).serve_forever()
```

## Examples

See the `examples/` directory:

- `camera_example.py`: info, snapshot, LED, and guarded reboot helpers
- `sd_card_example.py`: list, filter, download, preview playback, remove, and guarded format calls
- `battery_example.py`: one-shot battery status plus reconnect/online polling
- `motion_example.py`: motion status and watch mode
- `record_example.py`: duration and manual local stream recording
- `voice_example.py`: file, microphone, tone, and siren helpers
- `settings_example.py`: PIR and IR status plus guarded setting helpers
- `ptz_example.py`: list and recall stored PTZ presets
- `stream_example.py`: live MPEG-TS and HLS timeshift server from a dict config

Each example keeps camera settings as a small local dict near the top of the file. Edit those values directly or replace the dict with your own configuration loader.

## Internals

See `docs/` for a sorted internal documentation set that explains the core files, connection flow, login, encryption, Baichuan messages, UDP/P2P transport, SD-card downloads, media streaming, motion, voice, and camera controls.

## Credits

PyNeolink exists because of the reverse-engineering work done by the Neolink community. In particular, the Rust `neolink` implementations and documentation were used as the protocol reference for this Python port.

This Python implementation was developed with OpenAI Codex. Human testing against real cameras guided the implementation and bug fixes.

## Reference

The Rust `neolink/` checkout can be kept locally as a protocol reference, but it is ignored by Git in this workspace. The Python code here is intended to be understandable and hackable for people who want to experiment with Reolink cameras from Python.

## Tested Cameras

- Reolink Argus Eco
