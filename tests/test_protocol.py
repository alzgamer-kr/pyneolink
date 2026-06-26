from pyneolink import Camera, CameraConfig, Config, DangerousSdCardOperation, EVENTS, Settings, StreamServer, Voice, config_from_dict
from pyneolink.battery import Battery, BatteryInfoUpdates, parse_battery_xml
from pyneolink.sd_card import SdCard
from pyneolink.core.bc import Header, InvalidMagicError, Message, encode_modern, recv_message, xml_document
from pyneolink.core.crypto import Cipher, bc_xor, make_aes_key, md5_hex, udp_xor
from pyneolink.core.discovery import decode_discovery_packet, encode_discovery_xml
from pyneolink.core.const import MSG, MSG_CLASS, payloads
from pyneolink.core.media import MediaParser, extract_embedded_mp4
from pyneolink.motion import CameraEvent, CameraEvents, parse_motion_events
from pyneolink.settings import Ir, Pir
from pyneolink.cli import CLI
import queue
import struct
import threading
from pathlib import Path

from pyneolink.stream_server import (
    HlsSegment,
    MpegTsMuxer,
    _STREAM_END,
    _buffer_initial_video,
    _find_camera,
    _hls_playlist,
    _mpegts_null_packet,
    _packets_from_first_keyframe,
    _produce_mpegts_chunks,
    _read_until_keyframe,
)
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
from pyneolink.recorder import StreamRecorder
from pyneolink.internal.voice import (
    ImaAdpcmEncoder,
    adpcm_blocks_from_tone,
    adpcm_level_hint,
    audio_info_from_ffprobe,
    parse_talk_config,
    serialize_bcmedia_adpcm,
)


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
    header = Header(8, len(ext_raw) + len(payload_raw), 0, 0, 7, 200, MSG_CLASS.MODERN, len(ext_raw))
    msg = recv_message(FakeSocket(header.pack() + ext_raw + payload_raw), cipher)
    assert msg.payload == prefix + raw_tail
    assert msg.raw_payload_len == len(payload_raw)
    assert msg.encrypted_len == 4


def test_outgoing_binary_payload_is_not_encrypted():
    extension = payloads.extension_binary_data.format(channel_id=0)
    payload = b"raw-talk-data"
    packet = encode_modern(MSG.TALK, 7, payload, extension=extension, cipher=Cipher("bc"))
    header = Header.unpack_from(packet[:24])
    body = packet[24:]
    assert body[header.payload_offset :] == payload
    assert b"raw-talk-data" in packet


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


def test_mpegts_null_packet_is_valid_stuffing_packet():
    packet = _mpegts_null_packet()
    assert len(packet) == 188
    assert packet[0] == 0x47
    assert (((packet[1] & 0x1F) << 8) | packet[2]) == 0x1FFF


def test_mpegts_producer_queues_chunks_and_end_marker():
    raw = b"00dcH264"
    raw += (4).to_bytes(4, "little")
    raw += (4).to_bytes(4, "little")
    raw += (123).to_bytes(4, "little")
    raw += (0).to_bytes(4, "little")
    raw += (456).to_bytes(4, "little")
    raw += b"\0\0\0\1"
    raw += b"\0\0\0\0"
    first_packets = list(MediaParser().feed(raw))
    chunks = queue.Queue()
    _produce_mpegts_chunks(chunks, threading.Event(), "H264", 15, [], MediaParser(), first_packets)
    queued = []
    while True:
        item = chunks.get_nowait()
        if item is _STREAM_END:
            break
        queued.append(item)
    assert queued
    assert all(isinstance(item, bytes) and len(item) == 188 for item in queued)
    assert any((((item[1] & 0x1F) << 8) | item[2]) == MpegTsMuxer.VIDEO_PID for item in queued)


def test_hls_playlist_lists_sliding_segments():
    segments = [
        HlsSegment(7, 2.0, b"one", 1.0),
        HlsSegment(8, 2.25, b"two", 2.0),
    ]
    playlist = _hls_playlist(segments, 2.0)
    assert "#EXT-X-MEDIA-SEQUENCE:7" in playlist
    assert "#EXT-X-TARGETDURATION:3" in playlist
    assert "#EXTINF:2.000," in playlist
    assert "segments/8.ts" in playlist


