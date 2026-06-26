from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from pyneolink.core.bc import ProtocolError, find_text
from pyneolink.core.const import MSG, msg, payloads
from pyneolink.core.const.payloads import Raw
from pyneolink.core.xmlutil import xml_to_dict


_STATE_TO_MODE = {
    "open": "on",
    "close": "off",
    "auto": "auto",
}
_MODE_TO_STATE = {value: key for key, value in _STATE_TO_MODE.items()}


class Ir:
    def __init__(self, camera, *, channel_id: int | None = None) -> None:
        self.camera = camera
        self.channel_id = camera.config.channel_id if channel_id is None else channel_id

    def status(self) -> dict:
        element = self._get_config()
        xml_text = ET.tostring(element, encoding="unicode")
        state = (find_text(element, "state") or "").strip()
        light_state = (find_text(element, "lightState") or "").strip()
        return {
            "mode": _STATE_TO_MODE.get(state, state or None),
            "state": state or None,
            "channel_id": _int_text(element, "channelId"),
            "light_state": light_state or None,
            "raw": xml_to_dict(xml_text),
        }

    def on(self) -> dict:
        return self._set_mode("on")

    def off(self) -> dict:
        return self._set_mode("off")

    def auto(self) -> dict:
        return self._set_mode("auto")

    def _set_mode(self, mode: str) -> dict:
        element = self._get_config()
        state = element.find("state")
        if state is None:
            state = ET.SubElement(element, "state")
        state.text = _MODE_TO_STATE[mode]
        led_version = element.find("ledVersion")
        if led_version is not None:
            element.remove(led_version)
        self._set_config(element)
        return self.status()

    def _get_config(self) -> ET.Element:
        reply = self.camera.command(MSG.GET_LED, extension=payloads.extension.format(channel_id=self.channel_id))
        if reply.header.response_code != 200:
            raise ProtocolError(msg.Error.IrInfoFailed.format(response_code=reply.header.response_code))
        config = _find_led_state(reply.xml_root)
        if config is None:
            raise ProtocolError(msg.Error.LedStateMissing)
        return config

    def _set_config(self, element: ET.Element) -> None:
        config_xml = ET.tostring(element, encoding="unicode")
        payload = payloads.xml_document.format(inner=Raw(config_xml)).encode("utf-8")
        msg_num = self.camera.send(
            MSG.SET_LED,
            payload,
            extension=payloads.extension.format(channel_id=self.channel_id),
        )
        self._wait_for_set_reply(msg_num)

    def _wait_for_set_reply(self, msg_num: int) -> None:
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            try:
                reply = self.camera._recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            except TimeoutError:
                continue
            if reply.header.msg_num != msg_num:
                continue
            if reply.header.response_code != 200:
                raise ProtocolError(msg.Error.IrSetFailed.format(response_code=reply.header.response_code))
            return


def _find_led_state(root: ET.Element | None) -> ET.Element | None:
    if root is None:
        return None
    if root.tag == "LedState":
        return root
    return root.find(".//LedState")


def _int_text(root: ET.Element, tag: str) -> int | None:
    value = find_text(root, tag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
