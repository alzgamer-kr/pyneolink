import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pyneolink.core.media import MediaPacket
from pyneolink.core.udp_transport import RegistrationUnavailableError, connect_relay
from pyneolink.stream_server import HlsSession, MpegTsMuxer, _HlsSegmentBuilder, _hls_playlist


def session():
    return HlsSession(
        SimpleNamespace(name="offline"),
        "mainStream",
        state_path=None,
        debug=False,
        buffer_bytes=1024 * 1024,
        segment_seconds=2,
    )


def frame(timestamp, *, keyframe=True, marker=b"video"):
    magic = b"00dc" if keyframe else b"01dc"
    data = b"\x00\x00\x00\x01" + (b"\x65" if keyframe else b"\x41") + marker
    return magic + b"H264" + struct.pack("<IIII", len(data), 0, timestamp, 0) + data + b"\0" * (-len(data) % 8)


def info(fps):
    data = bytearray(b"1001" + struct.pack("<III", 32, 1920, 1080) + bytes(16))
    data[17] = fps
    return bytes(data)


def test_segment_duration_uses_media_time_despite_network_delay(monkeypatch):
    hls = session()
    builder = _HlsSegmentBuilder(hls, SimpleNamespace(sock=None))
    clock = iter(range(0, 10000, 30))
    monkeypatch.setattr("pyneolink.stream_server.time.monotonic", lambda: next(clock))
    builder.feed(info(2))
    for i in range(5):
        builder.feed(frame(i * 500000, keyframe=i % 4 == 0))
    builder.finish()
    builder.finish()
    assert [s.duration for s in hls.segments] == [2, 0.5]
    assert all(len(s.data) % 188 == 0 for s in hls.segments)


def test_sdk_transport_reconnect_resets_parser_and_waits_for_keyframe(monkeypatch):
    hls = session()

    class FakeCamera:
        sock = object()
        closed = False

        def connect(self):
            pass

        def login(self):
            pass

        def close(self):
            self.closed = True

        def read_stream_payloads(self, stream):
            yield info(2) + frame(10000000) + frame(10500000, keyframe=False)
            # Leave a long incomplete frame in the old parser at disconnect.
            yield b"00dcH264" + struct.pack("<IIII", 100000, 0, 11000000, 0)
            self.sock = object()
            yield info(2) + frame(0, keyframe=False, marker=b"discard-before-keyframe")
            yield frame(500000, marker=b"new-keyframe")
            yield frame(1000000, keyframe=False)
            yield frame(2500000)
            self.sock = object()
            yield info(4) + frame(0, marker=b"third-session")
            yield frame(2000000)

    camera = FakeCamera()
    monkeypatch.setattr("pyneolink.stream_server.Camera", lambda *a, **kw: camera)
    hls._run_stream()
    assert camera.closed
    assert hls._camera is None
    assert hls.reconnects == 2
    assert [s.duration for s in hls.segments] == [1, 2, 0.5, 2, 0.25]
    assert [s.discontinuity for s in hls.segments] == [False, True, False, True, False]
    assert [s.discontinuity_sequence for s in hls.segments] == [0, 1, 1, 2, 2]
    assert b"new-keyframe" in hls.segments[1].data
    assert b"third-session" in hls.segments[3].data
    assert b"discard-before-keyframe" not in b"".join(s.data for s in hls.segments)


@pytest.mark.parametrize("reset_timestamp", [0, 100])
def test_timestamp_reset_or_wrap_waits_for_keyframe(reset_timestamp):
    hls = session()
    builder = _HlsSegmentBuilder(hls, SimpleNamespace(sock=None))
    builder.feed(info(2) + frame(0xFFFFFF00))
    builder.feed(frame(reset_timestamp, keyframe=False, marker=b"discard-reset-pframe"))
    builder.feed(frame(reset_timestamp + 500000, keyframe=False))
    builder.feed(frame(reset_timestamp + 1000000))
    builder.finish()
    assert len(hls.segments) == 2
    assert [s.duration for s in hls.segments] == [0.5, 0.5]
    assert hls.segments[-1].discontinuity_sequence == 1
    assert b"discard-reset-pframe" not in b"".join(s.data for s in hls.segments)
    assert hls.reconnects == 0


def test_missing_timestamps_use_same_frame_rate_as_muxer():
    hls = session()
    builder = _HlsSegmentBuilder(hls, SimpleNamespace(sock=None))
    builder.feed(info(2))
    for i in range(5):
        builder._add_packet(MediaPacket("iframe" if i % 4 == 0 else "pframe", "H264", None, b"frame"))
    assert hls.segments[0].duration == 2


def test_playlist_discontinuity_sequence_survives_buffer_eviction():
    hls = session()
    hls._append_segment(b"old", 2)
    hls._mark_discontinuity()
    hls._mark_discontinuity()
    hls._append_segment(b"new", 2)
    hls._append_segment(b"next", 2)
    full = _hls_playlist(hls.segments, 2)
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in full
    assert "#EXT-X-DISCONTINUITY\n#EXTINF:2.000,\nsegments/1.ts" in full
    hls.buffer_bytes = 6
    hls._append_segment(b"last", 2)
    slid = _hls_playlist(hls.segments, 2)
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in slid
    assert "#EXT-X-DISCONTINUITY\n" not in slid