def test_hls_start_discards_packets_before_keyframe():
    packets = [
        type("Packet", (), {"kind": "aac"})(),
        type("Packet", (), {"kind": "iframe"})(),
        type("Packet", (), {"kind": "pframe"})(),
    ]
    assert [packet.kind for packet in _packets_from_first_keyframe(packets)] == ["iframe", "pframe"]


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
    config = Config(cameras=[CameraConfig(name="Home-Front")])
    assert _find_camera(config, "  home-front  ").name == "Home-Front"
    try:
        _find_camera(config, "Dorway")
    except ValueError as exc:
        assert "Available cameras: Home-Front" in str(exc)
    else:
        raise AssertionError("unknown camera should raise")


def test_public_stream_server_builds_encoded_urls():
    config = Config(bind="0.0.0.0", bind_port=8554, cameras=[CameraConfig(name="Home-Front")])
    server = StreamServer(config)
    assert server.urls() == [
        "http://127.0.0.1:8554/Home-Front/high",
        "http://127.0.0.1:8554/Home-Front/low",
        "http://127.0.0.1:8554/Home-Front/high/hls.m3u8",
        "http://127.0.0.1:8554/Home-Front/low/hls.m3u8",
    ]


def test_stream_server_accepts_dict_config():
    server = StreamServer(
        {
            "bind": "0.0.0.0",
            "bind_port": 8554,
            "cameras": [{"name": "Home-Front", "uid": "abc"}],
        }
    )
    assert server.config.camera("Home-Front").uid == "abc"
    assert server.urls() == [
        "http://127.0.0.1:8554/Home-Front/high",
        "http://127.0.0.1:8554/Home-Front/low",
        "http://127.0.0.1:8554/Home-Front/high/hls.m3u8",
        "http://127.0.0.1:8554/Home-Front/low/hls.m3u8",
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
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    assert camera.config.uid == "ABCDEF0123456789"
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
    reply = encode_modern(MSG.BATTERY, 1, payload, response_code=200, cipher=Cipher("none"))
    camera = Camera(uuid="ABCDEF0123456789", password="secret", channel_id=2, state_path=None)
    camera.sock = FakeSocket(reply)
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")
    with camera.battery().info(mode="online") as info:
        assert info["level_percent"] == 64
        assert info["charge_type"] == "none"
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    extension = sent[24 : 24 + (header.payload_offset or 0)]
    assert header.msg_id == MSG.BATTERY
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
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

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
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

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

    reply = encode_modern(MSG.VIDEO, 1, response_code=200, cipher=Cipher("none"))
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    camera.sock = FakeSocket(reply)
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")
    msg_num = camera.start_stream("low")
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    payload = sent[24:]
    assert msg_num == 1
    assert header.msg_id == MSG.VIDEO
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

    reply = encode_modern(MSG.VIDEO_STOP, 7, response_code=400, cipher=Cipher("none"))
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    camera.sock = FakeSocket(reply)
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")
    camera.binary_msg_nums.add(7)
    camera.stop_stream("low", 7)
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    payload = sent[24:]
    assert header.msg_id == MSG.VIDEO_STOP
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

    incoming = encode_modern(MSG.UDP_KEEPALIVE, 9, channel_id=3, stream_type=1, cipher=Cipher("none"))
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    camera.sock = FakeSocket(incoming)
    camera.cipher = Cipher("none")
    msg = camera._recv()
    sent = bytes(camera.sock.untracked)
    header = Header.unpack_from(sent[:24])
    assert msg.header.msg_id == MSG.UDP_KEEPALIVE
    assert bytes(camera.sock.sent) == b""
    assert header.msg_id == MSG.UDP_KEEPALIVE
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

    incoming = encode_modern(MSG.UDP_KEEPALIVE, 0, response_code=200, cipher=Cipher("none"))
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    camera.sock = FakeSocket(incoming)
    camera.cipher = Cipher("none")
    msg = camera._recv()
    sent = bytes(camera.sock.untracked)
    header = Header.unpack_from(sent[:24])
    assert msg.header.msg_id == MSG.UDP_KEEPALIVE
    assert msg.header.response_code == 200
    assert header.msg_id == MSG.UDP_KEEPALIVE
    assert header.msg_num == 0
    assert header.response_code == 200


def test_camera_sd_card_api_is_available():
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    sd_card = camera.sd_card()
    assert hasattr(sd_card, "list")
    assert hasattr(sd_card, "filter")
    assert hasattr(sd_card, "download")
    assert hasattr(sd_card, "remove")
    assert hasattr(sd_card, "format")


def test_motion_events_parse_alarm_event_list():
    xml = xml_document(
        '<AlarmEventList version="1.1">'
        '<AlarmEvent version="1.1"><channelId>0</channelId><status>MD</status><AItype>people</AItype><recording>1</recording><timeStamp>42</timeStamp></AlarmEvent>'
        '<AlarmEvent version="1.1"><channelId>0</channelId><status>MD</status><AItype>vehicle</AItype><recording>1</recording><timeStamp>43</timeStamp></AlarmEvent>'
        '<AlarmEvent version="1.1"><channelId>0</channelId><status>MD</status><AItype>none</AItype><recording>1</recording><timeStamp>44</timeStamp></AlarmEvent>'
        '<AlarmEvent version="1.1"><channelId>0</channelId><status>none</status><AItype>none</AItype><recording>0</recording><timeStamp>0</timeStamp></AlarmEvent>'
        "</AlarmEventList>"
    )
    root = Message(Header(MSG.MOTION, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml).xml_root
    events = parse_motion_events(root)

    assert [event.type for event in events] == [EVENTS.human, EVENTS.vehicle, EVENTS.motion, EVENTS.none]
    assert events[0] == EVENTS.human
    assert events[0].active is True
    assert events[-1].active is False


def test_camera_motion_api_is_available():
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    motion = camera.motion()
    assert hasattr(motion, "status")
    assert hasattr(motion.watch(), "__iter__")


def test_camera_events_normalizes_stop_to_last_active_type():
    camera = type("Camera", (), {"config": type("Config", (), {"channel_id": 0})()})()
    events = CameraEvents(camera)
    raw_events = [
        CameraEvent(EVENTS.human, active=True),
        CameraEvent(EVENTS.none, active=False),
    ]

    normalized = events._normalize_events(raw_events)

    assert [event.type for event in normalized] == [EVENTS.human, EVENTS.human]
    assert [event.active for event in normalized] == [True, False]


def test_camera_events_status_returns_immediate_motion_event():
    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    class FakeCamera:
        def __init__(self):
            self.config = type("Config", (), {"channel_id": 0})()
            self.replies = [
                Message(
                    Header(MSG.MOTION, 0, 0, 0, 2, 200, MSG_CLASS.MODERN),
                    payload=xml_document(
                        '<AlarmEventList version="1.1">'
                        '<AlarmEvent version="1.1"><channelId>0</channelId><status>MD</status><AItype>people</AItype></AlarmEvent>'
                        "</AlarmEventList>"
                    ),
                )
            ]

        def require_online(self):
            return Lease()

        def motion(self, *, channel_id=None):
            from pyneolink.motion import Motion

            return Motion(self, channel_id=channel_id)

        def command(self, msg_id, payload=b"", *, extension=b""):
            return Message(Header(msg_id, 0, 0, 0, 1, 200, MSG_CLASS.MODERN), payload=b"")

        def send(self, *args, **kwargs):
            pass

        def _recv(self, timeout=None):
            if self.replies:
                return self.replies.pop(0)
            raise TimeoutError("done")

    event, known = CameraEvents(FakeCamera()).status(timeout=0.1)

    assert known is True
    assert event.type == EVENTS.human
    assert event.active is True


def test_camera_motion_status_marks_timeout_as_unknown():
    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    class FakeCamera:
        def __init__(self):
            self.config = type("Config", (), {"channel_id": 0})()

        def require_online(self):
            return Lease()

        def motion(self, *, channel_id=None):
            from pyneolink.motion import Motion

            return Motion(self, channel_id=channel_id)

        def command(self, msg_id, payload=b"", *, extension=b""):
            return Message(Header(msg_id, 0, 0, 0, 1, 200, MSG_CLASS.MODERN), payload=b"")

        def send(self, *args, **kwargs):
            pass

        def _recv(self, timeout=None):
            raise TimeoutError("done")

    status = Camera.motion_status(FakeCamera(), timeout=0.01)

    assert status["type"] == "none"
    assert status["active"] is False
    assert status["known"] is False


def test_motion_watch_duration_stops_iterator():
    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    class FakeCamera:
        def __init__(self):
            self.config = type("Config", (), {"channel_id": 0})()

        def require_online(self):
            return Lease()

        def command(self, msg_id, payload=b"", *, extension=b""):
            return Message(Header(msg_id, 0, 0, 0, 1, 200, MSG_CLASS.MODERN), payload=b"")

        def send(self, *args, **kwargs):
            pass

        def _recv(self, timeout=None):
            raise TimeoutError("done")

    events = CameraEvents(FakeCamera(), duration=0.01)

    try:
        next(events)
    except StopIteration:
        pass
    else:
        raise AssertionError("duration-limited watch should stop")


def test_talk_ability_parses_voice_config():
    xml = xml_document(
        '<TalkAbility version="1.1">'
        "<duplexList><duplex>FDX</duplex></duplexList>"
        "<audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode></audioStreamModeList>"
        "<audioConfigList><audioConfig>"
        "<audioType>adpcm</audioType>"
        "<sampleRate>16000</sampleRate>"
        "<samplePrecision>16</samplePrecision>"
        "<lengthPerEncoder>512</lengthPerEncoder>"
        "<soundTrack>mono</soundTrack>"
        "</audioConfig></audioConfigList>"
        "</TalkAbility>"
    )
    root = Message(Header(MSG.TALKABILITY, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml).xml_root

    config = parse_talk_config(root, channel_id=0)

    assert config.audio_type == "adpcm"
    assert config.sample_rate == 16000
    assert config.block_align == 260
    assert config.samples_per_block == 513


def test_voice_adpcm_bcmedia_packet_shape():
    block = ImaAdpcmEncoder().encode_block([0, 1000, -1000, 500, -500])
    packet = serialize_bcmedia_adpcm(block)

    assert packet[:4] == b"01wb"
    assert struct.unpack("<H", packet[8:10])[0] == 0x0100
    assert packet[12:].startswith(block)


def test_voice_adpcm_packs_high_nibble_first():
    block = ImaAdpcmEncoder().encode_block([0, 1000, 2000])
    assert block[4] >> 4 != 0


def test_voice_adpcm_level_hint_reports_silence_as_zero():
    block = b"\x00\x00\x00\x00" + (b"\x00" * 64)

    assert adpcm_level_hint(block) == 0


def test_voice_tone_source_generates_non_silent_blocks():
    config = type("Config", (), {"sample_rate": 16000, "samples_per_block": 1025})()

    block = next(adpcm_blocks_from_tone(config, frequency=1000.0, seconds=0.1, volume=0.5))

    assert adpcm_level_hint(block) > 0


def test_voice_ffprobe_audio_info_accepts_mp3_metadata():
    info = audio_info_from_ffprobe(
        Path("voice.mp3"),
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "sample_rate": "44100",
                    "channels": 2,
                    "duration": "1.5",
                }
            ],
            "format": {"format_name": "mp3", "duration": "1.5"},
        },
    )

    assert info.format_name == "mp3"
    assert info.codec_name == "mp3"
    assert info.sample_rate == 44100
    assert info.channels == 2
    assert info.duration == 1.5


