"""
BLE provisioning (Blynk.Inject protocol) - adapted from blynk-edge-agent's
own agent/ble_provisioning.py, which is real, hardware-tested code (BlueZ/
NetworkManager/ModemManager quirks worked out against actual devices). The
only changes here are the three places that file depended on edge-agent-
specific objects (BlynkConfig, ComposeManager, MqttBridge) - replaced with
this library's own minimal _Identity (same shape: vendor_prefix/template_id/
auth_token/server/save()), a plain fw_version string, and an on_provisioned
callback. Every BLE/D-Bus/NetworkManager/ModemManager code path below is
otherwise unchanged from the original - see that file's own comments for the
real-hardware history behind each one.

Implements info/ifs/scan/set/connect/reset/reboot. Real WiFi devices are
reported (and can actually be scanned/connected) via NetworkManager's D-Bus
API. Cellular ("cell") is also implemented via NetworkManager + ModemManager.
"""

import asyncio
import json
import logging
import socket
import struct
import zlib
from typing import Callable, Optional

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.util import Adapter, get_message_bus
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import BaseAgent, AgentCapability
from dbus_next import Variant
from dbus_next.service import dbus_property, method
from dbus_next.constants import PropertyAccess

logger = logging.getLogger(__name__)


def _redact(message: dict) -> dict:
    """Copy of a BLE message safe to log - the app's 'set' carries the auth
    token, WiFi password and SIM PIN in plain text. Token keeps its last 4
    chars (same as get_credentials()'s identity line) so it's still
    recognizable; password/PIN are fully hidden."""
    safe = dict(message)
    if safe.get("blynk"):
        safe["blynk"] = f"...{str(safe['blynk'])[-4:]}"
    for field in ("pass", "pin"):
        if safe.get(field):
            safe[field] = "***"
    return safe


class AutoAcceptPairingAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentCapability.NO_INPUT_NO_OUTPUT)

    @method()
    def RequestAuthorization(self, device: "o"):  # type: ignore
        logger.info(f"BLE pairing agent: RequestAuthorization({device})")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # type: ignore
        logger.info(f"BLE pairing agent: RequestConfirmation({device}, {passkey})")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # type: ignore
        logger.info(f"BLE pairing agent: AuthorizeService({device}, {uuid})")


ADAPTER_PATH = "/org/bluez/hci0"


async def _get_adapter(bus, path: str = ADAPTER_PATH) -> Adapter:
    introspection = await bus.introspect("org.bluez", path)
    proxy = bus.get_proxy_object("org.bluez", path, introspection)
    return Adapter(proxy)


async def _forget_bonded_devices(bus, known_centrals: set = frozenset(), path: str = ADAPTER_PATH) -> None:
    """Forget stale phone bonds before a session - the edge-agent found on
    real hardware that a phone bonded against a previous registration of
    this GATT service can rely on a stale cached GATT database and never
    read our characteristics. But unlike a dedicated edge-agent box, this
    library runs on general-purpose machines, so it must NOT do what the
    edge-agent does (forget every bond): only devices that have talked to
    our service before (known_centrals, by Bluetooth address) or that
    BlueZ identifies as a phone. Keyboards, mice, headphones etc. stay."""
    try:
        root_introspection = await bus.introspect("org.bluez", "/")
        root_proxy = bus.get_proxy_object("org.bluez", "/", root_introspection)
        om = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await om.call_get_managed_objects()
    except Exception as e:
        logger.warning(f"Could not enumerate BlueZ devices to forget bonds: {e}")
        return

    adapter_introspection = await bus.introspect("org.bluez", path)
    adapter_proxy = bus.get_proxy_object("org.bluez", path, adapter_introspection)
    adapter_iface = adapter_proxy.get_interface("org.bluez.Adapter1")

    for device_path, interfaces in objects.items():
        device_props = interfaces.get("org.bluez.Device1")
        if device_props is None:
            continue
        paired = device_props.get("Paired")
        if not (paired and paired.value):
            continue
        address = device_props["Address"].value if "Address" in device_props else None
        icon = device_props["Icon"].value if "Icon" in device_props else None
        alias = device_props["Alias"].value if "Alias" in device_props else address
        if address not in known_centrals and icon != "phone":
            logger.info(f"Leaving bonded device '{alias}' ({icon or 'unknown type'}) alone")
            continue
        try:
            await adapter_iface.call_remove_device(device_path)
            logger.info(f"Forgot previously bonded phone '{alias}' before starting BLE provisioning")
        except Exception as e:
            logger.warning(f"Could not remove bonded device {device_path}: {e}")


