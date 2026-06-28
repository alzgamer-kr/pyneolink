from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from pyneolink.core.bc import ProtocolError, find_text
from pyneolink.core.const import MSG, msg, payloads
from pyneolink.core.const.payloads import Raw
from pyneolink.core.xmlutil import xml_to_dict


class Pir:
    """PIR motion sensor settings helper."""

    def __init__(self, camera, *, rf_id: int | None = None) -> None:
        """Create a PIR settings helper.

        :param camera: Connected or connectable `Camera` instance.
        :param rf_id: Optional RF/PIR id override. Defaults to camera channel.
        """
        self.camera = camera
        self.rf_id = camera.config.channel_id if rf_id is None else rf_id

    def status(self) -> dict:
        """Return PIR status and raw PIR configuration."""
        element = self._get_config()
        xml_text = ET.tostring(element, encoding="unicode")
        enable = _int_text(element, "enable")
        return {
            "enabled": enable == 1,
            "enable": enable,
            "rf_id": _int_text(element, "rfID"),
            "sensitivity": _int_text(element, "sensitivity"),
            "sensi_value": _int_text(element, "sensiValue"),
            "reduce_false_alarm": _bool_int_text(element, "reduceFalseAlarm"),
            "raw": xml_to_dict(xml_text),
        }

    def on(self) -> dict:
        """Enable PIR motion detection."""
        return self._set_enabled(True)

    def off(self) -> dict:
        """Disable PIR motion detection."""
        return self._set_enabled(False)

    def _set_enabled(self, enabled: bool) -> dict:
        element = self._get_config()
        enable = element.find("enable")
        if enable is None:
            enable = ET.SubElement(element, "enable")
        enable.text = "1" if enabled else "0"
        self._set_config(element)
        return self.status()

    def _get_config(self) -> ET.Element:
        for attempt in range(6):
            reply = self.camera.command(MSG.GET_PIR_ALARM, extension=payloads.extension_rf.format(rf_id=self.rf_id))
            if reply.header.response_code == 400 and attempt < 5:
                time.sleep(0.5)
                continue
            if reply.header.response_code != 200:
                raise ProtocolError(msg.Error.PirInfoFailed.format(response_code=reply.header.response_code))
            config = _find_pir_config(reply.xml_root)
            if config is None:
                raise ProtocolError(msg.Error.PirStateMissing)
            return config
        raise ProtocolError(msg.Error.PirStateMissing)

    def _set_config(self, element: ET.Element) -> None:
        config_xml = ET.tostring(element, encoding="unicode")
        payload = payloads.xml_document.format(inner=Raw(config_xml)).encode("utf-8")
        msg_num = self.camera.send(
            MSG.SET_PIR_ALARM,
            payload,
            extension=payloads.extension_rf.format(rf_id=self.rf_id),
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
                raise ProtocolError(msg.Error.PirSetFailed.format(response_code=reply.header.response_code))
            return


def _find_pir_config(root: ET.Element | None) -> ET.Element | None:
    if root is None:
        return None
    if root.tag == "rfAlarmCfg":
        return root
    return root.find(".//rfAlarmCfg")


def _int_text(root: ET.Element, tag: str) -> int | None:
    value = find_text(root, tag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool_int_text(root: ET.Element, tag: str) -> bool | None:
    value = _int_text(root, tag)
    return None if value is None else value == 1
