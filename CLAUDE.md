# blynk-provisioning - project notes for Claude

This repo was split out of [blynk-edge-agent](https://github.com/anthony-blynk/blynk-edge-agent)
on 2026-10-09. `src/blynk_provisioning/_ble.py` is an adapted copy of that
project's `agent/ble_provisioning.py` - same BLE/D-Bus/NetworkManager/
ModemManager engine, real-hardware-tested there first. Worth knowing the
sibling project exists if a fix or question here turns out to be about
shared BLE/NetworkManager mechanics rather than this package's own API.

## Scope - read before adding anything

This library does exactly one thing: get a device's Blynk credentials via
BLE pairing with the Blynk app, then hand back a working identity (server +
auth token). It deliberately does **not** wrap the Blynk MQTT device
protocol itself (topics, datastreams, OTA) - `paho-mqtt` already does MQTT
well, and Blynk's own topic conventions are simple enough to use directly.
A `blynk_provisioning.publish()`-style helper was explicitly rejected during
design - it would start making this look like it's trying to be an Edgent-
style device SDK, which is out of scope. See the README's own "Scope"
section for the user-facing version of this.

**One identity per template, per physical machine** - not per script, not
one overall. Two scripts sharing the same identity at the same time
disconnect each other (same auth token, same connection - confirmed on real
hardware), and that's correct behavior, not a bug to work around. Multiple
templates on one machine are supported (each gets its own identity, keyed
by template in the same credentials file) - see "Multiple templates" in the
README.

## Testing

No CI, no automated hardware testing yet. Testing so far has been entirely
manual against a real Raspberry Pi 5 (`ant@mypi5-2.local`) over direct
`scp` + SSH - not git-based (the repo didn't exist yet for most of this
testing). `python -m py_compile` / a local `python -m build` + `twine
check` are good cheap sanity checks for syntax/packaging, but nothing
touching BLE/NetworkManager/ModemManager can be verified without real
hardware - don't claim something works there without it actually being
run on a Pi.

## Real-hardware gotchas already found and fixed

All of these were found via actual BLE pairing attempts on a real Pi, not
code review - if touching related code, re-read the relevant commit/code
comment rather than assuming the obvious fix is enough:

1. **Never call `logging.basicConfig()`** in this library (correct,
   library hygiene) - but that means an essential "pairing is possible
   now" message needs an explicit `print()`, not just `logger.info()`, or
   someone running this interactively sees zero output for the whole
   300s timeout.

2. **The Blynk app's "finishing up..." step after BLE pairing needs a
   real MQTT checkin**, not just the BLE "connected" status. The app
   waits for a retained publish to `info/mcu`
   (`{tmpl, ver, build, type, rxbuff}` - same shape as the edge-agent's
   `agent.py`'s `_publish_device_info`) before it'll show the device
   online; otherwise it just times out. `Credentials.get_mqtt_client()`
   sends this automatically right after connecting.

3. **BLE provisioning needs `sudo` on a real Pi** for the NetworkManager
   parts specifically - WiFi scan/connect go through polkit, which only
   auto-authorizes an active local console session, not SSH. BlueZ/GATT
   advertising itself does *not* need root.

4. **Re-provisioning a device already on working WiFi must not touch
   NetworkManager at all.** The original code (copied from the
   edge-agent) unconditionally deleted the existing NM connection and
   recreated it even when the app resubmitted the identical SSID - this
   killed an active SSH session during testing. Fixed via
   `_current_wifi_ssid()` in `_ble.py`: if NM reports the wifi device
   already ACTIVATED on an AP with the requested SSID (and no static-IP
   fields are pending), skip the delete/recreate and report success
   immediately. This check is read-only D-Bus, so it works without
   `sudo` too. **Not yet ported back** to the edge-agent's own
   `agent/ble_provisioning.py`, which still has the same risk - flagged
   in that repo's `TODO.md` if picking this up from there instead.

5. **`sudo`-driven provisioning writes credentials as root**, making them
   unreadable by the normal user afterward - hits every user who needs
   `sudo` for #3. Fixed via `_chown_to_invoking_user_if_root()` using
   `SUDO_UID`/`SUDO_GID` (set by `sudo` itself). Caveat actually hit
   during testing: this only fixes a directory *created in that same
   run* - a `~/.blynk` dir left over root-owned from an earlier `sudo`
   run before this fix existed still needs a manual one-time
   `sudo chown <user>:<user> ~/.blynk`.
   Second half, found later (TestPyPI install, `sudo python3` on Pi OS):
   sudo also resets `HOME`, so `Path.home()` was `/root` and a sudo
   pairing saved to `/root/.blynk/credentials.env` - invisible to the
   user's normal non-sudo runs. Fixed via `_default_credentials_path()`
   (uses `SUDO_USER`'s home when running as root via sudo; an explicit
   `path=` is never redirected).

6. **`paho`'s `loop_stop()` leaves `_thread_terminate = True` forever**,
   and `loop_forever()` never resets it. So the original
   `get_mqtt_client()` (which used `loop_start()`/`loop_stop()` just to
   flush the one `info/mcu` publish) left the returned client in a state
   where a caller's subsequent `client.loop_forever()` would silently
   self-terminate within seconds - no exception, no error, just a dead
   connection. Fixed by pumping `client.loop()` manually instead of ever
   calling `loop_start()`/`loop_stop()` in `get_mqtt_client()`.

7. **MQTTv3.1.1 is fine for one-shot publish but unstable for sustained
   connections** against Blynk's broker (confirmed via real hardware,
   plain `paho` client, not just a bridge-specific thing) - settled
   `get_mqtt_client()`'s default to MQTTv5.

8. **Two separate install locations matter on a Pi**: `sudo pip install`
   and plain `pip install --user` go to different site-packages trees -
   a fix only reinstalled for one of them will silently keep running old
   code under the other. Reinstall (and retest) both if both `sudo` and
   non-`sudo` runs are being exercised.

9. **Blynk's broker rejects a bad/deleted token with a 3.1.1-style
   CONNACK code even on an MQTTv5 connection** - not a valid v5 reason
   code, so paho 2.1 crashes parsing it (`ValueError: Expected exactly
   one name, found []`) instead of reporting a failed connect. Hit when
   the device was deleted in the console while its token was still
   cached locally. `get_mqtt_client()` catches this and raises
   `CredentialsRejectedError` pointing at `get_credentials(...,
   reprovision=True)`. Deliberately *not* auto-re-pairing on rejection -
   a transient server problem would then wipe working credentials and
   leave a headless script stuck advertising. `connection_kwargs()` +
   `publish.single()` still gets the raw paho crash (not wrapped - out
   of scope, see above).

10. **Bond-forgetting deliberately diverges from the edge-agent.** The
    edge-agent forgets *every* bonded device before a session (stale GATT
    cache fix) - fine on a dedicated box, but on a general-purpose machine
    that unpairs the user's keyboard/headphones. `_forget_bonded_devices()`
    here only forgets devices recorded in `~/.blynk/ble_centrals.json`
    (phones that bonded during one of our sessions, by identity address)
    or with BlueZ Icon `phone`. Only *bonded* phones get recorded - an
    unbonded phone reports a rotating resolvable private address
    (confirmed on Pi 5 / BlueZ 5.82, where phones don't bond at all).
    Not yet tested on real hardware: a phone that does bond (old BlueZ,
    e.g. kiwibot's 5.66 with the EATT prompt), or a paired keyboard
    surviving a session.

11. **Python 3.8 (Ubuntu 20.04): construct asyncio objects inside the
    running loop.** On Python <3.10, `asyncio.Event()` binds to whatever
    loop exists at construction - `ProvisioningSession.__init__` creates
    one, so building the session before `asyncio.run()` failed with "got
    Future attached to a different loop" the moment pairing waited.
    `provision()` now constructs the session inside the coroutine. Minimum
    Python is 3.8 (code itself needs 3.7 per `vermin`; pip picks
    python-dotenv 1.0.1 there automatically). Installing from a git
    checkout on 3.8 still fails (build needs setuptools>=77 for the PEP 639
    license field, which needs Python 3.9+) - PyPI wheel installs are fine.
    Deliberately not reverting to the old license-table format: current
    setuptools stops accepting it after 2027-02-18.

12. **NetworkManager <1.24 has no `Device.HwAddress`** (Ubuntu 20.04 has
    1.22) - only the type-specific `Device.Wired`/`Device.Wireless`
    interfaces do. Without the fallback in `_hw_address()`, every device
    failed to read in `_do_ifs` and the app was only offered "eth".

13. **Never publish before the CONNACK** (found 2026-10-17 from a
    downstream BLE sensor bridge on a Pi Zero 2 W, device deleted on the
    server). `get_mqtt_client()` used to publish the `info/mcu` checkin
    straight after CONNECT. Blynk rejects a dead token and closes the
    connection in ~0.3s, so writing the checkin hit the closed socket and
    the connection reset discarded the unread rejection CONNACK - a
    deleted device then looked like "connection lost", and the old fixed
    5s wait treated that as success (the rejection resurfaced later as a
    crash in the caller's paho thread). It's racy, which is why it was
    first misread as Blynk delaying or throttling rejections - a bare
    paho connect with nothing else queued always got the rejection in
    0.3s. Now: phase 1 waits for the CONNACK alone (`CONNACK_TIMEOUT`,
    `TimeoutError` if none, `ConnectionError` immediately if the socket
    closes first - the latter also stops a 99.7% CPU busy-loop seen when
    `loop()` was called on a dead socket); phase 2 publishes the checkin
    only after acceptance. Network errors are deliberately not a
    rejection, so callers never re-pair over a flaky link.

14. **`configure_network=False` must report `eth`, not the real uplink
    type** (2026-10-17, CompuLab IOT-GATE-iMX8 on WiFi): reporting
    `{"name": "wifi", "status": "ready"}` with no `scan` flag still made
    the Blynk app ask for WiFi credentials. `eth` is the only type the
    current app treats as "online, nothing to configure", so the app
    labels the device "Ethernet" even when it isn't - raised with the
    Blynk app team.

15. **App's "already connected" choice: WiFi with no SSID.** The app team's
    fix for #14 (QA Android build, 2026-10-17 - not iOS or production as
    of 2026-10-18) lets the user pick a WiFi interface reported "ready"
    without entering settings; the app then sends `{"if": "wifi"}` with no
    `ssid`. `_connect_wifi` treats that as "keep the current connection"
    (net_fail `not_found` if WiFi isn't actually up). Before, it crashed on
    `ssid=None`. Keep #14's `eth` workaround, and don't add the cellular
    equivalent (cellular `if` has no `status` at all, and connecting
    always rebuilds the connection), until the app change ships on both
    platforms.

## Dynamic templates (1.2.0, 1.3.0) - found on real hardware 2026-10-24

The Blynk Modbus gateway (C:\Blynk\Modbus) pairs with no template built in;
these are the protocol facts that made it work (CompuLab + Android app,
developer account):

- `fw_type: "DYNAMIC"` in the BLE info reply makes the (newer, Compose)
  Android app offer a template picker; `tmpl_id` can be any placeholder.
  Developer accounts only; iOS untested.
- After the pick the app sends the template as **`"templateId"` in the BLE
  `set`** (undocumented). Rejecting unknown set keys = `set_fail` = "device
  configuration failed" in the app - fixed in 1.2.0, stored as
  `BLYNK_ASSIGNED_TEMPLATE_ID_<template_id>`, exposed as
  `Credentials.assigned_template_id` / `effective_template_id`.
- **"Reconfigure device"** (MQTT `downlink/reconfigure`, empty payload):
  the app then **rejects a board reporting a different template** from the
  device ("You've chosen the wrong device" - WrongTemplateId, reconfigure
  only), and its `set` carries **no templateId and a new token**. 1.3.0's
  `get_credentials(..., reprovision=True, keep_template=True)` reports the
  assigned template as tmpl_id/fw_type and keeps it. Without
  keep_template (a deleted device) it re-pairs as DYNAMIC.
- `configure_network=True` on a CompuLab lists every NM device: two
  unplugged Ethernet ports show as two identical "Ethernet" entries in the
  app, plus Wi-Fi and cellular. Listing only Ethernet ports with a cable was
  offered and deferred by the user.
- Test harness: `C:\Blynk\generic-fw\tools\dynamic_test.py` (pairs with
  chosen tmpl_id/fw_type, then holds an MQTT session).

## Releases

No CI. A "Release X.Y.Z" commit bumping `__version__` in
`src/blynk_provisioning/__init__.py`, an annotated tag `vX.Y.Z`, push
master + tag, `python -m build` + `twine check`, then the **user** uploads
to PyPI (`twine upload` with their ~/.pypirc - Claude's auto-mode refuses
PyPI publishing). Then a GitHub release on the tag in the existing style
("**New:** / **Fixed:** / Tested on: / Install:", no assets). 1.3.0 was
released this way 2026-10-24. Asking before any version bump still applies.

## Workflow preferences carried over from the parent project's sessions

These were explicit corrections from the user in blynk-edge-agent's own
Claude Code sessions - apply them here too unless told otherwise:

- Never include `Co-Authored-By: Claude` trailers in commits.
- Confirm before pushing to master/main, and before any version-number
  change - even a small follow-up right after a release. Ask first, every
  time, rather than assuming a prior approval carries forward.
- Don't commit/push just because a fix looks done - the user treats
  "tested on real hardware" as the bar, not "compiles" or "looks right."