SERVICE_UUID = "95e30001-5737-45a9-a092-a88e2e5dd659"
RX_UUID = "95e30002-5737-45a9-a092-a88e2e5dd659"
TX_UUID = "95e30003-5737-45a9-a092-a88e2e5dd659"

KNOWN_SET_FIELDS = {"if", "ssid", "pass", "pin", "apn", "blynk", "host", "ip", "mask", "gw", "dns", "dns2", "save"}

NM_BUS_NAME = "org.freedesktop.NetworkManager"
NM_ROOT_PATH = "/org/freedesktop/NetworkManager"
NM_IFACE = "org.freedesktop.NetworkManager"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_WIRELESS_IFACE = "org.freedesktop.NetworkManager.Device.Wireless"
NM_WIRED_IFACE = "org.freedesktop.NetworkManager.Device.Wired"
NM_AP_IFACE = "org.freedesktop.NetworkManager.AccessPoint"
NM_SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
NM_SETTINGS_IFACE = "org.freedesktop.NetworkManager.Settings"
NM_SETTINGS_CONNECTION_IFACE = "org.freedesktop.NetworkManager.Settings.Connection"

NM_DEVICE_TYPE_ETHERNET = 1
NM_DEVICE_TYPE_WIFI = 2
NM_DEVICE_TYPE_MODEM = 8
NM_DEVICE_TYPE_NAMES = {NM_DEVICE_TYPE_ETHERNET: "eth", NM_DEVICE_TYPE_WIFI: "wifi", NM_DEVICE_TYPE_MODEM: "cell"}

NM_DEVICE_STATE_ACTIVATED = 100
NM_DEVICE_STATE_FAILED = 120

NM_DEVICE_STATE_REASON_NO_SECRETS = 7
NM_DEVICE_STATE_REASON_SUPPLICANT_DISCONNECT = 8
NM_DEVICE_STATE_REASON_SUPPLICANT_TIMEOUT = 11
NM_DEVICE_STATE_REASON_SSID_NOT_FOUND = 53

NM_802_11_AP_FLAGS_PRIVACY = 0x1
NM_802_11_AP_SEC_KEY_MGMT_SAE = 0x400  # WPA3-Personal

NM_CONNECT_TIMEOUT = 30
NM_SCAN_GRACE_PERIOD = 4

MM_BUS_NAME = "org.freedesktop.ModemManager1"
MM_ROOT_PATH = "/org/freedesktop/ModemManager1"
MM_MODEM_IFACE = "org.freedesktop.ModemManager1.Modem"
MM_SIM_IFACE = "org.freedesktop.ModemManager1.Sim"
MM_OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

MM_MODEM_LOCK_NONE = 1
MM_MODEM_LOCK_SIM_PIN = 2


async def _nm_interface(bus, path: str, iface: str):
    introspection = await bus.introspect(NM_BUS_NAME, path)
    proxy = bus.get_proxy_object(NM_BUS_NAME, path, introspection)
    return proxy.get_interface(iface)


async def _mm_interface(bus, path: str, iface: str):
    introspection = await bus.introspect(MM_BUS_NAME, path)
    proxy = bus.get_proxy_object(MM_BUS_NAME, path, introspection)
    return proxy.get_interface(iface)


async def _get_wifi_device_path(bus):
    nm = await _nm_interface(bus, NM_ROOT_PATH, NM_IFACE)
    for path in await nm.call_get_devices():
        dev = await _nm_interface(bus, path, NM_DEVICE_IFACE)
        if await dev.get_device_type() == NM_DEVICE_TYPE_WIFI:
            return path
    return None


async def _get_modem_device_path(bus):
    nm = await _nm_interface(bus, NM_ROOT_PATH, NM_IFACE)
    for path in await nm.call_get_devices():
        dev = await _nm_interface(bus, path, NM_DEVICE_IFACE)
        if await dev.get_device_type() == NM_DEVICE_TYPE_MODEM:
            return path
    return None