def test_voice_ffprobe_audio_info_rejects_files_without_audio():
    try:
        audio_info_from_ffprobe(Path("image.jpg"), {"streams": [{"codec_type": "video"}], "format": {}})
    except ValueError as exc:
        assert "no audio stream" in str(exc)
    else:
        raise AssertionError("file without audio should be rejected")


def test_camera_voice_api_is_available():
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    assert hasattr(camera.voice(), "play")
    assert hasattr(camera.voice(), "microphone")
    assert hasattr(camera.voice(), "siren")


def test_camera_settings_api_is_available():
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    settings = camera.settings()

    assert isinstance(settings, Settings)
    assert isinstance(settings.ir, Ir)
    assert isinstance(settings.pir, Pir)
    assert hasattr(settings.ir, "status")
    assert hasattr(settings.ir, "on")
    assert hasattr(settings.ir, "off")
    assert hasattr(settings.ir, "auto")
    assert hasattr(settings.pir, "status")
    assert hasattr(settings.pir, "on")
    assert hasattr(settings.pir, "off")


def test_cli_pir_command_parses_action():
    args = CLI().parse_args(["pir", "--config", "file.conf", "--camera", "Camera name", "status"])

    assert args.command == "pir"
    assert args.config == "file.conf"
    assert args.camera == "Camera name"
    assert args.action == "status"


