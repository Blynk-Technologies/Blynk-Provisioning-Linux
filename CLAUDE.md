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

## Workflow preferences carried over from the parent project's sessions

These were explicit corrections from the user in blynk-edge-agent's own
Claude Code sessions - apply them here too unless told otherwise:

- Never include `Co-Authored-By: Claude` trailers in commits.
- Confirm before pushing to master/main, and before any version-number
  change - even a small follow-up right after a release. Ask first, every
  time, rather than assuming a prior approval carries forward.
- Don't commit/push just because a fix looks done - the user treats
  "tested on real hardware" as the bar, not "compiles" or "looks right."
