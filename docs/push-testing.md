# Push research and tests

## Branch scope

`codex/push-research` starts at the 0.5.0 main commit `536c1b1`.
Unverified reliability/HLS/download metadata changes are separately preserved
in `codex/reliability-unverified`, commit `1f6f66d`.

No public SDK API, dispatcher architecture, dependencies or release version
are changed here. The probe is experimental and is not a cloud receiver.
There is no working `Camera.push()` or `[push]` extra yet.

Verified on 2026-11-04: 195 tests passed on Python 3.14; the 32 push probe
tests also passed on Python 3.11. Ruff passed for the two new Python files.
The existing HTTP preview test required localhost access outside the sandbox.
No physical camera or cloud registration was exercised.

Approved physical target: **Home-Back**, using its exact config alias.
The owner reports mains power and can trigger motion manually. Do not use
Front, Remote or Friend for these tests. Stop on rejection or timeout; no
automatic registration loop, subscription deletion or changes to camera settings.

## Offline checks (ready now)

Run from the repository root in Windows CMD:

```bat
.venv\Scripts\python.exe -m pytest tests\test_push_protocol_probe.py -q
```

Replay synthetic FCM **data maps**, one JSON object per line:

```bat
.venv\Scripts\python.exe -u examples\push_protocol_probe.py replay --input tests\fixtures\push\events.jsonl
```

The fixtures were written for tests, not captured from a camera. They exercise
field extraction, unknown types, version validation, duplicate hints and raw
timestamps. No timezone or event delivery is inferred. The probe keeps no
unbounded in-memory event list. Its output excludes notification text, URLs,
tokens and arbitrary additional fields; camera IDs may still be present.

Fake-camera tests cover one command, no command retries, explicit opt-in,
target restriction, closure on failures and safe CLI diagnostics. These tests
do not demonstrate FCM connectivity or real dispatcher routing.

## Legacy registration check (receiver prerequisite)

**Do not run this until a dedicated FCM receiver has registered successfully.**
Obtaining a current, working headless FCM registration is still an open task.
Do not invent a token, use an unrelated Firebase project, or copy the phone's
token/client ID. Replacing the phone's registration could break its notifications.

Store the dedicated receiver's token and a stable, fresh uppercase UUID without
hyphens in ignored `.tmp/push/receiver.json`. The file schema is:

```json
{
  "purpose": "dedicated-test-receiver",
  "fcm_token": "<real token from the dedicated receiver>",
  "client_id": "<32 uppercase hexadecimal characters>"
}
```

The purpose marker is operator confirmation, not proof of token ownership.
Preserve the receiver's full transport credentials separately with restricted
file permissions; this small JSON file alone cannot restore an FCM connection.
Never commit credentials or include them in command-line arguments/logs.

Once the receiver is ready and the legacy path is appropriate for this camera:

```bat
.venv\Scripts\python.exe -u examples\push_protocol_probe.py register-legacy --config config.json --camera "Home-Back" --credentials .tmp\push\receiver.json --allow-registration
```

This command **changes the camera's push registration** using legacy msg_id 124
and `PushInfo/token`, `phoneType=reo_fcm`, `clientID`. It uses the existing
Camera/dispatcher, closes the camera session, disables command reconnect retries,
and prints only the reply ID/code. Normal SDK connection discovery may still
send its existing handshake retries. A 200 means command acceptance only.
A 400/timeout is inconclusive, not proof of lack of push support; preserve the
result and investigate capabilities/v2 without repeatedly registering.

The newer app also has native `BCSDK_RemoteGetPushClientId` and
`BCSDK_RemoteCreatePushListener`. Do not guess their Baichuan wire IDs from SDK
enum IDs. The legacy probe does not implement v2 or its cleanup procedure.

## Physical acceptance checklist (pending receiver implementation)

| Test | Procedure | Required evidence |
| --- | --- | --- |
| FCM registration | Start dedicated receiver on PC | Registration succeeds; no token in logs |
| Subscription | Register Back once using its supported path | Matching reply; phone registration preserved |
| Session independence | Close camera session and other SDK tests; put phone offline | No video/motion polling sessions remain |
| Motion | Owner enters view at a noted wall-clock time | PC receives matching camera/event; record receive time |
| Idle | Wait with no deliberate motion | Do not treat silence as failure or proof of no motion |
| Repeated motion | Repeat after configured camera push interval | Correlation by time; no assumption of one push per movement |
| Restart | Restart receiver with same persisted identity | Events return without new client registration each run |
| Network outage | Temporarily disconnect receiver network, restore it | Bounded reconnect/backoff; RAM/threads stable |
| Duplicate | Replay same delivery ID | Future receiver deduplicates with bounded retention; parser alone does not |
| Coexistence | Put phone online and trigger another event | Both subscriptions still work |
| Long run | Leave receiver running beyond 30-60 minutes | Events arrive without holding camera session open |
| Shutdown | Stop receiver | Worker/socket cleanup; no camera reconnect loop |

FCM is not assumed to provide a lossless motion history. Record delayed or missing
events rather than declaring every gap a protocol failure. Do not use `dup` alone
as a durable delivery identifier. Check account-scoped recipient filtering before
accepting system/account messages as camera motion.

## Evidence and next work

Local, untracked APK analysis is in `.tmp/research/reolink-android/push-research.md`.
Source references: Reolink 4.64.3.20261023 `FcmListenerService`, `PushMessageFactory`,
`Device.remoteAddPushToServer`, `Device.getClientIdPushV2`, and local Neolink
`crates/core/src/bc_protocol/pushinfo.rs` / `src/common/pushnoti.rs`.
No proprietary decompiled source or app binary is included in this branch.

Next: review current receiver libraries and their licenses, confirm the supported
registration path, then implement an opt-in headless receiver and real transport
recovery tests. Offline checks must not be reported as physical-camera validation.
