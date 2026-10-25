"""Home Assistant actions with explicit entry routing and shared validation."""

from functools import partial
import logging

from homeassistant.core import HomeAssistant
from homeassistant.core import ServiceCall
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import service as service_helper
import probatio as vol

from .const import COORDINATOR_CLIENTS_KEY
from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .control import BANDS
from .control import FEATURES
from .control import catalogue
from .control import normalize_mac
from .control import parse_params
from .control import redact

_LOGGER = logging.getLogger(__name__)
CONTROL_KEY = "control"

ACTION_FIELDS = {
    "query": {
        vol.Required("feature"): vol.In(["catalogue", "inventory", *FEATURES]),
        vol.Optional("operation"): str,
        vol.Optional("params_json", default="{}"): str,
    },
    "configure": {
        vol.Required("feature"): vol.In(FEATURES),
        vol.Required("operation"): str,
        vol.Optional("params_json", default="{}"): str,
    },
    "block_client": {
        vol.Required("mac"): str,
        vol.Required("name"): str,
        vol.Optional("client_type", default="other"): str,
    },
    "unblock_client": {vol.Required("mac"): str},
    "rename_client": {
        vol.Required("mac"): str,
        vol.Required("name"): str,
        vol.Optional("client_type"): str,
    },
    "set_reservation": {vol.Required("mac"): str, vol.Required("ip"): str},
    "remove_reservation": {vol.Required("mac"): str},
    "set_wifi": {
        vol.Required("network"): vol.In(["main", "guest", "iot", "mlo"]),
        vol.Required("enabled"): bool,
        vol.Optional("band"): vol.In(BANDS),
    },
    "reboot": {vol.Required("mac"): str},
    "set_polling": {vol.Required("enabled"): bool},
}
ACTION_DESCRIPTIONS = {
    "query": "Read a Deco feature. Use catalogue for documented operations (not detected support), inventory for cached mesh/firmware/clients. Credentials are redacted.",
    "configure": "Apply a documented Deco operation using params_json. Read the feature first. Firmware-specific payloads are required. May disrupt connectivity. An accepted reply is not verification; query the result. Never automatically repeat a failed write.",
    "block_client": "Block internet access for one client MAC; name labels the entry. Reads back the blocklist to verify. Can disconnect devices and agents.",
    "unblock_client": "Unblock one client MAC and read back the blocklist to verify.",
    "rename_client": "Rename one known client, optionally changing its client_type. Acknowledgement only; query clients to verify.",
    "set_reservation": "Add or change a fixed IPv4 reservation for a MAC; read back the reservation table to verify.",
    "remove_reservation": "Remove a fixed IPv4 reservation for a MAC; read back the table to verify.",
    "set_wifi": "Enable or disable main, guest, IoT or MLO Wi-Fi. Specify band for main/guest; omit it for IoT/MLO. Changes may disconnect HA or agents. Acknowledgement only; query wlan to verify.",
    "reboot": "Reboot exactly one Deco node by MAC from inventory. Interrupts connectivity. Sends once; no automatic retry.",
    "set_polling": "Pause or resume integration polling for this mesh.",
}


def resolve_entry(hass: HomeAssistant, entry_id: str | None) -> str:
    """Resolve a loaded mesh and reject ambiguous default targeting."""
    entries = hass.data.get(DOMAIN, {})
    if entry_id is None and len(entries) == 1:
        return next(iter(entries))
    if entry_id not in entries:
        raise ServiceValidationError("Specify entry_id for a loaded Deco integration")
    return entry_id


def inventory(hass: HomeAssistant, entry_id: str) -> dict:
    """Return current cached inventory and freshness, without router credentials."""
    data = hass.data[DOMAIN][entry_id]
    coordinator = data[COORDINATOR_DECOS_KEY]
    clients = data[COORDINATOR_CLIENTS_KEY]
    return {
        "entry_id": entry_id,
        "polling": not coordinator.paused,
        "last_update_success": coordinator.last_update_success,
        "last_successful_update": (
            coordinator.health.last_successful_update.isoformat()
            if coordinator.health.last_successful_update
            else None
        ),
        "features": catalogue(),
        "nodes": [
            {
                "mac": node.mac,
                "name": node.name,
                "model": node.device_model,
                "hardware_version": node.hw_version,
                "firmware": node.sw_version,
                "online": node.online,
                "master": node.master,
            }
            for node in coordinator.data.decos.values()
        ],
        "clients": [
            {
                "mac": client.mac,
                "name": client.name,
                "ip": client.ip_address,
                "online": client.online,
                "deco_mac": client.deco_mac,
            }
            for client in clients.data.values()
        ],
    }


