from __future__ import annotations

import xml.etree.ElementTree as ET


def xml_to_dict(text: str | bytes | None) -> dict:
    if not text:
        return {}
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    root = ET.fromstring(text)
    return {root.tag: _element_to_value(root)}


def _element_to_value(element: ET.Element):
    children = list(element)
    attrs = {f"@{key}": value for key, value in element.attrib.items()}
    text = (element.text or "").strip()
    if not children:
        if attrs:
            if text:
                attrs["#text"] = text
            return attrs
        return text
    data: dict = dict(attrs)
    for child in children:
        value = _element_to_value(child)
        if child.tag in data:
            if not isinstance(data[child.tag], list):
                data[child.tag] = [data[child.tag]]
            data[child.tag].append(value)
        else:
            data[child.tag] = value
    if text:
        data["#text"] = text
    return data
