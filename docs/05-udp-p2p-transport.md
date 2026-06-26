# UDP P2P Transport

Reolink UID/P2P складається з двох шарів:

1. discovery/register XML packets;
2. reliable-ish UDP data channel для Baichuan bytes.

Discovery helpers живуть у `core/discovery.py`.

Socket-like UDP Baichuan channel живе у `core/udp_transport.py`.

## Discovery packet format

`encode_discovery_xml(tid, xml)`:

1. XML кодується UTF-8;
2. payload шифрується `udp_xor(tid, payload)`;
3. рахується `neolink_crc32(payload)`;
4. додається header:

```text
MAGIC.DISCOVERY = 0x2A87CF3A
payload_size
1
tid
crc32
payload
```

`decode_discovery_packet()` робить зворотне: перевіряє magic, size, checksum і повертає `(tid, xml)`.

## Local discovery

`local_discover()` відправляє UDP broadcast на порт `2015`:

- `C2D_S` для загального пошуку;
- `C2D_C` для UID-specific пошуку.

Камера може відповісти XML або іншим UDP payload. Якщо UID збігається, створюється `DiscoveryHit`.

Цей метод використовується для пошуку, але прямий live Baichuan channel відкриває не він, а `connect_local_direct()`.

## Local direct connection

`connect_local_direct(uid)`:

1. відкриває UDP socket для Baichuan data;
2. відкриває discovery socket;
3. генерує `client_id`;
4. broadcast-ить `C2D_C` з UID, client port, cid, mtu;
5. чекає `D2C_C_R`;
6. перевіряє `cid`, `did`, `rsp`;
7. створює `UdpBcConnection(sock, addr, client_id, camera_id)`;
8. відправляє heartbeat.

Це найкоротший шлях, коли камера доступна у LAN.

## Remote relay connection

`connect_relay(uid)` відкриває P2P/relay шлях.

### 1. P2P lookup

`_lookup_with_socket()` відправляє `C2M_Q` на `p2p*.reolink.com:9999`.

Потрібна відповідь:

```xml
<M2C_Q_R>
  <reg>...</reg>
  <relay>...</relay>
  <t>...</t>
</M2C_Q_R>
```

Неповні відповіді без `reg`/`relay` ігноруються.

### 2. Register client

Далі `connect_relay()` відправляє на register server:

```xml
<C2R_C>
  <uid>...</uid>
  <cli><ip>local_ip</ip><port>local_port</port></cli>
  <relay><ip>relay_ip</ip><port>relay_port</port></relay>
  <cid>client_id</cid>
  ...
</C2R_C>
```

Потрібна відповідь `R2C_C_R`, з якої береться:

- `sid`;
- `dev`;
- `dmap`;
- `relay` або `relayt`.

### 3. Open registered channel

Код формує список candidates:

- `local`;
- `map`;
- `relay`.

На кожен candidate відправляється:

```xml
<C2D_T>
  <sid>...</sid>
  <conn>local|map|relay</conn>
  <cid>client_id</cid>
  <mtu>1350</mtu>
</C2D_T>
```

Перший valid `D2C_CFM` з правильним `cid`, `sid`, `did`, `conn` виграє.

Після цього відправляється `C2R_CFM` на register server і створюється `UdpBcConnection`.

## `UdpBcConnection`

Цей клас робить UDP канал схожим на TCP socket:

- `sendall(data)`;
- `recv(size)`;
- `recv_some(size)`;
- `settimeout(timeout)`;
- `close()`.

Baichuan code не знає, що під ним UDP: `recv_message(sock, cipher)` працює і з TCP socket, і з `UdpBcConnection`.

## UDP data packets

Data packet:

```text
MAGIC.UDP_DATA = 0x2A87CF10
connection_id
0
packet_id
payload_size
payload
```

ACK packet:

```text
MAGIC.UDP_ACK = 0x2A87CF20
connection_id
0
group_id
packet_id
latency
payload_size
payload
```

`sendall()` розбиває Baichuan bytes на chunks до `MTU - UDP_DATA_HEADER_SIZE`.

`recv()` збирає chunks у правильному порядку через `next_recv_id`.

## ACK, resend, heartbeat

`UdpBcConnection._maintenance()` викликається під час read timeout:

- форсує ACK, якщо давно не відправлявся;
- resend-ить unacked sent chunks;
- відправляє P2P heartbeat раз на секунду.

ACK payload описує, які packet ids після останнього contiguous packet вже отримані. Це допомагає при gaps.

## Debug snapshot

`debug_snapshot()` повертає:

- next/last/max packet ids;
- buffered bytes;
- pending gaps;
- data/duplicate/ignored counters;
- ACK counters;
- heartbeat/resend counters;
- seconds since data.

Це використовується у SD download diagnostics.
