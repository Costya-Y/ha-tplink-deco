"""Encrypted and bare response contracts for new local controls."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from custom_components.tplink_deco.api import TplinkDecoApi
from custom_components.tplink_deco.api import aes_encrypt
from custom_components.tplink_deco.api import check_data_error_code
from custom_components.tplink_deco.exceptions import ForbiddenException
from custom_components.tplink_deco.exceptions import TransientConnectionException
from custom_components.tplink_deco.exceptions import UnexpectedApiException


def make_api():
    """Create a logged-in API without touching a real network."""
    api = TplinkDecoApi(MagicMock(), "http://192.0.2.1", "admin", "secret", True)
    api._seq, api._stok, api._cookie = 1, "token", "sysauth=abc"
    api._aes_key_bytes = b"1234567890123456"
    api._aes_iv_bytes = b"6543210987654321"
    api._encode_payload = MagicMock(return_value="encrypted-request")
    api._async_post = AsyncMock()
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bare", [{"error_code": 0}, {"errorcode": "0"}, {"success": True, "result": {}}]
)
async def test_bare_success_acknowledgement(bare):
    """Modern firmware may acknowledge writes without a data field."""
    api = make_api()
    api._async_post.return_value = bare
    assert await api.async_request("client", "black_list", "block", {}) == {}


@pytest.mark.asyncio
async def test_encrypted_query_payload_and_readback():
    """The new request path reuses the existing crypto/session envelope."""
    api = make_api()
    value = {"error_code": 0, "result": {"client_list": []}}
    encrypted = aes_encrypt(
        api._aes_key_bytes, api._aes_iv_bytes, json.dumps(value).encode()
    )
    api._async_post.return_value = {"data": base64.b64encode(encrypted).decode()}
    assert (
        await api.async_request("client", "black_list", "list", {}, read_only=True)
        == value["result"]
    )
    assert (
        api._async_post.call_args.args[1]
        == "http://192.0.2.1/cgi-bin/luci/;stok=token/admin/client"
    )
    assert api._async_post.call_args.kwargs["params"] == {"form": "black_list"}


@pytest.mark.asyncio
async def test_app_handler_can_acknowledge_a_plain_data_result():
    """App forms may use data as the result property outside encryption."""
    api = make_api()
    api._async_post.return_value = {
        "success": True,
        "errorcode": 0,
        "data": {"owners": []},
    }
    assert await api.async_request(
        "smart_network", "patrol_owner", "list", {}, read_only=True
    ) == {"owners": []}


@pytest.mark.parametrize(
    "data",
    [
        {"error_code": 1},
        {"errorcode": "1"},
        {"error_code": "0", "errorcode": 7},
        {"success": False},
    ],
)
def test_router_rejection_is_not_success(data):
    """Both dispatcher error conventions and explicit rejection are checked."""
    with pytest.raises(UnexpectedApiException):
        check_data_error_code("test", data)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, [], {"data": {"nothing": "here"}}])
async def test_unknown_response_is_not_acknowledgement(response):
    """A malformed response must never be reported as an accepted write."""
    api = make_api()
    api._async_post.return_value = response
    with pytest.raises(UnexpectedApiException):
        await api.async_request("wireless", "wlan", "write", {})


@pytest.mark.asyncio
async def test_transport_failure_retries_only_reads():
    """Read recovery remains available, but writes cannot be duplicated."""
    api = make_api()
    api._async_post.side_effect = [TransientConnectionException(), {"result": {}}]
    assert await api.async_request("wireless", "wlan", "read", {}, read_only=True) == {}
    assert api._async_post.call_count == 2
    api._async_post.reset_mock(side_effect=True)
    api._async_post.side_effect = TransientConnectionException()
    with pytest.raises(TransientConnectionException):
        await api.async_request("wireless", "wlan", "write", {})
    assert api._async_post.call_count == 1


@pytest.mark.asyncio
async def test_reboot_is_single_send_and_accepts_bare_ack():
    """The legacy reboot route supports modern acknowledgement shapes too."""
    api = make_api()
    api._async_post.return_value = {"error_code": 0}
    await api.async_reboot_decos(["02-11-22-33-44-55"])
    assert api._encode_payload.call_args.args[0] == {
        "operation": "reboot",
        "params": {"mac_list": [{"mac": "02-11-22-33-44-55"}]},
    }
    api._async_post.reset_mock()
    api._async_post.side_effect = ForbiddenException()
    with pytest.raises(ForbiddenException):
        await api.async_reboot_decos(["02-11-22-33-44-55"])
    assert api._async_post.call_count == 1


@pytest.mark.asyncio
async def test_session_lock_serializes_polling_and_commands():
    """A control cannot overlap the session-critical part of a polling read."""
    api = make_api()
    active = 0
    peak = 0

    async def post(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return {"error_code": 0}

    api._async_post.side_effect = post
    await asyncio.gather(
        *(
            api.async_request("wireless", "wlan", "read", {}, read_only=True)
            for _ in range(5)
        ),
        api.async_reboot_decos(["02-11-22-33-44-55"])
    )
    assert peak == 1


@pytest.mark.asyncio
async def test_invalid_controller_rejected_before_network():
    """Local paths cannot escape the integration's fixed router origin."""
    api = make_api()
    with pytest.raises(ValueError):
        await api.async_request("../administration", "account", "write", {})
    api._async_post.assert_not_called()
