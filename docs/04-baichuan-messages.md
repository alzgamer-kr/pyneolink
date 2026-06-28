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

Important IDs in the current code:

- `MSG.LOGIN`
- `MSG.LOGOUT`
- `MSG.VIDEO`
- `MSG.FILE_REPLAY`
- `MSG.FILE_DOWNLOAD`
- `MSG.FILE_DOWNLOAD_VIDEO`
- `MSG.FILE_PLAYBACK`
- `MSG.FILE_PLAYBACK_STOP`
- `MSG.SNAP`
- `MSG.BATTERY`
- `MSG.MOTION_REQUEST`
- `MSG.MOTION`
- `MSG.TALKABILITY`
- `MSG.TALKCONFIG`
- `MSG.TALK`
- `MSG.TALKRESET`
- `MSG.PLAY_AUDIO`
- `MSG.GET_PIR_ALARM`
- `MSG.SET_PIR_ALARM`
- `MSG.GET_LED`
- `MSG.SET_LED`

See `pyneolink/core/const/flags.py` for the full list.

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
