from pyneolink import Camera, CameraConfig, Config, DangerousSdCardOperation, StreamServer, config_from_dict
from pyneolink.battery import Battery, BatteryInfoUpdates, parse_battery_xml
from pyneolink.sd_card import SdCard
from pyneolink.core.bc import CLASS_MODERN, MSG_BATTERY, MSG_UDP_KEEPALIVE, MSG_VIDEO, MSG_VIDEO_STOP, Header, InvalidMagicError, Message, encode_modern, recv_message, xml_document
from pyneolink.core.crypto import Cipher, bc_xor, make_aes_key, md5_hex, udp_xor
from pyneolink.core.discovery import decode_discovery_packet, encode_discovery_xml
from pyneolink.core.media import MediaParser, extract_embedded_mp4
from pyneolink.stream_server import MpegTsMuxer, _buffer_initial_video, _find_camera, _read_until_keyframe
from datetime import datetime

from pyneolink.sd_card import (
    _download_queries,
    _download_raw,
    _normalize_download_stream_type,
    _playback_download_payload,
    _replay_download_payload,
    _xml_file_size,
)
from pyneolink.core.udp_transport import UdpBcConnection, decode_udp_packet, encode_udp_ack


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


def test_full_aes_binary_keeps_raw_tail_after_encrypt_len():
    class FakeSocket:
        def __init__(self, data):
            self.data = bytearray(data)

        def settimeout(self, _timeout):
            pass

        def recv(self, size):
            chunk = bytes(self.data[:size])
            del self.data[:size]
            return chunk

    cipher = Cipher("aes", b"0123456789abcdef", full_media=True)
    extension = b"""<?xml version="1.0" encoding="UTF-8" ?>
<Extension version="1.1"><binaryData>1</binaryData><encryptLen>4</encryptLen></Extension>"""
    prefix = b"1002"
    raw_tail = b"raw-media-tail"
    ext_raw = cipher.encrypt(0, extension)
    payload_raw = cipher.encrypt(0, prefix) + raw_tail
    header = Header(8, len(ext_raw) + len(payload_raw), 0, 0, 7, 200, CLASS_MODERN, len(ext_raw))
    msg = recv_message(FakeSocket(header.pack() + ext_raw + payload_raw), cipher)
    assert msg.payload == prefix + raw_tail
    assert msg.raw_payload_len == len(payload_raw)
    assert msg.encrypted_len == 4


def test_replay_timestamp_header_has_payload_offset():
    packet = bytes.fromhex("f0debc0a050000001a55000018000000619082646a000000")
    header = Header.unpack_from(packet)
    assert header.msg_id == 5
    assert header.response_code == 0x9061
    assert header.msg_class == 0x6482
    assert header.payload_offset == 0x6A
    assert header.has_payload_offset


def test_invalid_magic_preserves_consumed_bytes():
    data = b"\x5d\x84\x3e\x93" + b"x" * 16
    try:
        Header.unpack_from(data)
    except InvalidMagicError as exc:
        assert exc.magic == 0x933E845D
        assert exc.data == data
    else:
        raise AssertionError("invalid magic should raise")


def test_discovery_packet_roundtrip():
    xml = "<P2P><C2D_S><to><port>12345</port></to></C2D_S></P2P>"
    packet = encode_discovery_xml(123, xml)
    assert decode_discovery_packet(packet) == (123, xml)


def test_udp_ack_packet_roundtrip():
    packet = encode_udp_ack(7, 5, b"\0\1\1", maybe_latency=42)
    assert decode_udp_packet(packet) == ("ack", 7, 0, 5, 42, b"\0\1\1")


def test_udp_ack_state_reports_missing_packets():
    connection = UdpBcConnection.__new__(UdpBcConnection)
    connection.next_recv_id = 10
    connection.recv_chunks = {11: b"a", 12: b"b", 14: b"c"}
    packet_id, payload, group_id = connection._ack_state()
    assert packet_id == 9
    assert payload == b"\0\1\1\0\1"
    assert group_id == 0


def test_udp_heartbeat_reuses_connection_tid():
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def settimeout(self, _timeout):
            pass

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    sock = FakeSocket()
    connection = UdpBcConnection(sock, ("127.0.0.1", 1234), 11, 22, heartbeat_tid=77)
    connection._send_heartbeat()
    tid, xml = decode_discovery_packet(sock.sent[0][0])
    assert tid == 77
    assert "<C2D_HB>" in xml