async def _get_modem_info(bus) -> dict:
    introspection = await bus.introspect(MM_BUS_NAME, MM_ROOT_PATH)
    proxy = bus.get_proxy_object(MM_BUS_NAME, MM_ROOT_PATH, introspection)
    om = proxy.get_interface(MM_OBJECT_MANAGER_IFACE)
    objects = await om.call_get_managed_objects()
    for path, interfaces in objects.items():
        modem_props = interfaces.get(MM_MODEM_IFACE)
        if modem_props is None:
            continue
        lock = modem_props.get("UnlockRequired")
        imei = modem_props.get("EquipmentIdentifier")
        info = {
            "modem_path": path,
            "imei": imei.value if imei else "",
            "unlock_required": lock is not None and lock.value == MM_MODEM_LOCK_SIM_PIN,
        }
        sim_variant = modem_props.get("Sim")
        sim_path = sim_variant.value if sim_variant else None
        if sim_path and sim_path != "/":
            info["sim_path"] = sim_path
            try:
                sim_introspection = await bus.introspect(MM_BUS_NAME, sim_path)
                sim_proxy = bus.get_proxy_object(MM_BUS_NAME, sim_path, sim_introspection)
                sim = sim_proxy.get_interface(MM_SIM_IFACE)
                info["imsi"] = await sim.get_imsi()
                info["iccid"] = await sim.get_sim_identifier()
            except Exception as e:
                logger.warning(f"Could not read SIM properties from {sim_path}: {e}")
        return info
    return {}


async def _current_wifi_ssid(bus, wifi_path: str) -> Optional[str]:
    """None unless the device is actually ACTIVATED on a real AP right now -
    used to skip tearing down a working connection when the app resubmits
    the same network it's already joined (confirmed on real hardware: an
    unconditional delete+recreate here drops an active SSH session even
    when the SSID/password submitted are identical to what's already
    connected)."""
    dev = await _nm_interface(bus, wifi_path, NM_DEVICE_IFACE)
    if await dev.get_state() != NM_DEVICE_STATE_ACTIVATED:
        return None
    wireless = await _nm_interface(bus, wifi_path, NM_WIRELESS_IFACE)
    ap_path = await wireless.get_active_access_point()
    if not ap_path or ap_path == "/":
        return None
    ap = await _nm_interface(bus, ap_path, NM_AP_IFACE)
    return bytes(await ap.get_ssid()).decode("utf-8", errors="replace")


async def _delete_existing_wifi_connections(bus, ssid: str) -> None:
    settings_iface = await _nm_interface(bus, NM_SETTINGS_PATH, NM_SETTINGS_IFACE)
    for path in await settings_iface.call_list_connections():
        try:
            conn = await _nm_interface(bus, path, NM_SETTINGS_CONNECTION_IFACE)
            wireless = (await conn.call_get_settings()).get("802-11-wireless")
            if not wireless:
                continue
            existing_ssid = bytes(wireless["ssid"].value).decode("utf-8", errors="replace")
            if existing_ssid == ssid:
                await conn.call_delete()
        except Exception as e:
            logger.warning(f"Could not inspect/delete existing NM connection {path}: {e}")


async def _delete_existing_cellular_connections(bus) -> None:
    settings_iface = await _nm_interface(bus, NM_SETTINGS_PATH, NM_SETTINGS_IFACE)
    for path in await settings_iface.call_list_connections():
        try:
            conn = await _nm_interface(bus, path, NM_SETTINGS_CONNECTION_IFACE)
            connection = (await conn.call_get_settings()).get("connection", {})
            existing_id = connection.get("id")
            if existing_id is not None and existing_id.value == "blynk-cellular":
                await conn.call_delete()
        except Exception as e:
            logger.warning(f"Could not inspect/delete existing NM connection {path}: {e}")


def _channel_from_frequency_mhz(freq: int) -> int:
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq >= 5000:
        return (freq - 5000) // 5
    return 0


def _ap_security_string(flags: int, wpa_flags: int, rsn_flags: int) -> str:
    if rsn_flags & NM_802_11_AP_SEC_KEY_MGMT_SAE:
        return "WPA3"
    if rsn_flags:
        return "WPA2"
    if wpa_flags:
        return "WPA"
    if flags & NM_802_11_AP_FLAGS_PRIVACY:
        return "WEP"
    return "OPEN"