async def async_execute(
    hass: HomeAssistant, entry_id: str, action: str, args: dict
) -> dict:
    """Execute a validated action against exactly one loaded mesh."""
    if action not in ACTION_FIELDS:
        raise ServiceValidationError("Unknown Deco action")
    args = vol.Schema(ACTION_FIELDS[action])(args)
    entry_id = resolve_entry(hass, entry_id)
    data = hass.data[DOMAIN][entry_id]
    coordinator = data[COORDINATOR_DECOS_KEY]
    control = data[CONTROL_KEY]
    try:
        if action == "query":
            feature = args["feature"]
            if feature == "catalogue":
                return {"features": catalogue()}
            if feature == "inventory":
                return inventory(hass, entry_id)
            result = await control.async_query(
                feature, args.get("operation"), parse_params(args["params_json"])
            )
            return {"feature": feature, "result": redact(result)}
        if action == "configure":
            return await control.async_configure(
                args["feature"], args["operation"], parse_params(args["params_json"])
            )
        if action in ("block_client", "unblock_client"):
            return await control.async_block_client(
                args["mac"],
                action == "block_client",
                args.get("name", ""),
                args.get("client_type", "other"),
            )
        if action == "rename_client":
            return await control.async_rename_client(**args)
        if action in ("set_reservation", "remove_reservation"):
            return await control.async_set_reservation(args["mac"], args.get("ip"))
        if action == "set_wifi":
            return await control.async_set_wifi(**args)
        if action == "set_polling":
            coordinator.paused = not args["enabled"]
            coordinator.async_set_updated_data(coordinator.data)
            if args["enabled"]:
                await coordinator.async_request_refresh()
                await data[COORDINATOR_CLIENTS_KEY].async_request_refresh()
            return {"status": "confirmed", "polling": not coordinator.paused}
        mac = normalize_mac(args["mac"])
        node_mac = next(
            (key for key in coordinator.data.decos if normalize_mac(key) == mac), None
        )
        if node_mac is None:
            raise ValueError("MAC does not identify a Deco node in this integration")
        await coordinator.api.async_reboot_decos([node_mac])
        return {"status": "accepted", "verified": False, "mac": mac}
    except (ValueError, KeyError, TypeError) as err:
        raise ServiceValidationError(str(err)) from err
    except Exception as err:
        # Transport errors may embed a session URL. Do not expose their text.
        _LOGGER.warning("Deco %s failed (%s)", action, type(err).__name__)
        message = (
            "Deco read failed"
            if action == "query"
            else "Deco command or verification failed; outcome may be unknown. Query current state before retrying"
        )
        raise ServiceValidationError(f"{message} ({type(err).__name__})") from None


async def _handle(hass: HomeAssistant, action: str, call: ServiceCall):
    """Resolve an action's entry and return its result to HA."""
    args = dict(call.data)
    entry_id = resolve_entry(hass, args.pop("entry_id", None))
    return await async_execute(hass, entry_id, action, args)


async def _legacy_reboot(hass: HomeAssistant, call: ServiceCall):
    """Route the existing device-id reboot action without last-entry capture."""
    targets = []
    registry = dr.async_get(hass)
    for device_id in call.data["device_id"]:
        device = registry.async_get(device_id)
        identifiers = (
            []
            if device is None
            else [mac for domain, mac in device.identifiers if domain == DOMAIN]
        )
        matches = [
            (entry_id, mac)
            for entry_id, data in hass.data[DOMAIN].items()
            for mac in identifiers
            if any(
                normalize_mac(node_mac) == normalize_mac(mac)
                for node_mac in data[COORDINATOR_DECOS_KEY].data.decos
            )
        ]
        if len(matches) != 1:
            raise ServiceValidationError(
                "Every device_id must identify one loaded Deco node"
            )
        targets.append(matches[0])
    # Validate every target before sending any request; deduplicate duplicates.
    for entry_id, mac in dict.fromkeys(targets):
        await async_execute(hass, entry_id, "reboot", {"mac": mac})


async def _legacy_polling(hass: HomeAssistant, enabled: bool, call: ServiceCall):
    """Preserve existing pause/resume action names with explicit entry routing."""
    await async_execute(
        hass,
        resolve_entry(hass, call.data.get("entry_id")),
        "set_polling",
        {"enabled": enabled},
    )


def async_register_services(hass: HomeAssistant) -> None:
    """Register once per domain, independent of any individual config entry."""
    if hass.services.has_service(DOMAIN, "query"):
        return
    for action, fields in ACTION_FIELDS.items():
        schema = vol.Schema({vol.Optional("entry_id"): str, **fields})
        service_helper.async_register_admin_service(
            hass,
            DOMAIN,
            action,
            partial(_handle, hass, action),
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )
    service_helper.async_register_admin_service(
        hass,
        DOMAIN,
        "reboot_deco",
        partial(_legacy_reboot, hass),
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.All(
                    lambda value: value if isinstance(value, list) else [value],
                    [str],
                    vol.Length(min=1),
                )
            }
        ),
    )
    for name, enabled in (("pause_polling", False), ("resume_polling", True)):
        service_helper.async_register_admin_service(
            hass,
            DOMAIN,
            name,
            partial(_legacy_polling, hass, enabled),
            schema=vol.Schema({vol.Optional("entry_id"): str}),
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove domain services only after the last entry has unloaded."""
    if hass.data.get(DOMAIN):
        return
    for name in (*ACTION_FIELDS, "reboot_deco", "pause_polling", "resume_polling"):
        hass.services.async_remove(DOMAIN, name)
