# Deco controls and MCP

This fork extends `amosyuen/ha-tplink-deco`. It requires Home Assistant
2026.10 or later. The intended target includes mixed BE25/BE67 meshes.
Support must be checked against the actual router firmware: the endpoint
catalogue is protocol documentation, not a claim that every feature exists
on every model. This is a prerelease until tested on the target hardware.
See [LIVE_VALIDATION.md](LIVE_VALIDATION.md) for the tested BE25/BE67 subset.

## Home Assistant

The existing integration entry, credentials, trackers and polling entity IDs
are retained. Each node gains a **Reboot** button. Exposing that button to
Assist makes it usable through the standard MCP press intent as well.

New administrator actions under `tplink_deco`:

| Action                                   | Purpose                                                 | Result                                              |
| ---------------------------------------- | ------------------------------------------------------- | --------------------------------------------------- |
| `query`                                  | Read inventory, the catalogue, or one router feature    | Fresh router read except cached inventory/catalogue |
| `configure`                              | Send a documented operation with firmware-specific JSON | Acknowledgement, not verified state                 |
| `block_client` / `unblock_client`        | Change a client's internet blocklist entry              | Reads back the blocklist                            |
| `rename_client`                          | Rename a client and optionally change its category      | Acknowledgement                                     |
| `set_reservation` / `remove_reservation` | Add, update or remove a DHCP reservation                | Reads back the table                                |
| `set_wifi`                               | Toggle main/guest per band, or mesh IoT/MLO             | Acknowledgement                                     |
| `reboot`                                 | Restart one node by MAC                                 | Acknowledgement                                     |
| `set_polling`                            | Pause or resume polling for one mesh                    | Local state confirmation                            |

`entry_id` is optional when exactly one Deco integration is loaded. Supply it
when there is more than one mesh. The original `reboot_deco` action still
accepts HA `device_id` values; `pause_polling` and `resume_polling` remain.
Actions are available to HA administrators and trusted local automations.

Read inventory from Developer Tools > Actions:

```yaml
action: tplink_deco.query
data:
  feature: inventory
```

Reserve an address (replace the example MAC and IP with your own):

```yaml
action: tplink_deco.set_reservation
data:
  mac: "02-11-22-33-44-55"
  ip: "192.168.68.50"
```

Enable guest Wi-Fi on one available band without changing its credentials:

```yaml
action: tplink_deco.set_wifi
data:
  network: guest
  band: band5_1
  enabled: true
```

For advanced settings, query the feature first. `params_json` is a JSON
object containing the router's exact `params`; SSIDs/passwords in advanced
Wi-Fi payloads use the router's base64 encoding. `set_wifi` handles the
documented enable flags and does not resend existing passwords. Example:

```yaml
action: tplink_deco.configure
data:
  feature: fast_roaming
  operation: write
  params_json: '{"enable": true}'
```

The catalogue covers Wi-Fi, fast roaming, beamforming, OFDMA, radio power,
MLO, schedules, channel optimization, LEDs, eco/time settings, speed tests,
clients, reservations, DHCP, WAN/LAN/IPv6/VLAN, UPnP, static routes, QoS,
local parental controls, port forwarding, DMZ, DDNS, VPN, IPv6 firewall and
IPTV. Some fields require app-specific support or a subscription. Unsupported
operations return a router error. No fallback guesses or cloud-account
changes are performed. Factory reset, firmware flashing, account/password
changes, cloud owner binding and debug/shell endpoints are not implemented.

## MCP agents

1. In the Deco integration's **Configure** options, enable **Enable Deco
   tools for administrator AI agents (MCP)**. This defaults to off.
2. In the **Model Context Protocol Server** integration's configuration,
   select the new **TP-Link Deco [entry title]** LLM API. If **Expose all LLM
   APIs** is enabled, it is included automatically. Keep administrator
   authentication required.
3. Reconnect/refresh the tools in your MCP client using the existing HA MCP
   connection. No new router credential, public port or standalone MCP server
   is required.

Ten tools are provided: `DecoQuery`, `DecoConfigure`, `DecoBlockClient`,
`DecoUnblockClient`, `DecoRenameClient`, `DecoSetReservation`,
`DecoRemoveReservation`, `DecoSetWifi`, `DecoReboot` and `DecoSetPolling`.
HA may namespace the names when multiple APIs are selected. Each API is bound
to one mesh; tools cannot select a different integration entry. An active
HA administrator identity and the current Deco opt-in are checked on every
call, including calls through previously created API instances. Entity
exposure controls for Assist do not limit this separate administrator API.

Ask the agent to query `inventory` and the relevant feature before making
changes. Router-provided names and strings are data, not instructions.
Returned credentials are redacted recursively. Advanced input values still
pass through HA's normal action/MCP processing: HA debug logging and
conversation traces may retain tool arguments. Do not submit passwords in
advanced tool calls unless that storage is acceptable.

## Results and failure handling

`status: accepted` means the router acknowledged the request. It does not
prove the change was applied. Query it to verify. `status: confirmed` with
`verified: true` for blocklist/reservation actions means the integration read
back the requested state. A failed verification can occur after a successful
write; inspect current state before issuing another command.

All requests share the integration's existing session and operation lock.
Compound control operations have an additional command lock. Reads may
recover/retry; writes and reboots are sent once. Losing a reply never triggers
an automatic second write. Rebooting, blocking clients, and changing Wi-Fi or
WAN/LAN configuration can disconnect HA and agents.

## Installation and rollback

Back up `config/custom_components/tplink_deco` before installing. Install this
fork through HACS as a custom integration, or replace that directory with the
directory from this repository. Both projects use the same integration domain;
install one implementation at a time. Restart HA to import the updated code.
Keep the existing integration entry instead of adding a duplicate.

To roll back, disable the Deco agent option, restore the previous component
directory and restart HA. No config-entry version migration is introduced by
this fork. The per-entry polling unique-ID fix preserves the polling entity ID
when upgrading; on rollback to upstream its old fixed unique ID may need to
be restored in the entity registry to avoid a duplicate polling entity.

## Development

```console
python -m pip install -r requirements.txt pycryptodome async-timeout
python -m pytest -o addopts='' -q tests
python -m black --check custom_components/tplink_deco tests
python -m isort --check-only custom_components/tplink_deco tests
python -m flake8 custom_components/tplink_deco tests
```

The unit suite uses documented response fixtures, injected failures and a
stateful reservation model. It does not contact real routers. Hardware
validation must be recorded separately from unit-test results.

Protocol reference: [tplink-deco-api endpoint documentation](https://github.com/roquerodrigo/tplink-deco-api/tree/2d4ddeae41b8d9c16a496ccd3fadeb3e9f4835f1/docs/endpoints).
The code reuses upstream's asynchronous transport rather than adding a second
owner session or a synchronous SDK dependency. Both upstream projects use MIT
licenses; no source code was copied from the reference SDK.
