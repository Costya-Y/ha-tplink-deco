"""Regression tests for MCP authorization and multi-mesh action routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from homeassistant.core import Context
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import llm
import probatio
import pytest

from custom_components.tplink_deco.const import CONF_ENABLE_AGENT_CONTROL
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.llm import DecoAPI
from custom_components.tplink_deco.llm import DecoTool
from custom_components.tplink_deco.services import ACTION_FIELDS
from custom_components.tplink_deco.services import CONTROL_KEY
from custom_components.tplink_deco.services import _handle
from custom_components.tplink_deco.services import _legacy_reboot
from custom_components.tplink_deco.services import async_execute
from custom_components.tplink_deco.services import async_register_services
from custom_components.tplink_deco.services import async_unregister_services
from custom_components.tplink_deco.services import resolve_entry

FIRST_MAC = "02-11-22-33-44-55"
SECOND_MAC = "02-11-22-33-44-66"


def make_mesh(mac):
    """Give each mesh independent API calls and observable polling state."""
    coordinator = SimpleNamespace(
        paused=False,
        data=SimpleNamespace(decos={mac: SimpleNamespace(mac=mac)}),
        api=SimpleNamespace(async_reboot_decos=AsyncMock()),
        async_set_updated_data=MagicMock(),
        async_request_refresh=AsyncMock(),
    )
    return {
        COORDINATOR_DECOS_KEY: coordinator,
        COORDINATOR_CLIENTS_KEY: SimpleNamespace(async_request_refresh=AsyncMock()),
        CONTROL_KEY: SimpleNamespace(
            async_query=AsyncMock(return_value={"password": "router-secret"})
        ),
    }


@pytest.fixture
def hass():
    """Model only the HA interfaces needed by authorization and routing."""
    entries = {
        entry_id: SimpleNamespace(
            entry_id=entry_id,
            title=entry_id,
            data={CONF_ENABLE_AGENT_CONTROL: True},
        )
        for entry_id in ("first", "second")
    }
    return SimpleNamespace(
        data={DOMAIN: {"first": make_mesh(FIRST_MAC), "second": make_mesh(SECOND_MAC)}},
        entries=entries,
        config_entries=SimpleNamespace(
            async_get_entry=MagicMock(side_effect=entries.get)
        ),
        auth=SimpleNamespace(
            async_get_user=AsyncMock(
                return_value=SimpleNamespace(is_admin=True, is_active=True)
            )
        ),
        services=SimpleNamespace(async_remove=MagicMock()),
    )


def make_context(user_id="admin"):
    """Use the real HA context shape passed through its MCP transports."""
    return llm.LLMContext(
        platform="mcp_server",
        context=Context(user_id=user_id),
        language="en",
        assistant="conversation",
        device_id=None,
    )


def polling_input():
    """Request a state change whose routing is observable without a router."""
    return llm.ToolInput(tool_name="DecoSetPolling", tool_args={"enabled": False})


@pytest.mark.asyncio
async def test_admin_tool_uses_its_bound_mesh(hass):
    """An admin tool changes the intended mesh even with another mesh loaded."""
    result = await DecoTool("second", "set_polling").async_call(
        hass, polling_input(), make_context()
    )
    assert isinstance(result, llm.ToolResult)
    assert result.data == {"status": "confirmed", "polling": False}
    assert hass.data[DOMAIN]["second"][COORDINATOR_DECOS_KEY].paused
    assert not hass.data[DOMAIN]["first"][COORDINATOR_DECOS_KEY].paused
    hass.auth.async_get_user.assert_awaited_once_with("admin")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        None,
        SimpleNamespace(is_admin=False, is_active=True),
        SimpleNamespace(is_admin=True, is_active=False),
    ],
)
async def test_tool_rejects_missing_nonadmin_and_inactive_users(hass, user):
    """Possessing an MCP tool description does not grant router access."""
    hass.auth.async_get_user.return_value = user
    with patch(
        "custom_components.tplink_deco.llm.async_execute", new_callable=AsyncMock
    ) as execute:
        with pytest.raises(HomeAssistantError, match="authenticated.*administrator"):
            await DecoTool("first", "set_polling").async_call(
                hass, polling_input(), make_context()
            )
        execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_context", [False, True])
async def test_tool_requires_authenticated_context(hass, missing_context):
    """System or absent contexts cannot silently bypass MCP admin checks."""
    context = make_context(user_id=None)
    if missing_context:
        context.context = None
    with patch(
        "custom_components.tplink_deco.llm.async_execute", new_callable=AsyncMock
    ) as execute:
        with pytest.raises(HomeAssistantError, match="authenticated.*administrator"):
            await DecoTool("first", "set_polling").async_call(
                hass, polling_input(), context
            )
        execute.assert_not_awaited()
    hass.auth.async_get_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [False, True])
async def test_cached_tool_rechecks_opt_in_and_entry_existence(hass, removed):
    """Disabling or removing a mesh revokes a previously acquired tool."""
    context = make_context()
    api = DecoAPI(hass, hass.entries["first"])
    instance = await api.async_get_api_instance(context)
    tool = next(tool for tool in instance.tools if tool.name == "DecoSetPolling")
    if removed:
        del hass.entries["first"]
    else:
        hass.entries["first"].data[CONF_ENABLE_AGENT_CONTROL] = False
    with patch(
        "custom_components.tplink_deco.llm.async_execute", new_callable=AsyncMock
    ) as execute:
        with pytest.raises(HomeAssistantError, match="agent control is disabled"):
            await tool.async_call(hass, polling_input(), context)
        execute.assert_not_awaited()
    hass.auth.async_get_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_validates_inputs_before_dispatch(hass):
    """MCP arguments cannot override a bound entry or omit required fields."""
    tool = DecoTool("first", "set_polling")
    with patch(
        "custom_components.tplink_deco.llm.async_execute", new_callable=AsyncMock
    ) as execute:
        for arguments in (
            {},
            {"enabled": "false"},
            {"enabled": False, "entry_id": "second"},
        ):
            with pytest.raises(probatio.Invalid):
                await tool.async_call(
                    hass,
                    llm.ToolInput(tool_name=tool.name, tool_args=arguments),
                    make_context(),
                )
        execute.assert_not_awaited()


@pytest.mark.parametrize("action", ACTION_FIELDS)
def test_every_tool_schema_serializes_for_native_mcp(action):
    """Exercise the native MCP server's schema codec for every advertised tool."""
    schema = probatio.to_openapi(
        DecoTool("first", action).parameters, openapi_version="3.1.0"
    )
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert "entry_id" not in schema["properties"]
    assert schema.get("required")


