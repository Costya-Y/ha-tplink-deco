"""Documented Deco controls shared by Home Assistant actions and MCP tools.

Protocol reference: roquerodrigo/tplink-deco-api, commit
2d4ddeae41b8d9c16a496ccd3fadeb3e9f4835f1, docs/endpoints.
An endpoint in this catalogue is a candidate, not proof of firmware support.
"""

import asyncio
import base64
from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import re
from typing import Any


@dataclass(frozen=True)
class Feature:
    """A documented local controller, form and its permitted operations."""

    controller: str
    form: str
    reads: tuple[str, ...] = ("read",)
    writes: tuple[str, ...] = ("write",)


FEATURES = {
    "wlan": Feature("wireless", "wlan"),
    "fast_roaming": Feature("wireless", "ieee80211r"),
    "beamforming": Feature("wireless", "beamforming"),
    "ofdma": Feature("wireless", "ofdma"),
    "wireless_power": Feature("wireless", "power"),
    "wireless_support": Feature("wireless", "get_support", writes=()),
    "mlo_network": Feature("wireless", "mlo_network"),
    "wifi_schedule": Feature("wireless", "wifi_schedule"),
    "bandwidth_enhance": Feature("wireless", "bandwidth_enhance"),
    "bandwidth_switch": Feature("wireless", "bandwidth_switch"),
    "backhaul_optimization": Feature("wireless", "backhaul_optimization"),
    "network_optimize": Feature("network_optimize", "acs_optimize"),
    "devices": Feature("device", "device_list", writes=()),
    "device_mode": Feature("device", "mode", writes=()),
    "led": Feature("device", "led"),
    "eco_mode": Feature("device", "eco_mode"),
    "time_settings": Feature("device", "timesetting"),
    "speed_test": Feature("device", "speedtest", writes=("write", "stop")),
    "clients": Feature("client", "client_list", writes=("write", "remove")),
    "blocked_clients": Feature(
        "client", "black_list", reads=("list",), writes=("block", "unblock")
    ),
    "reservations": Feature(
        "client",
        "addr_reservation",
        reads=("getlist",),
        writes=("add", "modify", "remove"),
    ),
    "client_isolation": Feature("client", "client_isolation"),
    "dhcp_leases": Feature("client", "lease", reads=("get",), writes=()),
    "internet": Feature("network", "internet", writes=()),
    "performance": Feature("network", "performance", writes=()),
    "wan": Feature("network", "wan_ipv4", writes=("write", "connect", "disconnect")),
    "lan": Feature("network", "lan_ip"),
    "ipv6": Feature("network", "ipv6"),
    "vlan": Feature("network", "vlan"),
    "upnp": Feature("network", "upnp"),
    "static_routes": Feature(
        "network",
        "routes_static",
        reads=("getlist",),
        writes=("add", "modify", "remove"),
    ),
    "dhcp": Feature("dhcp", "dhcp_info"),
    "qos": Feature("smart_network", "tm_qos"),
    "qos_bandwidth": Feature(
        "smart_network", "bandwidth", reads=("get",), writes=("set",)
    ),
    "parental_profiles": Feature(
        "smart_network",
        "patrol_owner",
        reads=("list", "get"),
        writes=("add", "del", "set", "block"),
    ),
    "parental_clients": Feature(
        "smart_network", "patrol_cli", reads=(), writes=("add", "del")
    ),
    "parental_filters": Feature(
        "smart_network", "patrol_filter", reads=("get",), writes=("add", "del")
    ),
    "port_forwarding": Feature(
        "nat", "vs", reads=("getlist",), writes=("add", "modify", "remove")
    ),
    "dmz": Feature("nat", "dmz"),
    "ddns": Feature("ddns", "ddns", reads=("get",), writes=("set",)),
    "vpn_client": Feature(
        "vpn_client",
        "config",
        reads=("read", "load"),
        writes=("write", "insert", "update", "remove"),
    ),
    "vpn_server": Feature(
        "vpn_server", "server", writes=("insert", "remove", "update")
    ),
    "ipv6_firewall": Feature(
        "ipv6_firewall", "firewall", writes=("write", "remove", "modify")
    ),
    "iptv": Feature("iptv", "iptv", reads=("get",), writes=("set",)),
}