def test_media_info_packet():
    raw = b"1002" + (32).to_bytes(4, "little") + (1920).to_bytes(4, "little") + (1080).to_bytes(4, "little")
    raw += bytes([0, 15, 126, 1, 1, 0, 0, 0, 126, 1, 1, 0, 0, 0]) + b"\0\0"
    packets = list(MediaParser().feed(raw))
    assert packets[0].kind == "info"
    assert packets[0].width == 1920
    assert packets[0].fps == 15


def test_media_video_packet_all_channels():
    raw = b"10dcH264"
    raw += (4).to_bytes(4, "little")
    raw += (4).to_bytes(4, "little")
    raw += (123).to_bytes(4, "little")
    raw += (0).to_bytes(4, "little")
    raw += (456).to_bytes(4, "little")
    raw += b"\0\0\0\1"
    raw += b"\0\0\0\0"
    packets = list(MediaParser().feed(raw))
    assert packets[0].kind == "iframe"
    assert packets[0].codec == "H264"
    assert packets[0].data == b"\0\0\0\1"


def test_extract_embedded_mp4_after_bcmedia_info_header(tmp_path):
    source = tmp_path / "clip.bcmedia"
    destination = tmp_path / "clip.mp4"
    mp4 = b"\x00\x00\x00\x18ftypiso4\x00\x00\x00\x01iso4hvc1" + b"payload"
    source.write_bytes(b"1002" + (32).to_bytes(4, "little") + b"\0" * 24 + mp4)
    assert extract_embedded_mp4(source, destination)
    assert destination.read_bytes() == mp4


def test_stream_server_reads_fps_before_keyframe():
    info = b"1002" + (32).to_bytes(4, "little") + (640).to_bytes(4, "little") + (360).to_bytes(4, "little")
    info += bytes([0, 12, 126, 1, 1, 0, 0, 0, 126, 1, 1, 0, 0, 0]) + b"\0\0"
    iframe = b"00dcH264"
    iframe += (4).to_bytes(4, "little")
    iframe += (4).to_bytes(4, "little")
    iframe += (123).to_bytes(4, "little")
    iframe += (0).to_bytes(4, "little")
    iframe += (456).to_bytes(4, "little")
    iframe += b"\0\0\0\1"
    iframe += b"\0\0\0\0"
    packets, codec, fps = _read_until_keyframe([info + iframe], MediaParser())
    assert codec == "H264"
    assert fps == 12
    assert [packet.kind for packet in packets] == ["info", "iframe"]


def test_stream_server_keeps_audio_before_keyframe():
    info = b"1002" + (32).to_bytes(4, "little") + (640).to_bytes(4, "little") + (360).to_bytes(4, "little")
    info += bytes([0, 12, 126, 1, 1, 0, 0, 0, 126, 1, 1, 0, 0, 0]) + b"\0\0"
    audio = b"05wb" + (7).to_bytes(2, "little") + b"\0\0" + b"\xff\xf1\x50\x80\x00\x1f\xfc" + b"\0"
    iframe = b"00dcH264"
    iframe += (4).to_bytes(4, "little")
    iframe += (4).to_bytes(4, "little")
    iframe += (123).to_bytes(4, "little")
    iframe += (0).to_bytes(4, "little")
    iframe += (456).to_bytes(4, "little")
    iframe += b"\0\0\0\1"
    iframe += b"\0\0\0\0"
    packets, codec, fps = _read_until_keyframe([info + audio + iframe], MediaParser())
    assert codec == "H264"
    assert fps == 12
    assert [packet.kind for packet in packets] == ["info", "aac", "iframe"]


def test_mpegts_muxer_emits_video_and_aac_packets():
    muxer = MpegTsMuxer("H264", fps=15)
    iframe = MediaParser().feed(
        b"00dcH264"
        + (4).to_bytes(4, "little")
        + (4).to_bytes(4, "little")
        + (123).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (456).to_bytes(4, "little")
        + b"\0\0\0\1"
        + b"\0\0\0\0"
    )
    chunks = []
    chunks.extend(muxer.feed(next(iframe)))
    chunks.extend(muxer.feed(type("Packet", (), {"kind": "aac", "data": b"\xff\xf1\x50\x80\x00\x1f\xfc", "timestamp_us": None})()))
    assert chunks
    assert all(len(chunk) == 188 and chunk[0] == 0x47 for chunk in chunks)
    assert any((((chunk[1] & 0x1F) << 8) | chunk[2]) == MpegTsMuxer.AUDIO_PID for chunk in chunks)


