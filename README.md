# deco-vlan

`deco-vlan` puts the IoT and guest Wi-Fi networks of a TP-Link Deco mesh on their own VLANs
while the Decos run in access point mode.

The stock firmware has an IoT network and a guest network, each a separate SSID, but in access
point mode it bridges both into the same LAN as everything else and offers no VLAN setting. With
`deco-vlan` each of them leaves the Decos as tagged 802.1Q frames, so your own router decides
what those devices may reach.

Nothing is flashed. The stock firmware, the Deco app and the mesh keep working; `deco-vlan`
adds one shell script to each Deco and a line of configuration that starts it at boot.

## Status

Alpha. It runs on one network, the author's.

| Model | Hardware | Firmware | Backhaul |
|---|---|---|---|
| Deco BE63 | V2.6 | 1.3.5 Build 26071712 | Wi-Fi, one and two hops |

The installer refuses other models and firmware unless told otherwise. Other Qualcomm-based
Decos are built the same way and may need little or no change; see
[what it relies on](docs/architecture.md#what-it-relies-on).

## Requirements

- Decos in access point mode.
- Satellites that reach the mesh over Wi-Fi. Wired backhaul is not supported yet.
- The main Deco on a switch port that carries your LAN untagged and the new VLANs tagged, and a
  router that serves those VLANs.
- The TP-Link ID that owns the Decos. It is the only credential; nothing works without it.
- A computer on the same LAN with Python 3.10 or newer.

## Install

```sh
git clone https://github.com/Mewski/deco-vlan.git
cd deco-vlan
uv tool install .
```

`pipx install .` works as well.

## Usage

Point any command at one Deco; the rest of the mesh is found from it.

```sh
deco-vlan status 192.168.1.20
deco-vlan auto-update 192.168.1.20 off
deco-vlan install 192.168.1.20 --iot 70 --guest 80
deco-vlan uninstall 192.168.1.20
```

```text
Unit          Role    Device         Watcher  Uplink  IoT 70  Guest 80
192.168.1.20  main    BE63 v2 1.3.5  active   eth0    ath08   ath01 ath11
192.168.1.21  1 hop   BE63 v2 1.3.5  active   ath23   ath08   ath01 ath11
192.168.1.22  2 hops  BE63 v2 1.3.5  active   ath23   ath08   ath01 ath11
```

The e-mail address and password of the TP-Link ID are asked for. To skip the questions, set
`DECO_VLAN_EMAIL` and `DECO_VLAN_PASSWORD_FILE`, or pass `--email` and `--password-file`. The
password is never taken from the command line.

### Installing

Give `install` a VLAN ID for the IoT network, the guest network, or both. A network without one
stays in the LAN. Running `install` again changes the choice: a network that loses its VLAN ID
returns to the LAN, one that gets a different ID moves.

It takes a few minutes. From then on the radios of each network are in its VLAN on every Deco,
and stay there across reboots. A network that is switched off in the Deco app is picked up when
it is switched on.

Devices on a network that moves are disconnected once, so that they reconnect and ask for an
address in their new network. Most are back within a couple of minutes. One that holds on to its
old address stays offline until it gives up; switching it off and on is quicker.

`uninstall` puts all radios back into the LAN and removes everything from the Decos.

### Guests

Guests reach your router and nothing else: not each other, on the same Deco or another one. This
follows the Deco app. With "Allow Local Access" switched on in the guest network's settings,
guests can reach each other inside the guest VLAN; what they reach beyond it is still up to your
router.

### Host keys

A Deco makes a new SSH host key at every boot. `deco-vlan` records the key of each Deco on first
contact and refuses to send the password to one whose key has changed. After a Deco has
restarted, and only then, add `--new-host-key` once:

```sh
deco-vlan --new-host-key status 192.168.1.20
```

### Firmware updates

An update can remove the access `deco-vlan` depends on, or change what the script relies on.
Switch automatic updates off with `deco-vlan auto-update HOST off` and update by hand. After an
update, run `status`, and `install` again if the installation is gone. If TP-Link closes the
access, the Decos behave as stock: both networks are back in the LAN.

### Looking around

`deco-vlan run HOST 'command'` runs a shell command as root on one Deco, and
`deco-vlan read HOST /path` prints a file. They are what the project was built with.

## Limits

- It delivers the networks on VLANs. What they may reach is what your router does with them.
- It does not tag the main network, and it does not give either network to the Ethernet ports
  of the Decos.
- Devices on these networks can no longer be found from the LAN by broadcast or mDNS, unless
  your router relays mDNS between the networks.
- If a Deco has no path upstream for a VLAN, the radios of that network stay out of the LAN and
  the devices on them have no network until the path returns.
- The guest network's bandwidth limit and its 6 GHz radio have not been tried.

## Safety

- Nothing is flashed. What persists is a directory of three small files on the Deco's data
  partition and one section in its saved firewall configuration. A factory reset removes both.
- The LAN bridge of the stock firmware is not modified, apart from the radios leaving it.
- Your TP-Link ID password is sent to the Deco over SSH, after the Deco's host key has been
  checked against the recorded one.
- Running commands needs a small runner on the Deco. It lives in RAM, takes its commands from
  files only root can write, and stops by itself after ten minutes.

## How it works

The mesh already carries the LAN between Decos as VLAN 1 on the backhaul radios. `deco-vlan`
adds a bridge per network on every Deco, puts that network's radios into it, and connects the
bridges with one more VLAN on the same backhaul radios. On the main Deco the VLANs are let
through the built-in Ethernet switch to the uplink port. A watcher keeps all of this in place.

- [docs/architecture.md](docs/architecture.md): the details, and what it relies on.
- [docs/research.md](docs/research.md): how it was worked out, with the commands to repeat each
  step on another model.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
shellcheck src/deco_vlan/payload/*.sh
```

The tests run without a Deco. They check the protocol against a stand-in service and the
watcher's parsers against output captured from a real unit.

## Credits

The way in was found and published by CPlusPlus17:
[deco-filepush-disclosure](https://github.com/CPlusPlus17/deco-filepush-disclosure) describes
the SSH service, the protocol behind it and the file read and write calls. The same author's
[deco-iot-vlan](https://github.com/CPlusPlus17/deco-iot-vlan) explores the same goal on the
Deco XE75 Pro and pointed to the firewall include as a place to start from at boot. `deco-vlan`
shares no code with either.

This project is not affiliated with TP-Link.

## License

Released into the public domain under the [Unlicense](LICENSE).
