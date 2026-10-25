"""
Custom integration to integrate TP-Link Deco with Home Assistant.

For more details about this integration, please refer to
https://github.com/amosyuen/ha-tplink-deco
"""

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.device_tracker.const import (
    DOMAIN as DEVICE_TRACKER_DOMAIN,
)
from homeassistant.components.device_tracker.const import CONF_CONSIDER_HOME
from homeassistant.components.device_tracker.const import CONF_SCAN_INTERVAL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry
from homeassistant.helpers import llm as ha_llm
from homeassistant.helpers import restore_state
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry

from .api import TplinkDecoApi
from .api import normalize_name
from .const import ATTR_DEVICE_TYPE
from .const import CONF_CLIENT_POSTFIX
from .const import CONF_CLIENT_PREFIX
from .const import CONF_DECO_POSTFIX
from .const import CONF_DECO_PREFIX
from .const import CONF_ENABLE_AGENT_CONTROL
from .const import CONF_LINK_CLIENT_DEVICES
from .const import CONF_TIMEOUT_ERROR_RETRIES
from .const import CONF_TIMEOUT_SECONDS
from .const import CONF_VERIFY_SSL
from .const import COORDINATOR_CLIENTS_KEY
from .const import COORDINATOR_DECOS_KEY
from .const import DEFAULT_CONSIDER_HOME
from .const import DEFAULT_DECO_POSTFIX
from .const import DEFAULT_LINK_CLIENT_DEVICES
from .const import DEFAULT_SCAN_INTERVAL
from .const import DEFAULT_TIMEOUT_ERROR_RETRIES
from .const import DEFAULT_TIMEOUT_SECONDS
from .const import DEVICE_TYPE_DECO
from .const import DOMAIN
from .const import PLATFORMS
from .control import DecoControl
from .coordinator import TpLinkDeco
from .coordinator import TpLinkDecoClient
from .coordinator import TpLinkDecoData
from .coordinator import TplinkDecoClientUpdateCoordinator
from .coordinator import TplinkDecoUpdateCoordinator
from .llm import DecoAPI
from .services import CONTROL_KEY
from .services import async_register_services
from .services import async_unregister_services

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_create_and_refresh_coordinators(
    hass: HomeAssistant,
    config_data: dict[str:Any],
    config_entry: ConfigEntry = None,
    consider_home_seconds=1,
    update_interval: timedelta = None,
    deco_data: TpLinkDecoData = None,
    client_data: dict[str:TpLinkDecoClient] = None,
):
    host = config_data.get(CONF_HOST)
    username = config_data.get(CONF_USERNAME)
    password = config_data.get(CONF_PASSWORD)
    timeout_error_retries = config_data.get(CONF_TIMEOUT_ERROR_RETRIES)
    timeout_seconds = config_data.get(CONF_TIMEOUT_SECONDS)
    verify_ssl = config_data.get(CONF_VERIFY_SSL)
    session = async_get_clientsession(hass)

    api = TplinkDecoApi(
        session,
        host,
        username,
        password,
        verify_ssl,
        timeout_error_retries,
        timeout_seconds,
    )
    deco_coordinator = TplinkDecoUpdateCoordinator(
        hass, api, config_entry, update_interval, deco_data
    )
    if config_entry is None:
        await deco_coordinator._async_update_data()
    else:
        await deco_coordinator.async_config_entry_first_refresh()
    clients_coordinator = TplinkDecoClientUpdateCoordinator(
        hass,
        api,
        config_entry,
        deco_coordinator,
        consider_home_seconds,
        update_interval,
        client_data,
    )
    return {
        COORDINATOR_DECOS_KEY: deco_coordinator,
        COORDINATOR_CLIENTS_KEY: clients_coordinator,
        CONTROL_KEY: DecoControl(api),
    }


async def async_create_config_data(hass: HomeAssistant, config_entry: ConfigEntry):
    consider_home_seconds = config_entry.data.get(
        CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME
    )
    scan_interval_seconds = config_entry.data.get(
        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    )
    update_interval = timedelta(seconds=scan_interval_seconds)

    # Load tracked entities from registry
    existing_entries = entity_registry.async_entries_for_config_entry(
        entity_registry.async_get(hass),
        config_entry.entry_id,
    )
    deco_data = TpLinkDecoData()
    client_data = {}

    # Populate client list with existing entries so that we keep track of disconnected clients
    # since deco list_clients only returns connected clients.
    restore_state_data = restore_state.async_get(hass)
    # async_get_stored_state is available starting with Home Assistant 2026.11.
    get_stored_state = getattr(restore_state_data, "async_get_stored_state", None)
    if get_stored_state is None:
        get_stored_state = restore_state_data.last_states.get
    for entry in existing_entries:
        if entry.domain != DEVICE_TRACKER_DOMAIN:
            continue
        state = get_stored_state(entry.entity_id)
        if state is None:
            continue
        device_type = state.state.attributes.get(ATTR_DEVICE_TYPE)
        if device_type is None:
            continue
        if device_type == DEVICE_TYPE_DECO:
            deco = TpLinkDeco(entry.unique_id)
            deco.name = normalize_name(entry.original_name)
            deco_data.decos[entry.unique_id] = deco
        else:
            client = TpLinkDecoClient(entry.unique_id)
            client.name = normalize_name(entry.original_name)
            client_data[entry.unique_id] = client

    return await async_create_and_refresh_coordinators(
        hass,
        config_entry.data,
        config_entry,
        consider_home_seconds,
        update_interval,
        deco_data,
        client_data,
    )


