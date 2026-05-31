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
    def __init__(self, sock: socket.socket, addr: tuple[str, int], client_id: int, camera_id: int, *, timeout: float = 10.0) -> None:
        self.sock = sock
        self.addr = addr
        self.client_id = client_id
        self.camera_id = camera_id
        self.timeout = timeout
        self.next_send_id = 0
        self.next_recv_id = 0
        self.recv_chunks: dict[int, bytes] = {}
        self.buffer = bytearray()
        self.closed = False
        self.sock.settimeout(0.2)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout or self.timeout

    def sendall(self, data: bytes) -> None:
        for chunk in _chunks(data, MTU - UDP_DATA_HEADER_SIZE):
            packet = encode_udp_data(self.camera_id, self.next_send_id, chunk)
            self.sock.sendto(packet, self.addr)
            self.next_send_id += 1

    def recv(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while len(self.buffer) < size:
            if time.monotonic() > deadline:
                raise TimeoutError("Timed out waiting for UDP Baichuan data")
            self._recv_one()
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def close(self) -> None:
        self.closed = True
        self.sock.close()

    def _recv_one(self) -> None:
        try:
            data, addr = self.sock.recvfrom(65535)
        except TimeoutError:
            self._send_ack()
            return
        parsed = decode_udp_packet(data)
        if not parsed:
            return
        kind = parsed[0]
        if kind == "data":
            _kind, connection_id, packet_id, payload = parsed
            if connection_id != self.client_id:
                return
            self.recv_chunks[packet_id] = payload
            self._send_ack()
            while self.next_recv_id in self.recv_chunks:
                self.buffer.extend(self.recv_chunks.pop(self.next_recv_id))
                self.next_recv_id += 1
        elif kind == "ack":
            return
        elif kind == "discovery":
            return

    def _send_ack(self) -> None:
        self.sock.sendto(encode_udp_ack(self.camera_id, self.next_recv_id - 1 if self.next_recv_id else 0xFFFFFFFF), self.addr)


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
        f"<cid>{client_id}</cid><debug>0</debug><family>4</family><p>MAC</p><r>3</r>"
        "</C2R_C></P2P>"
    )
    _debug(debug, "Registering client address with Reolink register server")
    reply_xml = _retry_discovery(sock, reg_xml, reg, lambda xml: "<R2C_C_R>" in xml, timeout=timeout, debug=debug, label="C2R_C")
    sid = _find_int(reply_xml, "sid")
    relay = _find_in_xml(reply_xml, "relay") or _find_in_xml(reply_xml, "relayt") or relay_lookup
    if sid is None or not relay:
        sock.close()
        raise TimeoutError("Reolink register did not return relay connection details")

    _debug(debug, f"Register ok: sid={sid} relay={relay[0]}:{relay[1]}")
    connect_xml = (
        "<P2P><C2D_T>"
        f"<sid>{sid}</sid><conn>relay</conn><cid>{client_id}</cid><mtu>{MTU}</mtu>"
        "</C2D_T></P2P>"
    )
    _debug(debug, "Opening relay channel")
    confirm_xml = _retry_discovery(sock, connect_xml, relay, lambda xml: "<D2C_CFM>" in xml and "<conn>relay</conn>" in xml, timeout=timeout, debug=debug, label="C2D_T relay")
    camera_id = _find_int(confirm_xml, "did")
    if camera_id is None:
        sock.close()
        raise TimeoutError("Relay did not return a camera connection id")

    cfm_xml = (
        "<P2P><C2R_CFM>"
        f"<sid>{sid}</sid><conn>relay</conn><rsp>0</rsp><cid>{client_id}</cid><did>{camera_id}</did>"
        "</C2R_CFM></P2P>"
    )
    for _ in range(3):
        sock.sendto(encode_discovery_xml(_tid(), cfm_xml), reg)

    return UdpBcConnection(sock, relay, client_id, camera_id, timeout=timeout)


def encode_udp_data(connection_id: int, packet_id: int, payload: bytes) -> bytes:
    return struct.pack("<IiII", MAGIC_DATA, connection_id, 0, packet_id) + struct.pack("<I", len(payload)) + payload


def encode_udp_ack(connection_id: int, packet_id: int, payload: bytes = b"", maybe_latency: int = 0) -> bytes:
    return struct.pack("<IiIIII", MAGIC_ACK, connection_id, 0, 0, packet_id, maybe_latency) + struct.pack("<I", len(payload)) + payload


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
    query = f"<P2P><C2M_Q><uid>{uid}</uid><p>MAC</p></C2M_Q></P2P>"
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
            packet = encode_discovery_xml(_tid(), query)
            for dest in destinations:
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


def _find_in_xml(xml: str, tag: str) -> tuple[str, int] | None:
    from .discovery import _find_ip_port

    return _find_ip_port(xml, tag)


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
