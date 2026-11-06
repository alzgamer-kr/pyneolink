"""Public Reolink Android client configuration, not receiver or server credentials.

Extracted from the owner's APK resources for com.mcu.reolink 4.64.3.20261023.
This profile was used in the physical FCM delivery tests. These client-side
identifiers do not grant camera access and cannot replace receiver credentials.
"""

from __future__ import annotations

SOURCE_APP_VERSION = "4.64.3.20261023"
# APK signer, not the Source Stamp certificate.
ANDROID_CERT_SHA1 = "B2D0246EFC055FA09DEEB3604330C89A35F6DC31"


def default_firebase_config() -> dict[str, str]:
    """Return a fresh copy so callers cannot modify the package default."""
    return {
        "project_id": "reolink-login",
        "app_id": "1:743639030586:android:86f60a4fb7143876",
        "api_key": "AIzaSyBDvKxhyUK_QVCOdGaclvV96_C3H-aPWiI",
        "messaging_sender_id": "743639030586",
        "bundle_id": "com.mcu.reolink",
    }
