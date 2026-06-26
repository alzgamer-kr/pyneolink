# Connection Flow

Основний клас підключення: `pyneolink.camera.Camera`.

`Camera` приймає або готовий `CameraConfig`, або параметри напряму:

- `uuid` / `uid`;
- `address`;
- `cached_address`;
- `username`;
- `password`;
- `discovery`;
- `channel_id`;
- `state_path`;
- `debug`.

## Вхідна точка

Типовий шлях:

```python
with Camera(uuid="ABCDEF0123456789", password="password") as camera:
    info = camera.info()
```

`__enter__()` викликає:

1. `connect()`
2. `login()`

Після цього `camera.sock`, `camera.cipher`, `camera.login_xml` готові для команд.

## `connect()`

`connect()` вибирає транспорт у такому порядку.

### 1. Local UDP P2P probe для UID

Якщо є `uid`, немає `address`/`cached_address`, і `discovery` дозволяє UID discovery, `Camera.connect()` спочатку пробує:

```python
connect_local_direct(uid)
```

Це локальний UDP handshake з камерою через broadcast. Якщо камера доступна в локальній мережі і відповідає, повертається `UdpBcConnection`.

Після успіху:

- `self.sock = UdpBcConnection(...)`;
- `self.connected_address = sock.addr`;
- `.pyneolink_state.json` оновлюється з `transport="udp-local"`.

Якщо локальний шлях не спрацював:

- при `discovery="local"` помилка пробрасывається;
- в інших режимах код переходить до наступного способу.

### 2. Explicit `address`

Якщо в конфігу є `address`, `_resolve_address()` повертає `host:port`. Далі:

```python
socket.create_connection((host, port))
```

Це TCP Baichuan шлях. За замовчуванням port `9000`, якщо порт не вказаний.

### 3. `cached_address`

Якщо є `cached_address`, використовується він. Це теж TCP шлях, якщо address не позначений як relay.

### 4. Cached TCP state

Якщо є `state_path`, `ConnectionState.get_address(camera_name, transport="tcp")` може повернути попередню TCP адресу.

### 5. UDP relay

Якщо є `uid` і `discovery` дорівнює `relay` або `cellular`, `_resolve_address()` повертає спеціальний marker:

```python
("", 0, "udp-relay")
```

Після цього `connect()` викликає:

```python
connect_relay(uid)
```

Це відкриває UDP P2P канал через Reolink register/relay інфраструктуру. Результат також `UdpBcConnection`.

Після успіху state оновлюється з `transport="udp-relay"`.

## `ensure_connected()`

Більшість публічних методів не вимагають, щоб користувач вручну викликав `connect()`:

```python
def ensure_connected(self):
    if self.sock is None:
        self.connect()
    if not self.login_xml:
        self.login()
```

Тобто `camera.info()`, `camera.command()`, `camera.sd_card()...` можуть самі підняти connection/login.

## `reconnect()`

`reconnect()` завжди робить:

1. `close()`
2. `connect()`
3. `login()`

Це використовується, коли запит отримує timeout/EOF/OSError, наприклад у battery polling або після невдалої download спроби.

## Online lease

`Camera.require_online()` повертає context manager, який збільшує `_online_required`.

Це потрібно для сценаріїв, де камера має залишатися online:

- live stream;
- HLS session;
- battery `mode="online"`;
- довгі операції, де не можна закривати socket між запитами.

Поки `online_required == True`, інші компоненти, які зазвичай могли б закрити connection, не повинні ламати поточний online сценарій.