def _map_nm_failure_reason(reason_code) -> str:
    return {
        NM_DEVICE_STATE_REASON_NO_SECRETS: "invalid_credentials",
        NM_DEVICE_STATE_REASON_SUPPLICANT_DISCONNECT: "invalid_credentials",
        NM_DEVICE_STATE_REASON_SUPPLICANT_TIMEOUT: "invalid_credentials",
        NM_DEVICE_STATE_REASON_SSID_NOT_FOUND: "not_found",
    }.get(reason_code, "timeout")


def _prefix_from_netmask(mask: str) -> int:
    try:
        return sum(bin(int(octet)).count("1") for octet in mask.split("."))
    except ValueError:
        return 24


def _ipv4_str_to_uint32(addr: str) -> int:
    return struct.unpack("=I", socket.inet_aton(addr))[0]


def _ipv4_settings(pending: dict) -> dict:
    ip = pending.get("ip")
    if not ip:
        return {"method": Variant("s", "auto")}

    settings = {
        "method": Variant("s", "manual"),
        "address-data": Variant("aa{sv}", [{
            "address": Variant("s", ip),
            "prefix": Variant("u", _prefix_from_netmask(pending.get("mask", "255.255.255.0"))),
        }]),
    }
    gw = pending.get("gw")
    if gw:
        settings["gateway"] = Variant("s", gw)
    dns_servers = [d for d in (pending.get("dns"), pending.get("dns2")) if d]
    if dns_servers:
        settings["dns"] = Variant("au", [_ipv4_str_to_uint32(d) for d in dns_servers])
    return settings


NAME_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
INACTIVITY_TIMEOUT = 300


def _device_serial() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return socket.gethostname()


def _short_suffix(unique_id: str, length: int = 4) -> str:
    value = zlib.crc32(unique_id.encode())
    chars = []
    for _ in range(length):
        value, rem = divmod(value, len(NAME_ALPHABET))
        chars.append(NAME_ALPHABET[rem])
    return "".join(chars)


def device_name(vendor_prefix: str = "Blynk") -> str:
    return f"{vendor_prefix} Device-{_short_suffix(_device_serial())}"


class FastAdvertisement(Advertisement):
    ADVERTISING_INTERVAL_MS = 100

    @dbus_property(PropertyAccess.READ)
    def MinInterval(self) -> "u":  # type: ignore
        return self.ADVERTISING_INTERVAL_MS

    @dbus_property(PropertyAccess.READ)
    def MaxInterval(self) -> "u":  # type: ignore
        return self.ADVERTISING_INTERVAL_MS

    _tx_power = 0

    @dbus_property(PropertyAccess.READWRITE)
    def TxPower(self) -> "n":  # type: ignore
        return self._tx_power

    @TxPower.setter
    def TxPower(self, value: "n") -> None:  # type: ignore
        self._tx_power = value


class ProvisioningService(Service):
    def __init__(self, on_rx):
        self._on_rx = on_rx
        super().__init__(SERVICE_UUID, True)

    @characteristic(RX_UUID, CharFlags.WRITE | CharFlags.WRITE_WITHOUT_RESPONSE)
    def rx(self, options):
        pass

    @rx.setter
    def rx(self, value, options):
        self._on_rx(bytes(value), options.device)

    @characteristic(TX_UUID, CharFlags.NOTIFY)
    def tx(self, options):
        pass

    def send(self, message: dict) -> None:
        logger.debug(f"phone <- {_redact(message)}")
        self.tx.changed(json.dumps(message).encode("utf-8"))


