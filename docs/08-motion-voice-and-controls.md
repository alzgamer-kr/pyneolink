# Motion, Voice, And Controls

This page describes higher-level camera features built on top of `Camera.command()`, stream payloads, and Baichuan messages.

## Motion

`Camera.motion()` returns a `Motion` object.

Current state:

```python
status = camera.motion().status(timeout=3.0)
```

Watch mode:

```python
with camera.motion().watch(duration=30) as events:
    for event in events:
        print(event.type, event.active)
```

Implementation notes:

- `Motion.watch()` creates a `CameraEvents` iterator.
- `CameraEvents.start()` acquires `camera.require_online()`.
- It sends `MSG.MOTION_REQUEST` once.
- It keeps the channel alive with `MSG.UDP_KEEPALIVE`.
- Incoming `MSG.MOTION` XML is parsed by `parse_motion_events()`.
- `EVENTS.human`, `EVENTS.vehicle`, `EVENTS.motion`, `EVENTS.unknown`, and `EVENTS.none` normalize camera-specific values.
- A camera may send `none stop`; PyNeolink normalizes that stop event to the last active event type when possible.

The old CLI `events` command is kept as a compatibility alias. SDK code should use `camera.motion().status()` or `camera.motion().watch()`.

## Voice And Talk

`Camera.voice()` returns a `Voice` object.

Supported sources:

- `voice.play("file.mp3")`
- `voice.microphone(seconds=10)`
- `voice.tone(frequency=1000, seconds=3)`

Implementation notes:

- `Voice.ability()` sends `MSG.TALKABILITY` and parses `<TalkAbility>`.
- The tested camera reports ADPCM, 16000 Hz, mono, 16-bit source.
- `Voice._start()` sends `MSG.TALKCONFIG`.
- Audio is converted to Reolink-compatible IMA ADPCM blocks in `pyneolink/internal/voice.py`.
- Talk packets are sent with `MSG.TALK`.
- `Voice.stop()` sends `MSG.TALKRESET` and drains old talk replies.

File playback uses FFmpeg/FFprobe for validation and conversion. Microphone mode uses `sounddevice`.

## Siren

`camera.voice().siren()` sends one `MSG.PLAY_AUDIO` command with the built-in siren mode.

The current implementation intentionally keeps this simple because the tested battery camera plays a built-in siren clip and stops by itself.

## Snapshot

`camera.snapshot(out="snapshots")` asks the camera for a JPEG snapshot. It can also return bytes when `out` is omitted.

## LED And Reboot

Basic controls live on `Camera`:

- `camera.led()` reads LED state XML.
- `camera.led("on")` and `camera.led("off")` send LED state commands.
- `camera.reboot()` sends a reboot command.

Examples keep reboot guarded because it intentionally interrupts the camera.