def test_stream_server_buffers_initial_video_packets():
    iframe = b"00dcH264"
    iframe += (4).to_bytes(4, "little")
    iframe += (4).to_bytes(4, "little")
    iframe += (123).to_bytes(4, "little")
    iframe += (0).to_bytes(4, "little")
    iframe += (456).to_bytes(4, "little")
    iframe += b"\0\0\0\1"
    iframe += b"\0\0\0\0"
    pframe = b"01dcH264"
    pframe += (4).to_bytes(4, "little")
    pframe += (4).to_bytes(4, "little")
    pframe += (123).to_bytes(4, "little")
    pframe += (0).to_bytes(4, "little")
    pframe += (456).to_bytes(4, "little")
    pframe += b"\0\0\0\1"
    pframe += b"\0\0\0\0"
    first_packets = list(MediaParser().feed(iframe))
    packets = _buffer_initial_video([pframe + pframe], MediaParser(), first_packets, fps=3, buffer_seconds=1.0)
    assert [packet.kind for packet in packets] == ["iframe", "pframe", "pframe"]


def test_stream_server_camera_lookup_is_whitespace_tolerant():
    config = Config(cameras=[CameraConfig(name="Scherbaka 41 - Front")])
    assert _find_camera(config, "  scherbaka   41 - front  ").name == "Scherbaka 41 - Front"
    try:
        _find_camera(config, "Shiferna 43 - Front")
    except ValueError as exc:
        assert "Available cameras: Scherbaka 41 - Front" in str(exc)
    else:
        raise AssertionError("unknown camera should raise")


def test_public_stream_server_builds_encoded_urls():
    config = Config(bind="0.0.0.0", bind_port=8554, cameras=[CameraConfig(name="Scherbaka 41 - Front")])
    server = StreamServer(config)
    assert server.urls() == [
        "http://127.0.0.1:8554/Scherbaka%2041%20-%20Front/high",
        "http://127.0.0.1:8554/Scherbaka%2041%20-%20Front/low",
    ]


def test_stream_server_accepts_dict_config():
    server = StreamServer(
        {
            "bind": "0.0.0.0",
            "bind_port": 8554,
            "cameras": [{"name": "Scherbaka 41 - Front", "uid": "abc"}],
        }
    )
    assert server.config.camera("Scherbaka 41 - Front").uid == "abc"
    assert server.urls() == [
        "http://127.0.0.1:8554/Scherbaka%2041%20-%20Front/high",
        "http://127.0.0.1:8554/Scherbaka%2041%20-%20Front/low",
    ]


def test_config_from_dict_uses_config_defaults():
    config = config_from_dict({"cameras": [{"name": "Front", "uid": "abc"}]})
    camera = config.camera("Front")
    assert config.bind == "0.0.0.0"
    assert config.bind_port == 8554
    assert camera.username == "admin"
    assert camera.password == "123456"
    assert camera.discovery == "relay"


def test_download_query_order_prefers_direct_download_before_replay():
    raw = {
        "name": "abc",
        "startTime": {"year": 2026, "month": 6, "day": 2, "hour": 0, "minute": 0, "second": 0},
        "endTime": {"year": 2026, "month": 6, "day": 2, "hour": 0, "minute": 0, "second": 30},
    }
    queries = _download_queries(0, "abc", raw)
    labels = [query.label for query in queries[:4]]
    assert labels == [
        "download13/id/class6482",
        "playback143/range-mainStream/bcmedia",
        "playback143/range-subStream/bcmedia",
        "download8/id/class6482",
    ]


