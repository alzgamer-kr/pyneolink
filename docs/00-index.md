# PyNeolink Internals

This directory documents how PyNeolink is currently structured: the core protocol layer, UID/P2P connection flow, Baichuan login, encryption, commands, media, SD-card downloads, motion events, voice, and camera controls.

Recommended reading order:

1. [01-core-map.md](01-core-map.md): file map for `pyneolink/core` and the public modules above it.
2. [02-connection-flow.md](02-connection-flow.md): how `Camera` finds and connects to a camera.
3. [03-authentication-and-encryption.md](03-authentication-and-encryption.md): login nonce, MD5 login hashes, BC XOR, and AES-CFB.
4. [04-baichuan-messages.md](04-baichuan-messages.md): Baichuan headers, message ids, request/response matching.
5. [05-udp-p2p-transport.md](05-udp-p2p-transport.md): Reolink UID/P2P lookup, register server, relay/local UDP transport.
6. [05-internal-helpers.md](05-internal-helpers.md): helper modules used to keep public modules smaller.
7. [06-sd-card-downloads.md](06-sd-card-downloads.md): SD-card file listing, pagination, and download strategies.
8. [07-media-and-streaming.md](07-media-and-streaming.md): BCMedia parsing, MPEG-TS, HLS timeshift, snapshots, and local recording.
9. [08-motion-voice-and-controls.md](08-motion-voice-and-controls.md): motion status/watch, two-way voice, siren, PIR, IR, and basic camera controls.

This is not official Reolink documentation. It describes the current PyNeolink implementation and the reverse-engineered behavior it relies on.
