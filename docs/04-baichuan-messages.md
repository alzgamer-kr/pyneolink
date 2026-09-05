# Baichuan Messages

Baichuan is the protocol layer used by PyNeolink to send commands and read camera replies.

Low-level implementation: `pyneolink/core/bc.py`.

High-level command API: `Camera.command()` and `Camera.send()`.

## Header

`Header.pack()` writes a little-endian structure:

```text
magic          uint32
msg_id         uint32
body_len       uint32
channel_id     uint8
reserved       uint8
msg_num        uint16
response_code  uint16
msg_class      uint16
[payload_offset uint32 for modern/file/replay]
```

The magic value is usually:

```text
0x2a87cf10
```

If the magic is different, `InvalidMagicError` is raised. SD-card download code may treat this as a raw media tail because some cameras send media bytes after a Baichuan response without another standard header.

## Message Classes

Important classes:

- `MSG_CLASS.LEGACY = 0x6514`: first legacy login;
- `MSG_CLASS.MODERN = 0x6414`: normal XML commands;
- `MSG_CLASS.FILE_DOWNLOAD = 0x6482`: SD-card file download;
- `MSG_CLASS.FILE_REPLAY = 0x6512`: replay/download path on some cameras;
- `MSG_CLASS.MODERN_ZERO = 0x0000`: some camera replies.

For modern/file/replay messages, the header includes `payload_offset`. It is the length of the extension block before the main payload.

## Message IDs

Message IDs are reverse-engineered. The tables below distinguish confirmed
mappings from working hypotheses and unnamed values extracted from the official
client. A value appearing in the official DLL is not enough to assign it a name.

### Known

These mappings are implemented and tested by PyNeolink, implemented by the
upstream Neolink protocol code, confirmed in captures, or directly tied to a
named serializer in the official SDK.

| ID | Name | Wire role |
|---:|---|---|
| 1 | `LOGIN` | Login and encryption negotiation. |
| 2 | `LOGOUT` | End authenticated session. |
| 3 | `VIDEO` | Start and carry a live video stream. |
| 4 | `VIDEO_STOP` | Stop a live video stream. |
| 5 | `FILE_REPLAY` | Start and carry SD-card replay. |
| 7 | `FILE_REPLAY_STOP` | Stop SD-card replay. |
| 8 | `FILE_DOWNLOAD_VIDEO` | Direct-download binary data. |
| 10 | `TALKABILITY` | Query two-way audio capabilities. |
| 11 | `TALKRESET` | Reset a talk session. |
| 13 | `FILE_DOWNLOAD` | Start direct file download/request metadata. |
| 18 | `PTZ_CONTROL` | PTZ movement control. |
| 19 | `PTZ_PRESET` | Recall or modify a PTZ preset. |
| 20 | `PTZ_CRUISE` | Configure a PTZ patrol/cruise and its preset sequence. |
| 23 | `REBOOT` | Reboot command. |
| 31 | `MOTION_REQUEST` | Start/query motion events. |
| 33 | `MOTION` | Motion event/status messages. |
| 36 | `SET_SERVICE_PORTS` | Write service-port settings. |
| 37 | `GET_SERVICE_PORTS` | Read service-port settings. |
| 42 | `GET_EMAIL` | Read email settings. |
| 43 | `SET_EMAIL` | Write email settings. |
| 58 | `GET_ABILITY_SUPPORT` | Query supported abilities. |
| 59 | `UPDATE_USER_LIST` | Write user list. |
| 78 | `VIDEO_INPUT` | Unsolicited/read response containing `VideoInput` image settings. |
| 79 | `SERIAL` | Unsolicited/read response containing serial/PTZ protocol settings. |
| 80 | `VERSION` | Firmware/version information. |
| 93 | `PING` | Baichuan application-level ping. |
| 102 | `HDD_INFO` | SD-card/storage information. |
| 103 | `HDD_INIT` | Initialize or format storage. |
| 104 | `GET_GENERAL` | Read general camera settings. |
| 105 | `SET_GENERAL` | Write general camera settings. |
| 109 | `SNAP` | Snapshot metadata and JPEG chunks. |
| 114 | `UID` | Camera UID information. |
| 123 | `REPLAY_SEEK` | Seek active replay. |
| 124 | `PUSH_INFO` | Push-notification settings. |
| 141 | `TEST_EMAIL` | Send test email. |
| 142 | `DAY_RECORDS` | Query days containing recordings. |
| 143 | `FILE_PLAYBACK` | Range/playback download and media chunks. |
| 144 | `FILE_PLAYBACK_STOP` | Stop range/playback download. |
| 146 | `STREAM_INFO_LIST` | Query stream profiles. |
| 151 | `ABILITY_INFO` | Detailed camera abilities. |
| 190 | `PTZ_PRESET_LIST` | Query PTZ presets. |
| 199 | `GET_SUPPORT` | Query supported features. |
| 201 | `TALKCONFIG` | Configure two-way audio. |
| 202 | `TALK` | Two-way audio payload. |
| 208 | `GET_LED` | Read status-light settings. |
| 209 | `SET_LED` | Write status-light settings. |
| 212 | `GET_PIR_ALARM` | Read PIR settings. |
| 213 | `SET_PIR_ALARM` | Write PIR settings. |
| 216 | `SET_EMAIL_TASK` | Write email-task settings. |
| 217 | `GET_EMAIL_TASK` | Read email-task settings. |
| 234 | `UDP_KEEPALIVE` | Reliable-UDP keepalive. |
| 250 | `LONG_TIME_PREVIEW` | Official SDK `LongTimePreview` command. Send timing and camera behavior remain under investigation. |
| 252 | `BATTERY_LIST` | Unsolicited battery-status list used to mark a battery-camera session ready. |
| 253 | `BATTERY` | Battery information. |
| 263 | `PLAY_AUDIO` | Play camera audio/siren. |
| 288 | `FLOODLIGHT_MANUAL` | Manual floodlight control. |
| 290 | `FLOODLIGHT_TASKS_WRITE` | Write floodlight schedule/tasks. |
| 291 | `FLOODLIGHT_STATUS_LIST` | Read floodlight status. |
| 294 | `GET_ZOOM_FOCUS` | Read zoom/focus state. |
| 295 | `SET_ZOOM_FOCUS` | Write zoom/focus state. |
| 438 | `FLOODLIGHT_TASKS_READ` | Read floodlight schedule/tasks. |
| 547 | `SIREN_STATUS_LIST` | Unsolicited `SirenStatusList` status response. |