@pytest.mark.asyncio
async def test_registered_action_routes_explicit_entry_and_redacts_response(hass):
    """Action routing reads the call target and never captures the last mesh."""
    call = SimpleNamespace(data={"entry_id": "second", "feature": "wlan"})
    result = await _handle(hass, "query", call)
    assert result == {"feature": "wlan", "result": {"password": "**REDACTED**"}}
    hass.data[DOMAIN]["first"][CONTROL_KEY].async_query.assert_not_awaited()
    hass.data[DOMAIN]["second"][CONTROL_KEY].async_query.assert_awaited_once_with(
        "wlan", None, {}
    )


@pytest.mark.asyncio
async def test_ambiguous_default_and_unloaded_entry_fail_without_call(hass):
    """A missing or stale target cannot fall through to another mesh."""
    for entry_id in (None, "unloaded"):
        with pytest.raises(ServiceValidationError, match="Specify entry_id"):
            resolve_entry(hass, entry_id)
    with pytest.raises(ServiceValidationError, match="Specify entry_id"):
        await _handle(hass, "query", SimpleNamespace(data={"feature": "wlan"}))
    for mesh in hass.data[DOMAIN].values():
        mesh[CONTROL_KEY].async_query.assert_not_awaited()
    del hass.data[DOMAIN]["second"]
    assert resolve_entry(hass, None) == "first"