async def async_pause_polling(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Pause Deco polling."""
    coordinator_decos = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]

    if coordinator_decos.paused:
        _LOGGER.info("TP-Link Deco polling is already paused")
        return

    coordinator_decos.paused = True
    coordinator_decos.async_set_updated_data(coordinator_decos.data)
    _LOGGER.info("TP-Link Deco polling paused")


async def async_resume_polling(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Resume Deco polling."""
    coordinator_decos = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]

    if not coordinator_decos.paused:
        _LOGGER.info("TP-Link Deco polling is already running")
        return

    coordinator_decos.paused = False
    coordinator_decos.async_set_updated_data(coordinator_decos.data)
    _LOGGER.info("TP-Link Deco polling resumed")
    await coordinator_decos.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Set up this integration using UI."""
    _LOGGER.debug("async_setup_entry: Config entry %s", config_entry.entry_id)

    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})

    data = await async_create_config_data(hass, config_entry)
    hass.data[DOMAIN][config_entry.entry_id] = data
    clients_coordinator = data[COORDINATOR_CLIENTS_KEY]

    # Preserve the original polling entity ID while making its unique ID per-entry.
    registry = entity_registry.async_get(hass)
    for entry in entity_registry.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        if entry.domain == "switch" and entry.unique_id == "tplink_deco_polling":
            registry.async_update_entity(
                entry.entity_id, new_unique_id=f"{config_entry.entry_id}_polling"
            )

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    config_entry.async_create_background_task(
        hass,
        clients_coordinator.async_request_refresh(),
        name="tplink_deco initial client refresh",
    )

    async_register_services(hass)
    if config_entry.data.get(CONF_ENABLE_AGENT_CONTROL, False):
        config_entry.async_on_unload(
            ha_llm.async_register_api(hass, DecoAPI(hass, config_entry))
        )

    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    _LOGGER.debug("async_unload_entry: Config entry %s", config_entry.entry_id)

    domain_data = hass.data.get(DOMAIN, {})
    data = domain_data.get(config_entry.entry_id)

    if data is None:
        _LOGGER.debug(
            "async_unload_entry: No stored data for config entry %s",
            config_entry.entry_id,
        )
        return True

    deco_coordinator = data.get(COORDINATOR_DECOS_KEY)
    clients_coordinator = data.get(COORDINATOR_CLIENTS_KEY)

    if deco_coordinator is not None:
        await deco_coordinator.async_close()
    if clients_coordinator is not None:
        await clients_coordinator.async_close()

    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(config_entry, platform)
                for platform in PLATFORMS
            ]
        )
    )

    if unloaded:
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
        async_unregister_services(hass)

    return unloaded


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.debug("update_listener: Reloading %s", config_entry.entry_id)
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removal of old client devices when MAC linking is disabled."""
    if config_entry.data.get(CONF_LINK_CLIENT_DEVICES, DEFAULT_LINK_CLIENT_DEVICES):
        return False
    return not any(domain == DOMAIN for domain, _ in device_entry.identifiers)


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    new = {**config_entry.data}
    version = config_entry.version

    if version == 1:
        version = 2
        new[CONF_VERIFY_SSL] = True

    if version == 2:
        version = 3
        new[CONF_TIMEOUT_ERROR_RETRIES] = DEFAULT_TIMEOUT_ERROR_RETRIES

    if version == 3:
        version = 4
        new[CONF_TIMEOUT_SECONDS] = DEFAULT_TIMEOUT_SECONDS

    if version == 4:
        version = 5
        new[CONF_CLIENT_PREFIX] = ""
        new[CONF_CLIENT_POSTFIX] = ""
        new[CONF_DECO_PREFIX] = ""
        new[CONF_DECO_POSTFIX] = DEFAULT_DECO_POSTFIX

    if version == 5:
        version = 6
        new[CONF_HOST] = f"http://{new[CONF_HOST]}"

    if version == 6:
        version = 7
        new[CONF_LINK_CLIENT_DEVICES] = DEFAULT_LINK_CLIENT_DEVICES

    hass.config_entries.async_update_entry(config_entry, data=new, version=version)

    _LOGGER.info("Migration to version %s successful", version)

    return True