def test_cli_ir_command_parses_action():
    args = CLI().parse_args(["ir", "--config", "file.conf", "--camera", "Camera name", "auto"])

    assert args.command == "ir"
    assert args.config == "file.conf"
    assert args.camera == "Camera name"
    assert args.action == "auto"


def test_cli_led_command_accepts_auto_alias():
    args = CLI().parse_args(["led", "--config", "file.conf", "--camera", "Camera name", "auto"])

    assert args.command == "led"
    assert args.config == "file.conf"
    assert args.camera == "Camera name"
    assert args.value == "auto"


def test_camera_led_auto_delegates_to_ir_setting():
    class FakeIr:
        def __init__(self):
            self.calls = []

        def status(self):
            self.calls.append("status")
            return {"mode": "off"}

        def on(self):
            self.calls.append("on")
            return {"mode": "on"}

        def off(self):
            self.calls.append("off")
            return {"mode": "off"}

        def auto(self):
            self.calls.append("auto")
            return {"mode": "auto"}

    class FakeSettings:
        def __init__(self, ir):
            self.ir = ir

    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    ir = FakeIr()
    camera.ensure_connected = lambda: None
    camera.settings = lambda: FakeSettings(ir)

    assert camera.led()["mode"] == "off"
    assert camera.led("auto")["mode"] == "auto"
    assert ir.calls == ["status", "auto"]


