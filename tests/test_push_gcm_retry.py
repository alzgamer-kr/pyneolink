"""Exercise the installed optional dependency using an in-memory HTTP session."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pyneolink.internal.push_fcm import GcmRegistrationRetry

try:
    from firebase_messaging import FcmRegisterConfig
    from firebase_messaging.fcmregister import FcmRegister
except ImportError:
    FcmRegister = None


@unittest.skipIf(FcmRegister is None, "Optional firebase-messaging dependency is not installed")
class RegistrationRetryTests(unittest.TestCase):
    def test_success_on_fifth_attempt_keeps_the_same_request_identity(self):
        calls = []

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def text(self):
                return "token=TEST-TOKEN" if len(calls) == 5 else "Error=PHONE_REGISTRATION_ERROR"

        class Session:
            def post(self, **kwargs):
                calls.append({"headers": dict(kwargs["headers"]), "data": dict(kwargs["data"])})
                return Response()

        class Registration(GcmRegistrationRetry, FcmRegister):
            pass

        config = FcmRegisterConfig(
            project_id="TEST",
            app_id="TEST",
            api_key="TEST",
            messaging_sender_id="123",
        )
        registration = Registration(config, http_client_session=Session())
        with (
            patch("firebase_messaging.fcmregister.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("firebase_messaging.fcmregister._logger"),
            patch("pyneolink.internal.push_fcm.emit"),
        ):
            result = asyncio.run(registration.gcm_register({"androidId": "123", "securityToken": "TEST"}))
        self.assertEqual(result["token"], "TEST-TOKEN")
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call == calls[0] for call in calls))
        self.assertEqual(sleep.await_count, 4)
        self.assertEqual(result["app_id"], calls[0]["data"]["X-subtype"])


if __name__ == "__main__":
    unittest.main()