def test_download_query_order_respects_forced_high_quality_stream():
    raw = {
        "name": "abc",
        "streamType": "mainStream",
        "_streamTypeForced": True,
        "startTime": {"year": 2026, "month": 6, "day": 2, "hour": 0, "minute": 0, "second": 0},
        "endTime": {"year": 2026, "month": 6, "day": 2, "hour": 0, "minute": 0, "second": 30},
    }
    labels = [query.label for query in _download_queries(0, "abc", raw)]
    assert labels == [
        "download13/full-high/class6482",
        "download8/full-high/class6482",
    ]
    assert "playback143/range-subStream/bcmedia" not in labels


def test_download_quality_aliases_map_to_reolink_streams():
    assert _normalize_download_stream_type(stream_type=None, quality="high") == "mainStream"
    assert _normalize_download_stream_type(stream_type=None, quality="low") == "subStream"
    assert _normalize_download_stream_type(stream_type="mainStream", quality=None) == "mainStream"


def test_playback_download_payload_matches_reolink_range_shape():
    payload = _playback_download_payload(
        0,
        datetime(2026, 6, 2, 7, 35, 0),
        datetime(2026, 6, 2, 7, 35, 35),
        "subStream",
    )
    assert b"<logicChnBitmap>255</logicChnBitmap>" in payload
    assert b"<supportSub>1</supportSub>" in payload
    assert b"<streamType>subStream</streamType>" in payload
    assert b"<startTime>" in payload
    assert b"<endTime>" in payload


def test_xml_file_size_reads_playback_size_words():
    xml = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<sizeL>847663</sizeL>
<sizeH>0</sizeH>
<FileCount>1</FileCount>
</FileInfo>
</FileInfoList>
</body>"""
    assert _xml_file_size(xml) == 847663


def test_replay_download_payload_uses_minimal_start_query():
    payload = _replay_download_payload(
        0,
        {
            "name": "ignored.mp4",
            "startTime": {"year": 2026, "month": 6, "day": 2, "hour": 0, "minute": 0, "second": 0},
            "endTime": {"year": 2026, "month": 6, "day": 2, "hour": 0, "minute": 1, "second": 0},
        },
    )
    assert b"<startTime>" in payload
    assert b"<playSpeed>1</playSpeed>" in payload
    assert b"<name>" not in payload
    assert b"<endTime>" not in payload


def test_download_raw_enriches_normalized_file_fields_for_replay():
    raw = _download_raw(
        {
            "file_name": "clip.mp4",
            "path": "/mnt/sda/Mp4Record/clip.mp4",
            "start_time": "2026-06-02T00:18:27",
            "end_time": "2026-06-02T00:18:57",
            "stream_type": "mainStream",
            "raw": {},
        }
    )
    queries = _download_queries(0, raw["Id"], raw)
    assert raw["startTime"] == "2026-06-02T00:18:27"
    labels = [query.label for query in queries[:5]]
    assert "playback143/range-mainStream/bcmedia" in labels
    assert "playback143/range-subStream/bcmedia" in labels
    assert "replay5/start/bcmedia" in labels


def test_hash_shapes():
    assert len(md5_hex("adminnonce")) == 31
    assert "\0" not in md5_hex("adminnonce")
    assert len(make_aes_key("nonce", "password")) == 16


def test_public_camera_constructor_accepts_uuid_alias():
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    assert camera.config.uid == "95270006R5KDROXI"
    assert camera.config.password == "secret"


def test_battery_xml_parses_status_fields():
    xml = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<BatteryList version="1.1">
<BatteryInfo>
<channelId>0</channelId>
<chargeStatus>charging</chargeStatus>
<adapterStatus>solarPanel</adapterStatus>
<voltage>4083</voltage>
<current>-396</current>
<temperature>32</temperature>
<batteryPercent>87</batteryPercent>
<lowPower>0</lowPower>
<batteryVersion>2</batteryVersion>
</BatteryInfo>
</BatteryList>
</body>"""
    info = parse_battery_xml(xml)
    assert info["level_percent"] == 87
    assert info["is_charging"] is True
    assert info["charge_type"] == "solar_panel"
    assert info["charge_type_label"] == "Сонячна панель"
    assert info["low_power"] is False
    assert info["raw"]["adapterStatus"] == "solarPanel"