def test_ir_status_parses_led_state():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 3})()

        def command(self, msg_id, payload=b"", *, extension=b""):
            self.msg_id = msg_id
            self.extension = extension
            xml = xml_document(
                '<LedState version="1.1">'
                "<channelId>3</channelId>"
                "<ledVersion>2</ledVersion>"
                "<state>auto</state>"
                "<lightState>open</lightState>"
                "</LedState>"
            )
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

    camera = FakeCamera()
    status = Ir(camera).status()

    assert camera.msg_id == MSG.GET_LED
    assert b"<channelId>3</channelId>" in camera.extension
    assert status["mode"] == "auto"
    assert status["state"] == "auto"
    assert status["channel_id"] == 3
    assert status["light_state"] == "open"


def test_ir_auto_updates_state_and_preserves_status_led():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.sent = []
            self.replies = [
                xml_document(
                    '<LedState version="1.1">'
                    "<channelId>0</channelId>"
                    "<ledVersion>2</ledVersion>"
                    "<state>close</state>"
                    "<lightState>open</lightState>"
                    "</LedState>"
                ),
                xml_document(
                    '<LedState version="1.1">'
                    "<channelId>0</channelId>"
                    "<state>auto</state>"
                    "<lightState>open</lightState>"
                    "</LedState>"
                ),
            ]

        def command(self, msg_id, payload=b"", *, extension=b""):
            xml = self.replies.pop(0)
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

        def send(self, msg_id, payload=b"", *, extension=b"", **_kwargs):
            self.sent.append((msg_id, payload, extension))
            return 11

        def _recv(self, timeout=None):
            return Message(Header(MSG.SET_LED, 0, 0, 0, 11, 200, MSG_CLASS.MODERN), payload=b"")

    camera = FakeCamera()
    status = Ir(camera).auto()

    msg_id, payload, extension = camera.sent[0]
    assert msg_id == MSG.SET_LED
    assert b"<channelId>0</channelId>" in extension
    assert b"<LedState" in payload
    assert b"<state>auto</state>" in payload
    assert b"<lightState>open</lightState>" in payload
    assert b"<ledVersion>" not in payload
    assert status["mode"] == "auto"


