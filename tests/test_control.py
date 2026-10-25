"""Control regressions: firmware payloads, ambiguous outcomes and concurrency."""

import asyncio
import base64
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.tplink_deco.control import DecoControl
from custom_components.tplink_deco.control import normalize_mac
from custom_components.tplink_deco.control import parse_params
from custom_components.tplink_deco.control import redact
from custom_components.tplink_deco.exceptions import TransientConnectionException

MAC = "02-11-22-33-44-55"
OTHER = "02-11-22-33-44-66"


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        "001122334455",
        "AA:BB-CC:DD:EE:FF",
        "01:11:22:33:44:55",
        "00:00:00:00:00:00",
    ],
)
def test_invalid_mac(value):
    """Reject malformed, multicast and zero MACs before a router call."""
    with pytest.raises(ValueError):
        normalize_mac(value)


def test_mac_and_redaction():
    """Normalize unicast MACs and redact nested credentials without mutation."""
    assert normalize_mac("02:ab:cd:ef:12:34") == "02-AB-CD-EF-12-34"
    value = {
        "ssid": "name",
        "host": [
            {
                "Password": "secret",
                "private_key": "pem",
                "pre_shared_key": "psk",
                "passphrase": "phrase",
                "login_pwd": "pwd",
                "channel": 11,
            }
        ],
    }
    result = redact(value)
    assert result["ssid"] == "name"
    assert result["host"][0]["channel"] == 11
    assert all(
        item == "**REDACTED**"
        for key, item in result["host"][0].items()
        if key != "channel"
    )
    assert value["host"][0]["Password"] == "secret"


@pytest.mark.parametrize(
    "value",
    ["[]", "null", '{"a":NaN}', '{"a":Infinity}', '{"broken"', " " * 65537],
    ids=["list", "null", "nan", "infinity", "malformed", "oversized"],
)
def test_invalid_json(value):
    """Advanced calls accept a bounded standard JSON object only."""
    with pytest.raises((ValueError, TypeError)):
        parse_params(value)


@pytest.mark.asyncio
async def test_read_write_allowlist():
    """A query cannot mutate, and controller paths cannot be injected."""
    api = SimpleNamespace(async_request=AsyncMock(return_value={}))
    control = DecoControl(api)
    for feature, operation in [("wlan", "write"), ("parental_clients", None)]:
        with pytest.raises(ValueError):
            await control.async_query(feature, operation)
    with pytest.raises(KeyError):
        await control.async_query("https://attacker.invalid")
    with pytest.raises(ValueError):
        await control.async_configure("devices", "remove", {})
    api.async_request.assert_not_called()


@pytest.mark.asyncio
async def test_block_and_empty_unblock_are_verified():
    """Match the documented blacklist payloads, including empty-object lists."""
    api = SimpleNamespace(
        async_request=AsyncMock(
            side_effect=[{}, {"client_list": [{"mac": MAC}]}, {}, {"client_list": {}}]
        )
    )
    control = DecoControl(api)
    assert (await control.async_block_client(MAC, True, "Café"))["verified"]
    payload = api.async_request.call_args_list[0].args[3]
    assert base64.b64decode(payload["client_list"][0]["name"]).decode() == "Café"
    assert (await control.async_block_client(MAC, False))["blocked"] is False
    assert api.async_request.call_args_list[2].args == (
        "client",
        "black_list",
        "unblock",
        {"client_list": [MAC]},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", [{}, {"client_list": "bad"}, {"client_list": [{"mac": MAC}]}]
)
async def test_unblock_requires_actual_readback(result):
    """An empty acknowledgement or retained entry is never confirmation."""
    api = SimpleNamespace(async_request=AsyncMock(side_effect=[{}, result]))
    with pytest.raises((ValueError, TypeError)):
        await DecoControl(api).async_block_client(MAC, False)


@pytest.mark.asyncio
async def test_name_payload_has_required_marker():
    """Firmware silently ignores renames lacking the user-set-name flag."""
    api = SimpleNamespace(async_request=AsyncMock(return_value={}))
    result = await DecoControl(api).async_rename_client(
        MAC, "Office TV", "entertainment"
    )
    item = api.async_request.call_args.args[3]["client_list"][0]
    assert item["user_set_name_type"] is True
    assert base64.b64decode(item["name"]).decode() == "Office TV"
    assert item["client_type"] == "entertainment"
    assert result["status"] == "accepted" and result["verified"] is False


