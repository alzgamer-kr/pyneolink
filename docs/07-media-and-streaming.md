# Media And Streaming

The media path is split between:

- `pyneolink/core/media.py`: BCMedia packet parsing and MP4 conversion helpers.
- `pyneolink/camera.py`: live stream start/stop and raw payload iteration.
- `pyneolink/stream_server.py`: HTTP MPEG-TS and HLS timeshift server.
- `pyneolink/recorder.py`: local MPEG-TS recording from the live stream.
- `pyneolink/internal/snapshot.py`: snapshot metadata and output path helpers.

## Live Stream Start

`Camera.start_stream(stream)`:

1. normalizes the stream name through `stream_params()`;
2. builds a `<Preview>` XML payload;
3. sends `MSG.VIDEO`;
4. waits for a response with the same `msg_num`;
5. adds that `msg_num` to `binary_msg_nums` when the response is accepted.

Stream aliases:

- `high`, `main`, `mainStream`, `clear` -> `mainStream`
- `low`, `sub`, `subStream`, `fluent` -> `subStream`
- `extern`, `externStream` -> `externStream`

For `mainStream` the preview handle is `0`. For `subStream` the preview handle is `256`.

## Stream Payload Loop

`Camera.read_stream_payloads(stream)`:

1. acquires an online lease with `require_online()`;
2. starts the stream;
3. periodically sends `MSG.UDP_KEEPALIVE`;
4. reads `_recv(timeout=1.0)`;
5. yields payloads from `MSG.VIDEO` messages with the stream `msg_num`;
6. tries to stop the stream in `finally`.

The generator yields raw BCMedia payloads. It does not parse H264/H265 itself.

## MediaParser

`MediaParser.feed(data)` accepts bytes and yields `MediaPacket` objects.

Known packet types:

- `1001`, `1002`: stream info, width, height, fps.
- `?0dc`: iframe H264/H265.
- `?1dc`: pframe H264/H265.
- `05wb`: AAC.
- `01wb`: ADPCM.

When the parser loses sync, `_resync()` searches for the next known packet magic.

## Direct HTTP Stream

`StreamServer` exposes:

```text
/{camera}/{quality}
```

Examples:

```text
/Home-Front/high
/Home-Front/low
```

The handler:

1. finds the camera config;
2. converts `quality` to `mainStream` or `subStream`;
3. opens a `Camera`;
4. logs in;
5. reads stream payloads;
6. waits for the first keyframe;
7. buffers startup frames;
8. writes MPEG-TS to the HTTP client.

## MPEG-TS Muxing

`MpegTsMuxer` writes:

- H264 or H265 video;
- AAC audio when packets contain ADTS AAC.

The muxer writes:

- PAT PID `0x0000`;
- PMT PID `0x0100`;
- video PID `0x0101`;
- audio PID `0x0102`.

If a camera stream pauses briefly, the direct server can write MPEG-TS null packets so players are less likely to close the stream immediately.

## HLS Timeshift

HLS URL:

```text
/{camera}/{quality}/hls.m3u8
```

Segments:

```text
/{camera}/{quality}/segments/{sequence}.ts
```

`HlsSession`:

1. starts a background thread;
2. opens a separate `Camera`;
3. reads stream payloads;
4. parses `MediaPacket` objects;
5. muxes them into MPEG-TS;
6. cuts segments around `hls_segment_seconds`;
7. keeps a sliding memory buffer up to `hls_buffer_bytes`.

The playlist uses active in-memory segments and gives a timeshift behavior: the player can lag behind live stream while the buffer moves forward with new segments.

## Snapshot

`Camera.snapshot(out=None, stream_type="main")`:

1. sends the requested explicit snapshot `stream_type`;
2. sends `MSG.SNAP` with a snapshot payload and channel extension;
3. reads XML metadata with file name and expected picture size;
4. reads binary snapshot payloads until response code `201`;
5. validates size when the camera reported it;
6. returns bytes or writes a JPEG file when `out` is provided.

## Local Recording

`Camera.record(out=..., duration=..., stream="mainStream")` creates a `StreamRecorder`.

This is client-side recording. PyNeolink starts the camera live stream, muxes the
received media packets into MPEG-TS, and writes a local `.ts` file. It does not
send a camera command that enables or disables the camera's own SD-card
recording.

SDK behavior:

- `out` can be a directory or a complete file path. Directories get an automatic
  `recording-YYYYMMDD-HHMMSS.ts` file name.
- `stream` accepts the live stream name, usually `mainStream` or `subStream`.
- With `duration`, `Camera.record()` waits for completion and returns the saved
  `Path`.
- Without `duration`, `Camera.record()` returns a running `StreamRecorder`.
  Call `stop()` to request shutdown and finalize the file.
- `StreamRecorder` can also be used as a context manager.

The recorder:

1. starts the camera stream;
2. parses BCMedia packets;
3. waits for an iframe before writing;
4. muxes packets into MPEG-TS;
5. writes directly to disk;
6. flushes periodically;
7. stops the stream on exit.

SDK examples:

```python
import time

from pyneolink import Camera

with Camera(uuid="ABCDEF0123456789", username="admin", password="password") as camera:
    path = camera.record(out="recordings", duration=30, stream="mainStream")

    with camera.record(out="recordings/live.ts", stream="subStream") as recorder:
        time.sleep(10)
```

When `duration` is omitted, the caller controls `recorder.stop()`. The CLI
catches Ctrl+C and stops the recorder so the file is finalized as cleanly as
possible.

CLI examples:

```powershell
python pyneolink/cli.py record --camera "Home-Front" --out recordings/ --duration 30 --quality high
python pyneolink/cli.py record --camera "Home-Front" --out recordings/live.ts --quality low
```

## SD-Card BCMedia Conversion

SD-card download can return BCMedia instead of a ready MP4. In that case `core/media.py`:

1. parses BCMedia packets;
2. extracts H264/H265 frames to a temporary raw stream;
3. runs `ffmpeg -c copy`;
4. creates MP4 without re-encoding.

If an MP4 box is already embedded in the downloaded bytes, `extract_embedded_mp4()` can extract it directly.
