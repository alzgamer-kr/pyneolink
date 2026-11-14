import errno
import struct
import threading
import multiprocessing
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pyneolink import Camera
from pyneolink.camera import _MessageSubscription
from pyneolink.core.bc import Header, Message, ProtocolError, InterruptedMessageError, recv_message
from pyneolink.core.const import MSG, MSG_CLASS
from pyneolink.core.crypto import Cipher
from pyneolink.core.media import _extract_media_streams
from pyneolink.core.state import ConnectionState
from pyneolink.sd_card import SdCard, SdCardFile, _download_binary_scope
from pyneolink.stream_server import HlsSession, MpegTsMuxer, StreamServer


def test_ts_packet_sizes_cover_every_tail_length():
    for pcr in (None, 90000):
        for size in range(1, 740):
            packets = list(MpegTsMuxer("H264")._packetize(257, b"x" * size, pcr=pcr))
            assert all(len(packet) == 188 for packet in packets), (pcr, size)
            restored = b"".join(packet[5 + packet[4] :] if packet[3] & 0x20 else packet[4:] for packet in packets)
            assert restored == b"x" * size


def test_h264_pframe_does_not_change_extracted_codec(tmp_path):
    def frame(magic, payload):
        header = magic + b"H264" + struct.pack("<IIII", len(payload), 0, 100000, 0)
        return header + payload + b"\0" * (-len(payload) % 8)

    source = tmp_path / "input.bcmedia"
    source.write_bytes(
        frame(b"00dc", bytes.fromhex("00000001658884")) + frame(b"01dc", bytes.fromhex("00000001419a22"))
    )
    codec, _, frames, _ = _extract_media_streams(source, tmp_path / "video", tmp_path / "audio")
    assert (codec, frames) == ("H264", 2)


@pytest.mark.parametrize("received", [5, 20, 22, 24, 30])
def test_partial_message_timeout_is_not_treated_as_idle(received, monkeypatch):
    wire = Header(MSG.BATTERY, 40, 0, 0, 1, 200, MSG_CLASS.MODERN).pack() + b"x" * 40

    class Socket:
        def __init__(self):
            self.remaining = wire[:received]

        def settimeout(self, timeout):
            pass

        def recv(self, size):
            if not self.remaining:
                raise TimeoutError("paused")
            result, self.remaining = self.remaining[:size], self.remaining[size:]
            return result

    clock = iter(range(0, 10000, 30))
    monkeypatch.setattr("pyneolink.core.bc.time.monotonic", lambda: next(clock))
    with pytest.raises(ProtocolError):
        recv_message(Socket(), Cipher("none"), timeout=0.5)