class ReservationRouter:
    """Independent stateful fixture implementing the documented table contract."""

    def __init__(self):
        """Start with an empty persistent table."""
        self.table = {}
        self.calls = []

    async def async_request(self, controller, form, operation, params, **kwargs):
        """Enforce preconditions as firmware would, allowing task interleavings."""
        self.calls.append(operation)
        assert (controller, form) == ("client", "addr_reservation")
        await asyncio.sleep(0)
        if operation == "getlist":
            return {
                "reservation_list": [
                    {"mac": mac, "ip": ip} for mac, ip in self.table.items()
                ]
            }
        if operation == "add":
            for item in params["reservation_list"]:
                assert item["mac"] not in self.table
                self.table[item["mac"]] = item["ip"]
        elif operation == "modify":
            assert self.table[params["original_mac"]] == params["original_ip"]
            del self.table[params["original_mac"]]
            self.table[params["mac"]] = params["ip"]
        elif operation == "remove":
            for mac in params["reservation_list"]:
                del self.table[mac]
        return {}


@pytest.mark.asyncio
async def test_reservation_upsert_remove_and_noop():
    """Upsert preserves original IP, conflicts fail before write, removal verifies."""
    router = ReservationRouter()
    control = DecoControl(router)
    assert (await control.async_set_reservation(MAC, "192.168.1.50"))["verified"]
    assert (await control.async_set_reservation(MAC, "192.168.1.51"))["verified"]
    assert "modify" in router.calls
    before = list(router.calls)
    await control.async_set_reservation(MAC, "192.168.1.51")
    assert router.calls[len(before) :] == ["getlist", "getlist"]
    with pytest.raises(ValueError, match="already reserved"):
        await control.async_set_reservation(OTHER, "192.168.1.51")
    assert (await control.async_set_reservation(MAC, None))["verified"]
    assert router.table == {}


@pytest.mark.asyncio
async def test_concurrent_reservation_changes_are_serialized():
    """Two competing upserts cannot both observe and add an absent MAC."""
    router = ReservationRouter()
    control = DecoControl(router)
    results = await asyncio.gather(
        control.async_set_reservation(MAC, "192.168.1.50"),
        control.async_set_reservation(MAC, "192.168.1.51"),
    )
    assert all(result["verified"] for result in results)
    assert router.table == {MAC: "192.168.1.51"}
    assert router.calls == ["getlist", "add", "getlist", "getlist", "modify", "getlist"]


@pytest.mark.asyncio
async def test_reservation_readback_mismatch():
    """An accepted but ignored reservation write must fail verification."""
    api = SimpleNamespace(
        async_request=AsyncMock(
            side_effect=[{"reservation_list": []}, {}, {"reservation_list": []}]
        )
    )
    with pytest.raises(ValueError, match="did not confirm"):
        await DecoControl(api).async_set_reservation(MAC, "192.168.1.50")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip", ["bad", "::1", "127.0.0.1", "224.1.1.1", "0.0.0.0", "255.255.255.255"]
)
async def test_invalid_reservation_address(ip):
    """Reject nonassignable IPv4 values without sending anything."""
    api = SimpleNamespace(async_request=AsyncMock())
    with pytest.raises(ValueError):
        await DecoControl(api).async_set_reservation(MAC, ip)
    api.async_request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "network,band,flag",
    [
        ("main", "band2_4", "enable_2g"),
        ("guest", "band6", "enable"),
        ("iot", None, "enable"),
        ("mlo", None, "enable"),
    ],
)
async def test_wifi_only_writes_requested_flag(network, band, flag):
    """Partial Wi-Fi writes cannot inadvertently echo credentials or other settings."""
    key = band or network
    child = "guest" if network == "guest" else "host"
    config = {key: {child: {"password": "secret", "ssid": "name", "enable": True}}}
    original = deepcopy(config)
    api = SimpleNamespace(async_request=AsyncMock(side_effect=[config, {}]))
    result = await DecoControl(api).async_set_wifi(network, False, band)
    assert api.async_request.call_args.args[3] == {key: {child: {flag: False}}}
    assert config == original
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_absent_wifi_band_and_ambiguous_network():
    """Absent bands and ambiguous main/guest targets cannot produce a write."""
    api = SimpleNamespace(async_request=AsyncMock(return_value={}))
    control = DecoControl(api)
    with pytest.raises(ValueError, match="explicit band"):
        await control.async_set_wifi("main", False)
    api.async_request.assert_not_called()
    with pytest.raises(ValueError, match="not exposed"):
        await control.async_set_wifi("guest", False, "band6")
    assert api.async_request.call_count == 1


@pytest.mark.asyncio
async def test_dropped_write_acknowledgement_is_not_replayed():
    """A command may already have applied when its acknowledgement is lost."""
    api = SimpleNamespace(
        async_request=AsyncMock(side_effect=TransientConnectionException("lost ack"))
    )
    with pytest.raises(TransientConnectionException):
        await DecoControl(api).async_configure("network_optimize", "write", {})
    assert api.async_request.call_count == 1
