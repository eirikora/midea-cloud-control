# Midea Cloud Control

Home Assistant integration for Midea-protocol air conditioners that **cannot be
controlled locally** because Midea retired the token service.

Works with **Ariston Clima**, **NetHome Plus**, and **Midea Air** accounts.

## Why this exists

Every existing Home Assistant integration for these units (`midea_ac_lan`,
`midea-ac-py`, and friends) controls the appliance **locally** over TCP 6444.
That requires a per-device `token` and `key`, which are only obtainable from
Midea's cloud endpoint:

```
POST /v1/iot/secure/getToken  →  {"msg":"system error","errorCode":9999}
```

**That endpoint has been switched off.** Appliances onboarded before the
shutdown keep working, because their token was already stored. Anything
onboarded afterwards cannot be added to Home Assistant at all — the vendor app
still works, but local control is permanently unavailable.

This integration takes a different route. The legacy endpoint

```
POST /v1/appliance/transparent/send
```

is still live. It relays a raw Midea protocol frame to the appliance *through
the cloud*, and needs **no token and no key** — only a normal app login. That is
the same path the vendor app uses, which is why the app keeps working when
Home Assistant cannot connect.

## Trade-offs

This is cloud control. Be clear-eyed about what that means:

- Requires internet access and Midea's servers being up.
- State is **polled** (105 s default, configurable to 60–120 s), not pushed.
- ~1 s round trip per command, versus near-instant local control.
- Midea is retiring these APIs progressively; `transparent/send` could follow
  `getToken`.

If you can run [ESPHome's Midea component](https://esphome.io/components/climate/midea.html)
on a replacement Wi-Fi dongle, **do that instead** — it is fully local, faster,
and depends on nothing external. This integration is for people who can't or
won't swap hardware.

## Installation

**HACS** — add this repository as a custom repository (type: Integration),
download, restart Home Assistant.

**Manual** — copy `custom_components/midea_cloud_control` into your
`/config/custom_components/` directory and restart.

Then: **Settings → Devices & Services → Add Integration → Midea Cloud Control**.
Enter the email and password of the app your appliances are registered in, pick
the matching app from the dropdown, then select one or more appliances. One
account entry stores the credentials once and creates one climate entity per
selected appliance.

> The app selector matters. Picking the wrong one fails with a vague error,
> because the clouds have separate user databases.

## Features

| | |
|---|---|
| HVAC modes | off, auto, cool, dry, heat, fan only |
| Target temperature | 16–30 °C, 0.5 °C steps |
| Current temperature | indoor sensor |
| Fan | silent, low, medium, high, max, auto |
| Swing | off, horizontal, vertical, both |
| Attributes | outdoor temperature, eco, turbo, sleep, filter alert, error code |

## Command-line probe

Useful for confirming your account and appliance work before touching Home
Assistant:

```bash
python tools/probe.py list  --user EMAIL --password PW
python tools/probe.py state --user EMAIL --password PW --id APPLIANCE_ID
python tools/probe.py set   --user EMAIL --password PW --id APPLIANCE_ID --temp 22
```

## Protocol notes

For anyone extending this. The frame sent in the `order` parameter is:

1. A standard Midea `0xAA` protocol frame (built here with `msmart-ng`).
2. Wrapped in a **40-byte `0x5A5A` packet header**, with byte 4 set to the
   total packet length, followed by 16 trailing zero bytes.
3. Serialised as comma-separated **signed** integers (values ≥ 128 become
   negative).
4. AES-ECB encrypted with a key derived from the login access token:
   `aes_ecb_decrypt(access_token, md5(app_key)[:16])`.
5. Hex-encoded.

Replies come back the same way. The decrypted reply is zero-padded past the end
of the frame, so **truncate to the length declared in byte 1** before parsing —
otherwise the padding is read as payload and fails CRC.

## Shared-session architecture

- `CloudSessionRegistry` owns one `MideaCloudClient` per normalized account and
  cloud name. Legacy single-device entries also reuse this client.
- Opening **Configure** borrows that same client through a reference-counted
  lease. A disabled entry therefore reuses any session held by another entry
  for the account instead of creating an independent login.
- A login lock collapses concurrent startup attempts into one real login.
- Session errors 3106 and 3144 replace the complete `MideaAirCloud` object
  before a bounded reauthentication attempt. This avoids carrying an expired
  `sessionId` into login.
- Requests share a single-slot semaphore and a minimum start interval, matching
  midea-local's serialized account lock. A round-robin scheduler gives every
  appliance a stable phase within the common interval instead of sending a
  periodic burst. Migrated single-appliance entries derive a deterministic
  phase from the appliance ID.
- Error 9999 and empty/network responses use bounded retries with exponential
  backoff and jitter. Repeated login failures enter a cooldown.
- NetHome Plus error 3176 (missing asynchronous reply) is treated as transient
  for idempotent status reads, with two attempts separated by 3–5 seconds and
  without reauthentication. Short transient outages retain the last known
  state for up to two polls. Commands are never
  blindly replayed after a missing acknowledgement; the requested fields are
  verified through status reads and an unverified result remains an error.
- Home Assistant diagnostics expose non-sensitive request counters, response
  timing, last successful update, last known online status, and protocol errors.
- Reauthentication updates every entry sharing the same account while retaining
  appliance-based entity unique IDs.
- Config entry version 3 represents an account containing multiple appliances
  and a fixed account-wide polling interval.
  Climate unique IDs remain the appliance IDs, preserving existing entity IDs
  where Home Assistant's entity registry permits it.

Version-1 entries migrate safely to version 3 as one-appliance account entries.
They share a runtime session immediately, but are not automatically merged
because deleting or merging config entries during startup could lose entity
registry associations. Use **Configure** on one migrated entry to select all
appliances, validate the entity mapping, and only then remove the other disabled
legacy entries.

Version-2 entries using the former 90-second base interval migrate to a fixed
105-second interval. This preserves approximately the same request rate as the
old 90–120-second per-device spread.

The transparent-send transport necessarily uses private `midea-local` members
(`_access_token`, `_make_general_data`, and `_api_request`). The manifest pins
`midea-local==12.2.0`; upgrades require protocol tests and source review.

## Development and tests

This branch is tested with Python 3.13 and Home Assistant 2026.2.3:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check custom_components tests tools
```

Tests use scripted cloud objects only. They do not require credentials, contact
Midea, or send appliance commands. Covered cases include 1/16-device session
sharing, concurrent login, 3106/3144/3176/9999 handling, stale-state recovery,
reauthentication, reference counting, corrupt replies, command verification,
and stable appliance-based entity IDs.

See [the technical analysis](docs/ANALYSIS.md) and the
[don-aurora deployment and rollback runbook](docs/DEPLOYMENT_DON_AURORA.md).

## Credits

Builds on [`midea-local`](https://github.com/wuwentao/midea-local) for cloud
authentication and [`msmart-ng`](https://github.com/mill1000/midea-msmart) for
protocol frame encoding and decoding.

## License

MIT