def test_camera_battery_info_requests_channel_extension():
    class FakeSocket:
        def __init__(self, reply):
            self.reply = bytearray(reply)
            self.sent = bytearray()
            self.discarded = 0

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.sent.extend(data)

        def recv(self, size):
            chunk = bytes(self.reply[:size])
            del self.reply[:size]
            return chunk

        def discard_sent(self):
            self.discarded += 1

    payload = xml_document(
        "<BatteryInfo>"
        "<channelId>2</channelId>"
        "<chargeStatus>none</chargeStatus>"
        "<adapterStatus>none</adapterStatus>"
        "<batteryPercent>64</batteryPercent>"
        "</BatteryInfo>"
    )
    reply = encode_modern(MSG_BATTERY, 1, payload, response_code=200, cipher=Cipher("none"))
    camera = Camera(uuid="95270006R5KDROXI", password="secret", channel_id=2, state_path=None)
    camera.sock = FakeSocket(reply)
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")
    with camera.battery().info(mode="online") as info:
        assert info["level_percent"] == 64
        assert info["charge_type"] == "none"
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    extension = sent[24 : 24 + (header.payload_offset or 0)]
    assert header.msg_id == MSG_BATTERY
    assert b"<channelId>2</channelId>" in extension
    assert camera.sock.discarded == 1


def test_battery_refresh_reconnects_after_timeout():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.commands = 0
            self.reconnects = 0

        def command(self, msg_id, payload=b"", extension=b""):
            self.commands += 1
            if self.commands == 1:
                raise TimeoutError("Timed out waiting for UDP Baichuan data")
            xml = xml_document(
                "<BatteryInfo>"
                "<channelId>0</channelId>"
                "<chargeStatus>charging</chargeStatus>"
                "<adapterStatus>solarPanel</adapterStatus>"
                "<batteryPercent>99</batteryPercent>"
                "</BatteryInfo>"
            )
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, CLASS_MODERN), payload=xml)

        def reconnect(self):
            self.reconnects += 1

        def close(self):
            pass

    camera = FakeCamera()
    info = Battery(camera).refresh()
    assert info["level_percent"] == 99
    assert camera.commands == 2
    assert camera.reconnects == 1


def test_battery_reconnect_mode_closes_only_when_not_online_required():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.closed = 0
            self._online_required = 0

        @property
        def online_required(self):
            return self._online_required > 0

        def command(self, msg_id, payload=b"", extension=b""):
            xml = xml_document(
                "<BatteryInfo>"
                "<channelId>0</channelId>"
                "<chargeStatus>charging</chargeStatus>"
                "<adapterStatus>solarPanel</adapterStatus>"
                "<batteryPercent>98</batteryPercent>"
                "</BatteryInfo>"
            )
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, CLASS_MODERN), payload=xml)

        def close(self):
            self.closed += 1

    camera = FakeCamera()
    assert Battery(camera).refresh(mode="reconnect")["level_percent"] == 98
    assert camera.closed == 2

    camera._online_required = 1
    assert Battery(camera).refresh(mode="reconnect")["level_percent"] == 98
    assert camera.closed == 2


def test_battery_watch_sends_keepalive_between_updates():
    class FakeBattery:
        def __init__(self):
            self.keepalives = 0
            self.refreshes = 0
            self.camera = type("Camera", (), {"reconnect": lambda self: None})()

        def refresh(self, mode="reconnect"):
            self.refreshes += 1
            return {"level_percent": self.refreshes}

        def keepalive(self):
            self.keepalives += 1

    battery = FakeBattery()
    updates = BatteryInfoUpdates(battery, interval=0.001, count=2, mode="online", keepalive_interval=0.001)
    assert next(updates)["level_percent"] == 1
    assert next(updates)["level_percent"] == 2
    assert battery.keepalives >= 1


def test_camera_start_stream_uses_neolink_substream_preview():
    class FakeSocket:
        def __init__(self, reply):
            self.reply = bytearray(reply)
            self.sent = bytearray()

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.sent.extend(data)

        def recv(self, size):
            chunk = bytes(self.reply[:size])
            del self.reply[:size]
            return chunk

    reply = encode_modern(MSG_VIDEO, 1, response_code=200, cipher=Cipher("none"))
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    camera.sock = FakeSocket(reply)
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")
    msg_num = camera.start_stream("low")
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    payload = sent[24:]
    assert msg_num == 1
    assert header.msg_id == MSG_VIDEO
    assert header.stream_type == 1
    assert b"<handle>256</handle>" in payload
    assert b"<streamType>subStream</streamType>" in payload
    assert msg_num in camera.binary_msg_nums