def test_pir_status_parses_rf_alarm_cfg():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 2})()

        def command(self, msg_id, payload=b"", *, extension=b""):
            self.msg_id = msg_id
            self.extension = extension
            xml = xml_document(
                '<rfAlarmCfg version="1.1">'
                "<rfID>2</rfID>"
                "<enable>1</enable>"
                "<sensitivity>80</sensitivity>"
                "<sensiValue>3</sensiValue>"
                "<reduceFalseAlarm>1</reduceFalseAlarm>"
                "</rfAlarmCfg>"
            )
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

    camera = FakeCamera()
    status = Pir(camera).status()

    assert camera.msg_id == MSG.GET_PIR_ALARM
    assert b"<rfId>2</rfId>" in camera.extension
    assert status["enabled"] is True
    assert status["rf_id"] == 2
    assert status["sensitivity"] == 80
    assert status["sensi_value"] == 3
    assert status["reduce_false_alarm"] is True


def test_pir_on_updates_enable_and_preserves_payload_shape():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.sent = []
            self.replies = [
                xml_document(
                    '<rfAlarmCfg version="1.1">'
                    "<rfID>0</rfID>"
                    "<enable>0</enable>"
                    "<sensitivity>70</sensitivity>"
                    "<sensiValue>2</sensiValue>"
                    "<reduceFalseAlarm>0</reduceFalseAlarm>"
                    "<timeBlockList><timeBlock><enable>1</enable></timeBlock></timeBlockList>"
                    "</rfAlarmCfg>"
                ),
                xml_document(
                    '<rfAlarmCfg version="1.1">'
                    "<rfID>0</rfID>"
                    "<enable>1</enable>"
                    "<sensitivity>70</sensitivity>"
                    "<sensiValue>2</sensiValue>"
                    "<reduceFalseAlarm>0</reduceFalseAlarm>"
                    "<timeBlockList><timeBlock><enable>1</enable></timeBlock></timeBlockList>"
                    "</rfAlarmCfg>"
                ),
            ]

        def command(self, msg_id, payload=b"", *, extension=b""):
            xml = self.replies.pop(0)
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

        def send(self, msg_id, payload=b"", *, extension=b"", **_kwargs):
            self.sent.append((msg_id, payload, extension))
            return 9

        def _recv(self, timeout=None):
            return Message(Header(MSG.SET_PIR_ALARM, 0, 0, 0, 9, 200, MSG_CLASS.MODERN), payload=b"")

    camera = FakeCamera()
    status = Pir(camera).on()

    msg_id, payload, extension = camera.sent[0]
    assert msg_id == MSG.SET_PIR_ALARM
    assert b"<rfId>0</rfId>" in extension
    assert b"<rfAlarmCfg" in payload
    assert b"<enable>1</enable>" in payload
    assert b"<sensitivity>70</sensitivity>" in payload
    assert b"<timeBlockList>" in payload
    assert status["enabled"] is True


