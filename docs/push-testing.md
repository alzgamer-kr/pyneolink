# Push research and tests

## Current Commit Preparation (2026-11-06)

The sections below are a chronological research log, including superseded
commands and restrictions. README describes the current SDK/CLI. Physical tests
have since confirmed multi-camera reception, unread-only read, watch recovery
after network loss, and an LPC payload with LOW BATTERY WARNING on Argus Eco.
The percentage threshold is still unknown; language configuration is not yet
implemented or physically tested.

Final offline checks cover typed PushEvent helpers, full payload preservation,
legacy inbox rows, deduplication, and identical read/watch event formatting.
Both CLI commands now emit JSONL rather than read emitting a JSON array.
No camera or subscription changes are required for these offline tests.

Commit-preparation validation: 571 passed and 2 skipped on both Python 3.14
and 3.11. The additional real firebase-messaging crypto round-trip passed in
the optional push environment. Ruff E/F/W, PEP 257 checks on the push/storage
modules, and wheel/sdist metadata checks passed. Language configuration remains
follow-up work, not a shipped capability.

Before committing, smoke-check read and watch against the existing subscription
without reinitializing its receiver. Confirm title appears in compact output,
--full-response includes response, and --raw-output appends incoming envelopes.
Do not commit local .tmp captures, SQLite databases, credentials, or lock files.

## Fresh GCM Registration Retest (2026-11-05)

