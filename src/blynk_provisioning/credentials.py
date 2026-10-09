"""
Public entry point: get_credentials() returns this machine's one Blynk
identity for a given template, provisioning it via BLE (pairing with the
Blynk app) if it isn't already stored locally - one identity per physical
machine per template, not one per script. Two scripts using the same
identity at the same time will disconnect each other (same auth token,
same username, to Blynk's own broker) - that's expected, not a bug this
library tries to work around; if you need several logical devices running
from one machine at once, use the full edge-agent's own MQTT Gateway
feature instead, which doesn't need BLE pairing per device at all.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values
import paho.mqtt.client as mqtt

from . import _ble

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = Path.home() / ".blynk" / "credentials.env"

# Same topic/payload shape as the edge-agent's own agent.py
# (_publish_device_info) - this is how the Blynk app/console actually
# recognizes a device as online and checks in, distinct from the BLE
# "connected" status which only confirms the pairing exchange itself.
# Confirmed on real hardware: without this, the Blynk app's "finishing
# up..." step after BLE pairing just times out, since nothing ever tells
# Blynk Cloud the device has really connected over MQTT.
TOPIC_INFO = "info/mcu"


class ProvisioningError(Exception):
    """Raised when get_credentials() can't produce a usable identity -
    BLE provisioning was abandoned, timed out, or failed outright."""


def _template_keys(template_id: str) -> tuple:
    """Per-template key names, so one credentials file can hold several
    templates' identities side by side without overwriting each other -
    each physical machine can have one real identity per template, same
    as the docstring above says, just potentially more than one template."""
    return (
        f"BLYNK_SERVER_{template_id}",
        f"BLYNK_AUTH_TOKEN_{template_id}",
        f"BLYNK_TEMPLATE_ID_{template_id}",
    )


def _chown_to_invoking_user_if_root(path: Path) -> None:
    """BLE/NetworkManager D-Bus calls need root (see README), so a first
    provisioning run is normally done via `sudo` - without this, the saved
    credentials file (and the ~/.blynk directory, if this run created it)
    would end up root-owned and unreadable by whichever user runs the rest
    of the script afterward, every single time, for every user of this
    library. `sudo` sets SUDO_UID`/`SUDO_GID` for exactly this case -
    chown back to the real invoking user so a later, non-sudo run can
    actually read what this one just wrote."""
    if os.geteuid() != 0:
        return
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid is None or sudo_gid is None:
        return
    try:
        os.chown(path, int(sudo_uid), int(sudo_gid))
    except OSError as e:
        logger.warning(f"Could not chown {path} back to the invoking user: {e}")


@dataclass
class _Identity:
    """Internal - shaped to match exactly what _ble.ProvisioningSession
    expects (vendor_prefix/template_id/auth_token/server/save()), same
    role BlynkConfig plays in the edge-agent itself."""
    vendor_prefix: str
    template_id: str
    auth_token: Optional[str]
    server: Optional[str]
    path: Path

    def save(self) -> None:
        created_parent = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Merge into whatever's already there instead of overwriting the
        # whole file - other templates' entries (and a co-located
        # edge-agent's own unsuffixed BLYNK_* keys, if this path is shared
        # with one) need to survive this save untouched.
        existing = dict(dotenv_values(self.path)) if self.path.exists() else {}
        server_key, token_key, template_key = _template_keys(self.template_id)
        existing[server_key] = self.server or ""
        existing[token_key] = self.auth_token or ""
        existing[template_key] = self.template_id

        new_path = self.path.with_suffix(".new")
        new_path.write_text("".join(f"{key}={value}\n" for key, value in existing.items()))
        os.chmod(new_path, 0o600)
        _chown_to_invoking_user_if_root(new_path)
        new_path.replace(self.path)
        if created_parent:
            _chown_to_invoking_user_if_root(self.path.parent)


@dataclass
class Credentials:
    """What get_credentials() hands back - just the resolved identity.
    Deliberately has no publish()/subscribe() of its own: this library's
    job stops at getting you connected, not at deciding how you talk to
    Blynk once you are."""
    server: str
    auth_token: str
    template_id: str
    fw_version: str = "unknown"

    def connection_kwargs(self, qos: int = 1) -> dict:
        """Ready to spread into paho.mqtt.publish.single()/multiple() for a
        one-shot publish - confirmed on real hardware that a quick connect-
        publish-disconnect is fine on mqttv311, but this still defaults to
        mqttv5 for consistency with get_mqtt_client() below (which does
        need mqttv5 - a sustained mqttv311 connection was confirmed
        unstable, repeatedly disconnecting/reconnecting, on real hardware
        against Blynk's own broker)."""
        return {
            "hostname": self.server,
            "port": 8883,
            "auth": {"username": "device", "password": self.auth_token},
            "tls": {},
            "protocol": mqtt.MQTTv5,
            "qos": qos,
        }

    def get_mqtt_client(self) -> mqtt.Client:
        """Returns a connected paho Client, ready for ongoing use (subscribe,
        ongoing publishes) - mqttv5 only, confirmed necessary on real
        hardware for anything sustained. Does NOT start the network loop
        for you - call client.loop_forever() to block and process messages,
        or client.loop_start() to run it in a background thread instead;
        which one you want depends on your own script's structure, not
        something this library should decide for you."""
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client.username_pw_set("device", self.auth_token)
        client.tls_set()
        client.connect(self.server, 8883)

        # Mandatory device checkin, not a general-purpose publish - see
        # TOPIC_INFO above. Pumped manually via client.loop() rather than
        # loop_start()/loop_stop() - confirmed on real hardware that
        # loop_stop() leaves paho's internal _thread_terminate flag set to
        # True and loop_forever() never resets it, so a caller's own
        # client.loop_forever() right after this would see that stale flag,
        # find nothing left queued, and silently self-terminate within a
        # few seconds - no error, no exception, just a dead connection.
        # Calling client.loop() directly here never touches that flag at
        # all, so the client comes back genuinely untouched for whichever
        # of loop_forever()/loop_start() the caller uses next.
        payload = {
            "tmpl": self.template_id,
            "ver": self.fw_version,
            "build": time.strftime("%b %d %Y %H:%M:%S"),
            "type": self.template_id,
            "rxbuff": 1024,
        }
        info = client.publish(TOPIC_INFO, json.dumps(payload), qos=1, retain=True)
        deadline = time.monotonic() + 5
        while not info.is_published() and time.monotonic() < deadline:
            client.loop(timeout=1)
        return client


def get_credentials(template_id: str, server: Optional[str] = None,
                     path: Optional[os.PathLike] = None,
                     vendor_prefix: str = "Blynk",
                     fw_version: str = "unknown") -> Credentials:
    """Returns this machine's Blynk identity for the given template -
    reusing it from local storage if already provisioned (fast, no BLE
    involved), or running BLE provisioning (pairing with the Blynk app) if
    not. Blocks for the whole provisioning ceremony the first time; returns
    immediately on every call after that.

    Calling this with a different template_id provisions and stores a
    separate identity alongside any others already in the same file - they
    don't overwrite each other, so a machine can hold one real identity per
    template, each independently provisioned.

    path defaults to ~/.blynk/credentials.env - pass an explicit path (e.g.
    "/opt/blynk/blynk.env") to deliberately share identity with something
    else already provisioned at that location, such as a co-located
    edge-agent. server is normally left unset: the Blynk app supplies it
    during BLE pairing (supports regional/self-hosted servers, not just
    the public default) - only pass it to force a specific server up front.
    """
    store_path = Path(path) if path else DEFAULT_CREDENTIALS_PATH
    server_key, token_key, _ = _template_keys(template_id)

    if store_path.exists():
        values = dotenv_values(store_path)
        stored_token = values.get(token_key)
        stored_server = values.get(server_key)
        if not stored_token or not stored_server:
            # Fall back to the older unsuffixed keys - either a file
            # written before multi-template support, or a co-located
            # edge-agent's own blynk.env, which only ever manages one
            # identity and always uses these exact unsuffixed names.
            if values.get("BLYNK_TEMPLATE_ID") == template_id:
                stored_token = values.get("BLYNK_AUTH_TOKEN")
                stored_server = values.get("BLYNK_SERVER")
        if stored_token and stored_server:
            return Credentials(server=stored_server, auth_token=stored_token, template_id=template_id,
                                fw_version=fw_version)

    print(f"No stored credentials for template {template_id!r} at {store_path} - starting BLE pairing...")
    identity = _Identity(
        vendor_prefix=vendor_prefix,
        template_id=template_id,
        auth_token=None,
        server=server,
        path=store_path,
    )
    ok = _ble.provision(identity, fw_version=fw_version)
    if not ok or not identity.auth_token or not identity.server:
        raise ProvisioningError("BLE provisioning did not complete - abandoned, timed out, or failed")
    return Credentials(server=identity.server, auth_token=identity.auth_token, template_id=template_id,
                        fw_version=fw_version)
