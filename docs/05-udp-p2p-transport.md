# UDP P2P Transport

Reolink UID/P2P has two layers:

1. discovery/registration XML packets;
2. a reliable-ish UDP data channel that carries Baichuan bytes.

Discovery helpers live in `pyneolink/core/discovery.py`.

The socket-like UDP Baichuan channel lives in `pyneolink/core/udp_transport.py`.

## Discovery Packet Format

Discovery XML packets are encoded as:

1. XML is encoded as UTF-8;
2. payload is encrypted with `udp_xor(tid, payload)`;
3. `neolink_crc32(payload)` is calculated;
4. a header is added:

```text
magic
tid
checksum
payload_size
```

`decode_discovery_packet()` reverses the process: it validates magic, size, and checksum, then returns `(tid, xml)`.

## Local Discovery

`local_discover()` sends UDP broadcast to port `2015`:

- `C2D_S` for general discovery;
- `C2D_C` for UID-specific discovery.

The camera may reply with XML or another UDP payload. If the UID matches, a `DiscoveryHit` is produced.

This method is used for discovery, but it does not open the live Baichuan channel. That is done by `connect_local_direct()`.

## Local Direct UDP

`connect_local_direct(uid)`:

1. opens a UDP socket for Baichuan data;
2. opens a discovery socket;
3. generates `client_id`;
4. broadcasts `C2D_C` with UID, client port, cid, and MTU;
5. waits for `D2C_C_R`;
6. validates `cid`, `did`, and `rsp`;
7. creates `UdpBcConnection(sock, addr, client_id, camera_id)`;
8. sends a heartbeat.

This is the shortest path when the camera is reachable on the LAN.

## Remote UID Lookup

`connect_relay(uid)` opens the P2P/relay path.

The first step is:

```python
_lookup_with_socket()
```

It sends `C2M_Q` to `p2p*.reolink.com:9999`.

The useful reply contains:

- `reg`: register server address;
- `relay`: relay server address;
- `t`: auxiliary relay/tunnel address;
- camera/client ids;
- NAT mapping information.

Incomplete replies without `reg`/`relay` are ignored.

## Register Server

Next, `connect_relay()` sends `C2R_C` to the register server.

The expected reply is `R2C_C_R`, which provides:

- `sid`;
- local camera address candidate;
- mapped public address candidate;
- relay or relay-t address.

## Candidate Selection

The code builds a candidate list:

- local;
- map;
- relay.

It sends `C2D_T` to every candidate.

The first valid `D2C_CFM` with the expected `cid`, `sid`, `did`, and `conn` wins.

After that, `C2R_CFM` is sent to the register server and a `UdpBcConnection` is created.

## `UdpBcConnection`

This class makes the UDP channel behave like a socket:

- `sendall(data)`;
- `recv(size)`;
- `recv_some(size)`;
- `settimeout(value)`;
- `close()`.

Baichuan code does not need to know whether it is using TCP or UDP: `recv_message(sock, cipher)` works with both a TCP socket and `UdpBcConnection`.

## Data Chunks

`sendall()` splits Baichuan bytes into chunks up to:

```text
MTU - UDP_DATA_HEADER_SIZE
```

`recv()` reassembles chunks in order using `next_recv_id`.

The connection tracks:

- sent chunks waiting for ACK;
- received chunks waiting for missing gaps;
- duplicate packets;
- ignored packets;
- data byte counters.

## ACK, Resend, And Heartbeat

`UdpBcConnection._maintenance()` runs during read timeouts:

- forces ACK when no ACK has been sent recently;
- resends unacked sent chunks;
- sends a P2P heartbeat once per second.

ACK payloads describe which packet ids after the last contiguous packet have already been received. This helps with gaps.

## Debug Snapshot

`debug_snapshot()` returns transport counters such as:

- `udp_next_recv_id`;
- `udp_max_packet_id`;
- `udp_pending_chunks`;
- `udp_pending_gaps`;
- `udp_buffered_bytes`;
- `udp_data_packets`;
- `udp_data_bytes`;
- `udp_duplicates`;
- `udp_acks_sent`;
- `udp_acks_received`;
- `udp_resend_packets`;
- `udp_seconds_since_data`.

This is used in SD-card download diagnostics.
