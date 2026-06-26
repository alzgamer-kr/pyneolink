# Core Map

`pyneolink/core` contains the low-level protocol pieces. Public modules such as `camera.py`, `sd_card.py`, `battery.py`, `motion.py`, `voice.py`, `recorder.py`, and `stream_server.py` use these primitives instead of building UDP packets, Baichuan headers, or encryption directly.

## `pyneolink/core/const/flags.py`

Typed protocol constants:

- `MAGIC`: Baichuan, discovery, UDP ACK/DATA magic values.
- `MSG_CLASS`: Baichuan message classes.
- `MSG`: camera message ids such as login, video, battery, snapshot, motion, talk, and SD-card messages.
- `EVENTS`: normalized motion event names.
- Media and voice helper constants.

Most values are `IntEnum`/`StrEnum`, so code can use names like `MSG.LOGIN` while remaining compatible with integer packing and comparisons.

## `pyneolink/core/const/payloads.py`

Preformatted XML payload templates. Examples:

- `payloads.login.format(username=..., password=...)`
- `payloads.extension.format(channel_id=...)`
- `payloads.preview_start.format(...)`
- SD-card list/download payloads
- snapshot, motion, talk, and siren payloads

This keeps inline XML strings out of the public modules.

## `pyneolink/core/const/msg.py`

Centralized text for errors and logs:

- `msg.Error`: user-facing and exception text.
- `msg.Log`: CLI/debug text.

Parameterized messages use normal `.format(...)`, for example `msg.Error.LoginFailed.format(response_code=400)`.

## `pyneolink/core/bc.py`

Baichuan framing:

- `Header`: packs and unpacks the Baichuan header.
- `Message`: parsed message with header, extension, payload, and length metadata.
- `encode_legacy_login()`: first login packet that negotiates encryption.
- `encode_modern()`: normal Baichuan packet writer.
- `recv_message()`: reads, decrypts, and parses a response.

Public code should usually work with `Message`, not raw bytes.

## `pyneolink/core/crypto.py`

Cryptography and protocol checksums:

- `md5_hex()`: login hash helper.
- `make_aes_key()`: derives an AES key from nonce and password.
- `bc_xor()`: Baichuan XOR payload encryption.
- `udp_xor()`: discovery XML XOR.
- `neolink_crc32()`: discovery packet checksum.
- `Cipher`: wrapper for `none`, `bc`, and `aes`.

`Cipher.encrypt()` is used by `encode_modern()`. `Cipher.decrypt()` is used by `recv_message()`.

## `pyneolink/core/discovery.py`

Local and remote UID discovery:

- `local_discover()`: LAN discovery.
- `remote_uid_lookup()`: Reolink P2P server lookup.
- `DiscoveryHit`: normalized result containing address, UID, source, and transport.

Discovery only finds candidates. The actual UDP Baichuan channel is opened by `udp_transport.py`.

## `pyneolink/core/udp_transport.py`

Socket-like UDP transport for Baichuan:

- `connect_local_direct()`: direct local UDP P2P handshake.
- `connect_relay()`: UID lookup, register server handshake, relay/local/map candidate selection.
- `UdpBcConnection`: socket-like wrapper with `sendall()`, `recv()`, `settimeout()`, and `close()`.
- ACK, resend, heartbeat, pending chunk buffering, and debug snapshots.

Upper layers can pass `UdpBcConnection` to `recv_message()` almost like a TCP socket.

## `pyneolink/core/state.py`

Small JSON cache for the last working camera address:

- reads and writes `.pyneolink_state.json`;
- stores address, transport, UID, and timestamp;
- helps repeat connections avoid unnecessary discovery when a known address still works.

## `pyneolink/core/xmlutil.py`

XML to Python dictionary conversion used by `Camera.info()` and other APIs that expose parsed camera responses.

## `pyneolink/core/media.py`

BCMedia parsing and conversion:

- `MediaParser.feed()`: bytes to `MediaPacket` objects.
- H264/H265 iframe/pframe parsing.
- AAC and ADPCM packet detection.
- `bcmedia_to_mp4()` through FFmpeg.
- `extract_embedded_mp4()` fallback for payloads containing an MP4 box.

This module does not know about camera login or transport. It only handles media bytes.

## Public modules above `core`

- `pyneolink/camera.py`: connection, login, command API, snapshot, stream start/stop, high-level module factories.
- `pyneolink/sd_card.py`: SD-card listing, pagination, filtering, download, guarded remove/format.
- `pyneolink/battery.py`: battery XML request, normalization, reconnect/online polling.
- `pyneolink/motion.py`: motion status and event watch iterator.
- `pyneolink/voice.py`: talk ability, microphone/file/tone ADPCM voice, siren trigger.
- `pyneolink/recorder.py`: local stream recording to MPEG-TS.
- `pyneolink/stream_server.py`: live MPEG-TS and HLS timeshift HTTP server.
- `pyneolink/cli.py`: command-line wrapper around the same public APIs.