def test_camera_stop_stream_ignores_camera_400_reply():
    class FakeSocket:
        def __init__(self, reply):
            self.reply = bytearray(reply)
            self.sent = bytearray()

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.sent.extend(data)

        def recv(self, size):
            chunk = bytes(self.reply[:size])
            del self.reply[:size]
            return chunk

    reply = encode_modern(MSG_VIDEO_STOP, 7, response_code=400, cipher=Cipher("none"))
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    camera.sock = FakeSocket(reply)
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")
    camera.binary_msg_nums.add(7)
    camera.stop_stream("low", 7)
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    payload = sent[24:]
    assert header.msg_id == MSG_VIDEO_STOP
    assert header.stream_type == 1
    assert b"<handle>256</handle>" in payload
    assert 7 not in camera.binary_msg_nums


def test_camera_replies_to_incoming_keepalive():
    class FakeSocket:
        def __init__(self, reply):
            self.reply = bytearray(reply)
            self.sent = bytearray()
            self.untracked = bytearray()

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.sent.extend(data)

        def send_untracked(self, data):
            self.untracked.extend(data)

        def recv(self, size):
            chunk = bytes(self.reply[:size])
            del self.reply[:size]
            return chunk

    incoming = encode_modern(MSG_UDP_KEEPALIVE, 9, channel_id=3, stream_type=1, cipher=Cipher("none"))
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    camera.sock = FakeSocket(incoming)
    camera.cipher = Cipher("none")
    msg = camera._recv()
    sent = bytes(camera.sock.untracked)
    header = Header.unpack_from(sent[:24])
    assert msg.header.msg_id == MSG_UDP_KEEPALIVE
    assert bytes(camera.sock.sent) == b""
    assert header.msg_id == MSG_UDP_KEEPALIVE
    assert header.msg_num == 9
    assert header.channel_id == 3
    assert header.stream_type == 1
    assert header.response_code == 200


def test_camera_replies_to_keepalive_even_when_response_is_200():
    class FakeSocket:
        def __init__(self, reply):
            self.reply = bytearray(reply)
            self.untracked = bytearray()

        def settimeout(self, _timeout):
            pass

        def sendall(self, _data):
            raise AssertionError("keepalive replies should not be tracked for resend")

        def send_untracked(self, data):
            self.untracked.extend(data)

        def recv(self, size):
            chunk = bytes(self.reply[:size])
            del self.reply[:size]
            return chunk

    incoming = encode_modern(MSG_UDP_KEEPALIVE, 0, response_code=200, cipher=Cipher("none"))
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    camera.sock = FakeSocket(incoming)
    camera.cipher = Cipher("none")
    msg = camera._recv()
    sent = bytes(camera.sock.untracked)
    header = Header.unpack_from(sent[:24])
    assert msg.header.msg_id == MSG_UDP_KEEPALIVE
    assert msg.header.response_code == 200
    assert header.msg_id == MSG_UDP_KEEPALIVE
    assert header.msg_num == 0
    assert header.response_code == 200


def test_camera_sd_card_api_is_available():
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    sd_card = camera.sd_card()
    assert hasattr(sd_card, "list")
    assert hasattr(sd_card, "filter")
    assert hasattr(sd_card, "download")
    assert hasattr(sd_card, "remove")
    assert hasattr(sd_card, "format")


def test_sd_card_filter_and_danger_guards():
    camera = Camera(uuid="95270006R5KDROXI", password="secret", state_path=None)
    sd_card = camera.sd_card()
    files = [
        {"file_name": "front_2026-06-01.mp4", "start_time": "2026-06-01T10:00:00", "end_time": "2026-06-01T10:05:00"},
        {"file_name": "front_2026-06-02.mp4", "start_time": "2026-06-02T10:00:00", "end_time": "2026-06-02T10:05:00"},
    ]
    assert len(sd_card.filter(files, start="2026-06-02", end="2026-06-02")) == 1
    try:
        sd_card.format()
    except DangerousSdCardOperation:
        pass
    else:
        raise AssertionError("format must require an explicit confirmation")


