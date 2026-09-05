"""Stored PTZ preset support for Baichuan cameras."""

from __future__ import annotations

from dataclasses import dataclass
import xml.etree.ElementTree as ET

from .core.bc import ProtocolError, find_text
from .core.const import MSG, msg, payloads


@dataclass(frozen=True)
class PtzPreset:
    """One PTZ preset reported by the camera."""

    id: int
    name: str | None
    enabled: bool | None


class Ptz:
    """Read and recall PTZ presets through the camera's shared dispatcher."""

    def __init__(self, camera, *, channel_id: int | None = None) -> None:
        """Create a stored-preset helper for one camera channel."""
        self.camera = camera
        self.channel_id = camera.config.channel_id if channel_id is None else channel_id

    def presets(self) -> tuple[PtzPreset, ...]:
        """Return stored PTZ preset IDs, names, and enabled state."""
        reply = self.camera.command(
            MSG.PTZ_PRESET_LIST,
            extension=payloads.extension.format(channel_id=self.channel_id),
            retry_on_timeout=False,
            reconnect_retries=0,
        )
        if reply.header.response_code != 200:
            raise ProtocolError(msg.Error.PtzPresetListFailed.format(response_code=reply.header.response_code))
        return _parse_preset_list(reply.xml_root)

    def goto_preset(self, preset_id: int | str) -> None:
        """Recall a stored preset by ID or name.

        The command is deliberately not retried after sending: if a response is
        lost, the camera may already be moving and a duplicate recall offers no
        benefit. Some battery camera models require their caller to perform a
        snapshot exchange in the same authenticated session before recall.

        :param preset_id: Numeric preset ID or exact case-insensitive name.
        """
        validated_id = self._resolve_preset_id(preset_id)
        reply = self.camera.command(
            MSG.PTZ_PRESET,
            payloads.ptz_preset.format(channel_id=self.channel_id, preset_id=validated_id),
            extension=payloads.extension.format(channel_id=self.channel_id),
            retry_on_timeout=False,
            reconnect_retries=0,
        )
        if reply.header.response_code != 200:
            raise ProtocolError(msg.Error.PtzPresetRecallFailed.format(response_code=reply.header.response_code))

    def _resolve_preset_id(self, preset_id: int | str) -> int:
        if not isinstance(preset_id, str):
            return _validate_preset_id(preset_id)

        name = preset_id.strip()
        if not name:
            raise ValueError(msg.Error.PtzPresetName)

        matches = [preset for preset in self.presets() if (preset.name or "").casefold() == name.casefold()]
        if not matches:
            raise ValueError(msg.Error.PtzPresetNotFound.format(name=name))
        if len(matches) > 1:
            raise ValueError(msg.Error.PtzPresetAmbiguous.format(name=name))
        return matches[0].id


def _validate_preset_id(preset_id: int) -> int:
    if isinstance(preset_id, bool) or not isinstance(preset_id, int) or not 0 <= preset_id <= 255:
        raise ValueError(msg.Error.PtzPresetId)
    return preset_id


def _parse_preset_list(root: ET.Element | None) -> tuple[PtzPreset, ...]:
    if root is None:
        raise ProtocolError(msg.Error.PtzPresetListMalformed)

    presets: list[PtzPreset] = []
    for element in root.findall(".//presetList/preset"):
        raw_id = find_text(element, "id")
        if raw_id is None:
            raise ProtocolError(msg.Error.PtzPresetListMalformed)
        try:
            preset_id = _validate_preset_id(int(raw_id))
        except ValueError as exc:
            raise ProtocolError(msg.Error.PtzPresetListMalformed) from exc
        raw_enabled = find_text(element, "enable")
        enabled = None if raw_enabled is None else raw_enabled.strip().lower() not in {"0", "false"}
        name = find_text(element, "name")
        presets.append(PtzPreset(id=preset_id, name=name, enabled=enabled))
    return tuple(presets)
