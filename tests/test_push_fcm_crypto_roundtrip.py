"""Optional, network-free integration check in the isolated push environment."""

import json
import sys
import tempfile
import unittest
from base64 import urlsafe_b64encode
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyneolink.internal.push_fcm import DataMessageDiagnostics, capture_notifications  # noqa: E402

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from firebase_messaging import FcmRegisterConfig
    from firebase_messaging.fcmpushclient import DataMessageStanza, FcmPushClient
    from http_ece import encrypt
except ImportError:
    OPTIONAL_AVAILABLE = False
else:
    OPTIONAL_AVAILABLE = True


@unittest.skipUnless(OPTIONAL_AVAILABLE, "Requires the isolated push-probe dependencies")
class CryptoRoundtripTests(unittest.TestCase):
    def test_unpadded_and_multi_parameter_headers(self):
        class Client(DataMessageDiagnostics, FcmPushClient):
            pass

        recipient = ec.generate_private_key(ec.SECP256R1())
        sender = ec.generate_private_key(ec.SECP256R1())
        auth, salt = b"a" * 16, b"s" * 16

        def public(key):
            return key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )

        def b64(data):
            return urlsafe_b64encode(data).decode("ascii")

        credentials = {
            "gcm": {"app_id": "test-app"},
            "keys": {
                "private": b64(
                    recipient.private_bytes(
                        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
                    )
                ),
                "secret": b64(auth),
            },
        }
        event = {"data": {"pushVersion": "v2a", "uid": "TEST", "type": "motion"}}
        ciphertext = encrypt(
            json.dumps(event).encode(),
            salt=salt,
            private_key=sender,
            dh=public(recipient),
            auth_secret=auth,
            version="aesgcm",
        )
        config = FcmRegisterConfig(
            project_id="test", app_id="test", api_key="test", messaging_sender_id="123", bundle_id="org.example.test"
        )
        for multiple in (False, True):
            with self.subTest(multiple=multiple):
                msg = DataMessageStanza()
                msg.persistent_id = "synthetic-id"
                msg.raw_data = ciphertext
                dh = "dh=" + b64(public(sender)).rstrip("=")
                if multiple:
                    dh = "p256ecdsa=IGNORED; " + dh
                for key, value in (
                    ("crypto-key", dh),
                    ("encryption", "salt=" + b64(salt).rstrip("=")),
                    ("subtype", "test-app"),
                ):
                    msg.app_data.add(key=key, value=value)
                received = []
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "raw.jsonl"
                    with capture_notifications(path) as capture:

                        def on_message(notification, persistent_id, context):
                            capture(notification, persistent_id)
                            received.append((notification, persistent_id, context))

                        client = Client(on_message, config, credentials=credentials)
                        client._handle_data_message(msg)
                        row = json.loads(path.read_text(encoding="utf-8"))
                        self.assertEqual(row["notification"], event)
                        self.assertEqual(row["persistent_id"], "synthetic-id")
                self.assertEqual(received, [(event, "synthetic-id", None)])


if __name__ == "__main__":
    unittest.main()