@pytest.mark.asyncio
async def test_reboot_mac_must_belong_to_selected_entry(hass):
    """A node found in another mesh is not a valid reboot target."""
    with pytest.raises(ServiceValidationError, match="does not identify a Deco node"):
        await async_execute(hass, "first", "reboot", {"mac": SECOND_MAC})
    for mesh in hass.data[DOMAIN].values():
        mesh[COORDINATOR_DECOS_KEY].api.async_reboot_decos.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_reboot_accepts_extra_identifiers_and_deduplicates(hass):
    """Registry devices may have identifiers from more than one integration."""
    devices = {
        "node-one": SimpleNamespace(
            identifiers={(DOMAIN, FIRST_MAC), ("other", "id-one")}
        ),
        "node-two": SimpleNamespace(
            identifiers={(DOMAIN, SECOND_MAC.replace("-", ":")), ("other", "id-two")}
        ),
    }
    registry = SimpleNamespace(async_get=MagicMock(side_effect=devices.get))
    with patch(
        "custom_components.tplink_deco.services.dr.async_get", return_value=registry
    ):
        await _legacy_reboot(
            hass,
            SimpleNamespace(data={"device_id": ["node-one", "node-two", "node-one"]}),
        )
    hass.data[DOMAIN]["first"][
        COORDINATOR_DECOS_KEY
    ].api.async_reboot_decos.assert_awaited_once_with([FIRST_MAC])
    hass.data[DOMAIN]["second"][
        COORDINATOR_DECOS_KEY
    ].api.async_reboot_decos.assert_awaited_once_with([SECOND_MAC])


@pytest.mark.asyncio
async def test_legacy_reboot_validates_entire_batch_before_sending(hass):
    """A later invalid target prevents the earlier valid node from rebooting."""
    registry = SimpleNamespace(
        async_get=MagicMock(
            side_effect=lambda device_id: (
                SimpleNamespace(identifiers={(DOMAIN, FIRST_MAC)})
                if device_id == "valid"
                else None
            )
        )
    )
    with patch(
        "custom_components.tplink_deco.services.dr.async_get", return_value=registry
    ), pytest.raises(ServiceValidationError, match="Every device_id"):
        await _legacy_reboot(
            hass, SimpleNamespace(data={"device_id": ["valid", "missing"]})
        )
    for mesh in hass.data[DOMAIN].values():
        mesh[COORDINATOR_DECOS_KEY].api.async_reboot_decos.assert_not_awaited()


def test_actions_register_once_with_admin_wrapper(hass):
    """Additional meshes cannot replace domain actions with entry closures."""
    registrations = {}

    def register(_hass, domain, name, handler, **kwargs):
        registrations[name] = (domain, handler, kwargs)

    hass.services.has_service = lambda domain, name: name in registrations
    with patch(
        "custom_components.tplink_deco.services.service_helper.async_register_admin_service",
        side_effect=register,
    ) as register_admin:
        async_register_services(hass)
        count = register_admin.call_count
        async_register_services(hass)
        assert register_admin.call_count == count == len(ACTION_FIELDS) + 3
    for name in ACTION_FIELDS:
        domain, _, options = registrations[name]
        assert domain == DOMAIN
        assert options["supports_response"] == SupportsResponse.OPTIONAL
    assert set(registrations) == {
        *ACTION_FIELDS,
        "reboot_deco",
        "pause_polling",
        "resume_polling",
    }


def test_domain_actions_survive_until_last_mesh_unloads(hass):
    """Unloading one entry cannot remove controls used by another mesh."""
    del hass.data[DOMAIN]["first"]
    async_unregister_services(hass)
    hass.services.async_remove.assert_not_called()
    hass.data[DOMAIN].clear()
    async_unregister_services(hass)
    removed = {call.args for call in hass.services.async_remove.call_args_list}
    assert removed == {
        (DOMAIN, name)
        for name in (*ACTION_FIELDS, "reboot_deco", "pause_polling", "resume_polling")
    }
