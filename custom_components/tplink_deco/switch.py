from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .device import create_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up switch."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]

    async_add_entities([DecoPollingSwitch(coordinator, config_entry)])


class DecoPollingSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control Deco polling."""

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Polling"
        self._attr_unique_id = f"{config_entry.entry_id}_polling"
        self._attr_icon = "mdi:lan-connect"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach switch to the master Deco device."""
        master_deco = self.coordinator.data.master_deco
        return create_device_info(master_deco, master_deco, self.coordinator)

    @property
    def is_on(self) -> bool:
        """Return True if polling is active."""
        return not self.coordinator.paused

    async def async_turn_off(self, **kwargs):
        """Pause polling."""
        self.coordinator.paused = True
        self.coordinator.async_set_updated_data(self.coordinator.data)

    async def async_turn_on(self, **kwargs):
        """Resume polling."""
        self.coordinator.paused = False
        self.coordinator.async_set_updated_data(self.coordinator.data)
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()