def test_outer_hls_retry_also_marks_discontinuity(monkeypatch):
    hls = session()
    attempts = []

    def run_stream():
        attempts.append(1)
        if len(attempts) == 1:
            hls._append_segment(b"old", 2)
            raise EOFError("transport unavailable")
        hls._append_segment(b"new", 2)
        hls._stop.set()

    monkeypatch.setattr(hls, "_run_stream", run_stream)
    monkeypatch.setattr(hls._stop, "wait", lambda timeout: False)
    hls._run()
    assert hls.reconnects == 1
    assert hls.segments[-1].discontinuity


def test_stop_does_not_publish_pending_segment():
    hls = session()
    builder = _HlsSegmentBuilder(hls, SimpleNamespace(sock=None))
    builder.feed(frame(0))
    hls._stop.set()
    builder.finish()
    assert hls.segments == []


@pytest.mark.parametrize("pcr", [None, 90000])
def test_ts_discontinuity_flag_preserves_payload_and_packet_size(pcr):
    for size in range(1, 560):
        muxer = MpegTsMuxer("H264")
        muxer._discontinuity_pids.add(muxer.VIDEO_PID)
        packets = list(muxer._packetize(muxer.VIDEO_PID, b"x" * size, start=True, pcr=pcr))
        assert all(len(packet) == 188 for packet in packets)
        assert packets[0][3] & 0x20
        assert packets[0][4] >= 1
        assert packets[0][5] & 0x80
        restored = b"".join(packet[5 + packet[4] :] if packet[3] & 0x20 else packet[4:] for packet in packets)
        assert restored == b"x" * size
        following = list(muxer._packetize(muxer.VIDEO_PID, b"next", start=True, pcr=pcr))
        assert not following[0][5] & 0x80


def test_hls_signals_new_timeline_on_each_ts_pid_once():
    hls = session()
    builder = _HlsSegmentBuilder(hls, SimpleNamespace(sock=None))
    builder.feed(info(2) + frame(0))
    builder._add_packet(MediaPacket("aac", None, None, b"\xff\xf1\x50\x80\x00\x1f\xfc"))
    builder.feed(frame(2000000))
    builder.finish()
    first_by_pid = {}
    for segment in hls.segments:
        for offset in range(0, len(segment.data), 188):
            packet = segment.data[offset : offset + 188]
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            marked = bool(packet[3] & 0x20 and packet[4] and packet[5] & 0x80)
            if pid not in first_by_pid:
                assert marked
                first_by_pid[pid] = packet
            else:
                assert not marked
    assert set(first_by_pid) == {0, 256, 257, 258}


def test_registration_failures_use_long_wait_but_stream_failures_do_not(monkeypatch):
    hls = session()
    failures = iter(
        [
            RegistrationUnavailableError("no details"),
            RegistrationUnavailableError("no reply"),
            TimeoutError("stream stalled"),
            None,
        ]
    )
    waits = []
    uniform = Mock(return_value=180.0)

    def run_stream():
        error = next(failures)
        if error is not None:
            raise error
        hls._stop.set()

    monkeypatch.setattr(hls, "_run_stream", run_stream)
    monkeypatch.setattr(hls._stop, "wait", waits.append)
    monkeypatch.setattr("pyneolink.stream_server.random.uniform", uniform)
    hls._run()
    assert waits == [180, 180, 5]
    assert uniform.call_count == 2
    uniform.assert_called_with(120.0, 300.0)


def test_stop_interrupts_registration_wait_without_another_connection(monkeypatch):
    hls = session()
    waiting = threading.Event()
    original_wait = hls._stop.wait
    attempts = Mock(side_effect=RegistrationUnavailableError("no details"))

    def wait(timeout):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(hls, "_run_stream", attempts)
    monkeypatch.setattr(hls._stop, "wait", wait)
    hls._thread = threading.Thread(target=hls._run)
    hls._thread.start()
    try:
        assert waiting.wait(timeout=2)
    finally:
        hls.stop()
    assert not hls._thread.is_alive()
    assert attempts.call_count == 1


@pytest.mark.parametrize("timeout", [False, True])
def test_register_failure_is_typed_timeout_and_closes_socket(monkeypatch, timeout):
    sock = Mock()
    sock.getsockname.return_value = ("", 12345)
    monkeypatch.setattr("pyneolink.core.udp_transport.socket.socket", lambda *args: sock)
    monkeypatch.setattr("pyneolink.core.udp_transport._lookup_with_socket", lambda *args, **kw: SimpleNamespace(xml=""))
    monkeypatch.setattr("pyneolink.core.udp_transport._find_in_xml", lambda *args: ("127.0.0.1", 9999))
    monkeypatch.setattr("pyneolink.core.udp_transport._local_ip_for", lambda *args: "127.0.0.1")
    reply = Mock(return_value="<R2C_C_R/>", side_effect=TimeoutError("no reply") if timeout else None)
    monkeypatch.setattr("pyneolink.core.udp_transport._retry_discovery", reply)
    with pytest.raises(RegistrationUnavailableError) as raised:
        connect_relay("offline")
    assert isinstance(raised.value, TimeoutError)
    sock.close.assert_called_once()
