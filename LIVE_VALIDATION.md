# Hardware validation — 2026-10-25

Environment: Home Assistant 2026.10.1; a mixed mesh of two BE25 nodes and one
BE67. The live local API reported BE25 firmware 1.3.1 and BE67 firmware 1.3.6.
This validates that combination, not every firmware build or mesh topology.

Passed:

- HA configuration check, installation and startup with the existing entry.
- Existing trackers retained; three per-node reboot buttons registered.
- Native MCP `tools/list` returned all ten Deco tools alongside the existing
  Assist tools. An authenticated native `DecoQuery` inventory call succeeded
  and returned three nodes.
- Live reads of WLAN configuration (including IoT/MLO objects), client
  blocklist, DHCP reservations, fast roaming and beamforming.
- Further successful reads: device mode, time settings, speed-test results,
  internet status, performance, WAN information, VLAN configuration and the
  160 MHz enable flag. Wireless-power information returned only its DFS
  support flag, so transmit-power control remains unverified. A fresh client
  list succeeded with `params_json: '{"device_mac":"default"}'`.
- Polling pause, readback and restoration of the original enabled state.
- Block and unblock of a synthetic MAC absent from the mesh/client/blocklist
  inventory. Both changes were read back and confirmed; the test entry was
  removed. Existing clients were not used as test targets.
- 85 automated tests against HA 2026.10.1, including startup/unload,
  authorization, schema serialization, routing, races and lost replies.
- All repository pre-commit checks.

Default read requests were rejected for `ofdma`, `wireless_support`, the
standalone `mlo_network` form, `led`, `qos`, `qos_bandwidth`,
`client_isolation`, `wifi_schedule`, `bandwidth_switch`,
`backhaul_optimization`, `network_optimize`, `eco_mode`, `dhcp_leases`,
`lan`, `ipv6`, `upnp`, `static_routes`, `dhcp`, `parental_profiles`,
`parental_filters`, `port_forwarding`, `dmz`, `ddns`, `vpn_client`,
`vpn_server`, `ipv6_firewall` and `iptv`. These failures do not establish
that the hardware lacks the feature: a different supported payload or
app/cloud protocol may be necessary. The combined `wlan` read does return
MLO configuration.

Actual node reboots, changes to Wi-Fi/routing/firewall settings and live
DHCP reservation writes were not exercised. Their protocol/validation tests
pass, but hardware write support remains unverified. Advanced catalogue
entries remain explicitly labelled unverified; this release does not claim
complete Deco-app parity.

Existing MCP clients may need to reconnect and refresh their tool lists after
the HA restart and MCP selection change. A successful native server call is
distinct from verifying a client with stale cached tool metadata.