`LONG_TIME_PREVIEW` is confirmed by `libBCSDKWrapper.dll`: the dispatcher at
`0x18016A980` maps `bccmd=0x896` to `msg_id=0xFA`, and its serializer constructs
the `LongTimePreview`/`Preview`/`continuePreview` XML. This confirms the ID, but
not whether every camera honors it or when it must be sent.

`PTZ_CRUISE` is confirmed by the same dispatcher. Its compressed jump table
maps `bccmd=0x851` to `msg_id=0x14`. Callback factory `FUN_180113c70` references
vtable `0x1803FF978`; its invoke function `FUN_180153CB0` calls serializer
`FUN_18014DE70`, which constructs `PtzCruise` XML with `channelId`, `patrolId`,
`enable`, `valueTable`, and a `keyPosList` containing preset, speed, dwell-time,
and tracking fields.

### Possible

| ID | Working name | Uncertainty |
|---:|---|---|
| 14 | `FILE_INFO_LIST` | File-list variant used by PyNeolink probes; exact firmware/model mapping is not confirmed. |
| 15 | `FILE_INFO_LIST_ALT` | Alternate file-list request; exact semantics overlap with IDs 14/16. |
| 16 | `FILE_INFO_LIST_ALT2` | Alternate file-list request; exact semantics overlap with IDs 14/15. |

### Official SDK Static Catalog

All 190 previously unnamed dispatcher values have now been correlated with an
XML serializer, a named `BCSDK_Remote*` export, or both. The complete mapping,
including internal `bccmd` values and evidence type, is in
[10-official-sdk-message-catalog.md](10-official-sdk-message-catalog.md).

These static names do not by themselves establish request/response direction,
camera-model support, or safe public API behavior. Those details still require
controlled captures and physical tests before implementation.

### Runtime-Confirmed Status Values

