from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from collections import OrderedDict

from .discovery import (
    DiscoveryHit,
    decode_discovery_packet,
    encode_discovery_xml,
)
from .const import MAGIC, msg

MTU = 1350
UDP_DATA_HEADER_SIZE = 20
UDP_ACK_INTERVAL_SECONDS = 0.031
UDP_HEARTBEAT_INTERVAL_SECONDS = 1.0
UDP_RESEND_INTERVAL_SECONDS = 0.5
UDP_SOCKET_BUFFER_BYTES = 4 * 1024 * 1024


class UdpBcConnection:
    """Socket-like reliable UDP channel for Baichuan bytes."""

    def __init__(
        self,
        sock: socket.socket,
        addr: tuple[str, int],
        client_id: int,
        camera_id: int,
        *,
        timeout: float = 10.0,
        heartbeat_tid: int | None = None,
        auto_maintenance: bool = True,
        auto_receive: bool | None = None,
    ) -> None:
        """Create a UDP Baichuan connection.

        :param sock: UDP socket.
        :param addr: Remote camera/relay address.
        :param client_id: Local/client connection id.
        :param camera_id: Remote/camera connection id.
        :param timeout: Read timeout in seconds.
        :param heartbeat_tid: Optional discovery heartbeat transaction id.
        :param auto_maintenance: Start a background ACK/heartbeat worker.
        :param auto_receive: Start a worker that continuously drains the UDP
            socket. Defaults to the value of ``auto_maintenance``.
        """
        self.sock = sock
        self.addr = addr
        self.client_id = client_id
        self.camera_id = camera_id
        self.heartbeat_tid = heartbeat_tid if heartbeat_tid is not None else _tid()
        self.timeout = timeout
        self.next_send_id = 0
        self.next_recv_id = 0
        self.sent_chunks: OrderedDict[int, bytes] = OrderedDict()
        self.recv_chunks: dict[int, bytes] = {}
        self.buffer = bytearray()
        self.max_pending_chunks: int | None = None
        self.closed = False
        self.last_ack_at = 0.0
        self.last_ack_packet_id: int | None = None
        self.last_resend_at = 0.0
        self.last_heartbeat_at = 0.0
        self.ack_receive_rate = 0
        self._ack_receive_bytes = 0
        self._ack_receive_window_started_at = time.monotonic()
        self.data_packets_received = 0
        self.data_bytes_received = 0
        self.duplicate_packets_received = 0
        self.ignored_packets = 0
        self.unknown_packets = 0
        self.acks_sent = 0
        self.acks_received = 0
        self.heartbeats_sent = 0
        self.resend_packets_sent = 0
        self.last_data_packet_id: int | None = None
        self.max_data_packet_id: int | None = None
        self.last_data_at = 0.0
        self._state_lock = threading.RLock()
        self._data_ready = threading.Condition(self._state_lock)
        self._send_lock = threading.RLock()
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._receive_stop = threading.Event()
        self._receive_thread: threading.Thread | None = None
        self._receive_error: BaseException | None = None
        self._receive_requested = auto_maintenance if auto_receive is None else auto_receive
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_SOCKET_BUFFER_BYTES)
        except (AttributeError, OSError):
            pass
        try:
            self.socket_receive_buffer = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        except (AttributeError, OSError):
            self.socket_receive_buffer = None
        self.sock.settimeout(0.01)
        if self._receive_requested:
            self.start_receiver()
        if auto_maintenance:
            self.start_maintenance()

    def settimeout(self, timeout: float | None) -> None:
        """
        Set the socket-level receive timeout used by the stream wrapper.

        :param timeout: Timeout in seconds, or ``None`` to keep the current value.
        """

        if timeout is not None:
            self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        """
        Send bytes as tracked UDP chunks.

        :param data: Bytes to send.
        """

        packets = []
        with self._state_lock:
            for chunk in _chunks(data, MTU - UDP_DATA_HEADER_SIZE):
                packet_id = self.next_send_id
                packet = encode_udp_data(self.camera_id, packet_id, chunk)
                self.sent_chunks[packet_id] = chunk
                self.next_send_id += 1
                packets.append(packet)
            self.last_resend_at = time.monotonic()
        self._send_packets(packets)

    def send_untracked(self, data: bytes) -> None:
        """
        Send bytes as UDP chunks without storing them for resend.

        :param data: Bytes to send.
        """

        packets = []
        with self._state_lock:
            for chunk in _chunks(data, MTU - UDP_DATA_HEADER_SIZE):
                packet_id = self.next_send_id
                packet = encode_udp_data(self.camera_id, packet_id, chunk)
                self.next_send_id += 1
                packets.append(packet)
        self._send_packets(packets)

    def recv(self, size: int) -> bytes:
        """
        Receive up to ``size`` bytes from the buffered UDP stream.

        This mirrors ``socket.recv`` semantics. Returning available data lets
        callers such as ``recv_exact`` reset their timeout after each part of a
        large Baichuan message instead of abandoning a partially received body.

        :param size: Number of bytes to return.
        """

        if size <= 0:
            return b""
        return self._recv_buffered(size)

    def recv_some(self, size: int) -> bytes:
        """
        Receive up to ``size`` bytes once buffered data is available.

        :param size: Maximum number of bytes to return.
        """

        return self._recv_buffered(size)

    def _recv_buffered(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while True:
            with self._data_ready:
                if self.buffer:
                    take = min(size, len(self.buffer))
                    result = bytes(self.buffer[:take])
                    del self.buffer[:take]
                    return result
                if self._receive_error is not None:
                    raise self._receive_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(msg.Error.UdpBaichuanTimeout)
                if self._receive_requested:
                    self._data_ready.wait(timeout=remaining)
                    continue
            self._recv_one()

    def close(self) -> None:
        if self.closed:
            return
        with self._data_ready:
            self.closed = True
            self._data_ready.notify_all()
        self.stop_receiver()
        self.stop_maintenance()
        if hasattr(self.sock, "close"):
            self.sock.close()

    def maintain(self) -> None:
        self._maintenance()

    def start_receiver(self) -> None:
        """Start the worker that drains and reorders incoming UDP packets."""

        if self.closed:
            return
        thread = self._receive_thread
        if thread is not None and thread.is_alive():
            return
        self._receive_requested = True
        self._receive_error = None
        self._receive_stop.clear()
        self._receive_thread = threading.Thread(
            target=self._receive_loop,
            name="pyneolink-udp-receive",
            daemon=True,
        )
        self._receive_thread.start()

    def stop_receiver(self) -> None:
        """Stop the background UDP receive worker."""

        self._receive_stop.set()
        with self._data_ready:
            self._data_ready.notify_all()
        thread = self._receive_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._receive_thread = None

    def start_maintenance(self) -> None:
        """Start independent UDP ACK/heartbeat maintenance."""

        if self.closed:
            return
        thread = self._maintenance_thread
        if thread is not None and thread.is_alive():
            return
        self._maintenance_stop.clear()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="pyneolink-udp-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def stop_maintenance(self) -> None:
        """Stop independent UDP ACK/heartbeat maintenance."""

        self._maintenance_stop.set()
        thread = self._maintenance_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._maintenance_thread = None

    def discard_sent(self) -> None:
        with self._state_lock:
            self.sent_chunks.clear()

    def set_max_pending_chunks(self, limit: int | None) -> None:
        """
        Limit how many future UDP chunks are buffered before older gaps are skipped.

        :param limit: Maximum pending chunk count, or ``None`` for no limit.
        """

        with self._state_lock:
            self.max_pending_chunks = limit

    def _recv_one(self) -> None:
        self._maintenance()
        try:
            data, addr = self.sock.recvfrom(65535)
        except TimeoutError:
            return
        parsed = decode_udp_packet(data)
        if not parsed:
            return
        kind = parsed[0]
        if kind == "data":
            _kind, connection_id, packet_id, payload = parsed
            with self._state_lock:
                if connection_id != self.client_id:
                    self.ignored_packets += 1
                    return
                if packet_id in self.recv_chunks or packet_id < self.next_recv_id:
                    self.duplicate_packets_received += 1
                    self.last_data_at = time.monotonic()
                    return
                self.recv_chunks[packet_id] = payload
                self.data_packets_received += 1
                self.data_bytes_received += len(payload)
                self._ack_receive_bytes += len(payload)
                self._refresh_ack_receive_rate()
                self.last_data_packet_id = packet_id
                self.max_data_packet_id = (
                    packet_id if self.max_data_packet_id is None else max(self.max_data_packet_id, packet_id)
                )
                self.last_data_at = time.monotonic()
                self._raise_if_pending_overflow()
                while self.next_recv_id in self.recv_chunks:
                    self.buffer.extend(self.recv_chunks.pop(self.next_recv_id))
                    self.next_recv_id += 1
                if self.buffer:
                    self._data_ready.notify_all()
        elif kind == "ack":
            _kind, connection_id, _group_id, packet_id, _latency, payload = parsed
            if connection_id == self.client_id:
                with self._state_lock:
                    self.acks_received += 1
                    self._handle_ack(packet_id, payload)
        elif kind == "discovery":
            return
        else:
            with self._state_lock:
                self.unknown_packets += 1

    def _send_ack(self) -> None:
        with self._state_lock:
            packet_id, payload, group_id = self._ack_state()
            self._refresh_ack_receive_rate()
            receive_rate = self.ack_receive_rate
        packet = encode_udp_ack(self.camera_id, packet_id, payload, group_id, maybe_latency=receive_rate)
        self._send_packets([packet])
        with self._state_lock:
            if packet_id != 0xFFFFFFFF:
                self.last_ack_packet_id = packet_id
            self.acks_sent += 1
            self.last_ack_at = time.monotonic()

    def _raise_if_pending_overflow(self) -> None:
        with self._state_lock:
            if self.max_pending_chunks is None or len(self.recv_chunks) <= self.max_pending_chunks:
                return
            self.recv_chunks.clear()
            self.buffer.clear()
            if self.max_data_packet_id is not None:
                self.next_recv_id = self.max_data_packet_id + 1
        raise TimeoutError(msg.Error.UdpBaichuanTimeout)

    def _ack_state(self) -> tuple[int, bytes, int]:
        with self._state_lock:
            if self.next_recv_id == 0:
                return 0xFFFFFFFF, b"", 0xFFFFFFFF
            first_missing = self.next_recv_id
            while first_missing in self.recv_chunks:
                first_missing += 1
            end = max(self.recv_chunks.keys(), default=first_missing - 1)
            payload = bytes(1 if packet_id in self.recv_chunks else 0 for packet_id in range(first_missing, end + 1))
            return first_missing - 1, payload, 0

    def _handle_ack(self, packet_id: int, payload: bytes) -> None:
        with self._state_lock:
            if packet_id != 0xFFFFFFFF:
                for sent_id in list(self.sent_chunks):
                    if sent_id <= packet_id:
                        del self.sent_chunks[sent_id]
                for idx, value in enumerate(payload):
                    sent_id = packet_id + 1 + idx
                    if value:
                        self.sent_chunks.pop(sent_id, None)

    def _maintenance(self) -> None:
        now = time.monotonic()
        with self._state_lock:
            ack_due = now - self.last_ack_at >= UDP_ACK_INTERVAL_SECONDS
            if ack_due:
                self.last_ack_at = now
        if ack_due:
            self._send_ack()
        if now - self.last_resend_at >= UDP_RESEND_INTERVAL_SECONDS:
            self._resend_pending(now)
        if now - self.last_heartbeat_at >= UDP_HEARTBEAT_INTERVAL_SECONDS:
            self._send_heartbeat()

    def _refresh_ack_receive_rate(self, now: float | None = None) -> None:
        """Update the receive-rate value carried in outgoing UDP ACK packets."""

        current = time.monotonic() if now is None else now
        elapsed = current - self._ack_receive_window_started_at
        if elapsed < 1.0:
            return
        self.ack_receive_rate = int(self._ack_receive_bytes / elapsed)
        self._ack_receive_bytes = 0
        self._ack_receive_window_started_at = current

    def _send_heartbeat(self) -> None:
        xml = f"<P2P><C2D_HB><cid>{self.client_id}</cid><did>{self.camera_id}</did></C2D_HB></P2P>"
        self._send_packets([encode_discovery_xml(self.heartbeat_tid, xml)])
        with self._state_lock:
            self.heartbeats_sent += 1
            self.last_heartbeat_at = time.monotonic()

    def _resend_pending(self, now: float | None = None) -> None:
        with self._state_lock:
            if not self.sent_chunks:
                return
            packets = [
                encode_udp_data(self.camera_id, packet_id, chunk) for packet_id, chunk in list(self.sent_chunks.items())
            ]
            self.last_resend_at = time.monotonic() if now is None else now
        self._send_packets(packets)
        with self._state_lock:
            self.resend_packets_sent += len(packets)

    def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.wait(UDP_ACK_INTERVAL_SECONDS):
            if self.closed:
                return
            try:
                self._maintenance()
            except OSError:
                return

    def _receive_loop(self) -> None:
        while not self._receive_stop.is_set():
            if self.closed:
                return
            try:
                self._recv_one()
            except BaseException as exc:
                if self.closed or self._receive_stop.is_set():
                    return
                with self._data_ready:
                    self._receive_error = exc
                    self._data_ready.notify_all()
                return

    def _send_packets(self, packets: list[bytes]) -> None:
        if not packets:
            return
        with self._send_lock:
            if self.closed:
                return
            for packet in packets:
                self.sock.sendto(packet, self.addr)

    def debug_snapshot(self) -> dict:
        now = time.monotonic()
        with self._state_lock:
            max_id = self.max_data_packet_id
            ack_packet_id, ack_payload, _ack_group_id = self._ack_state()
            pending_gaps = 0
            if max_id is not None and self.next_recv_id <= max_id:
                pending_gaps = sum(
                    1 for packet_id in range(self.next_recv_id, max_id + 1) if packet_id not in self.recv_chunks
                )
            seconds_since_data = round(now - self.last_data_at, 3) if self.last_data_at else None
            return {
                "udp_next_recv_id": self.next_recv_id,
                "udp_last_packet_id": self.last_data_packet_id,
                "udp_max_packet_id": max_id,
                "udp_pending_chunks": len(self.recv_chunks),
                "udp_pending_gaps": pending_gaps,
                "udp_pending_send_chunks": len(self.sent_chunks),
                "udp_buffered_bytes": len(self.buffer),
                "udp_data_packets": self.data_packets_received,
                "udp_data_bytes": self.data_bytes_received,
                "udp_duplicates": self.duplicate_packets_received,
                "udp_ignored": self.ignored_packets,
                "udp_unknown": self.unknown_packets,
                "udp_acks_sent": self.acks_sent,
                "udp_acks_received": self.acks_received,
                "udp_last_ack_packet_id": self.last_ack_packet_id,
                "udp_ack_packet_id": ack_packet_id,
                "udp_ack_payload_bytes": len(ack_payload),
                "udp_ack_missing_packets": ack_payload.count(0),
                "udp_ack_receive_rate": self.ack_receive_rate,
                "udp_receiver_alive": bool(self._receive_thread and self._receive_thread.is_alive()),
                "udp_socket_receive_buffer": self.socket_receive_buffer,
                "udp_heartbeats_sent": self.heartbeats_sent,
                "udp_resend_packets": self.resend_packets_sent,
                "udp_seconds_since_data": seconds_since_data,
            }


def connect_local_direct(
    uid: str, *, timeout: float = 8.0, listen_port: int = 0, debug: bool = False
) -> UdpBcConnection:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", listen_port))
    except OSError:
        sock.bind(("", 0))
    sock.settimeout(0.4)
    discovery_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        discovery_sock.bind(("", 2015))
    except OSError:
        discovery_sock.bind(("", 0))

    client_id = _client_id()
    local_port = sock.getsockname()[1]
    tid = _tid()
    queries = [
        (
            "<P2P><C2D_C>"
            f"<uid>{uid}</uid>"
            f"<cli><port>{local_port}</port></cli>"
            f"<cid>{client_id}</cid><mtu>{MTU}</mtu><debug>0</debug><p>WIN</p>"
            "</C2D_C></P2P>"
        ),
        (
            "<P2P><C2D_C>"
            f"<uid>{uid}</uid>"
            f"<cli><port>{local_port}</port></cli>"
            f"<cid>{client_id}</cid><mtu>{MTU}</mtu><debug>0</debug><p>MAC</p>"
            "</C2D_C></P2P>"
        ),
    ]
    destinations = [("255.255.255.255", 2015), ("255.255.255.255", 2018)]
    packets = [encode_discovery_xml(tid, query) for query in queries]
    deadline = time.monotonic() + timeout
    sent_at = 0.0
    _debug(debug, f"Trying local UDP P2P for UID {uid} from UDP port {local_port}")
    while time.monotonic() < deadline:
        if time.monotonic() - sent_at >= 0.5:
            for dest in destinations:
                for packet in packets:
                    discovery_sock.sendto(packet, dest)
            _debug(debug, f"Sent local C2D_C broadcast from UDP port {discovery_sock.getsockname()[1]}")
            sent_at = time.monotonic()
        try:
            data, addr = sock.recvfrom(8192)
        except (TimeoutError, ConnectionResetError):
            continue
        decoded = decode_discovery_packet(data)
        if not decoded:
            continue
        _reply_tid, xml = decoded
        if "<D2C_C_R>" not in xml:
            continue
        cid = _find_int(xml, "cid")
        camera_id = _find_int(xml, "did")
        rsp = _find_int(xml, "rsp")
        _debug(debug, f"Received local D2C_C_R from {addr[0]}:{addr[1]} cid={cid} did={camera_id} rsp={rsp}")
        if cid == client_id and camera_id is not None and rsp != -1 and rsp != -3:
            discovery_sock.close()
            conn = UdpBcConnection(sock, addr, client_id, camera_id, timeout=timeout, heartbeat_tid=tid)
            conn._send_heartbeat()
            return conn
    discovery_sock.close()
    sock.close()
    raise TimeoutError(msg.Error.LocalUdpP2pNoReply)


def connect_relay(uid: str, *, timeout: float = 20.0, listen_port: int = 16577, debug: bool = False) -> UdpBcConnection:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", listen_port))
    except OSError:
        sock.bind(("", 0))
    sock.settimeout(0.4)

    _debug(debug, f"P2P lookup for UID {uid} from UDP port {sock.getsockname()[1]}")
    lookup = _lookup_with_socket(sock, uid, timeout=timeout, debug=debug)
    reg = _find_in_xml(lookup.xml or "", "reg")
    relay_lookup = _find_in_xml(lookup.xml or "", "relay")
    if not reg or not relay_lookup:
        sock.close()
        raise TimeoutError(msg.Error.P2pLookupNoServers)

    client_id = _client_id()
    local_ip = _local_ip_for(reg)
    local_port = sock.getsockname()[1]
    lookup_detail = (
        f"P2P lookup ok: reg={reg[0]}:{reg[1]} "
        f"relay={relay_lookup[0]}:{relay_lookup[1]} "
        f"local={local_ip}:{local_port} cid={client_id}"
    )
    _debug(debug, lookup_detail)
    reg_xml = (
        "<P2P><C2R_C>"
        f"<uid>{uid}</uid>"
        f"<cli><ip>{local_ip}</ip><port>{local_port}</port></cli>"
        f"<relay><ip>{relay_lookup[0]}</ip><port>{relay_lookup[1]}</port></relay>"
        f"<cid>{client_id}</cid><debug>251658240</debug><family>4</family><p>WIN</p><r>3</r>"
        "</C2R_C></P2P>"
    )
    _debug(debug, "Registering client address with Reolink register server")
    reply_xml = _retry_discovery(
        sock, reg_xml, reg, lambda xml: "<R2C_C_R>" in xml, timeout=timeout, debug=debug, label="C2R_C"
    )
    sid = _find_int(reply_xml, "sid")
    relay = _find_in_xml(reply_xml, "relay") or _find_in_xml(reply_xml, "relayt") or relay_lookup
    candidates = [
        ("local", _find_in_xml(reply_xml, "dev")),
        ("map", _find_in_xml(reply_xml, "dmap")),
        ("relay", relay),
    ]
    candidates = [(conn, addr) for conn, addr in candidates if addr]
    if sid is None or not candidates:
        sock.close()
        raise TimeoutError(msg.Error.RegisterNoConnectionDetails)

    _debug(
        debug,
        f"Register ok: sid={sid} candidates={', '.join(f'{conn}={addr[0]}:{addr[1]}' for conn, addr in candidates)}",
    )
    conn_name, final_addr, confirm_xml, heartbeat_tid = _open_registered_channel(
        sock, sid, client_id, candidates, timeout=timeout, debug=debug
    )
    camera_id = _find_int(confirm_xml, "did")
    if camera_id is None:
        sock.close()
        raise TimeoutError(msg.Error.MissingCameraConnectionId)

    cfm_xml = (
        "<P2P><C2R_CFM>"
        f"<sid>{sid}</sid><conn>{conn_name}</conn><rsp>0</rsp><cid>{client_id}</cid><did>{camera_id}</did>"
        "</C2R_CFM></P2P>"
    )
    for _ in range(3):
        sock.sendto(encode_discovery_xml(_tid(), cfm_xml), reg)

    conn = UdpBcConnection(sock, final_addr, client_id, camera_id, timeout=timeout, heartbeat_tid=heartbeat_tid)
    conn._send_heartbeat()
    return conn


def encode_udp_data(connection_id: int, packet_id: int, payload: bytes) -> bytes:
    return struct.pack("<IiII", MAGIC.UDP_DATA, connection_id, 0, packet_id) + struct.pack("<I", len(payload)) + payload


def encode_udp_ack(
    connection_id: int, packet_id: int, payload: bytes = b"", group_id: int = 0, maybe_latency: int = 0
) -> bytes:
    return (
        struct.pack("<IiIIII", MAGIC.UDP_ACK, connection_id, 0, group_id, packet_id, maybe_latency)
        + struct.pack("<I", len(payload))
        + payload
    )


def decode_udp_packet(data: bytes):
    if len(data) < 4:
        return None
    magic = struct.unpack("<I", data[:4])[0]
    if magic == MAGIC.UDP_DATA and len(data) >= 20:
        _magic, connection_id, _zero, packet_id, size = struct.unpack("<IiIII", data[:20])
        if size > len(data) - 20:
            return None
        return "data", connection_id, packet_id, data[20 : 20 + size]
    if magic == MAGIC.UDP_ACK and len(data) >= 28:
        _magic, connection_id, _zero, group_id, packet_id, latency, size = struct.unpack("<IiIIIII", data[:28])
        if size > len(data) - 28:
            return None
        return "ack", connection_id, group_id, packet_id, latency, data[28 : 28 + size]
    decoded = decode_discovery_packet(data)
    if decoded:
        return "discovery", decoded[0], decoded[1]
    return None


def _lookup_with_socket(sock: socket.socket, uid: str, *, timeout: float, debug: bool = False) -> DiscoveryHit:
    # Reuse the same socket that will later receive relay traffic. This mirrors Neolink's flow.
    queries = [
        f"<P2P><C2M_Q><uid>{uid}</uid><ver>3</ver><p>WIN</p></C2M_Q></P2P>",
        f"<P2P><C2M_Q><uid>{uid}</uid><p>MAC</p></C2M_Q></P2P>",
    ]
    destinations = []
    from .discovery import P2P_RELAY_HOSTNAMES

    for hostname in P2P_RELAY_HOSTNAMES:
        try:
            destinations.extend(
                info[4] for info in socket.getaddrinfo(hostname, 9999, socket.AF_INET, socket.SOCK_DGRAM)
            )
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    sent_at = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() - sent_at >= 0.5:
            packets = [encode_discovery_xml(_tid(), query) for query in queries]
            for dest in destinations:
                for packet in packets:
                    sock.sendto(packet, dest)
            _debug(debug, f"Sent C2M_Q to {len(destinations)} P2P server addresses")
            sent_at = time.monotonic()
        try:
            data, _addr = sock.recvfrom(8192)
        except (TimeoutError, ConnectionResetError):
            continue
        decoded = decode_discovery_packet(data)
        if decoded and "<M2C_Q_R>" in decoded[1]:
            reg = _find_in_xml(decoded[1], "reg")
            relay = _find_in_xml(decoded[1], "relay")
            target = _find_in_xml(decoded[1], "t")
            lookup_reply = (
                f"Received M2C_Q_R from {_addr[0]}:{_addr[1]} "
                f"reg={_fmt_addr(reg)} relay={_fmt_addr(relay)} t={_fmt_addr(target)}"
            )
            _debug(debug, lookup_reply)
            if reg and relay:
                return DiscoveryHit(
                    uid, target or _addr, xml=decoded[1], raw=data, source="remote:p2p", transport="udp"
                )
            _debug(debug, "Ignoring incomplete M2C_Q_R and waiting for another P2P server")
    raise TimeoutError(msg.Error.P2pLookupNoReply)


def _retry_discovery(
    sock: socket.socket,
    xml: str,
    dest: tuple[str, int],
    accept,
    *,
    timeout: float,
    debug: bool = False,
    label: str = "discovery",
) -> str:
    deadline = time.monotonic() + timeout
    sent_at = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() - sent_at >= 0.5:
            sock.sendto(encode_discovery_xml(_tid(), xml), dest)
            _debug(debug, f"Sent {label} to {dest[0]}:{dest[1]}")
            sent_at = time.monotonic()
        try:
            data, _addr = sock.recvfrom(8192)
        except (TimeoutError, ConnectionResetError):
            continue
        decoded = decode_discovery_packet(data)
        if decoded and accept(decoded[1]):
            _debug(debug, f"Accepted {label} reply from {_addr[0]}:{_addr[1]}")
            return decoded[1]
    raise TimeoutError(msg.Error.NoAcceptedDiscoveryReply.format(dest=dest))


def _open_registered_channel(
    sock: socket.socket,
    sid: int,
    client_id: int,
    candidates: list[tuple[str, tuple[str, int]]],
    *,
    timeout: float,
    debug: bool = False,
) -> tuple[str, tuple[str, int], str, int]:
    deadline = time.monotonic() + timeout
    sent_at = 0.0
    heartbeat_tid = _tid()
    packets = [
        (
            conn,
            addr,
            encode_discovery_xml(
                heartbeat_tid,
                f"<P2P><C2D_T><sid>{sid}</sid><conn>{conn}</conn><cid>{client_id}</cid><mtu>{MTU}</mtu></C2D_T></P2P>",
            ),
        )
        for conn, addr in candidates
    ]
    while time.monotonic() < deadline:
        if time.monotonic() - sent_at >= 0.5:
            for conn, addr, packet in packets:
                sock.sendto(packet, addr)
                _debug(debug, f"Sent C2D_T {conn} to {addr[0]}:{addr[1]}")
            sent_at = time.monotonic()
        try:
            data, addr = sock.recvfrom(8192)
        except (TimeoutError, ConnectionResetError):
            continue
        decoded = decode_discovery_packet(data)
        if not decoded:
            continue
        _reply_tid, xml = decoded
        if "<D2C_CFM>" not in xml:
            continue
        cid = _find_int(xml, "cid")
        reply_sid = _find_int(xml, "sid")
        did = _find_int(xml, "did")
        conn = _find_text(xml, "conn")
        if cid == client_id and reply_sid == sid and did is not None and conn:
            _debug(debug, f"Accepted D2C_CFM {conn} from {addr[0]}:{addr[1]} did={did}")
            return conn, addr, xml, heartbeat_tid
    raise TimeoutError(msg.Error.RegisteredConnectionNoReply)


def _find_in_xml(xml: str, tag: str) -> tuple[str, int] | None:
    from .discovery import _find_ip_port

    return _find_ip_port(xml, tag)


def _find_text(xml: str, tag: str) -> str | None:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    value = root.findtext(f".//{tag}")
    return value


def _find_int(xml: str, tag: str) -> int | None:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    value = root.findtext(f".//{tag}")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _local_ip_for(dest: tuple[str, int]) -> str:
    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tmp.connect(dest)
        return tmp.getsockname()[0]
    finally:
        tmp.close()


def _chunks(data: bytes, size: int):
    for idx in range(0, len(data), size):
        yield data[idx : idx + size]


def _tid() -> int:
    import random

    return random.randint(0, 255)


def _client_id() -> int:
    import random

    return random.randint(-(2**31), 2**31 - 1)


def _debug(enabled: bool, message: str) -> None:
    if enabled:
        print(msg.Log.Pyneolink.format(message=message), file=sys.stderr)


def _fmt_addr(addr: tuple[str, int] | None) -> str:
    if not addr:
        return "-"
    return f"{addr[0]}:{addr[1]}"
