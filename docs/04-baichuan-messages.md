# Baichuan Messages

Baichuan - це протокольний шар, у якому PyNeolink надсилає команди і читає відповіді камери.

Низькорівнева реалізація: `pyneolink/core/bc.py`.

Верхній API для команд: `Camera.command()` і `Camera.send()`.

## Header

`Header.pack()` пише little-endian структуру:

```text
magic        uint32
msg_id       uint32
body_len     uint32
channel_id   uint8
stream_type  uint8
msg_num      uint16
response     uint16
msg_class    uint16
[payload_offset uint32 для modern/file/replay]
```

Valid magic:

- `0x0ABCDEF0`;
- `0x0FEDCBA0`.

Якщо magic інший, кидається `InvalidMagicError`. Download code іноді обробляє це як raw tail, бо деякі камери після Baichuan response можуть продовжити media bytes без стандартного header.

## Message classes

Основні класи:

- `MSG_CLASS.LEGACY = 0x6514`: перший legacy login;
- `MSG_CLASS.MODERN = 0x6414`: звичайні XML-команди;
- `MSG_CLASS.MODERN_REPLY = 0x6614`: modern reply class;
- `MSG_CLASS.FILE_DOWNLOAD = 0x6482`: file download payload;
- `MSG_CLASS.MODERN_ZERO = 0x0000`: деякі відповіді камери.

Для modern/file/replay header має `payload_offset`. Це довжина extension частини перед основним payload.

## Message ids

Найважливіші ids у поточному коді:

- `MSG.LOGIN = 1`;
- `MSG.VIDEO = 3`;
- `MSG.VIDEO_STOP = 4`;
- `MSG.FILE_REPLAY = 5`;
- `MSG.FILE_REPLAY_STOP = 7`;
- `MSG.FILE_DOWNLOAD_VIDEO = 8`;
- `MSG.FILE_DOWNLOAD = 13`;
- `MSG.FILE_INFO_LIST = 14`;
- `MSG.FILE_INFO_LIST_ALT = 15`;
- `MSG.FILE_INFO_LIST_ALT2 = 16`;
- `MSG.REBOOT = 23`;
- `MSG.HDD_INFO = 102`;
- `MSG.HDD_INIT = 103`;
- `MSG.UID = 114`;
- `MSG.REPLAY_SEEK = 123`;
- `MSG.DAY_RECORDS = 142`;
- `MSG.FILE_PLAYBACK = 143`;
- `MSG.FILE_PLAYBACK_STOP = 144`;
- `MSG.GET_LED = 208`;
- `MSG.SET_LED = 209`;
- `MSG.UDP_KEEPALIVE = 234`;
- `MSG.BATTERY = 253`.

## Request lifecycle

Звичайний command flow:

1. `Camera.command(msg_id, payload, extension=...)`
2. `ensure_connected()`
3. `send()`
4. `encode_modern()`
5. socket `sendall()`
6. loop `_recv()` until response with the same `msg_num`
7. `recv_message()` returns `Message`
8. if `msg_num` matched, command returns it

`msg_num` - це correlation id. Камера може прислати unrelated packet, keepalive або stream data. `Camera.command()` ігнорує unmatched `msg_num`, поки не дочекається потрібної відповіді або timeout.

## `send()` vs `command()`

`command()` підходить для XML request -> XML response.

`send()` тільки відправляє packet і повертає `msg_num`. Воно використовується для:

- stream start/stop;
- download, де відповідей багато;
- keepalive;
- випадків з custom `msg_class`, `channel_id`, `msg_num`.

## Extension

`extension_xml()` створює XML extension:

```xml
<Extension version="1.1">
  <channelId>0</channelId>
  <binaryData>1</binaryData>
</Extension>
```

Extension шифрується окремо від payload. У header `payload_offset` показує, де закінчується extension і починається payload.

## XML payload

`xml_document(inner)` загортає XML у:

```xml
<?xml version="1.0" encoding="UTF-8" ?>
<body>
...
</body>
```

Більшість запитів до камери - це XML payload.

## Response handling

`recv_message()`:

1. читає header;
2. дочитує optional `payload_offset`;
3. читає body;
4. розділяє body на extension і payload;
5. вибирає cipher для reply;
6. дешифрує extension;
7. визначає, чи payload binary;
8. дешифрує або залишає payload raw;
9. повертає `Message`.

Для XML відповідей `Message.xml_text` і `Message.xml_root` дають готовий доступ до тексту/XML tree.

## Keepalive

Якщо `_recv()` отримує `MSG.UDP_KEEPALIVE`, камера відповідає автоматично через `_reply_keepalive()`.

Для UDP stream/download також є активні keepalive packets:

- `Camera.read_stream_payloads()` періодично відправляє `MSG.UDP_KEEPALIVE`;
- `SdCard._send_download_keepalive()` робить те саме під час download.
