# PTZ Presets

`camera.ptz()` returns a `Ptz` helper for stored preset slots. It intentionally
does not implement arbitrary directional movement.

```python
presets = camera.ptz().presets()

# Required by the tested Argus PT Ultra before preset recall.
camera.snapshot()
camera.ptz().goto_preset(3)
```

## Protocol

Preset listing sends Baichuan message `190` (`MSG.PTZ_PRESET_LIST`) with the
normal channel extension. The response contains `<PtzPreset><presetList>`
entries with an ID, optional name, and optional enabled flag.

Preset recall sends Baichuan message `19` (`MSG.PTZ_PRESET`) with a modern
encrypted XML payload:

```xml
<PtzPreset version="1.1">
  <channelId>0</channelId>
  <presetList><preset><id>3</id><command>toPos</command></preset></presetList>
</PtzPreset>
```

The request uses the standard channel extension and normal `Camera.command()`
framing/encryption. A response code of `200` means the camera accepted the
recall; the helper does not stay connected to wait for physical movement.

## Argus PT Ultra snapshot prerequisite

On the tested battery-powered Argus PT Ultra, a `PtzPreset/toPos` request sent
immediately after login receives response `400`. The same request receives
`200` after one `Camera.snapshot()` exchange in the same authenticated session.
SDK callers targeting that model must perform and discard the snapshot before
the recall. `Ptz.goto_preset()` intentionally does not do this automatically:
it has no model-specific side effects and does not start continuous video.

This behavior is verified on that model and firmware only. Other models may not
need the snapshot prerequisite, so applications should apply it only where
their camera model requires it.