Live decrypted dispatcher tracing confirmed the previously uncertain IDs `78`,
`79`, and `547`: their payload roots are respectively `VideoInput`, `Serial`,
and `SirenStatusList`. ID `252` is an unsolicited `BatteryList` update commonly
observed about every six seconds while a battery camera session is active.

The local research reports preserve dispatcher targets, serializers, XML roots,
named SDK exports, and the corrected `bccmd -> msg_id` mappings. The compressed
jump table must be decoded through both selector tables; displayed decompiler
case numbers are not reliable `bccmd` values.

### Identifying an Unknown ID

The packaged `libBCSDKWrapper.dll` contains this CodeView path:

```text
D:\pc\reolink-pc\packages\sdk\build\Windows\Release\libBCSDKWrapper.pdb
```

The matching PDB is not shipped with the official application. Original enum,
class, and internal function names cannot be recovered automatically without
that exact PDB (matching GUID and age) or the original source. A decompiler can
reconstruct control flow and types, but names compiled out of the binary must be
reconstructed from the remaining evidence.

Use independent evidence instead of assigning names from numeric proximity:

1. Find the ID in the `BaichuanConfigurator` dispatcher at `0x18016A980`.
2. Record the internal `bccmd`, message builder, and nearby callback factory.
3. Open the callback factory, follow its vtable reference, and identify the
   request serializer and reply parser functions.
4. Read the XML root and field strings referenced by those functions. This
   normally supplies the strongest static command name.
5. Follow callers in the opposite direction to a named `BCSDK_*` export or a
   `/json_api/` endpoint when one exists.
6. Capture one controlled official-client action and correlate its timestamp,
   direction, `msg_id`, `msg_num`, response, and payload length.

A named serializer plus matching runtime traffic is enough to promote a mapping
to Known. A serializer or capture alone remains Possible. A number extracted
only from the dispatcher remains Unknown.

## Command Flow

Normal command flow:

```python
msg_num = camera.send(msg_id, payload)
reply = camera._recv_expected(msg_num)
```

`msg_num` is the correlation id. A camera may send unrelated packets, keepalive packets, or stream data. `Camera.command()` ignores unmatched `msg_num` values until it receives the expected reply or times out.

## `command()` vs `send()`

`command()` is for one XML request followed by one XML response.

`send()` only writes a packet and returns the `msg_num`. It is used for:

- stream start, where media messages continue afterwards;
- SD-card downloads, where many messages may follow;
- custom `msg_class`, `channel_id`, or `msg_num` cases.

## Extensions

`extension_xml()` creates an XML extension:

```xml
<Extension version="1.1">
  <channelId>0</channelId>
</Extension>
```

Extensions are encrypted separately from payloads. In the header, `payload_offset` marks where the extension ends and the payload begins.

## XML Payloads

`xml_document(inner)` wraps XML into:

```xml
<?xml version="1.0" encoding="UTF-8" ?>
<body>...</body>
```

Most camera requests use XML payloads.

## Receiving

`recv_message(sock, cipher)`:

1. reads the header;
2. reads the optional `payload_offset`;
3. reads the body;
4. splits the body into extension and payload;
5. chooses the reply cipher;
6. decrypts the extension;
7. detects whether the payload is binary;
8. decrypts or keeps the payload raw;
9. returns `Message`.

For XML replies, `Message.xml_text` and `Message.xml_root` provide ready access to the response text/tree.

## Keepalive

If `_recv()` receives `MSG.UDP_KEEPALIVE`, the camera is answered automatically through `_reply_keepalive()`.

UDP stream/download paths also send active keepalive packets:

- `Camera.read_stream_payloads()` periodically sends `MSG.UDP_KEEPALIVE`;
- `SdCard._send_download_keepalive()` does the same during downloads.

## Reliable UDP acknowledgements

Reliable-UDP ACK packets contain the last contiguous packet id, a bitmap for
later packets, and a 32-bit runtime metric. Captures from the official client
show that this metric follows the received UDP payload rate in bytes per
second and is refreshed roughly once per second. It is not the interval
between ACK packets. PyNeolink therefore measures accepted payload bytes over
a one-second window and includes that rate in every outgoing ACK.

This field matters most for relay downloads. Sending an ACK-interval value in
its place produced values around `22000-28000`, while the official client sent
approximately `600000-700000` for a transfer receiving that many payload bytes
per second.