Front initialization repeatedly completed check-in with HTTP 200, then received
`PHONE_REGISTRATION_ERROR` from both register3 attempts. The dependency creates
one check-in and subtype per registration call, but defaults to only two
attempts one second apart. A downstream investigation of the same library
reported this pattern and successful bounded retry mitigation:
[cookidoo-api PR 270](https://github.com/miaucl/cookidoo-api/pull/270).
This is evidence for testing a retry budget, not proof of Front's root cause.

The SDK now passes five attempts to the existing GCM call. It does not patch
site-packages, change payloads/headers, rotate IDs between those attempts, or
retry the entire Firebase flow. The existing 60-second registration cap remains.
One-second retry spacing is still owned by firebase-messaging 0.4.5.
Existing Back credentials are not modified. Physical verification is pending;
run Front init once and inspect the attempt count and stage reached. Repeated
failure is not a reason to reset the database or copy Back credentials.

This is a chronological research log. Old probe commands and intermediate
limitations below are historical, not the current API. For current SDK/CLI
commands see README's Cloud Push section and "Push SDK/CLI Physical Retest"
below. SQLite storage and finite unread-only read are Unreleased and require a
new physical retest; earlier delivery evidence does not validate those changes.

## Branch scope

`codex/push-research` starts at the 0.5.0 main commit `536c1b1`.
Unverified reliability/HLS/download metadata changes are separately preserved
in `codex/reliability-unverified`, commit `1f6f66d`.

No public SDK API, dispatcher architecture, SDK dependencies or release version
are changed here. The protocol probe is not a receiver; the separate FCM probe
below is an experimental receiver wrapper, not a validated SDK feature.
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

### FCM receiver experiment

`examples/push_fcm_probe.py` now provides a standalone receiver experiment with
`firebase-messaging==0.4.5` (MIT), not a public SDK dependency. Its registration
path uses the Firebase installations/registrations endpoints. The old local
Neolink fcm-push-listener 2.0.3 cannot be treated as a current working baseline:
its upstream README says registration below 3.0 stopped working in June 2024.

References:
- https://github.com/sdb9696/firebase-messaging
- https://github.com/RandomEngy/fcm-push-listener

Create/install in an isolated environment, once (already prepared on this PC):

```bat
.venv\Scripts\python.exe -m venv .tmp\push-venv
.tmp\push-venv\Scripts\python.exe -m pip install -r examples\requirements-push-probe.txt
```

Offline configuration check, using the local JADX export by default:

```bat
.tmp\push-venv\Scripts\python.exe -u examples\push_fcm_probe.py
```

Explicitly allow Google registration/check-in and MCS listening for 10 minutes:

```bat
.tmp\push-venv\Scripts\python.exe -u examples\push_fcm_probe.py --allow-network --duration 600
```

No camera connections or Reolink subscription changes occur in this script.
It extracts application Firebase configuration from the export, not your phone's
private state. `registered` means credentials were saved; `receiver-state` with
`STARTED` means the MCS login completed. Neither proves Reolink event delivery.
Failure should be investigated before any camera registration command is sent.
Registration is not yet successful: on 2026-11-04 Google check-in succeeded,
but Firebase Installations rejected the request with HTTP 403,
`PERMISSION_DENIED` / `API_KEY_ANDROID_APP_BLOCKED`. The Python dependency does
not provide the Android package/certificate headers used by the Android client.

The probe now attaches `X-Android-Package` and `X-Android-Cert` only to the two
Firebase HTTPS endpoint hosts, and rejects their redirects. The default package
is `com.mcu.reolink`; the signer SHA-1 is
`B2D0246EFC055FA09DEEB3604330C89A35F6DC31`, extracted from the supplied
4.64.3.20261023 APK using Android SDK `apksigner verify --print-certs`.
This is the APK signer, not its separate Google Source Stamp certificate.
Signature verification confirms the file's signature, not independent proof of
publisher authenticity. No phone credentials or private signing key are used.
For another verified APK signer, use `--android-cert-sha1` (40 hex characters).
The owner verified the updated probe on 2026-11-04 at 17:49 UTC:
Firebase installations and registration returned 200, credentials were saved,
and the MCS receiver reached STARTED. This proves registration/connectivity,
not token compatibility with Reolink or event delivery.

The subsequent single legacy registration on Home-Back returned
`msg_id=124, response_code=55534`; the camera session was then closed.
`55534 = 0xD8EE` (signed 16-bit interpretation: `-10002`). Its wire-level
meaning is unknown. Do not classify this as unsupported, invalid token, a
session limit or success without further evidence. Do not repeat registration
or replace the receiver credentials to work around this result.

Google documents these Android API-key headers at
https://docs.cloud.google.com/docs/authentication/api-keys .

State defaults to `.tmp/push/receiver.json`, compatible with the legacy command
below, plus full FCM credentials and a bounded recent delivery-ID list. Never
share this file. POSIX credentials are owner-only; on Windows protect the folder
with your user ACLs. It is not encrypted at rest. No raw dependency log bodies or
tokens are printed. A `.running` file rejects simultaneous receivers; after a
crash, verify no probe is running before removing the stale marker manually.

State is reused on restart; malformed state and changed check-in identities fail
closed. There is no automatic token-refresh/re-registration workflow yet. The
upstream listener handles heartbeat/reconnect within a bounded experiment, not a
proven production service. Restart restores the recent dedup window only. Unknown
payload formats are reported without raw bodies; events are not a durable queue.
Current delivery-ID tracking does not guarantee lossless or exactly-once delivery.

**Do not run this until a dedicated FCM receiver has registered successfully.**
Obtaining a current, working headless FCM registration is still an open task.
Do not invent a token, use an unrelated Firebase project, or copy the phone's
token/client ID. Replacing the phone's registration could break its notifications.

The FCM probe writes `.tmp/push/receiver.json` automatically. For another receiver,
store its token and a stable, fresh uppercase UUID without hyphens. The minimum
legacy registration file schema is:

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

Static follow-up: the existing Windows SDK catalog maps `PushCreateListener`
to wire ID 524, internal command `0x97e`, serializer `FUN_18011df50`.
The local decompilation has `clientID`, `tag` (resolved by the earlier Ghidra
string export), and `channelId` in this serializer.
`BCSDK_RemoteGetPushClientId` uses internal command `0x97c`, not a wire ID.
Tracing its separately registered handler `FUN_180158a30` found a direct
`FUN_1801947c0(..., 0x20a, ...)` call: wire ID **522**.
`FUN_180139710` encodes `body/PushClientID`, version `1.1`, with
`clientType` and `pushToken`. `FUN_1801b8400` parses a `clientID` string into a
65-byte buffer (up to 64 characters); its `0x17db` is an internal data type,
not another wire command. These are static Windows SDK findings, not yet a
successful response from the test camera. Android `Device.java`
`remoteOpenPushV2` obtains a client ID first when none is stored, then creates
the listener. Resolve that complete flow and capability selection before any
v2 physical test. Do not blindly substitute 524 for 124 or reuse the legacy
UUID as a v2 server-issued client ID. The catalog's ID 125 `PushInfo` entry
is not evidence that it is a registration alternative to 124.

### V2 client-ID step (one command, no listener)

Stop the FCM probe first and keep `.tmp/push/receiver.json`. The new
`examples/push_v2_probe.py` uses that dedicated receiver's token and sends
only 522 with no command retry or reconnect. Its Get operation may create a
server-side identity; it therefore requires explicit registration approval.
It does not create a listener, enable push, or change the phone's subscription.
The existing SDK handles camera discovery/login and closes the camera afterward.

Offline validation (no camera connection):

```bat
.venv\Scripts\python.exe -u examples\push_v2_probe.py
```

One physical request on the approved Back camera:

```bat
.venv\Scripts\python.exe -u examples\push_v2_probe.py --config config.json --camera "Home-Back" --credentials .tmp\push\receiver.json --allow-registration
```

Success requires response 200 and exactly one valid `body/PushClientID/clientID`.
The ID is stored in `.tmp/push/reolink-v2.json`, separately from the unchanged
FCM state, and bound to a SHA-256 digest of its FCM token. Treat both JSON files
as private credentials. An existing v2 file is never replaced. Unexpected XML
or response codes do not trigger another command or listener creation; share
only the sanitized console output. If the step fails, investigate before retrying.
No motion test or running FCM receiver is needed for this step.

The owner completed this step at 18:00:49 UTC on 2026-11-04: response 522/200,
`client_id_saved=true`, `listener_created=false`. No client-ID regeneration is
needed for the next step.

Verification: 85 offline push tests passed on 2026-11-04. The v2 checks cover
request XML, target/approval guards, rejection, malformed response, cleanup,
single-command behavior, output redaction, and preservation of existing files.

### V2 listener step (ready for physical validation)

Android `Device.remoteAddPushV2ToServer` sets the client ID and **device UID**
as the channel UID. Its zero-initialized structure leaves `tag` empty. The
Windows XML serializer emits `body/PushCreateListener version="1.1"` with
`clientID`, `tag`, and `channelId`. Thus `channelId` here is the device UID,
not the numeric Baichuan header channel. This probe only supports the approved
standalone Back camera on header channel 0, not NVR/multi-channel devices.

Offline validation of saved identity, FCM token binding, and target UID:

```bat
.venv\Scripts\python.exe -u examples\push_v2_probe.py --action create-listener
```

First start the FCM receiver again, reusing the same credential file:

```bat
.tmp\push-venv\Scripts\python.exe -u examples\push_fcm_probe.py --allow-network --duration 600
```

Once it reaches STARTED, in another terminal run **once**:

```bat
.venv\Scripts\python.exe -u examples\push_v2_probe.py --action create-listener --config config.json --camera "Home-Back" --allow-registration
```

This sends only 524 and closes the camera session. It uses the existing v2
client ID; it does not regenerate an ID, delete another listener, change alarm
schedules or enable global push settings. Defaults use the same two files from
the preceding steps. It writes `listener_status=pending-or-unknown` before
sending, then saves the reply as accepted/not-accepted. A timeout or crash
leaves uncertainty recorded. All attempted states prevent automatic repeated
creation; do not remove the marker or identity files to retry blindly.

For 524/200 and `listener_created=true`, trigger motion and look for `event`
in the receiver terminal. Acceptance does not prove delivery or coexistence
with the phone; test both. For rejection/timeout, share only sanitized console
output and stop registration attempts. The identity files remain private.
99 offline push tests passed after adding listener validation and cleanup tests.

The owner received 524/200 (`listener_created=true`) at 18:03:56 UTC on
2026-11-04. Do not create the listener again. The receiver had reached STARTED
at 18:03:39, but at 18:04:41 its `_listen` task logged an error and stopped.
No event was reported, so delivery remains unproven. The old safe logger hid
exceptions passed as log formatting arguments, making this cause unknown.

The receiver now logs exception type, function/line and allowlisted categories,
plus data-message shape (counts and presence of known keys, not values).
Upstream 0.4.5 assumes Web Push crypto headers; plain Android app-data and
encrypted-payload decoding errors are distinct possibilities. No decoder or
ACK behavior has been changed without evidence. Restart only the FCM receiver
with existing state and share the sanitized diagnostic lines. Tests: 105 passed.
Related upstream reports (not proof of this failure's cause):
https://github.com/sdb9696/firebase-messaging/issues/40 and
https://github.com/sdb9696/firebase-messaging/issues/42 .

At 18:08:17 UTC, the owner captured a data stanza with five app-data fields,
660 raw bytes and all three Web Push headers, followed by `b64decode` /
`invalid-base64-padding`. This confirms an encrypted-payload decoding failure,
not missing Android app-data headers. Immediate delivery on restart is consistent
with an unacknowledged message being redelivered; message identity is not proven.

The probe now selects `dh`/`salt` using a parameter parser, restores base64url
padding, and validates parameter sizes before delegating to the unchanged
dependency decryption/callback/ACK flow. Multiple Crypto-Key parameters and
parameter order are supported; ambiguous/invalid fields still fail visibly,
not silently skipped and acknowledged. No installed dependency files were edited.
127 offline tests passed. Additionally, a synthetic encrypted round-trip test
passed using the actual isolated dependencies (unpadded and multi-parameter
headers), without reading real credentials or connecting to the network:

```bat
.tmp\push-venv\Scripts\python.exe tests\test_push_fcm_crypto_roundtrip.py
```

Next physical check: restart only the existing FCM receiver, then trigger motion.
Do not repeat 522/524 or replace any credential files.

## First physical delivery confirmed

On 2026-11-04 at 18:10:51 UTC (20:10:51 Europe/Kyiv), the owner received
two decrypted `v2` / `PEOPLE` events. Their UID matches the configured
Home-Back camera (checked locally without a camera connection).
Server timestamps were 18:04:38.523Z and 18:05:09.652Z, so these were delayed
events delivered after receiver restart, not measurements of normal latency.
This confirms actual event receipt with the existing FCM identity and v2
subscription; registration was not repeated. The receiver itself makes no
camera connections. Subsequent checks are recorded below; outage recovery and
long-run stability remain unverified.

The observed `channel_id` value is a UID, not the numeric channel 0; keep it
as received and do not coerce it into an integer. Missing event_id and
duplicate_hint=false do not establish exactly-once delivery.

Subsequent owner-observed events on 2026-11-04 (UTC):

| Server timestamp | PC receive timestamp | Difference | Observation |
| --- | --- | --- | --- |
| 18:12:07.598Z | 18:12:09.811435Z | 2.213 s | Fresh PEOPLE event, no receiver restart |
| 18:16:04.429Z | 18:16:06.617767Z | 2.189 s | PC still receives after phone push toggle; phone also reported one notification, exact correlation not confirmed |
| 18:18:45.813Z | 18:18:47.939973Z | 2.127 s | Owner disabled phone internet, walked past Back, PC received fresh PEOPLE event |

The last check confirms delivery without an online phone in this experiment.
Differences above assume synchronized clocks and are not motion-to-notification
latency measurements. Phone delivery had been intermittent/delayed before the
experiment as well; simultaneous, reliably correlated delivery to both clients
is not yet established. No 522/524 re-registration was needed for these events.
Keep the existing identities; next checks are receiver restart, network recovery
and longer runs, not repeated subscription creation.

At 18:20:42.421313Z the PC received `TEST` after the owner pressed the app's
test button (server timestamp 18:20:40.262Z, difference 2.159 s). TEST is useful
for transport checks but does not replace motion/detection tests. The phone
showed no notification, including subsequent tests on another camera and after
phone reboot. This broadens the suspected phone/app/FCM-delivery issue; neither
a phone fault nor harmlessness of the experimental registration is proven.
Do not cycle registrations to diagnose this; preserve both identities.

The receiver ended at 18:21:01 with TimeoutError, about 600+10 seconds after
startup. This strongly matches the probe's duration limit followed by its
10-second task-drain timeout. The installed dependency waits indefinitely for
TLS writer close. The probe now bounds that wait to three seconds, aborts the
transport if needed, and logs `receiver-stopping` / `duration-elapsed` separately
from `transport-close-forced`. Live shutdown validation is still pending.
131 offline tests passed, including clean, stalled, cancelled and failed close.

## Physical acceptance checklist (partially verified)

### Owner-requested PC unsubscribe experiment

#### Unified experimental CLI

Use `examples/push_probe.py` with `subscribe`, `status`, or `unsubscribe`.
These remain research commands, not public SDK/production CLI additions.
Defaults select only Home-Back and the existing dedicated PC
credentials. Stop the receiver first; the shared lock prevents concurrent
identity use. Do not use phone credentials.

```cmd
.venv\Scripts\python.exe -u examples\push_probe.py subscribe
.venv\Scripts\python.exe -u examples\push_probe.py subscribe --confirm
.venv\Scripts\python.exe -u examples\push_probe.py status
.venv\Scripts\python.exe -u examples\push_probe.py status --via-camera
.venv\Scripts\python.exe -u examples\push_probe.py status --save-diagnostic
.venv\Scripts\python.exe -u examples\push_probe.py unsubscribe
.venv\Scripts\python.exe -u examples\push_probe.py unsubscribe --confirm
```

- `subscribe` without `--confirm` performs offline validation only. With
  confirmation it requests 522 only when no v2 identity exists, then 524;
  it uses brief camera sessions, unlike the subsequent FCM receiver. It
  requires FCM credentials already generated by `push_fcm_probe.py`.
- An accepted existing registration is reported as local historical state,
  not verified current server status, and is not registered again. Failed,
  pending or previously removed registrations are not reset automatically.
- `status` sends one read-only cloud GET, without a camera session. Its
  `subscription_status` remains `unknown`: the successful list response
  schema has not yet been captured and validated. HTTP 200 alone does not
  establish membership. Cloudflare 1010 is explicitly identified when present.
- `status --via-camera` instead sends one read-only Baichuan 526 request,
  then closes the camera connection. This opt-in path does not use HTTP.
  An identity-matched complete `PushClientState/channelIdList` yields
  `subscribed` or `unsubscribed` for the target UID, with
  `verified_by=camera-client-state`. Errors, missing lists or mismatched IDs
  remain `unknown`. The receiver itself still requires no camera connection.
- `unsubscribe` without `--confirm` is an offline preflight. With confirmation
  it allows one scoped removal POST, without camera connections or global
  push changes. HTTP acceptance is distinct from verified removal.
- A recorded removal attempt (including the current 403) blocks another
  mutation with `reason=previous-removal-attempt`. Do not delete that marker
  or identity files to force a retry. The commands do not bypass the observed
  cloud access block. No live mutation was run while preparing this CLI.

Legacy probe flags continue to work. The receiver remains a separate process;
none of these commands starts it. The unified command tests use fake camera
and cloud responses with network connections forbidden.

To compare phone delivery without the PC subscription, the owner authorized
removing that dedicated listener. This is distinct from deleting the Firebase
installation/token or stopping the local process. The APK's `closePush` uses
POST `https://pushx.reolink.com/v2/client/listening-devices/deletion`, bearer
client ID, and JSON `uid` plus `channelIds`; its standalone default uses the
device UID in both. `examples/push_unsubscribe_probe.py` reproduces this one
request with the **PC's** saved client ID, never phone credentials or global
camera push settings.

Stop the FCM receiver first (Ctrl+C). Offline validation:

```bat
.venv\Scripts\python.exe -u examples\push_unsubscribe_probe.py
```

Explicitly submit the removal once:

```bat
.venv\Scripts\python.exe -u examples\push_unsubscribe_probe.py --allow-unsubscribe
```

Defaults target only Home-Back and the existing two private state
files. Both identities are retained. The script rejects an active receiver,
identity/UID/token mismatches, and repeated removal attempts. It uses verified
TLS with no redirects, automatic retries or camera connections. Only HTTP
status is printed; a 2xx means `removal_accepted=true`, **not** independently
verified removal. A timeout leaves a pending/unknown result in the v2 state.
Do not delete state or blindly retry. Once accepted, test phone delivery with
the PC receiver stopped. Phone recovery alone does not establish causality;
record results. New 522/524 registration is not part of this experiment.
146 offline tests passed, including removal scope and state guards. No live
unsubscribe request has been executed by the assistant.

Owner run on 2026-11-04 at 18:34:41 UTC returned HTTP 403, with both
`removal_accepted` and `removal_verified` false. Its cause is not established.
Do not repeat deletion or discard either identity file. With the receiver
stopped, inspect authorization using the APK's read-only list endpoint:

```cmd
.venv\Scripts\python.exe -u examples\push_unsubscribe_probe.py --check-subscription
```

This sends one GET to `/v2/client/listening-devices` with the dedicated PC
client ID, without changing registration state or connecting to the camera.
It works after a recorded removal attempt. Diagnostics expose only status,
allowlisted content type, response size, JSON root shape and a bounded numeric
top-level code, never raw response text, subscription identities or tokens.
Responses are capped at 64 KiB plus one byte to detect truncation; redirects
and retries remain disabled. A successful GET alone does not verify removal.

The owner's GET at 18:38:51 UTC also returned 403 (JSON object, 16 root
members, 707 bytes). No removal or cause is confirmed. APK
`ChallengeInterceptor.java` checks nested `error.code == 8214` and
`error.metadata.challenge.type` (`captcha/1` or `pow/1`); absence of a
`WWW-Authenticate` header does not rule out that separate mechanism.
Read-only diagnostics now report these markers, bounded nested numeric codes
and allowlisted field names, without challenge IDs, URLs, messages or tokens.
No challenge is solved and no request is automatically retried. This is a
candidate explanation to check, not a diagnosis of the observed 403.

At 18:41:26 UTC the owner saw the same 403 with top-level
`detail/status/timestamp/title/type`, no `error` object. This does not match
the inspected 8214 response schema. To examine the actual explanation without
exposing response contents in terminal logs, run once with:

```cmd
.venv\Scripts\python.exe -u examples\push_unsubscribe_probe.py --check-subscription --save-diagnostic
```

The bounded response is saved privately in
`.tmp/push/subscription-http-diagnostic.json`; it may contain identifying
server data, so do not publish it. Request headers are not saved, and literal
client-ID echoes are redacted. Existing credential/state files are preserved.
Windows permissions are inherited from the containing directory; POSIX uses
the existing owner-only state writer. This remains a single GET, not deletion,
registration, or automatic challenge handling.

The saved response at 18:42:53 UTC identifies Cloudflare error 1010,
`browser_signature_banned`, with `retryable=false` and
`owner_action_required=true`. The detail attributes denial to the client's
browser signature. This establishes the cause of this captured HTTP 403,
not invalidity of the Reolink client ID, Firebase token, or phone subscription.
Earlier 403 bodies were not retained, so identical causes for those requests
remain an inference. No PC subscription removal has been confirmed.
Stop repeating these cloud probes. Keep both identities and the stopped
receiver intact. Contact Reolink for a supported client/API access or removal
route; the private diagnostic contains the timestamp and request reference.
See [Cloudflare error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).
Do not treat stopping the receiver or deleting local credentials as server-side
unsubscribe, and do not infer that the observed phone delivery failure is
caused by this HTTP block.

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

### Owner's 30-minute receiver test, 2026-11-05

Local log: `.tmp/push/receiver-30min.log`. The existing receiver identity was
reused; this probe does not create a Camera connection or send 522/524.
Reception ran from 07:22:57 to 07:52:57 UTC (09:22:57 to 09:52:57 Kyiv).
Eight events were decoded: two PEOPLE and six MD. PEOPLE arrived at 09:23:33
and 09:50:09 Kyiv, consistent with the owner's beginning/end walk checks.
The last MD arrived at 09:52:39. Server-to-receiver timestamp differences for
these eight events were about 2.68-2.80 seconds; this is not measured
motion-to-notification latency and depends on clock alignment.

No receiver failure or reconnect was logged during reception. On scheduled
shutdown, TLS close exceeded the three-second bound: `transport-close-forced`
with `tls-close-timeout`, then `probe-ended` at 07:53:00 UTC with
`connected=true` and `reason=duration-elapsed`. The bounded cleanup worked,
but the peer did not complete a graceful TLS close within that limit.
The current probe hardcodes `delivery_proven=false` in its final summary;
this field is not an event counter and does not negate the decoded events.
This test does not establish lossless delivery or network-outage recovery.

### Android unsubscribe call-chain inspection, 2026-11-05

Static analysis of the supplied 4.64.3.20261023 JADX export:

- `PushHomePresenter` calls `PushHomeModel.closePush` for disabling push.
  The HTTP branch calls `Device.HTTPRemovePushTokenFromServer` with the
  selected channel index. The V2 branch delegates to
  `PushInterfaceService.closePush`, rather than a Baichuan delete command.
- The request is POST `/v2/client/listening-devices/deletion`, with
  `Authorization: Bearer <MMKVManager.getClientId()>` and JSON `uid` plus
  `channelIds`. For a standalone camera the default channel list contains
  its UID. NVR/channel handling is separate. This targets the current app's
  client identity, not every receiver of that camera.
- The generated `closePush` request uses Drake Net's `BodyRequest` and
  `NetConfig` client. The exported default is a fresh OkHttp client with
  `NetOkHttpInterceptor`; no application override of that configuration was
  found in this export. OkHttp's fallback user agent is `okhttp/4.11.0`.
  This is static evidence, not an observed on-wire header capture.
- Correction to the earlier investigation: the `ChallengeInterceptor` and
  custom Reolink headers found in other HTTP code are not established as part
  of this request path. Do not assume those headers are required here.
- `closePush` returns Kotlin Result success, not `response.isSuccessful()`.
  The default NetConverter returns a raw Response without checking its status
  when the requested type is Response; the inspected NetOkHttpInterceptor
  also returns HTTP errors without status-based exceptions. In this visible
  configuration, an HTTP rejection can therefore be treated as success and
  update local `isPushOn=false`. Runtime overrides could alter this behavior;
  this does not establish that the owner's phone encountered an HTTP error.
- `PushManager.closePushIfDeviceNotExisted` also schedules removal if a
  notification arrives for a locally disabled device. A local off switch
  alone is not sufficient evidence of server-side unsubscribe.

No requests or phone/camera setting changes were made during this inspection.
Our probe should retain its explicit HTTP-status checks and behavioral
verification, not reproduce the apparent success-check weakness.

At the owner's explicit request, one unchanged read-only GET was performed
on 2026-11-05 at 08:20:36 UTC (10:20:36 Kyiv). It again returned HTTP 403
with Cloudflare 1010 / `browser_signature_banned`, `retryable=false`.
No server-side subscription list was obtained, no deletion was attempted,
and no camera connections were made. The earlier successful event delivery
remains evidence of activity at that time, not a fresh API status result.

### Native client-state discovery, 2026-11-05

The earlier HTTP-only status conclusion was incomplete. Android
`Device.getPushClientState()` calls `BCSDK_RemoteGetPushClientState` with its
client ID and a zeroed (empty) channel filter. Windows export internal command
0x984 maps to handler `FUN_180158cb0`, which selects wire ID 0x20e (526),
not 523. Serializer `FUN_1801300d0` emits `PushClientState version="1.1"`,
`clientID`, and `channelIdList`. Parser `FUN_1801b8750` reads the echoed ID
and up to 64 `channelId` entries. This supports the opt-in status path above.
206 focused offline tests passed after implementation; real-camera validation
of 526 remains pending. No corresponding native listener-delete command has
been established. A full subscribe/status/unsubscribe/resubscribe lifecycle
is not yet validated and must not be advertised as complete.

Local, untracked APK analysis is in `.tmp/research/reolink-android/push-research.md`.
Source references: Reolink 4.64.3.20261023 `FcmListenerService`, `PushMessageFactory`,
`Device.remoteAddPushToServer`, `Device.getClientIdPushV2`, and local Neolink
`crates/core/src/bc_protocol/pushinfo.rs` / `src/common/pushnoti.rs`.
No proprietary decompiled source or app binary is included in this branch.

Next: verify transport recovery and sustained delivery with the established
v2 subscription. Offline checks must not be reported as physical-camera validation.
# Push SDK/CLI Physical Retest

The probes below are historical research notes. Current commands are in
`pyneolink.cli`, the SDK API is `PushClient`, and the single example is
`examples/push.py`. See README's Cloud Push section for initialization and the
full current command cycle. Do not run the retired `examples/push_*_probe.py`
paths. Existing `.tmp/push` credentials are reused; do not delete them.

State now lives in `~/pyneolink/{camera}.db`. Install updated core dependencies
and the optional receiver extra into the same environment used for testing:

```cmd
.tmp\push-venv\Scripts\python.exe -m pip install -e ".[push]"
```

Stop old receivers. Initialize/import once using the existing dedicated PC
identity; this is not a request to create another subscription:

```cmd
.tmp\push-venv\Scripts\python.exe -u pyneolink\cli.py --camera "Home-Back" push init --legacy-state-dir .tmp\push
```

Use README's subscribe/status/unsubscribe cycle. With the subscription active
and watch stopped, trigger an event, wait for cloud delivery, then run:

```cmd
.tmp\push-venv\Scripts\python.exe -u pyneolink\cli.py --camera "Home-Back" push read --sync-timeout 30 --idle-timeout 5 --json
.tmp\push-venv\Scripts\python.exe -u pyneolink\cli.py --camera "Home-Back" push read --no-sync --json
```

The first command returns unread backlog without a camera connection. The
second should return `[]` if the first batch exhausted the inbox and no later
events arrived during synchronization. Read is bounded, not a guarantee that
FCM had nothing left to deliver. Repeat online read after triggering a new event.
Unknown server timestamps use receipt time and may appear on the second call.
Test `--limit 1` with multiple unread events, then read the remainder. Restart
the process and confirm consumed events do not return. Stop read before watch:

```cmd
.tmp\push-venv\Scripts\python.exe -u pyneolink\cli.py --camera "Home-Back" push watch --duration 1800 --json
```

The bundled Firebase profile removes the need for Android resources. To test
another owner-approved camera, use its exact config alias for init, subscribe,
status and watch. Omit `--legacy-state-dir` for a new camera. Its database and
receiver identity must be separate; never copy `receiver.json` or another
camera's database. Test cameras sequentially first, then run separate watch
processes if simultaneous delivery is to be checked. Watch/read still do not
connect to the camera. A shared public Firebase profile does not imply a shared
FCM token or Reolink listener.

Watch yields new events; its consumed events must not return in a later read.
Skipped backlog is retained unread in SQLite. A second receiver for the same
database must refuse to run. Do not run old JSON-based probes simultaneously.
After forced termination, a database lease can require up to 120s to expire.

Retest: initialize config once, subscribe, status, read, watch (trigger a fresh event),
Ctrl+C, unsubscribe, status, subscribe, watch again. Repeat subscribe while
active to verify it only reads status. Check that normal output is concise,
`--json` is structured, `--debug` has no credentials, and Ctrl+C releases locks.
Then test a PC network interruption: watch should wait 30/60/120/240/300 seconds
between repeated transient failures, without creating a camera session or
discarding its original skip-previous cutoff. Watch defaults to unlimited;
use `--duration 1800` for a bounded physical test. Do not alter camera settings.

# Subscribe and Watch Separately (2026-11-05, Research History)

## Subscription Results and Timeout Recovery

An ordinary `unsubscribe --confirm` need not have used `--verify-via-camera`:
the next confirmed subscribe performs 526 itself. If that authoritative response
reports absence and a removal attempt is recorded, it marks that removal verified
and proceeds to one 524 in the same invocation. It keeps the previous HTTP result
as evidence; HTTP acceptance alone still does not establish absence. Unknown
526 results do not unlock creation. A timed-out creation without a pending
removal remains protected from automatic retries.

Confirmed `subscribe` now checks an existing client with 526 before considering
524. `already-subscribed` means camera-verified, no create request sent, and local
state reconciled to `accepted`. A second identical invocation is safe and checks
again, without creating another listener. Plain `status` remains read-only.

An unavailable/invalid state check returns `verification-unavailable`, exit 1,
without sending 524. If an old attempt is unconfirmed and 526 reports absence,
the command reports `not-subscribed` and requires review; it does not blindly
retry the old mutation. A verified removal or never-attempted listener can be
created following a successful absent-state check.

524/200 reports `subscription-accepted` (delivery is not yet proven). If creation
times out, one 526 check follows: `subscribed-after-timeout` is success and updates
local state; otherwise `creation-unconfirmed` exits 1 and preserves uncertainty.
There are no automatic 524 retries. These checks use brief camera connections;
the separate watch/read path is unchanged and never connects to the camera.

For the currently active physical subscription, run this twice, sequentially:

```cmd
.venv\Scripts\python.exe -u examples\push_probe.py subscribe --confirm
```

Both should report `already-subscribed`, `subscription_verified=true`, and
`create_request_sent=false`. Stop the receiver first because identity files use
an exclusive lock. Do not disable the camera or its network to induce failures;
timeout and rejected-response cases are covered by offline fake-camera tests.

Physical unsubscribe succeeded: HTTP 200 followed by two independent camera
526/200 checks reporting `unsubscribed`, with zero channels. A different
User-Agent was used for that successful request; this alone does not prove the
cause of the earlier 403 responses.

Resubscribe using the existing PC identity (no new 522/FCM identity):

```cmd
.venv\Scripts\python.exe -u examples\push_probe.py subscribe --confirm
```

The saved removal must be camera-verified. The one-shot 524 operation consumes
that verified-removal state before sending and records uncertainty durably.
An interrupted or rejected creation is not retried automatically. The last
removal is retained as `last_unsubscribe`; credentials are not deleted.

Verify registration independently:

```cmd
.venv\Scripts\python.exe -u examples\push_probe.py status --via-camera
```

Watch for 30 minutes with the optional FCM environment:

```cmd
.tmp\push-venv\Scripts\python.exe -u examples\push_probe.py watch --duration 1800
```

`read` is an alias of `watch`. Neither opens a camera session, loads camera
configuration, creates a Reolink listener, nor creates new FCM credentials.
Existing receiver credentials are required; Google check-in and the FCM receive
connection are still necessary. Stopping watch does not unsubscribe.

`skip_previous=True` is the default, exposed as `--skip-previous` /
`--no-skip-previous`. The cutoff is UTC wall-clock time at watch invocation,
before network setup, not the first received event or the last reconnect.
Events with cloud server time strictly before that cutoff are acknowledged and
recorded for duplicate suppression, but not delivered as `event` output.
ISO-8601 (including the older suffix-less UTC cloud format) and epoch seconds /
milliseconds are supported. Device time is not used because camera clocks can
drift. Keep the PC clock synchronized. Missing or invalid server time is delivered
with `age_filter=unknown-server-time`, rather than silently losing the event.

To include previously queued events still available from FCM:

```cmd
.tmp\push-venv\Scripts\python.exe -u examples\push_probe.py watch --duration 1800 --no-skip-previous
```

This does not replay messages already acknowledged/deduplicated, including those
skipped during an earlier watch. It is not a server history API.
The lower-level FCM probe also defaults to skipping previous messages; use its
`--no-skip-previous` option for backlog diagnostics. Resubscription and the new
watch CLI still need physical validation; offline tests are not delivery proof.

# Controlled Unsubscribe Retest (2026-11-05)

Physical status via command 526 returned 200, `subscribed`, and one channel
for the dedicated PC client. This verifies status, not deletion.

Stop the FCM receiver before running this one-line command:

```cmd
.venv\Scripts\python.exe -u examples\push_probe.py unsubscribe --confirm --retry-rejected --verify-via-camera
```

The probe sends one HTTPS POST to the Android application's deletion endpoint,
scoped to the saved PC client ID and approved Back camera UID. It identifies
itself as `PyNeolinkPushProbe/1.0`; it does not impersonate the Android client.
This User-Agent change is experimental, not a confirmed fix for Cloudflare 1010.
No redirects, automatic retries, token deletion, or global push changes occur.

`--retry-rejected` permits a deliberate retry after a recorded, known HTTP
rejection. It does not clear state or repeat an accepted/uncertain operation.
The attempt count is retained. `--verify-via-camera` makes one brief read-only
526 camera connection after the POST, including after a transport failure.
Only a valid response with the target absent sets `removal_verified=true`.
A still-subscribed or unknown result exits unsuccessfully; an immediate negative
result can require a later read-only status check because cloud propagation time
is unknown. Credentials remain intact regardless of the outcome.

Receiving push still requires no camera session. The extra connection above is
only an explicitly requested verification step. Successful deletion and the
subsequent resubscription lifecycle were pending at this step. See the later
results and resubscription handling above; do not delete state files to bypass guards.