BANDS = ("band2_4", "band5_1", "band5_2", "band6", "band6_2")
MAIN_ENABLE = dict(
    zip(BANDS, ("enable_2g", "enable_5g", "enable_5g2", "enable_6g", "enable_6g2"))
)
REDACTED = "**REDACTED**"


def normalize_mac(value: str) -> str:
    """Validate a unicast MAC and normalize to the router's dash format."""
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}",
        value,
    ):
        raise ValueError("Expected a MAC address with colon or dash separators")
    result = value.upper().replace(":", "-")
    if int(result[:2], 16) & 1 or result == "00-00-00-00-00-00":
        raise ValueError("Expected a nonzero unicast MAC address")
    return result


def redact(value: Any) -> Any:
    """Remove credentials from nested router responses before returning them."""
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            REDACTED
            if any(
                word in re.sub(r"[^a-z0-9]", "", str(key).lower())
                for word in (
                    "password",
                    "passwd",
                    "passphrase",
                    "pwd",
                    "secret",
                    "token",
                    "cookie",
                    "privatekey",
                    "preshared",
                    "psk",
                    "credential",
                )
            )
            or str(key).lower() in ("key", "stok", "username")
            else redact(item)
        )
        for key, item in value.items()
    }


def parse_params(value: str) -> dict:
    """Parse advanced parameters without permitting arbitrary Python objects."""
    if not isinstance(value, str) or len(value) > 65536:
        raise ValueError("Parameters must be a JSON object of at most 65536 characters")

    def invalid_constant(_value):
        raise ValueError("Parameters must use standard JSON values")

    params = json.loads(value, parse_constant=invalid_constant)
    if not isinstance(params, dict):
        raise TypeError("Parameters must be a JSON object")
    return params


def catalogue() -> dict:
    """Describe documented endpoints without asserting hardware support."""
    return {
        name: {
            "controller": spec.controller,
            "form": spec.form,
            "read_operations": list(spec.reads),
            "write_operations": list(spec.writes),
            "support": "unverified_on_this_firmware",
        }
        for name, spec in FEATURES.items()
    }