def test_empty_socket_timeout_remains_idle():
    sock = SimpleNamespace(settimeout=lambda timeout: None, recv=Mock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        recv_message(sock, Cipher("none"), timeout=0.5)


def test_short_gap_keeps_partial_header_and_following_message():
    packet = Header(MSG.BATTERY, 0, 0, 0, 1, 200, MSG_CLASS.MODERN).pack()

    class Socket:
        def __init__(self):
            self.chunks = [packet[:5], TimeoutError("short gap"), packet[5:] + packet]

        def settimeout(self, timeout):
            pass

        def recv(self, size):
            chunk = self.chunks.pop(0)
            if isinstance(chunk, Exception):
                raise chunk
            if len(chunk) > size:
                self.chunks.insert(0, chunk[size:])
            return chunk[:size]

    sock = Socket()
    for _ in range(2):
        assert recv_message(sock, Cipher("none"), timeout=0.5).header.msg_id == MSG.BATTERY


def test_subscription_overflow_fails_instead_of_returning_incomplete_data():
    camera = Camera(uid="test", state_path=None)
    subscription = _MessageSubscription(camera, MSG.FILE_PLAYBACK, 1, maxsize=2)
    for response in (200, 200, 300):
        subscription.put(Message(Header(MSG.FILE_PLAYBACK, 0, 0, 0, 1, response, MSG_CLASS.MODERN)))
    with pytest.raises(ProtocolError, match="overflow"):
        subscription.recv(timeout=0)


def test_camera_context_failure_closes_transport(monkeypatch):
    camera = Camera(uid="test", state_path=None)
    sock = SimpleNamespace(close=Mock())
    camera.sock = sock
    monkeypatch.setattr(camera, "connect", lambda: None)
    monkeypatch.setattr(camera, "login", Mock(side_effect=ProtocolError("login failed")))
    with pytest.raises(ProtocolError):
        with camera:
            pass
    sock.close.assert_called_once()
    assert camera.sock is None


@pytest.fixture
def receive_clock(monkeypatch):
    """Advance receive deadlines without sleeping or hanging on regressions."""
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds
        assert elapsed[0] <= 1, "Receive exceeded its timeout budget"

    monkeypatch.setattr("pyneolink.camera.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("pyneolink.camera.time.sleep", sleep)
    return elapsed


@pytest.mark.parametrize("timeout, expected", [(None, 0.05), (0.02, 0.02), (0, 0)])
def test_dispatched_receive_has_bounded_default_timeout(receive_clock, timeout, expected):
    camera = Camera(uid="test", state_path=None, timeout=0.05)
    with pytest.raises(TimeoutError):
        camera._recv_dispatched(timeout=timeout)
    assert receive_clock[0] == pytest.approx(expected)


@pytest.mark.parametrize("nonce_received", [False, True])
def test_reconnect_login_timeout_closes_transport(monkeypatch, receive_clock, nonce_received):
    camera = Camera(uid="test", state_path=None, timeout=0.05)
    camera._dispatcher_requested = True
    transport = SimpleNamespace(sendall=Mock(), close=Mock())

    def connect():
        camera.sock = transport
        if nonce_received:
            camera._dispatch_unmatched.append(
                Message(
                    Header(MSG.LOGIN, 0, 0, 0, 1, 0xDC00, MSG_CLASS.MODERN),
                    payload=b"<body><nonce>test-nonce</nonce></body>",
                )
            )

    monkeypatch.setattr(camera, "connect", connect)
    # Keep the real login and dispatcher wait; simulate a reader with no replies.
    monkeypatch.setattr(camera, "_ensure_dispatcher_active", lambda: None)
    with pytest.raises(TimeoutError):
        camera.reconnect()
    assert receive_clock[0] == pytest.approx(0.05)
    assert transport.sendall.call_count == (2 if nonce_received else 1)
    transport.close.assert_called_once()
    assert camera.sock is None
    assert camera.login_xml == ""
    assert camera._dispatcher_requested


def test_dispatched_receive_returns_queued_reply_before_zero_timeout():
    camera = Camera(uid="test", state_path=None)
    reply = Message(Header(MSG.BATTERY, 0, 0, 0, 1, 200, MSG_CLASS.MODERN))
    camera._dispatch_unmatched.append(reply)
    assert camera._recv_dispatched(timeout=0) is reply


@pytest.mark.parametrize("error", [EOFError("closed"), ConnectionResetError("reset")])
def test_download_retries_disconnection(tmp_path, monkeypatch, error):
    camera = SimpleNamespace(config=SimpleNamespace(channel_id=0), reconnect=Mock(), sock=None)
    sd = SdCard(camera)
    output = tmp_path / "out.mp4"
    download = Mock(side_effect=[error, output])
    monkeypatch.setattr(sd, "_download_once", download)
    path = sd._download_file(
        {"file_name": "clip.mp4", "size": 100},
        output=output,
        reconnect_retries=1,
        reconnect_delay=0,
        reconnect_jitter=0,
    )
    assert path == output
    camera.reconnect.assert_called_once()


def test_disk_error_does_not_reconnect_camera(tmp_path, monkeypatch):
    camera = SimpleNamespace(config=SimpleNamespace(channel_id=0), reconnect=Mock(), sock=None)
    sd = SdCard(camera)
    monkeypatch.setattr(sd, "_download_once", Mock(side_effect=OSError(errno.ENOSPC, "disk full")))
    with pytest.raises(OSError) as error:
        sd._download_file({"file_name": "clip.mp4"}, output=tmp_path / "out.mp4")
    assert error.value.errno == errno.ENOSPC
    camera.reconnect.assert_not_called()


def test_sd_listing_does_not_report_partial_pages_as_success(monkeypatch):
    sd = SdCard(SimpleNamespace())
    reply = Message(Header(1, 0, 0, 0, 1, 200, MSG_CLASS.MODERN))
    monkeypatch.setattr("pyneolink.sd_card._send_query", Mock(side_effect=[reply, None]))
    monkeypatch.setattr("pyneolink.sd_card._parse_file_list", lambda root: [SdCardFile(file_name="clip.mp4")])
    with pytest.raises((ProtocolError, TimeoutError)):
        sd._list_handle_files(0, "handle", [], set(), [])


@pytest.mark.parametrize("shared_instance", [False, True])
def test_state_concurrent_updates_are_merged(tmp_path, shared_instance):
    path = tmp_path / "state.json"
    states = [ConnectionState(path), ConnectionState(path)]
    if shared_instance:
        states[1] = states[0]
    start = threading.Barrier(2)
    errors = []

    def update(index):
        try:
            start.wait(timeout=5)
            for number in range(10):
                states[index].update_address(str(index), f"127.0.0.1:{9000 + number}")
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=update, args=(0,))
    second = threading.Thread(target=update, args=(1,))
    first.start()
    second.start()
    first.join(15)
    second.join(15)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    saved = ConnectionState(path)
    assert saved.get_address("0") == "127.0.0.1:9009"
    assert saved.get_address("1") == "127.0.0.1:9009"
    assert path.with_suffix(".db").is_file()
    assert not path.exists()
    assert not path.with_suffix(".json.lock").exists()


def test_server_stop_stops_hls_workers():
    session = HlsSession(None, "high", state_path=None, debug=False, buffer_bytes=1024, segment_seconds=2)
    entered = threading.Event()

    def run_stream():
        entered.set()
        session._stop.wait(3)

    session._run_stream = run_stream
    session.start()
    assert entered.wait(3)
    server = StreamServer({"cameras": []}, state_path=None)
    server._server = SimpleNamespace(
        shutdown=Mock(),
        server_close=Mock(),
        hls_sessions={"test": session},
        hls_sessions_lock=threading.Lock(),
    )
    try:
        server.stop()
        assert session._stop.is_set()
        assert not session._thread.is_alive()
    finally:
        session.stop()
        if getattr(session, "_thread", None):
            session._thread.join(4)


def test_stopping_dispatcher_cannot_fail_new_subscriptions(monkeypatch):
    camera = Camera(uid="test", state_path=None)
    entered = threading.Event()
    release = threading.Event()

    def receive(**kwargs):
        entered.set()
        release.wait(5)
        raise ConnectionResetError("old connection closed")

    monkeypatch.setattr(camera, "_recv_direct", receive)
    camera.start_dispatcher()
    old_thread = camera._dispatch_thread
    try:
        assert entered.wait(3)
        camera.stop_dispatcher()
        assert old_thread.is_alive()
        with pytest.raises(ProtocolError, match="still stopping"):
            camera.start_dispatcher()
        subscription = _MessageSubscription(camera, MSG.BATTERY, 2)
        camera._dispatch_waiters[(MSG.BATTERY, 2)] = [subscription]
        release.set()
        old_thread.join(3)
        assert not old_thread.is_alive()
        assert subscription.active
        assert subscription._error is None
    finally:
        release.set()
        old_thread.join(5)
        camera.close()


def test_close_interrupts_reader_inside_message():
    camera = Camera(uid="test", state_path=None)
    reader, writer = socket.socketpair()
    camera.sock = reader
    writer.sendall(Header(MSG.BATTERY, 100, 0, 0, 1, 200, MSG_CLASS.MODERN).pack() + b"partial")
    camera.start_dispatcher()
    thread = camera._dispatch_thread
    try:
        camera.close()
        assert not thread.is_alive()
        assert camera.sock is None
        assert camera._dispatch_thread is None
    finally:
        writer.close()
        reader.close()


def test_subscription_close_wakes_blocked_receiver():
    camera = Camera(uid="test", state_path=None)
    subscription = _MessageSubscription(camera, MSG.BATTERY, 1)
    entered = threading.Event()
    errors = []

    def receive():
        entered.set()
        try:
            subscription.recv()
        except TimeoutError as error:
            errors.append(error)

    thread = threading.Thread(target=receive)
    thread.start()
    assert entered.wait(3)
    subscription.close()
    thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1


def test_download_binary_routes_are_scoped_to_transfer():
    camera = Camera(uid="test", state_path=None)
    camera.binary_msg_nums.add(10)
    with pytest.raises(ValueError), _download_binary_scope(camera) as mark:
        mark(10)
        mark(11)
        mark(0)
        assert camera.binary_msg_nums == {10, 11}
        raise ValueError("disk error")
    assert camera.binary_msg_nums == {10}
    camera.close()
    assert not camera.binary_msg_nums


def test_old_binary_scope_does_not_remove_new_session_routes():
    camera = Camera(uid="test", state_path=None)
    camera.sock = SimpleNamespace(close=lambda: None)
    with _download_binary_scope(camera) as mark:
        mark(11)
        camera.close()
        camera.sock = SimpleNamespace(close=lambda: None)
        camera.binary_msg_nums.add(11)
    assert camera.binary_msg_nums == {11}
    camera.close()


def test_message_numbers_skip_active_routes():
    camera = Camera(uid="test", state_path=None)
    camera.msg_num = 65535
    camera.binary_msg_nums.add(1)
    camera._dispatch_waiters[(MSG.BATTERY, 2)] = []
    assert camera._next_msg() == 3


def test_explicit_close_does_not_restart_stream(monkeypatch):
    camera = Camera(uid="test", state_path=None)
    monkeypatch.setattr(camera, "ensure_connected", lambda: None)
    reconnect = Mock()
    monkeypatch.setattr(camera, "reconnect", reconnect)

    def payloads(*args, **kwargs):
        yield b"frame"
        camera.close()
        raise EOFError("closed")

    monkeypatch.setattr(camera, "_read_stream_payloads_dispatched", payloads)
    assert list(camera.read_stream_payloads()) == [b"frame"]
    reconnect.assert_not_called()


def test_command_retries_partial_frame_error(monkeypatch):
    camera = Camera(uid="test", state_path=None)
    reply = Message(Header(MSG.BATTERY, 0, 0, 0, 1, 200, MSG_CLASS.MODERN))
    exchange = SimpleNamespace(recv=Mock(side_effect=[InterruptedMessageError("frame"), reply]))

    from contextlib import nullcontext

    monkeypatch.setattr(camera, "request_messages", lambda *args, **kwargs: nullcontext(exchange))
    reconnect = Mock()
    monkeypatch.setattr(camera, "reconnect", reconnect)
    assert camera.command(MSG.BATTERY) is reply
    reconnect.assert_called_once()


def test_binary_exchange_releases_owned_route(monkeypatch):
    camera = Camera(uid="test", state_path=None)
    camera.sock = SimpleNamespace(sendall=Mock(), close=Mock())
    monkeypatch.setattr(camera, "ensure_connected", lambda: None)
    with camera.request_messages(MSG.SNAP, binary_reply=True) as exchange:
        number = exchange.msg_num
        assert number in camera.binary_msg_nums
    assert number not in camera.binary_msg_nums
    camera.close()


def test_hls_idle_shutdown_and_restart(monkeypatch):
    session = HlsSession(None, "high", state_path=None, debug=False, buffer_bytes=1024, segment_seconds=2)
    monkeypatch.setattr(session, "_run_stream", lambda: session._stop.wait(3))
    session.start()
    first_thread = session._thread
    try:
        session._last_access -= 120
        assert session.idle_expired()
        session.stop_if_idle()
        assert not first_thread.is_alive()
        assert not session.started
        session.start()
        assert session._thread is not first_thread
        assert not session._stop.is_set()
        assert not session.idle_expired()
    finally:
        session.stop()


def _update_state_process(path, name, ready, start):
    state = ConnectionState(path)
    ready.put(name)
    if not start.wait(10):
        raise RuntimeError("State test start timed out")
    state.update_address(name, "127.0.0.1:9000")


def test_state_merges_updates_from_separate_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    path = tmp_path / "state.json"
    processes = [
        context.Process(target=_update_state_process, args=(str(path), name, ready, start))
        for name in ("front", "back")
    ]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=15), ready.get(timeout=15)} == {"front", "back"}
        start.set()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        saved = ConnectionState(path)
        assert saved.get_address("front") == "127.0.0.1:9000"
        assert saved.get_address("back") == "127.0.0.1:9000"
        assert path.with_suffix(".db").is_file()
        assert not path.exists()
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        ready.close()
        ready.join_thread()
