# Authentication And Encryption

Авторизація відбувається у `Camera.login()` і складається з двох кроків:

1. legacy login request для отримання nonce і режиму шифрування;
2. modern login request з MD5 username/password.

## Крок 1: legacy login

`Camera.login()` бере новий `msg_num`:

```python
msg_num = self._next_msg()
```

Потім відправляє legacy packet:

```python
encode_legacy_login(msg_num, max_encryption="aes", channel_id=...)
```

У `bc.py` це packet з:

- `msg_id = MSG.LOGIN`;
- `msg_class = MSG_CLASS.LEGACY`;
- `response_code = 0xDC12` для `max_encryption="aes"`.

Камера відповідає XML, у якому має бути `nonce`.

## Вибір cipher

Після першої відповіді `Camera.login()` дивиться на low byte `response_code`:

```python
low = reply.header.response_code & 0xFF
```

Поточна логіка:

- `0`: `Cipher("none")`;
- `1`: `Cipher("bc")`;
- `2`, `3`, `0x12`: `Cipher("aes", make_aes_key(...))`;
- `0x12`: AES з `full_media=True`.

`full_media=True` означає, що для binary/media payload камера може шифрувати більшу частину потоку, а не тільки XML.

## Крок 2: modern login

Коли nonce отримано, username/password не відправляються як plain text.

Код робить:

```python
username = md5_hex(username + nonce)
password = md5_hex(password + nonce)
```

`md5_hex()` повертає uppercase MD5 і за замовчуванням обрізає до 31 символа. Це відповідає поведінці, яку очікує камера.

Далі формується XML:

```xml
<LoginUser version="1.1">
  <userName>...</userName>
  <password>...</password>
  <userVer>1</userVer>
</LoginUser>
<LoginNet version="1.1">
  <type>LAN</type>
  <udpPort>0</udpPort>
</LoginNet>
```

Цей XML загортається через `xml_document()` у `<body>...</body>` і відправляється як modern `MSG.LOGIN`.

## Чому AES login все одно йде через BC

У `encode_modern()` є спеціальний випадок:

```python
wire_cipher = Cipher("bc") if msg_id == MSG.LOGIN and cipher.name == "aes" else cipher
```

Тобто навіть якщо майбутній режим AES, modern login payload кодується BC XOR. Після успішного login наступні запити вже використовують обраний `self.cipher`.

## BC XOR

`bc_xor(offset, data)` використовує `BC_XML_KEY` і `channel_id` як offset.

Властивість XOR: одна й та сама функція використовується для encrypt і decrypt.

У `Cipher.encrypt()` / `Cipher.decrypt()`:

- `name == "none"` повертає data як є;
- `name == "bc"` застосовує `bc_xor()`;
- `name == "aes"` застосовує AES, крім media fallback випадку.

## AES-CFB

AES ключ:

```python
make_aes_key(nonce, password)
```

Формула:

```text
MD5("{nonce}-{password}").upper() + "\0"
```

Після цього беруться перші 16 ASCII bytes.

AES mode:

```python
AES-CFB
IV = b"0123456789abcdef"
```

Реалізація використовує пакет `cryptography`.

## Binary payload і `encryptLen`

`recv_message()` дешифрує extension окремо від payload. Якщо extension містить:

```xml
<binaryData>1</binaryData>
```

payload вважається binary.

Якщо в extension є `encryptLen`, а cipher AES з `full_media=True`, тоді:

1. перші `encryptLen` bytes payload дешифруються;
2. хвіст payload додається як raw bytes.

Це важливо для live media і download, де камера може змішувати encrypted header/metadata і raw media data.
