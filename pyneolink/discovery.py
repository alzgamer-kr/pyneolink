from __future__ import annotations

import random
import socket
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .crypto import neolink_crc32, udp_xor

MAGIC_DISCOVERY = 0x2A87CF3A
P2P_RELAY_HOSTNAMES = [
    "p2p.reolink.com",
    "p2p1.reolink.com",
    "p2p2.reolink.com",
    "p2p3.reolink.com",
    "p2p4.reolink.com",
    "p2p5.reolink.com",
    "p2p6.reolink.com",
    "p2p7.reolink.com",
    "p2p8.reolink.com",
    "p2p9.reolink.com",
    "p2p10.reolink.com",
    "p2p11.reolink.com",
    "p2p12.reolink.com",
    "p2p13.reolink.com",
    "p2p14.reolink.com",
    "p2p15.reolink.com",
    "p2p16.reolink.com",
]


@dataclass
class DiscoveryHit:
    uid: str | None
    address: tuple[str, int]
    xml: str | None = None
    raw: bytes = b""
    source: str = "local"
    transport: str = "tcp"


def encode_discovery_xml(tid: int, xml: str) -> bytes:
    payload = udp_xor(tid, xml.encode("utf-8"))
    return struct.pack("<IIIII", MAGIC_DISCOVERY, len(payload), 1, tid, neolink_crc32(payload)) + payload


def decode_discovery_packet(data: bytes) -> tuple[int, str] | None:
    if len(data) < 20:
        return None
    magic, size, _one, tid, checksum = struct.unpack("<IIIII", data[:20])
    if magic != MAGIC_DISCOVERY or len(data) < 20 + size:
        return None
    payload = data[20 : 20 + size]
    if neolink_crc32(payload) != checksum:
        return None
    return tid, udp_xor(tid, payload).decode("utf-8", errors="replace")


def local_discover(uid: str | None = None, *, timeout: float = 5.0, listen_port: int = 0) -> list[DiscoveryHit]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", listen_port))
    sock.settimeout(0.25)
    local_port = sock.getsockname()[1]
    tid = random.randint(0, 255)
    messages = [
        ("255.255.255.255", 2015, f"<P2P><C2D_S><to><port>{local_port}</port></to></C2D_S></P2P>"),
    ]
    if uid:
        cid = random.randint(10000, 999999)
        messages.append(
            (
                "255.255.255.255",
                2015,
                f"<P2P><C2D_C><uid>{uid}</uid><cli><port>{local_port}</port></cli><cid>{cid}</cid><mtu>1350</mtu><debug>0</debug><p>MAC</p></C2D_C></P2P>",
            )
        )
    hits: list[DiscoveryHit] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for host, port, xml in messages:
            sock.sendto(encode_discovery_xml(tid, xml), (host, port))
        sock.sendto(bytes.fromhex("aaaa0000"), ("255.255.255.255", 2000))
        end_round = time.monotonic() + 0.5
        while time.monotonic() < end_round:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                break
            decoded = decode_discovery_packet(data)
            if decoded:
                _reply_tid, xml = decoded
                if uid and uid not in xml:
                    continue
                found_uid = _find_xml_text(xml, "uid")
                hits.append(DiscoveryHit(found_uid, addr, xml=xml, raw=data, source="local", transport="udp"))
            elif b"." in data or (uid and uid.encode() in data):
                hits.append(DiscoveryHit(uid if uid and uid.encode() in data else None, addr, raw=data, source="local", transport="udp"))
    sock.close()
    return hits


def remote_uid_lookup(uid: str, *, timeout: float = 8.0, listen_port: int = 16577) -> list[DiscoveryHit]:
    hits: list[DiscoveryHit] = []
    query = f"<P2P><C2M_Q><uid>{uid}</uid><p>MAC</p></C2M_Q></P2P>"
    tid = random.randint(0, 255)
    packet = encode_discovery_xml(tid, query)
    deadline = time.monotonic() + timeout
    addresses: list[tuple[str, int]] = []
    for hostname in P2P_RELAY_HOSTNAMES:
        try:
            addresses.extend(socket.getaddrinfo(hostname, 9999, socket.AF_INET, socket.SOCK_DGRAM))
        except OSError:
            continue
    destinations = [(info[4][0], info[4][1]) for info in addresses]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", listen_port))
    except OSError:
        sock.bind(("", 0))
    sock.settimeout(0.4)
    sent_at = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() - sent_at >= 0.5:
            for dest in destinations:
                sock.sendto(packet, dest)
            sent_at = time.monotonic()
        try:
            data, addr = sock.recvfrom(4096)
        except (TimeoutError, ConnectionResetError):
            continue
        decoded = decode_discovery_packet(data)
        if not decoded:
            continue
        _reply_tid, xml = decoded
        if "<M2C_Q_R>" not in xml:
            continue
        for tag in ("t", "dev", "dmap", "reg", "relay"):
            ip_port = _find_ip_port(xml, tag)
            if ip_port:
                ip, port = ip_port
                hits.append(DiscoveryHit(uid, (ip, port), xml=xml, raw=data, source=f"remote:{tag}", transport="udp"))
        if hits:
            break
    sock.close()
    return _dedupe_hits(hits)


def _find_xml_text(xml: str, tag: str) -> str | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    found = root.find(f".//{tag}")
    return found.text if found is not None else None


def _find_ip_port(xml: str, tag: str) -> tuple[str, int] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    node = root.find(f".//{tag}")
    if node is None:
        return None
    ip = node.findtext("ip")
    port = node.findtext("port")
    if not ip or not port:
        return None
    try:
        return ip, int(port)
    except ValueError:
        return None


def _dedupe_hits(hits: list[DiscoveryHit]) -> list[DiscoveryHit]:
    seen: set[tuple[str, int, str]] = set()
    result: list[DiscoveryHit] = []
    for hit in hits:
        key = (hit.address[0], hit.address[1], hit.source)
        if key not in seen:
            seen.add(key)
            result.append(hit)
    return result