def test_sd_card_list_parses_file_info_reply():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def command(self, msg_id, payload=b"", extension=b""):
            self.msg_id = msg_id
            self.payload = payload
            self.extension = extension
            xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<fileName>01_20260601120000.mp4</fileName>
<fileSize>123</fileSize>
<beginTime><year>2026</year><month>6</month><day>1</day><hour>12</hour><minute>0</minute><second>0</second></beginTime>
<endTime><year>2026</year><month>6</month><day>1</day><hour>12</hour><minute>5</minute><second>0</second></endTime>
</FileInfo>
</FileInfoList>
</body>"""
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, CLASS_MODERN), payload=xml)

    camera = FakeCamera()
    files = SdCard(camera).list(start="2026-06-01", end="2026-06-01")
    assert files[0]["file_name"] == "01_20260601120000.mp4"
    assert files[0]["size"] == 123
    assert b"<FileInfoList" in camera.payload


def test_sd_card_list_sorts_recordings_by_time():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def command(self, msg_id, payload=b"", extension=b""):
            xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<fileName>new.mp4</fileName>
<beginTime><year>2026</year><month>6</month><day>2</day><hour>20</hour><minute>0</minute><second>0</second></beginTime>
<endTime><year>2026</year><month>6</month><day>2</day><hour>20</hour><minute>1</minute><second>0</second></endTime>
</FileInfo>
<FileInfo>
<fileName>old.mp4</fileName>
<beginTime><year>2026</year><month>6</month><day>2</day><hour>10</hour><minute>0</minute><second>0</second></beginTime>
<endTime><year>2026</year><month>6</month><day>2</day><hour>10</hour><minute>1</minute><second>0</second></endTime>
</FileInfo>
</FileInfoList>
</body>"""
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, CLASS_MODERN), payload=xml)

    sd_card = SdCard(FakeCamera())
    asc = sd_card.list(start="2026-06-02", end="2026-06-02")
    desc = sd_card.list(start="2026-06-02", end="2026-06-02", sort="desc")
    assert [item["file_name"] for item in asc] == ["old.mp4", "new.mp4"]
    assert [item["file_name"] for item in desc] == ["new.mp4", "old.mp4"]


def test_sd_card_list_reads_all_handle_pages():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.detail_calls = 0

        def command(self, msg_id, payload=b"", extension=b""):
            if b"<DayRecords" in payload:
                xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body><DayRecords version="1.1" /></body>"""
            elif b"<handle>" not in payload:
                xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo><channelId>0</channelId><handle>1</handle></FileInfo>
</FileInfoList>
</body>"""
            else:
                self.detail_calls += 1
                if self.detail_calls == 1:
                    xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<fileName>page1-a.mp4</fileName>
<beginTime><year>2026</year><month>6</month><day>2</day><hour>10</hour><minute>0</minute><second>0</second></beginTime>
<endTime><year>2026</year><month>6</month><day>2</day><hour>10</hour><minute>1</minute><second>0</second></endTime>
</FileInfo>
<FileInfo>
<fileName>page1-b.mp4</fileName>
<beginTime><year>2026</year><month>6</month><day>2</day><hour>10</hour><minute>2</minute><second>0</second></beginTime>
<endTime><year>2026</year><month>6</month><day>2</day><hour>10</hour><minute>3</minute><second>0</second></endTime>
</FileInfo>
</FileInfoList>
</body>"""
                elif self.detail_calls == 2:
                    xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<fileName>page2-a.mp4</fileName>
<beginTime><year>2026</year><month>6</month><day>2</day><hour>11</hour><minute>0</minute><second>0</second></beginTime>
<endTime><year>2026</year><month>6</month><day>2</day><hour>11</hour><minute>1</minute><second>0</second></endTime>
</FileInfo>
</FileInfoList>
</body>"""
                else:
                    xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
<body><FileInfoList version="1.1" /></body>"""
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, CLASS_MODERN), payload=xml)

    sd_card = SdCard(FakeCamera())
    files = sd_card.list(start="2026-06-02", end="2026-06-02")
    assert [item["file_name"] for item in files] == ["page1-a.mp4", "page1-b.mp4", "page2-a.mp4"]
    assert [item["label"] for item in sd_card.last_successes] == [
        "day-records/range",
        "handle/mainStream",
        "files/handle-1",
        "files/handle-1/page-2",
        "files/handle-1/page-3",
    ]
