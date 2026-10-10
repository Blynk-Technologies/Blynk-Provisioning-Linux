# blynk-provisioning

Get a device's Blynk credentials via BLE pairing with the Blynk app, so a
plain Python script can talk to Blynk directly - no need for the full
[blynk-edge-agent](https://github.com/anthony-blynk/blynk-edge-agent) stack.

**Scope, deliberately narrow:** this library does exactly one thing - get
your device paired with the Blynk app and hand back a real, working
identity (server + auth token). It doesn't wrap the Blynk MQTT device
protocol itself (topics, datastreams, OTA, any of that) - `paho-mqtt`
already does MQTT well, and Blynk's own topic conventions
(`ds/<name>`, `get/ds`, `downlink/ds/<name>`) are simple enough to use
directly. If you want the fuller device SDK experience, see Blynk's
Edgent libraries for Arduino/ESP-IDF, or the
[edge-agent](https://github.com/anthony-blynk/blynk-edge-agent) project,
which do considerably more than this.

**One identity per template, per physical machine** - not one per script,
and not one overall. Blynk's own device model is one real device = one
real auth token = one connection; this library doesn't try to paper over
that. Two scripts using the *same* identity at the same time will
disconnect each other. A machine provisioning more than one template gets
a separate identity for each (see "Multiple templates" below) - that's
normal, not a workaround. If you need several logical devices represented
under a *single* template from one machine, that's what the edge-agent's
own MQTT Gateway feature is for - it doesn't need BLE pairing per device
at all.

## Install

```bash
pip install --break-system-packages blynk-provisioning
```

(not yet published to PyPI - for now, install from this directory: `pip install --break-system-packages .`)

Needs a real BlueZ-capable Linux host (confirmed on Raspberry Pi) - reaches
BlueZ and NetworkManager over D-Bus, same as the edge-agent's own BLE
provisioning. Not for Windows/macOS.

## Quick test - Subscribe and print datastream updates

Paste and run as-is (swap in your own template ID for TMPLxxxxxxxx first):

```bash
python3 -c "
import blynk_provisioning
creds = blynk_provisioning.get_credentials(template_id='TMPLxxxxxxxx')
client = creds.get_mqtt_client()
client.on_message = lambda c, u, m: print(f'{m.topic}: {m.payload.decode()}')
client.subscribe('downlink/#')
client.loop_forever()
"
```

## One-off publish

```bash
python3 -c "
import paho.mqtt.publish as publish
import blynk_provisioning
creds = blynk_provisioning.get_credentials(template_id='TMPLxxxxxxxx')
publish.single('ds/Test', payload='42', **creds.connection_kwargs())
"
```

The first run for a given template ID blocks until you've paired via the
Blynk app (BLE advertising starts, same flow as pairing a new edge-agent
device). Every run after that returns instantly - credentials are cached in `~/.blynk/credentials.env`.

`get_mqtt_client()` connects for you but doesn't start the network loop - call
`client.loop_forever()` to block and process messages, or
`client.loop_start()` if your script needs to do other things
concurrently.

## Firmware version

Blynk shows a firmware version for each device - `unknown` unless you pass
your script's own version:

```python
creds = blynk_provisioning.get_credentials(template_id="TMPLxxxxxxxx", fw_version="1.0.3")
```

The build date shown alongside it is the script file's last-modified time.

## Debug output

To see every message exchanged with the Blynk app during pairing, plus the
MQTT checkin, pass `debug=True` to `get_credentials()` or set `BLYNK_DEBUG=1`:

```bash
BLYNK_DEBUG=1 python3 my_script.py
sudo BLYNK_DEBUG=1 python3 my_script.py   # not "BLYNK_DEBUG=1 sudo ..." - sudo drops it
```

The auth token is shown as its last 4 characters; WiFi password and SIM PIN
are masked. Only this library's own log is enabled - paho and D-Bus stay quiet.

## Multiple templates

One identity per template, not one per script (see above) - but a machine
can legitimately need more than one template's identity (e.g. two
different kinds of sensor script running on the same Pi). Calling
`get_credentials()` with a different `template_id` provisions and stores a
separate identity alongside any already in the same file - they're keyed
by template internally, so they don't overwrite each other:

```python
temp_creds = blynk_provisioning.get_credentials(template_id="TMPL_temperature")
door_creds = blynk_provisioning.get_credentials(template_id="TMPL_doorsensor")
```

Both get their own BLE pairing ceremony the first time, and both are
cached in `~/.blynk/credentials.env` from then on.

## Custom vendor prefix

Defaults to advertising as `"Blynk Device-XXXX"` over BLE. Enterprise/
white-labeled Blynk apps are typically configured to only discover devices
broadcasting their own vendor prefix, so pass yours if that applies:

```python
creds = blynk_provisioning.get_credentials(
    template_id="TMPLxxxxxxxx",
    vendor_prefix="Acme",
)
```

## Sharing identity with a co-located edge-agent

If this script runs on the same machine as a full edge-agent install,
point at its own credentials file instead of provisioning a separate
identity:

```python
creds = blynk_provisioning.get_credentials(
    template_id="TMPLxxxxxxxx",
    path="/opt/blynk/blynk.env",
)
```

## Re-pairing (device deleted on the server)

Deleting the device in Blynk invalidates its token, but the local copy
stays. `get_mqtt_client()` then raises `CredentialsRejectedError` saying so.
To pair again, replacing just that template's stored identity:

```python
creds = blynk_provisioning.get_credentials(template_id="TMPLxxxxxxxx", reprovision=True)
```

## Status

Early (`0.1.0`) - the BLE/NetworkManager/ModemManager engine underneath is
adapted from blynk-edge-agent's own `agent/ble_provisioning.py` (real,
hardware-tested code), same as every other hardware-facing thing in the
parent project.