class ProvisioningSession:
    """Runs the state machine for one provisioning attempt. Blocking (via
    asyncio.run in provision())."""

    def __init__(self, identity, fw_version: str = "unknown",
                 on_provisioned: Optional[Callable[[bool], None]] = None,
                 reprovisioning: bool = False):
        self.identity = identity
        self.fw_version = fw_version
        self.on_provisioned = on_provisioned or (lambda did_network_change: None)
        self.reprovisioning = reprovisioning
        self.service = ProvisioningService(self._on_rx)
        self.bus = None
        self._pending = {}
        if reprovisioning and identity.auth_token:
            self._pending["blynk"] = identity.auth_token
            self._pending["host"] = identity.server
        self._watched_devices = set()
        self._background_tasks = set()
        self._done = asyncio.Event()
        self._result = False

    def _spawn(self, coro) -> "asyncio.Task":
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _on_rx(self, raw: bytes, device_path=None) -> None:
        if device_path and device_path not in self._watched_devices:
            self._watched_devices.add(device_path)
            self._spawn(self._watch_central_disconnect(device_path))

        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (ValueError, UnicodeDecodeError):
            self.service.send({"t": "error", "msg": "wrong format"})
            return

        logger.debug(f"phone -> {_redact(data)}")
        handler = {
            "info": self._handle_info,
            "ifs": self._handle_ifs,
            "scan": self._handle_scan,
            "set": self._handle_set,
            "connect": self._handle_connect,
            "reset": self._handle_reset,
            "reboot": self._handle_reboot,
        }.get(data.get("t"))

        if handler is None:
            self.service.send({"t": "error", "msg": "invalid command"})
            return
        handler(data)

    async def _watch_central_disconnect(self, device_path: str) -> None:
        try:
            introspection = await self.bus.introspect("org.bluez", device_path)
            proxy = self.bus.get_proxy_object("org.bluez", device_path, introspection)
            props_iface = proxy.get_interface("org.freedesktop.DBus.Properties")
        except Exception as e:
            logger.warning(f"Could not watch central {device_path} for disconnect: {e}")
            return

        def on_properties_changed(interface_name, changed, invalidated):
            if interface_name != "org.bluez.Device1":
                return
            connected = changed.get("Connected")
            if connected is not None and connected.value is False:
                logger.info("BLE central disconnected mid-session, clearing staged config")
                self._pending.clear()
                if self.reprovisioning and self.identity.auth_token:
                    self._pending["blynk"] = self.identity.auth_token
                    self._pending["host"] = self.identity.server

        props_iface.on_properties_changed(on_properties_changed)

    def _handle_info(self, data) -> None:
        self.service.send({
            "t": "info",
            "vendor": self.identity.vendor_prefix,
            "tmpl_id": self.identity.template_id,
            "fw_type": "0",
            "fw_ver": self.fw_version,
            "name": device_name(self.identity.vendor_prefix),
        })

    def _handle_ifs(self, data) -> None:
        self._spawn(self._do_ifs())

    async def _do_ifs(self) -> None:
        self.service.send({"t": "ifs_start"})
        try:
            nm = await _nm_interface(self.bus, NM_ROOT_PATH, NM_IFACE)
            device_paths = await nm.call_get_devices()
        except Exception as e:
            logger.error(f"Failed to reach NetworkManager to enumerate network interfaces: {e}")
            device_paths = []

        sent_any = False
        for path in device_paths:
            try:
                dev = await _nm_interface(self.bus, path, NM_DEVICE_IFACE)
                name = NM_DEVICE_TYPE_NAMES.get(await dev.get_device_type())
                if name is None:
                    continue
                if name == "cell":
                    msg = await self._cell_if_message()
                else:
                    state = await dev.get_state()
                    msg = {
                        "t": "if",
                        "name": name,
                        "mac": await self._hw_address(dev, path, name),
                        "status": "ready" if state >= NM_DEVICE_STATE_ACTIVATED else "unavailable",
                        "static_ip": 1,
                    }
                    if name == "wifi":
                        msg["scan"] = 1
            except Exception as e:
                logger.error(f"Failed to read a NetworkManager device ({path}): {e}")
                continue
            self.service.send(msg)
            sent_any = True
        if not sent_any:
            self.service.send({"t": "if", "name": "eth", "status": "ready"})
        self.service.send({"t": "ifs_end"})

    async def _hw_address(self, dev, path: str, name: str) -> str:
        """Device.HwAddress only exists on NetworkManager >= 1.24; older NM
        (confirmed on real hardware: Ubuntu 20.04, NM 1.22) only has it on
        the type-specific Wired/Wireless interface. Without this fallback
        every device failed to read and the app was only offered "eth"."""
        try:
            return await dev.get_hw_address()
        except AttributeError:
            specific = await _nm_interface(
                self.bus, path, NM_WIRELESS_IFACE if name == "wifi" else NM_WIRED_IFACE
            )
            return await specific.get_hw_address()

    async def _cell_if_message(self) -> dict:
        try:
            info = await _get_modem_info(self.bus)
        except Exception as e:
            logger.warning(f"Could not read modem/SIM info via ModemManager: {e}")
            info = {}
        pin_required = bool(info.get("unlock_required")) and "sim_path" in info
        return {
            "t": "if",
            "name": "cell",
            "imei": info.get("imei", ""),
            "imsi": info.get("imsi", ""),
            "iccid": info.get("iccid", ""),
            "scan": 0,
            "pin": 1 if pin_required else 0,
            "apn": 1,
        }

    def _handle_scan(self, data) -> None:
        self._spawn(self._do_scan())

    async def _do_scan(self) -> None:
        self.service.send({"t": "scan_start"})
        try:
            wifi_path = await _get_wifi_device_path(self.bus)
            if wifi_path is None:
                self.service.send({"t": "error", "msg": "no wifi"})
                self.service.send({"t": "scan_end"})
                return

            wireless = await _nm_interface(self.bus, wifi_path, NM_WIRELESS_IFACE)
            await wireless.call_request_scan({})
            await asyncio.sleep(NM_SCAN_GRACE_PERIOD)

            best_by_ssid = {}
            for ap_path in await wireless.call_get_all_access_points():
                ap = await _nm_interface(self.bus, ap_path, NM_AP_IFACE)
                ssid = bytes(await ap.get_ssid()).decode("utf-8", errors="replace")
                if not ssid:
                    continue
                rssi = await ap.get_strength() // 2 - 100
                if rssi < -90:
                    continue
                if ssid in best_by_ssid and best_by_ssid[ssid]["rssi"] >= rssi:
                    continue
                best_by_ssid[ssid] = {
                    "ssid": ssid,
                    "bssid": await ap.get_hw_address(),
                    "rssi": rssi,
                    "sec": _ap_security_string(
                        await ap.get_flags(), await ap.get_wpa_flags(), await ap.get_rsn_flags()
                    ),
                    "ch": _channel_from_frequency_mhz(await ap.get_frequency()),
                }

            for network in sorted(best_by_ssid.values(), key=lambda n: n["rssi"], reverse=True)[:30]:
                self.service.send({"t": "scan", **network})
        except Exception as e:
            logger.error(f"WiFi scan failed: {e}")
        self.service.send({"t": "scan_end"})

    def _handle_set(self, data) -> None:
        unknown = set(data.keys()) - {"t"} - KNOWN_SET_FIELDS
        if unknown:
            self.service.send({"t": "set_fail"})
            return
        for field in ("if", "blynk", "host", "ssid", "pass", "pin", "apn", "ip", "mask", "gw", "dns", "dns2"):
            if field in data:
                self._pending[field] = data[field]
        self.service.send({"t": "set_ok"})

    def _handle_connect(self, data) -> None:
        token = self._pending.get("blynk", "")
        if len(token) != 32:
            self.service.send({"t": "connect_fail", "msg": "configuration invalid"})
            return

        self.service.send({"t": "connecting"})
        self._spawn(self._do_connect(token, self._pending.get("host")))

    async def _do_connect(self, token: str, host) -> None:
        interface = self._pending.get("if")
        ssid = self._pending.get("ssid")
        did_network_change = False

        if interface == "cell":
            did_network_change = True
            self.service.send({"t": "status", "s": "connecting_net"})
            try:
                ok, reason = await self._connect_cellular(self._pending.get("pin"), self._pending.get("apn"))
            except Exception as e:
                logger.error(f"Cellular connect failed: {e}")
                ok, reason = False, "generic"
            if not ok:
                self.service.send({"t": "net_fail", "reason": reason})
                self._pending.pop("pin", None)
                return
        elif interface == "wifi" or (not interface and ssid):
            did_network_change = True
            self.service.send({"t": "status", "s": "connecting_net"})
            try:
                ok, reason = await self._connect_wifi(ssid, self._pending.get("pass"))
            except Exception as e:
                logger.error(f"WiFi connect failed: {e}")
                ok, reason = False, "generic"
            if not ok:
                self.service.send({"t": "net_fail", "reason": reason})
                for field in ("ssid", "pass", "ip", "mask", "gw", "dns", "dns2"):
                    self._pending.pop(field, None)
                return

        self.service.send({"t": "status", "s": "connecting_cloud"})

        self.identity.auth_token = token
        if host:
            self.identity.server = host
        self.identity.save()
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.on_provisioned(did_network_change)
        )

        self.service.send({"t": "status", "s": "connected"})
        await asyncio.sleep(0.3)
        self._result = True
        self._done.set()

    async def _connect_cellular(self, pin, apn) -> tuple:
        try:
            info = await _get_modem_info(self.bus)
        except Exception as e:
            logger.error(f"Could not query ModemManager: {e}")
            return False, "generic"
        if not info:
            return False, "generic"
        if "sim_path" not in info:
            return False, "sim_missing"
        if info["unlock_required"]:
            if not pin:
                return False, "sim_locked"
            try:
                sim_introspection = await self.bus.introspect(MM_BUS_NAME, info["sim_path"])
                sim_proxy = self.bus.get_proxy_object(MM_BUS_NAME, info["sim_path"], sim_introspection)
                sim = sim_proxy.get_interface(MM_SIM_IFACE)
                await sim.call_send_pin(pin)
            except Exception as e:
                logger.error(f"SIM PIN unlock failed: {e}")
                return False, "sim_wrong_pin"

        modem_device_path = await _get_modem_device_path(self.bus)
        if modem_device_path is None:
            return False, "generic"

        await _delete_existing_cellular_connections(self.bus)

        settings = {
            "connection": {
                "id": Variant("s", "blynk-cellular"),
                "type": Variant("s", "gsm"),
            },
            "gsm": {
                "apn": Variant("s", apn or ""),
            },
        }

        dev = await _nm_interface(self.bus, modem_device_path, NM_DEVICE_IFACE)
        result = {}
        done = asyncio.Event()

        def on_state_changed(new_state, old_state, reason):
            if new_state == NM_DEVICE_STATE_ACTIVATED:
                result["ok"] = True
                done.set()
            elif new_state == NM_DEVICE_STATE_FAILED:
                result["ok"] = False
                result["reason"] = reason
                done.set()

        async def activate():
            nm = await _nm_interface(self.bus, NM_ROOT_PATH, NM_IFACE)
            await nm.call_add_and_activate_connection(settings, modem_device_path, "/")
            await done.wait()

        dev.on_state_changed(on_state_changed)
        try:
            try:
                await asyncio.wait_for(activate(), timeout=NM_CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                return False, "timeout"
        finally:
            dev.off_state_changed(on_state_changed)

        if result.get("ok"):
            return True, None
        return False, _map_nm_failure_reason(result.get("reason"))

    async def _connect_wifi(self, ssid: str, password) -> tuple:
        wifi_path = await _get_wifi_device_path(self.bus)
        if wifi_path is None:
            return False, "generic"

        # Already joined to exactly this network and no static-IP change
        # was requested alongside it - nothing to do. If NM is ACTIVATED on
        # this SSID at all, the stored credentials must already be correct
        # (a wrong password would never have reached ACTIVATED), so there's
        # no reason to delete/recreate the connection and force a rejoin.
        if "ip" not in self._pending:
            current_ssid = await _current_wifi_ssid(self.bus, wifi_path)
            logger.debug(f"WiFi currently on {current_ssid!r}, app requested {ssid!r}")
            if current_ssid == ssid:
                logger.info(f"Already connected to '{ssid}' - skipping WiFi reconfiguration")
                return True, None

        await _delete_existing_wifi_connections(self.bus, ssid)

        settings = {
            "connection": {
                "id": Variant("s", ssid),
                "type": Variant("s", "802-11-wireless"),
            },
            "802-11-wireless": {
                "ssid": Variant("ay", ssid.encode("utf-8")),
                "mode": Variant("s", "infrastructure"),
            },
            "ipv4": _ipv4_settings(self._pending),
            "ipv6": {"method": Variant("s", "auto")},
        }
        if password:
            settings["802-11-wireless-security"] = {
                "key-mgmt": Variant("s", "wpa-psk"),
                "psk": Variant("s", password),
            }

        dev = await _nm_interface(self.bus, wifi_path, NM_DEVICE_IFACE)
        result = {}
        done = asyncio.Event()

        def on_state_changed(new_state, old_state, reason):
            if new_state == NM_DEVICE_STATE_ACTIVATED:
                result["ok"] = True
                done.set()
            elif new_state == NM_DEVICE_STATE_FAILED:
                result["ok"] = False
                result["reason"] = reason
                done.set()

        async def activate():
            nm = await _nm_interface(self.bus, NM_ROOT_PATH, NM_IFACE)
            await nm.call_add_and_activate_connection(settings, wifi_path, "/")
            await done.wait()

        dev.on_state_changed(on_state_changed)
        try:
            try:
                await asyncio.wait_for(activate(), timeout=NM_CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                return False, "timeout"
        finally:
            dev.off_state_changed(on_state_changed)

        if result.get("ok"):
            return True, None
        return False, _map_nm_failure_reason(result.get("reason"))

    def _handle_reset(self, data) -> None:
        if not self.reprovisioning:
            self.identity.auth_token = None
            self.identity.save()
        for field in ("if", "ssid", "pass", "pin", "apn", "ip", "mask", "gw", "dns", "dns2"):
            self._pending.pop(field, None)
        self.service.send({"t": "reset_ok"})

    def _handle_reboot(self, data) -> None:
        # No-op for this standalone library - a simple script running this
        # has no equivalent of the edge-agent's own privileged host-reboot
        # path, and shouldn't assume it's safe to reboot its own machine.
        logger.info("BLE 'reboot' command received - no-op in this library")

    def _known_centrals(self) -> set:
        known = getattr(self.identity, "known_centrals", None)
        return known() if known else set()

    async def _remember_centrals(self) -> None:
        """Record the Bluetooth address of each phone that wrote to our
        service *and bonded* this session - only a bond can go stale, and
        only a bonded device's Address is its stable identity address.
        Confirmed on real hardware (Pi 5, BlueZ 5.82): a phone that never
        bonded reports a resolvable private address (changes every few
        minutes), which would never match anything later and would just
        pile up in the file. Read at the end of the session, after any
        mid-session bonding has resolved the address."""
        remember = getattr(self.identity, "remember_centrals", None)
        if not remember:
            return
        addresses = set()
        for device_path in self._watched_devices:
            try:
                introspection = await self.bus.introspect("org.bluez", device_path)
                proxy = self.bus.get_proxy_object("org.bluez", device_path, introspection)
                device = proxy.get_interface("org.bluez.Device1")
                if await device.get_paired():
                    addresses.add(await device.get_address())
            except Exception as e:
                logger.debug(f"Could not read address of central {device_path}: {e}")
        try:
            remember(addresses)
        except OSError as e:
            logger.warning(f"Could not save known BLE centrals: {e}")

    async def run(self) -> bool:
        bus = await get_message_bus()
        self.bus = bus
        try:
            adapter = await _get_adapter(bus)
            await adapter.set_powered(True)
            await _forget_bonded_devices(bus, self._known_centrals())

            await self.service.register(bus, adapter=adapter)

            agent = AutoAcceptPairingAgent()
            await agent.register(bus)
            logger.info("BLE pairing agent registered with BlueZ")

            name = device_name(self.identity.vendor_prefix)
            advert = FastAdvertisement(name, [SERVICE_UUID], appearance=0, timeout=0)
            await advert.register(bus, adapter=adapter)

            # A plain print(), not just logger.info() - this library
            # deliberately never calls logging.basicConfig() itself (that's
            # the calling application's call to make, not a library's), but
            # that means this one essential "what to do right now" message
            # would otherwise be silently swallowed by Python's default
            # logging config, leaving someone running this interactively
            # with no indication pairing is even possible, let alone which
            # device name to look for - confirmed on real hardware that
            # this is exactly what happened on a first real test.
            print(f"Advertising as '{name}' - open the Blynk app and add this device "
                  f"within {INACTIVITY_TIMEOUT}s.")
            try:
                await asyncio.wait_for(self._done.wait(), timeout=INACTIVITY_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("BLE provisioning timed out waiting for the app")

            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)

            await self._remember_centrals()

            try:
                await self.service.unregister()
                await agent.unregister(bus)
            except Exception as e:
                logger.warning(f"BLE cleanup after provisioning: {e}")
        finally:
            bus.disconnect()

        return self._result


def provision(identity, fw_version: str = "unknown",
              on_provisioned: Optional[Callable[[bool], None]] = None,
              reprovisioning: bool = False) -> bool:
    """Blocks until provisioning succeeds, is abandoned, or times out.
    Returns True only if a valid token was received."""
    # The session must be constructed *inside* the running loop: on Python
    # <3.10, asyncio.Event() binds to whatever loop exists at construction,
    # so building it before asyncio.run() creates its new loop fails with
    # "got Future attached to a different loop" the moment pairing waits
    # (confirmed on real hardware, Ubuntu 20.04 / Python 3.8).
    async def _run() -> bool:
        return await ProvisioningSession(identity, fw_version, on_provisioned, reprovisioning).run()

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.error(f"BLE provisioning failed: {e}")
        return False
