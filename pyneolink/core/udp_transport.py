from __future__ import annotations

import socket
import struct
import sys
import time
from collections import OrderedDict

from .discovery import (
    DiscoveryHit,
    decode_discovery_packet,
    encode_discovery_xml,
    remote_uid_lookup,
)

MAGIC_ACK = 0x2A87CF20
MAGIC_DATA = 0x2A87CF10
MTU = 1350
UDP_DATA_HEADER_SIZE = 20


class UdpBcConnection:
    def __init__(
        self,
        sock: socket.socket,
        addr: tuple[str, int],
        client_id: int,
        camera_id: int,
        *,
        timeout: float = 10.0,
        heartbeat_tid: int | None = None,
    ) -> None:
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
        self.closed = False
        self.last_ack_at = 0.0
        self.last_ack_packet_id: int | None = None
        self.last_resend_at = 0.0
        self.last_heartbeat_at = 0.0
        self.ack_latency = 0
        self._ack_latency_values: list[int] = []
        self._last_ack_latency_recv_at: float | None = None
        self._last_ack_latency_display_at: float | None = None
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
        self.sock.settimeout(0.01)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout or self.timeout

    def sendall(self, data: bytes) -> None:
        for chunk in _chunks(data, MTU - UDP_DATA_HEADER_SIZE):
            packet_id = self.next_send_id
            packet = encode_udp_data(self.camera_id, packet_id, chunk)
            self.sent_chunks[packet_id] = chunk
            self.sock.sendto(packet, self.addr)
            self.next_send_id += 1
        self.last_resend_at = time.monotonic()

    def recv(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while len(self.buffer) < size:
            if time.monotonic() > deadline:
                raise TimeoutError("Timed out waiting for UDP Baichuan data")
            self._recv_one()
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def recv_some(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while not self.buffer:
            if time.monotonic() > deadline:
                raise TimeoutError("Timed out waiting for UDP Baichuan data")
            self._recv_one()
        take = min(size, len(self.buffer))
        result = bytes(self.buffer[:take])
        del self.buffer[:take]
        return result

    def close(self) -> None:
        self.closed = True
        self.sock.close()

    def _recv_one(self) -> None:
        try:
            data, addr = self.sock.recvfrom(65535)
        except TimeoutError:
            self._maintenance()
            return
        parsed = decode_udp_packet(data)
        if not parsed:
            return
        kind = parsed[0]
        if kind == "data":
            _kind, connection_id, packet_id, payload = parsed
            if connection_id != self.client_id:
                self.ignored_packets += 1
                return
            if packet_id in self.recv_chunks or packet_id < self.next_recv_id:
                self.duplicate_packets_received += 1
            self.recv_chunks[packet_id] = payload
            self.data_packets_received += 1
            self.data_bytes_received += len(payload)
            self.last_data_packet_id = packet_id
            self.max_data_packet_id = packet_id if self.max_data_packet_id is None else max(self.max_data_packet_id, packet_id)
            self.last_data_at = time.monotonic()
            self._feed_ack_latency()
            self._maybe_send_ack()
            while self.next_recv_id in self.recv_chunks:
                self.buffer.extend(self.recv_chunks.pop(self.next_recv_id))
                self.next_recv_id += 1
        elif kind == "ack":
            _kind, connection_id, _group_id, packet_id, _latency, payload = parsed
            if connection_id == self.client_id:
                self.acks_received += 1
                self._handle_ack(packet_id, payload)
        elif kind == "discovery":
            return
        else:
            self.unknown_packets += 1

    def _send_ack(self) -> None:
        packet_id, payload, group_id = self._ack_state()
        self.sock.sendto(encode_udp_ack(self.camera_id, packet_id, payload, group_id, maybe_latency=self.ack_latency), self.addr)
        if packet_id != 0xFFFFFFFF:
            self.last_ack_packet_id = packet_id
        self.acks_sent += 1
        self.last_ack_at = time.monotonic()

    def _maybe_send_ack(self, *, force: bool = False) -> None:
        packet_id, payload, _group_id = self._ack_state()
        if force:
            self._send_ack()
            return
        if payload:
            self._send_ack()
            return
        if packet_id == 0xFFFFFFFF:
            return
        if self.last_ack_packet_id is None or packet_id - self.last_ack_packet_id >= 16:
            self._send_ack()
            return
        if time.monotonic() - self.last_ack_at >= 0.15:
            self._send_ack()

    def _ack_state(self) -> tuple[int, bytes, int]:
        if self.next_recv_id == 0:
            return 0xFFFFFFFF, b"", 0xFFFFFFFF
        first_missing = self.next_recv_id
        while first_missing in self.recv_chunks:
            first_missing += 1
        end = max(self.recv_chunks.keys(), default=first_missing - 1)
        payload = bytes(1 if packet_id in self.recv_chunks else 0 for packet_id in range(first_missing, end + 1))
        return first_missing - 1, payload, 0

    def _handle_ack(self, packet_id: int, payload: bytes) -> None:
        if packet_id != 0xFFFFFFFF:
            for sent_id in list(self.sent_chunks):
                if sent_id <= packet_id:
                    del self.sent_chunks[sent_id]
            for idx, value in enumerate(payload):
                sent_id = packet_id + 1 + idx
                if value:
                    self.sent_chunks.pop(sent_id, None)
        self._feed_ack_latency()

    def _maintenance(self) -> None:
        now = time.monotonic()
        if now - self.last_ack_at >= 0.2:
            self._maybe_send_ack(force=True)
        if self.sent_chunks and now - self.last_resend_at >= 0.5:
            for packet_id, chunk in list(self.sent_chunks.items()):
                self.sock.sendto(encode_udp_data(self.camera_id, packet_id, chunk), self.addr)
                self.resend_packets_sent += 1
            self.last_resend_at = now
        if now - self.last_heartbeat_at >= 1.0:
            self._send_heartbeat()

    def _feed_ack_latency(self) -> None:
        now = time.monotonic()
        if self._last_ack_latency_recv_at is not None:
            self._ack_latency_values.append(int((now - self._last_ack_latency_recv_at) * 1_000_000))
        self._last_ack_latency_recv_at = now
        if self._last_ack_latency_display_at is None:
            self._last_ack_latency_display_at = now
            self.ack_latency = 0
        elif now - self._last_ack_latency_display_at > 1.0:
            self._last_ack_latency_display_at = now
            if self._ack_latency_values:
                self.ack_latency = sum(self._ack_latency_values) // len(self._ack_latency_values)
                self._ack_latency_values = []

    def _send_heartbeat(self) -> None:
        xml = f"<P2P><C2D_HB><cid>{self.client_id}</cid><did>{self.camera_id}</did></C2D_HB></P2P>"
        self.sock.sendto(encode_discovery_xml(self.heartbeat_tid, xml), self.addr)
        self.heartbeats_sent += 1
        self.last_heartbeat_at = time.monotonic()

    def debug_snapshot(self) -> dict:
        now = time.monotonic()
        max_id = self.max_data_packet_id
        pending_gaps = 0
        if max_id is not None and self.next_recv_id <= max_id:
            pending_gaps = sum(1 for packet_id in range(self.next_recv_id, max_id + 1) if packet_id not in self.recv_chunks)
        return {
            "udp_next_recv_id": self.next_recv_id,
            "udp_last_packet_id": self.last_data_packet_id,
            "udp_max_packet_id": max_id,
            "udp_pending_chunks": len(self.recv_chunks),
            "udp_pending_gaps": pending_gaps,
            "udp_buffered_bytes": len(self.buffer),
            "udp_data_packets": self.data_packets_received,
            "udp_data_bytes": self.data_bytes_received,
            "udp_duplicates": self.duplicate_packets_received,
            "udp_ignored": self.ignored_packets,
            "udp_unknown": self.unknown_packets,
            "udp_acks_sent": self.acks_sent,
            "udp_acks_received": self.acks_received,
            "udp_heartbeats_sent": self.heartbeats_sent,
            "udp_resend_packets": self.resend_packets_sent,
            "udp_seconds_since_data": round(now - self.last_data_at, 3) if self.last_data_at else None,
        }


def connect_local_direct(uid: str, *, timeout: float = 8.0, listen_port: int = 0, debug: bool = False) -> UdpBcConnection:
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
    raise TimeoutError("No accepted local UDP P2P reply from camera")


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
        raise TimeoutError("Reolink P2P lookup did not return register/relay servers")

    client_id = _client_id()
    local_ip = _local_ip_for(reg)
    local_port = sock.getsockname()[1]
    _debug(debug, f"P2P lookup ok: reg={reg[0]}:{reg[1]} relay={relay_lookup[0]}:{relay_lookup[1]} local={local_ip}:{local_port} cid={client_id}")
    reg_xml = (
        "<P2P><C2R_C>"
        f"<uid>{uid}</uid>"
        f"<cli><ip>{local_ip}</ip><port>{local_port}</port></cli>"
        f"<relay><ip>{relay_lookup[0]}</ip><port>{relay_lookup[1]}</port></relay>"
        f"<cid>{client_id}</cid><debug>251658240</debug><family>4</family><p>WIN</p><r>3</r>"
        "</C2R_C></P2P>"
    )
    _debug(debug, "Registering client address with Reolink register server")
    reply_xml = _retry_discovery(sock, reg_xml, reg, lambda xml: "<R2C_C_R>" in xml, timeout=timeout, debug=debug, label="C2R_C")
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
        raise TimeoutError("Reolink register did not return connection details")

    _debug(debug, f"Register ok: sid={sid} candidates={', '.join(f'{conn}={addr[0]}:{addr[1]}' for conn, addr in candidates)}")
    conn_name, final_addr, confirm_xml, heartbeat_tid = _open_registered_channel(sock, sid, client_id, candidates, timeout=timeout, debug=debug)
    camera_id = _find_int(confirm_xml, "did")
    if camera_id is None:
        sock.close()
        raise TimeoutError("Connection did not return a camera connection id")

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
    return struct.pack("<IiII", MAGIC_DATA, connection_id, 0, packet_id) + struct.pack("<I", len(payload)) + payload


def encode_udp_ack(connection_id: int, packet_id: int, payload: bytes = b"", group_id: int = 0, maybe_latency: int = 0) -> bytes:
    return struct.pack("<IiIIII", MAGIC_ACK, connection_id, 0, group_id, packet_id, maybe_latency) + struct.pack("<I", len(payload)) + payload


def decode_udp_packet(data: bytes):
    if len(data) < 4:
        return None
    magic = struct.unpack("<I", data[:4])[0]
    if magic == MAGIC_DATA and len(data) >= 20:
        _magic, connection_id, _zero, packet_id, size = struct.unpack("<IiIII", data[:20])
        return "data", connection_id, packet_id, data[20 : 20 + size]
    if magic == MAGIC_ACK and len(data) >= 28:
        _magic, connection_id, _zero, group_id, packet_id, latency, size = struct.unpack("<IiIIIII", data[:28])
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
            destinations.extend(info[4] for info in socket.getaddrinfo(hostname, 9999, socket.AF_INET, socket.SOCK_DGRAM))
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
            _debug(debug, f"Received M2C_Q_R from {_addr[0]}:{_addr[1]} reg={_fmt_addr(reg)} relay={_fmt_addr(relay)} t={_fmt_addr(target)}")
            if reg and relay:
                return DiscoveryHit(uid, target or _addr, xml=decoded[1], raw=data, source="remote:p2p", transport="udp")
            _debug(debug, "Ignoring incomplete M2C_Q_R and waiting for another P2P server")
    raise TimeoutError("No Reolink P2P lookup reply with register/relay servers")


def _retry_discovery(sock: socket.socket, xml: str, dest: tuple[str, int], accept, *, timeout: float, debug: bool = False, label: str = "discovery") -> str:
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
    raise TimeoutError(f"No accepted discovery reply from {dest}")


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
                "<P2P><C2D_T>"
                f"<sid>{sid}</sid><conn>{conn}</conn><cid>{client_id}</cid><mtu>{MTU}</mtu>"
                "</C2D_T></P2P>",
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
    raise TimeoutError("No accepted C2D_T/D2C_CFM reply from registered connection candidates")


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
        print(f"[pyneolink] {message}", file=sys.stderr)


def _fmt_addr(addr: tuple[str, int] | None) -> str:
    if not addr:
        return "-"
    return f"{addr[0]}:{addr[1]}"
