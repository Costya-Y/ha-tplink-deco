# Integration review

The reviewed installation uses the `amosyuen/ha-tplink-deco` integration
3.10.6. This fork starts from upstream main at the time of development and
preserves the existing integration domain and config-entry version.

Findings addressed:

- Services were registered inside each entry's setup and captured that
  entry's coordinator. Loading another entry could redirect operations to
  the last loaded mesh. Services now resolve the selected entry at call time.
- Reboot validation assumed a device had exactly one identifier, then
  indexed `None` when it did not. Target resolution now tolerates other
  integration identifiers, validates every target before sending any reboot,
  and checks it belongs to a currently loaded Deco mesh.
- Unloading any entry removed polling services for remaining entries and
  left the reboot service registered after the last unload. Registration and
  cleanup now follow the domain's loaded-entry lifetime.
- Polling switches used the same unique ID for every mesh. New IDs include
  the config entry; upgrading migrates the existing entity while retaining
  its HA entity ID. The switch now subscribes to coordinator updates so
  service-driven polling changes update its state.
- The installed MCP connection exposed a polling switch but no integration
  management or arbitrary action API. A separate, opt-in Deco LLM API now
  exposes administrator actions through HA's native authenticated MCP server.
- Existing polling/tracking was not a full configuration API. This fork adds
  typed client, reservation and Wi-Fi actions and a documented, restricted
  advanced endpoint catalogue. Firmware support must still be established
  by actual reads/writes; a catalogue entry is not a detected capability.
- New controls and reboot parsing accept bare acknowledgements and encrypted
  results. Mutating requests are never automatically replayed, and successful
  readback is distinguished from acknowledgement.

The BE25 and BE67 firmware variants have not been certified for every
catalogued operation. Unit tests cannot establish hardware support. No
network configuration, router reboot, firmware update or factory reset is
needed for the source review or unit suite.
