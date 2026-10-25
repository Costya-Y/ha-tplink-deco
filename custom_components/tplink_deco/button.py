"""Per-node reboot buttons for Home Assistant and exposed Assist/MCP control."""

from homeassistant.components.button import ButtonDeviceClass
from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .const import SIGNAL_DECO_ADDED
from .device import create_device_info
from .services import async_execute


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Create buttons for this mesh and any subsequently discovered nodes."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR_DECOS_KEY]
    added = set()

    @callback
    def add_nodes():
        new_macs = set(coordinator.data.decos) - added
        async_add_entities(
            [DecoRebootButton(coordinator, config_entry, mac) for mac in new_macs]
        )
        added.update(new_macs)

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DECO_ADDED, add_nodes)
    )
    # Device-added signals are sent before coordinator.data is replaced. The
    # coordinator listener also runs after replacement and catches new nodes.
    config_entry.async_on_unload(coordinator.async_add_listener(add_nodes))
    add_nodes()


class DecoRebootButton(CoordinatorEntity, ButtonEntity):
    """Reboot one known Deco; preserve the integration's existing session."""

    _attr_has_entity_name = True
    _attr_name = "Reboot"
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, coordinator, config_entry, mac):
        """Bind a button to its entry and node MAC."""
        super().__init__(coordinator)
        self.entry_id = config_entry.entry_id
        self.mac = mac
        self._attr_unique_id = f"{self.entry_id}_{mac}_reboot"

    @property
    def device_info(self):
        """Attach the button to its actual Deco node."""
        return create_device_info(
            self.coordinator.data.decos.get(self.mac),
            self.coordinator.data.master_deco,
            self.coordinator,
        )

    @property
    def available(self):
        """Only offer the button while its node is still present and online."""
        node = self.coordinator.data.decos.get(self.mac)
        return super().available and node is not None and bool(node.online)

    async def async_press(self):
        """Send one reboot request; never replay a lost acknowledgement."""
        await async_execute(self.hass, self.entry_id, "reboot", {"mac": self.mac})
