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
import sys
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


class CredentialsRejectedError(ProvisioningError):
    """Raised by get_mqtt_client() when the Blynk server refuses the stored
    auth token - in practice almost always because the device was deleted
    (or its token regenerated) on the server, leaving a dead token cached
    locally. Not handled automatically: re-pairing is the caller's call,
    via get_credentials(..., reprovision=True)."""


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


def _default_credentials_path() -> Path:
    """~/.blynk/credentials.env - but under `sudo`, the *invoking* user's
    home, not root's. Confirmed on real hardware (Pi OS, `sudo python3`):
    sudo resets HOME, so Path.home() is /root and a sudo pairing run saved
    credentials where the user's later non-sudo runs never look. Pairing
    usually needs sudo (NetworkManager/polkit), so "pair once with sudo,
    then run normally" has to land in the same file. A process genuinely
    running as root (e.g. a systemd unit) has no SUDO_USER and keeps
    root's home."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".blynk" / "credentials.env"
        except (ImportError, KeyError):
            pass
    return DEFAULT_CREDENTIALS_PATH


def _enable_debug_logging() -> None:
    """Opt-in console output for this package's own logger only (BLE
    messages to/from the phone, NetworkManager steps) - never the root
    logger, so paho/dbus-next stay quiet and a caller's own logging setup
    is left alone. Idempotent: repeated get_credentials(debug=True) calls
    don't stack duplicate handlers."""
    pkg_logger = logging.getLogger("blynk_provisioning")
    pkg_logger.setLevel(logging.DEBUG)
    if not any(getattr(h, "_blynk_debug", False) for h in pkg_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[blynk_provisioning] %(message)s"))
        handler._blynk_debug = True
        pkg_logger.addHandler(handler)


def _build_date() -> str:
    """info/mcu's "build" - on Edgent firmware that's compile time, so it
    only changes when the firmware does. Nearest script equivalent: the
    running script's last-modified time. Falls back to now for
    `python3 -c` / interactive use, where there's no script file."""
    script =sys.argv[0] if sys.argv else ""
    when = os.path.getmtime(script) if script and os.path.isfile(script) else time.time()
    return time.strftime("%b %d %Y %H:%M:%S", time.localtime(when))


def _print_identity(prefix: str, template_id: str, server: str, auth_token: str, store_path: Path) -> None:
    """A plain print(), not logger.info(), for the same reason as the
    "advertising" message in _ble.py - this library never configures
    logging, so logger output is invisible unless the caller sets it up,
    and which identity a script ended up with is worth always seeing.
    Only the token's last 4 chars - enough to tell identities apart
    without leaking a usable credential into terminal scrollback/logs."""
    print(f"{prefix}: template {template_id}, server {server}, "
          f"auth token ...{auth_token[-4:]} ({store_path})")


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
        connack = {}
        client.on_connect = lambda c, u, flags, reason_code, props: connack.update(rc=reason_code)
        logger.debug(f"MQTT connecting to {self.server}:8883 (mqttv5)")
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
            "build": _build_date(),
            "type": self.template_id,
            "rxbuff": 1024,
        }
        info = client.publish(TOPIC_INFO, json.dumps(payload), qos=1, retain=True)
        deadline = time.monotonic() + 5
        try:
            while not info.is_published() and time.monotonic() < deadline:
                client.loop(timeout=1)
                if "rc" in connack and connack["rc"].is_failure:
                    break
        except ValueError:
            # Confirmed on real hardware (device deleted on the server):
            # Blynk's broker answers a rejected mqttv5 CONNECT with a
            # 3.1.1-style return code, which isn't a valid v5 reason code,
            # so paho crashes parsing it ("Expected exactly one name,
            # found []") instead of reporting a failed connect.
            if "rc" in connack:
                raise
            connack["rc"] = None
        client.on_connect = None
        if "rc" in connack and (connack["rc"] is None or connack["rc"].is_failure):
            reason = f" ({connack['rc']})" if connack["rc"] is not None else ""
            raise CredentialsRejectedError(
                f"{self.server} rejected auth token ...{self.auth_token[-4:]} for template "
                f"{self.template_id}{reason} - most likely the device was deleted on the server. "
                f"Re-pair with get_credentials({self.template_id!r}, reprovision=True)."
            ) from None
        if info.is_published():
            logger.debug(f"MQTT device checkin sent: {TOPIC_INFO} {payload}")
        else:
            logger.warning(f"MQTT device checkin ({TOPIC_INFO}) not confirmed within 5s - "
                           "the Blynk app may not show this device online")
        return client


def get_credentials(template_id: str, server: Optional[str] = None,
                     path: Optional[os.PathLike] = None,
                     vendor_prefix: str = "Blynk",
                     fw_version: str = "unknown",
                     debug: bool = False,
                     reprovision: bool = False) -> Credentials:
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

    debug=True (or BLYNK_DEBUG=1 in the environment) prints this library's
    debug log to the console - every BLE message to/from the phone, with
    the auth token, WiFi password and SIM PIN masked.

    reprovision=True ignores any stored identity for this template and
    pairs again via BLE, replacing just this template's entry - e.g. after
    get_mqtt_client() raised CredentialsRejectedError because the device
    was deleted on the server.
    """
    if debug or os.environ.get("BLYNK_DEBUG", "").lower() in ("1", "true", "yes"):
        _enable_debug_logging()

    store_path = Path(path) if path else _default_credentials_path()
    server_key, token_key, _ = _template_keys(template_id)

    if not reprovision and store_path.exists():
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
            _print_identity("Using stored credentials", template_id, stored_server, stored_token, store_path)
            return Credentials(server=stored_server, auth_token=stored_token, template_id=template_id,
                                fw_version=fw_version)

    if reprovision:
        print(f"Re-pairing template {template_id!r} (ignoring stored credentials at {store_path})...")
    else:
        print(f"No stored credentials for template {template_id!r} at {store_path} - starting BLE pairing...")
    identity = _Identity(
        vendor_prefix=vendor_prefix,
        template_id=template_id,
        auth_token=None,
        server=server,
        path=store_path,
    )
    try:
        ok = _ble.provision(identity, fw_version=fw_version)
    except KeyboardInterrupt:
        # Still propagate Ctrl-C (a library shouldn't swallow it, and it
        # must not become a ProvisioningError a caller might retry on) -
        # just without asyncio's internal CancelledError chain attached,
        # which otherwise buries it under ~30 lines of traceback. BlueZ
        # drops the advert/agent itself once _ble.run()'s finally closes
        # the D-Bus connection, so nothing is left registered.
        print("Pairing cancelled.")
        raise KeyboardInterrupt from None
    if not ok or not identity.auth_token or not identity.server:
        raise ProvisioningError("BLE provisioning did not complete - abandoned, timed out, or failed")
    _print_identity("Provisioned", template_id, identity.server, identity.auth_token, store_path)
    return Credentials(server=identity.server, auth_token=identity.auth_token, template_id=template_id,
                        fw_version=fw_version)
