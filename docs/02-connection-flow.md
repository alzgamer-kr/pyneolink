# Connection Flow

The main connection class is `pyneolink.camera.Camera`.

`Camera` accepts either a ready `CameraConfig` object or direct keyword arguments:

- `uuid` / `uid`;
- `address`;
- `cached_address`;
- `username`;
- `password`;
- `discovery`;
- `channel_id`;
- `state_path`;
- `debug`.

## Entry Point

Typical usage:

```python
with Camera(uuid="ABCDEF0123456789", password="password") as camera:
    info = camera.info()
```

`__enter__()` calls:

1. `connect()`
2. `login()`

After that, `camera.sock`, `camera.cipher`, and `camera.login_xml` are ready for commands.

## `connect()`

`connect()` chooses a transport in the order below.

### 1. Local UDP P2P Probe For UID

When `uid` is present, `address` and `cached_address` are not set, and `discovery` allows UID discovery, `Camera.connect()` first tries:

```python
connect_local_direct(uid)
```

This is a local UDP handshake with the camera through broadcast. If the camera is reachable on the LAN and replies, the result is a `UdpBcConnection`.

After success:

- `self.sock = UdpBcConnection(...)`;
- `self.connected_address = sock.addr`;
- `.pyneolink_state.json` is updated with `transport="udp-local"`.

If the local path fails:

- with `discovery="local"`, the error is propagated;
- in other modes, the code moves to the next connection method.

### 2. Explicit `address`

If the config contains `address`, `_resolve_address()` returns `host:port`. Then `connect()` opens:

```python
socket.create_connection((host, port))
```

This is the TCP Baichuan path. The default port is `9000` when no port is specified.

### 3. `cached_address`

If `cached_address` is set, it is used as the next candidate. This is also a TCP path unless the cached address marks a relay transport.

### 4. Cached TCP State

If `state_path` is enabled, `ConnectionState.get_address(camera_name, transport="tcp")` may return a previously working TCP address.

### 5. UDP Relay

If `uid` is present and `discovery` is `relay` or `cellular`, `_resolve_address()` returns a special marker:

```python
("", 0, "udp-relay")
```

Then `connect()` calls:

```python
connect_relay(uid)
```

This opens a UDP P2P channel through the Reolink register/relay infrastructure. The result is also a `UdpBcConnection`.

After success, state is updated with `transport="udp-relay"`.

## `ensure_connected()`

Most public methods do not require the caller to manually call `connect()`:

```python
def ensure_connected(self):
    if self.sock is None:
        self.connect()
    if not self.login_xml:
        self.login()
```

This means `camera.info()`, `camera.command()`, and `camera.sd_card()` operations can bring up connection/login on demand.

## `reconnect()`

`reconnect()` always performs:

1. `close()`
2. `connect()`
3. `login()`

It is used when an operation receives timeout/EOF/OSError conditions, for example during battery polling or after an interrupted SD-card download.

## Online Lease

`Camera.require_online()` returns a context manager that increments `_online_required`.

This is used for scenarios where the camera must stay online:

- live stream;
- HLS session;
- battery `mode="online"`;
- long operations where the socket must not be closed between requests.

While `online_required == True`, components that normally might close or reconnect should avoid breaking the active online scenario.