class DecoControl:
    """Serialize control commands, including read/modify/write transactions."""

    def __init__(self, api) -> None:
        """Reuse the integration's authenticated session; never create another."""
        self.api = api
        self._lock = asyncio.Lock()

    async def async_query(
        self, feature: str, operation: str | None = None, params: dict | None = None
    ):
        """Read one documented feature, without hiding firmware failures."""
        spec = FEATURES[feature]
        operation = operation or (spec.reads[0] if spec.reads else "")
        if operation not in spec.reads:
            raise ValueError("Unsupported read operation for this feature")
        return await self.api.async_request(
            spec.controller, spec.form, operation, params or {}, read_only=True
        )

    async def async_configure(self, feature: str, operation: str, params: dict):
        """Send one explicit configuration command, without automatic replay."""
        spec = FEATURES[feature]
        if operation not in spec.writes:
            raise ValueError("Unsupported write operation for this feature")
        async with self._lock:
            result = await self.api.async_request(
                spec.controller, spec.form, operation, params
            )
        return {"status": "accepted", "verified": False, "result": redact(result)}

    async def async_block_client(
        self, mac: str, blocked: bool, name: str = "", client_type: str = "other"
    ):
        """Set and read back the blocklist; preserve the router's name encoding."""
        mac = normalize_mac(mac)
        params = {
            "client_list": (
                [
                    {
                        "mac": mac,
                        "name": base64.b64encode(name.encode()).decode(),
                        "client_type": client_type,
                    }
                ]
                if blocked
                else [mac]
            )
        }
        async with self._lock:
            await self.api.async_request(
                "client", "black_list", "block" if blocked else "unblock", params
            )
            result = await self.async_query("blocked_clients")
            items = result.get("client_list")
            if items == {}:
                items = []
            if not isinstance(items, list):
                raise TypeError(
                    "Router returned an invalid blocklist; write outcome is unverified"
                )
            found = any(normalize_mac(item["mac"]) == mac for item in items)
            if found != blocked:
                raise ValueError("Router did not confirm the requested blocklist state")
        return {"status": "confirmed", "verified": True, "mac": mac, "blocked": blocked}

    async def async_rename_client(
        self, mac: str, name: str, client_type: str | None = None
    ):
        """Rename a client using the firmware's user-set-name marker."""
        mac = normalize_mac(mac)
        if not name.strip() or len(name.encode()) > 128:
            raise ValueError("Client name must contain 1 to 128 UTF-8 bytes")
        entry = {
            "mac": mac,
            "name": base64.b64encode(name.encode()).decode(),
            "user_set_name_type": True,
        }
        if client_type is not None:
            entry["client_type"] = client_type
        return await self.async_configure("clients", "write", {"client_list": [entry]})

    async def async_set_reservation(self, mac: str, ip: str | None):
        """Add, update or remove a reservation and verify the resulting table."""
        mac = normalize_mac(mac)
        if ip is not None:
            address = IPv4Address(ip)
            if (
                address.is_multicast
                or address.is_unspecified
                or address.is_loopback
                or int(address) == 0xFFFFFFFF
            ):
                raise ValueError("Expected an assignable IPv4 address")
            ip = str(address)
        async with self._lock:
            table = await self.async_query("reservations")
            items = table.get("reservation_list")
            if not isinstance(items, list):
                raise TypeError("Router returned an invalid reservation table")
            current = next(
                (item for item in items if normalize_mac(item["mac"]) == mac), None
            )
            if ip is None and current is not None:
                await self.api.async_request(
                    "client", "addr_reservation", "remove", {"reservation_list": [mac]}
                )
            elif ip is not None and (current is None or current["ip"] != ip):
                if any(
                    item["ip"] == ip and normalize_mac(item["mac"]) != mac
                    for item in items
                ):
                    raise ValueError(
                        "This IPv4 address is already reserved for another client"
                    )
                params = (
                    {"reservation_list": [{"mac": mac, "ip": ip}]}
                    if current is None
                    else {
                        "mac": mac,
                        "ip": ip,
                        "original_mac": mac,
                        "original_ip": current["ip"],
                    }
                )
                await self.api.async_request(
                    "client",
                    "addr_reservation",
                    "add" if current is None else "modify",
                    params,
                )
            table = await self.async_query("reservations")
            items = table.get("reservation_list")
            if not isinstance(items, list):
                raise TypeError(
                    "Router returned an invalid reservation table; write outcome is unverified"
                )
            current = next(
                (item for item in items if normalize_mac(item["mac"]) == mac), None
            )
            if (ip is None and current is not None) or (
                ip is not None and (current is None or current.get("ip") != ip)
            ):
                raise ValueError("Router did not confirm the requested reservation")
        return {"status": "confirmed", "verified": True, "mac": mac, "ip": ip}

    async def async_set_wifi(
        self, network: str, enabled: bool, band: str | None = None
    ):
        """Toggle only the requested SSID object, leaving credentials untouched."""
        if type(enabled) is not bool:
            raise ValueError("Enabled must be a boolean")
        if network not in ("main", "guest", "iot", "mlo"):
            raise ValueError("Unknown Wi-Fi network")
        if network in ("main", "guest") and band not in BANDS:
            raise ValueError("An explicit band is required for main or guest Wi-Fi")
        if network in ("iot", "mlo") and band is not None:
            raise ValueError("IoT and MLO are mesh-wide networks; omit band")
        async with self._lock:
            state = await self.async_query("wlan")
            key = band if network in ("main", "guest") else network
            child = "guest" if network == "guest" else "host"
            if not isinstance(state.get(key), dict) or not isinstance(
                state[key].get(child), dict
            ):
                raise ValueError(  # noqa: TRY004 - missing firmware feature, not a caller type error
                    "The requested network/band is not exposed by this firmware"
                )
            flag = MAIN_ENABLE[band] if network == "main" else "enable"
            result = await self.api.async_request(
                "wireless", "wlan", "write", {key: {child: {flag: enabled}}}
            )
        return {
            "status": "accepted",
            "verified": False,
            "network": network,
            "band": band,
            "enabled": enabled,
            "result": redact(result),
        }