def test_voice_siren_sends_play_audio_payload():
    class FakeCamera:
        def __init__(self):
            self.config = type("Config", (), {"channel_id": 2})()
            self.calls = []
            self.debug = False

        def command(self, msg_id, payload=b"", *, extension=b""):
            self.calls.append((msg_id, payload, extension))
            return Message(Header(msg_id, 0, 0, 0, 1, 200, MSG_CLASS.MODERN), payload=b"")

    camera = FakeCamera()
    Voice(camera).siren()

    msg_id, payload, extension = camera.calls[0]
    assert msg_id == MSG.PLAY_AUDIO
    assert b"<channelId>2</channelId>" in extension
    assert b"<audioPlayInfo" in payload
    assert b"<playDuration>0</playDuration>" in payload
    assert b"<playTimes>1</playTimes>" in payload
    assert b"<onOff>0</onOff>" in payload


def test_camera_snapshot_collects_binary_snap_packets(tmp_path):
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

    info_xml = xml_document(
        '<Snap version="1.1"><channelId>0</channelId><fileName>front.jpg</fileName><pictureSize>4</pictureSize></Snap>'
    )
    binary_ext = payloads.extension_binary.format(channel_id=0)
    def snap_replies(msg_num):
        return b"".join(
            [
                encode_modern(MSG.SNAP, msg_num, info_xml, response_code=200, cipher=Cipher("none")),
                encode_modern(MSG.SNAP, 77, b"\xff\xd8", extension=binary_ext, response_code=200, cipher=Cipher("none")),
                encode_modern(MSG.SNAP, 77, b"\xff\xd9", extension=binary_ext, response_code=201, cipher=Cipher("none")),
            ]
        )

    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
    camera.sock = FakeSocket(snap_replies(1))
    camera.login_xml = "<logged-in />"
    camera.cipher = Cipher("none")

    data = camera.snapshot()
    sent = bytes(camera.sock.sent)
    header = Header.unpack_from(sent[:24])
    payload = sent[24:]

    assert data == b"\xff\xd8\xff\xd9"
    assert header.msg_id == MSG.SNAP
    assert b"<Snap" in payload
    assert b"<streamType>main</streamType>" in payload

    camera.sock = FakeSocket(snap_replies(2))
    path = camera.snapshot(out=tmp_path)
    assert path == tmp_path / "front.jpg"
    assert path.read_bytes() == b"\xff\xd8\xff\xd9"


def test_stream_recorder_writes_mpegts_and_stops_stream(tmp_path):
    class FakeCamera:
        def __init__(self):
            self.sent = []
            self.stopped = []
            self.replies = [
                Message(
                    Header(MSG.VIDEO, 0, 0, 0, 7, 200, MSG_CLASS.MODERN),
                    payload=_bcmedia_info(fps=15) + _bcmedia_iframe(),
                )
            ]

        def start_stream(self, stream):
            self.stream = stream
            return 7

        def send(self, *args, **kwargs):
            self.sent.append((args, kwargs))

        def _recv(self, timeout=None):
            if self.replies:
                return self.replies.pop(0)
            raise TimeoutError("done")

        def stop_stream(self, stream, msg_num):
            self.stopped.append((stream, msg_num))

    out = tmp_path / "clip"
    camera = FakeCamera()
    path = StreamRecorder(camera, out=out, stream="mainStream", duration=0.01).start().wait()
    data = path.read_bytes()

    assert path == tmp_path / "clip.ts"
    assert data.startswith(b"\x47")
    assert len(data) % 188 == 0
    assert camera.stopped == [("mainStream", 7)]


def _bcmedia_info(*, fps: int) -> bytes:
    data = bytearray(32)
    data[:4] = b"1001"
    data[4:16] = struct.pack("<III", 32, 2304, 1296)
    data[17] = fps
    return bytes(data)


def _bcmedia_iframe() -> bytes:
    payload = b"\x00\x00\x00\x01\x65\x88\x84"
    header = b"00dc" + b"H264" + struct.pack("<IIII", len(payload), 0, 0, 0)
    padding = b"\x00" * ((8 - len(payload) % 8) % 8)
    return header + payload + padding


def test_sd_card_filter_and_danger_guards():
    camera = Camera(uuid="ABCDEF0123456789", password="secret", state_path=None)
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
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

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
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

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
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

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
