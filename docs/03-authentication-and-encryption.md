# Authentication And Encryption

Authentication happens in `Camera.login()` and has two stages:

1. a legacy login request that asks the camera for a nonce and encryption mode;
2. a modern login request with MD5 username/password values.

## Step 1: Legacy Login

`Camera.login()` gets a new `msg_num`:

```python
msg_num = self._next_msg_num()
```

Then it sends a legacy packet:

```python
encode_legacy_login(msg_num, username, max_encryption="aes")
```

In `bc.py`, this packet uses:

- `MSG.LOGIN`;
- `MSG_CLASS.LEGACY`;
- a legacy XML body;
- `response_code = 0xDC12` for `max_encryption="aes"`.

The camera should reply with XML that contains a `nonce`.

## Cipher Selection

After the first reply, `Camera.login()` inspects the low byte of `response_code`:

```python
mode = reply.header.response_code & 0xFF
```

Current behavior:

- `0`: no encryption;
- `1`: BC XOR;
- `2`: AES;
- `0x12`: AES with `full_media=True`.

`full_media=True` means binary/media payloads may have more of the stream encrypted, not only XML metadata.

## Step 2: Modern Login

Once the nonce is available, username and password are not sent as plain text.

The code computes:

```python
login_username = md5_hex(username + nonce)
login_password = md5_hex(password + nonce)
```

`md5_hex()` returns uppercase MD5 and truncates to 31 characters by default. This matches what the camera expects.

The login XML is then built:

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

This XML is wrapped with `xml_document()` into `<body>...</body>` and sent as modern `MSG.LOGIN`.

## Why AES Login Still Uses BC

`encode_modern()` has a special case:

```python
if msg_id == MSG.LOGIN:
    wire_cipher = Cipher("bc")
```

So even when the negotiated future mode is AES, the modern login payload itself is encoded with BC XOR. After login succeeds, later requests use the selected `self.cipher`.

## BC XOR

`bc_xor(offset, data)` uses `BC_XML_KEY` and `channel_id` as the offset.

Because XOR is symmetric, the same function is used for encryption and decryption.

In `Cipher.encrypt()` / `Cipher.decrypt()`:

- `name == "none"` returns data unchanged;
- `name == "bc"` applies `bc_xor()`;
- `name == "aes"` applies AES except for media fallback cases.

## AES-CFB

AES key derivation:

```python
make_aes_key(nonce, password)
```

Formula:

```text
md5_hex(nonce + "-" + password)[:16].encode("ascii")
```

The first 16 ASCII bytes become the AES key.

The IV is:

```text
b"\x00" * 16
```

The implementation uses the `cryptography` package.

## Binary Payloads And `encryptLen`

`recv_message()` decrypts extension data separately from payload data. If the extension contains:

```xml
<binaryData>1</binaryData>
```

the payload is treated as binary.

If the extension contains `encryptLen` and the cipher is AES with `full_media=True`, then:

1. the first `encryptLen` payload bytes are decrypted;
2. the remaining payload bytes are kept as raw bytes.

This matters for live media and SD-card downloads, where the camera can mix encrypted headers/metadata with raw media bytes.
