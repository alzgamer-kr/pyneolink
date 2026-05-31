from pyneolink.bc import Header, encode_modern, xml_document
from pyneolink.crypto import bc_xor, make_aes_key, md5_hex, udp_xor
from pyneolink.discovery import decode_discovery_packet, encode_discovery_xml
from pyneolink.media import MediaParser


def test_bc_xor_roundtrip():
    data = b"<?xml version=\"1.0\"?><body/>"
    assert bc_xor(3, bc_xor(3, data)) == data


def test_udp_xor_roundtrip():
    data = b"<P2P><C2D_S /></P2P>"
    assert udp_xor(87, udp_xor(87, data)) == data


def test_modern_packet_header_roundtrip():
    payload = xml_document("<Ping version=\"1.1\" />")
    packet = encode_modern(93, 7, payload)
    header = Header.unpack_from(packet[:24])
    assert header.msg_id == 93
    assert header.msg_num == 7
    assert header.body_len == len(packet) - 24
    assert header.payload_offset == 0


def test_discovery_packet_roundtrip():
    xml = "<P2P><C2D_S><to><port>12345</port></to></C2D_S></P2P>"
    packet = encode_discovery_xml(123, xml)
    assert decode_discovery_packet(packet) == (123, xml)


def test_media_info_packet():
    raw = b"1002" + (32).to_bytes(4, "little") + (1920).to_bytes(4, "little") + (1080).to_bytes(4, "little")
    raw += bytes([0, 15, 126, 1, 1, 0, 0, 0, 126, 1, 1, 0, 0, 0]) + b"\0\0"
    packets = list(MediaParser().feed(raw))
    assert packets[0].kind == "info"
    assert packets[0].width == 1920
    assert packets[0].fps == 15


def test_hash_shapes():
    assert len(md5_hex("adminnonce")) == 31
    assert "\0" not in md5_hex("adminnonce")
    assert len(make_aes_key("nonce", "password")) == 16
