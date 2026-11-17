# deco-vlan

`deco-vlan` puts the IoT and guest Wi-Fi networks of a TP-Link Deco mesh on their own VLANs
while the Decos run in access point mode.

The stock firmware has an IoT network and a guest network, and each is a separate SSID. In
access point mode it bridges both into the same LAN as everything else and offers no VLAN
setting. With `deco-vlan` each network leaves the Decos as tagged 802.1Q frames, so your own
router decides what those devices may reach.

Nothing is flashed. The stock firmware, the Deco app and the mesh keep working. `deco-vlan` adds
one shell script to each Deco and a line of configuration that starts it at boot.

## Status

This is alpha software that runs on one network, the author's.

| Model | Hardware | Firmware | Backhaul |
|---|---|---|---|
| Deco BE63 | V2.6 | 1.3.5 Build 26071712 | Wi-Fi over one and two hops, and a cable to a switch |

The installer refuses other models and firmware unless you tell it otherwise. Other
Qualcomm-based Decos are built the same way and may need little or no change. The architecture
notes list [what it relies on](docs/architecture.md#what-it-relies-on).

## Requirements

- The Decos run in access point mode.
- Every Deco on a cable is on a switch port that carries your LAN untagged and the new VLANs
  tagged. The main Deco always is on a cable.
- Loop protection is switched off on those switch ports. That means spanning tree, BPDU guard
  and loop detection.
- Your router serves those VLANs.
- You have the TP-Link ID that owns the Decos. It is the only credential, and nothing works
  without it.
- You have a computer on the same LAN with Python 3.10 or newer.

## Install

```sh
git clone https://github.com/Mewski/deco-vlan.git
cd deco-vlan
uv tool install .
```

`pipx install .` works as well.

## Usage

Point any command at one Deco, and the rest of the mesh is found from it.

```sh
deco-vlan status 192.168.1.20
deco-vlan auto-update 192.168.1.20 off
deco-vlan install 192.168.1.20 --iot 70 --guest 80
deco-vlan uninstall 192.168.1.20
```

```text
Unit          Role    Device         Watcher  Uplink  IoT 70  Guest 80
192.168.1.20  main    BE63 v2 1.3.5  active   eth0.1  ath08   ath01 ath11
192.168.1.21  1 hop   BE63 v2 1.3.5  active   ath23   ath08   ath01 ath11
192.168.1.22  2 hops  BE63 v2 1.3.5  active   ath23   ath08   ath01 ath11
```

The command asks for the e-mail address and password of the TP-Link ID. To skip the questions,
set `DECO_VLAN_EMAIL` and `DECO_VLAN_PASSWORD_FILE`, or pass `--email` and `--password-file`.
The password is never taken from the command line.

Two more commands help when you look around a Deco. `deco-vlan run HOST 'command'` runs a shell
command as root, and `deco-vlan read HOST /path` prints a file.

### Installing

Give `install` a VLAN ID for the IoT network, the guest network, or both. A network without a
VLAN ID stays in the LAN. You can run `install` again to change the choice. A network that loses
its VLAN ID returns to the LAN, and a network that gets a different ID moves to it.

The installation takes a few minutes. From then on the radios of each network are in its VLAN on
every Deco, and they stay there across reboots. A network that is switched off in the Deco app
is picked up when you switch it on.

Devices on a network that moves are disconnected once, so that they reconnect and ask for an
address in their new network. Most devices are back within a couple of minutes. A device that
holds on to its old address stays offline until it gives up, and switching it off and on is
quicker.

`uninstall` puts all radios back into the LAN and removes everything from the Decos.

### Cables

A satellite can be on Wi-Fi or on a cable, and you can plug it in and unplug it at any time.
Both networks follow the path the Deco uses. `status` shows that path in the Uplink column. It
is a radio such as `ath23`, or an Ethernet port such as `eth0.1`, which is port 1.

The switch port has to carry the VLANs tagged. On a port that carries only the LAN, the main
network works and the other two have no connection.

A Deco bridges its cable and its Wi-Fi backhaul for a moment when it is plugged in, which is a
loop. This is the stock firmware's behaviour, with or without `deco-vlan`. A managed switch
that reacts to the loop can shut the port of the main Deco and take the whole mesh down with
it. With loop protection off on the Decos' ports, the change took about ten seconds, and
devices on other Decos lost their connection for that long.

### Guests

Guests reach your router and nothing else. They cannot reach each other, whether they are on the
same Deco or on different ones. The "Allow Local Access" switch in the guest settings of the
Deco app changes this. When you switch it on, guests can reach each other inside the guest
VLAN. What they reach beyond the VLAN is still up to your router.

This holds for Decos on cables too, where guests would otherwise meet in your switch. Each Deco
lets in from upstream only what the router sent. For that the router has to use the same MAC
address in the guest network as in your LAN, which a router that serves both normally does.

### Host keys

A Deco makes a new SSH host key at every boot. `deco-vlan` records the key of each Deco on first
contact and refuses to send the password to a Deco whose key has changed. After a Deco has
restarted, and only then, add `--new-host-key` once.

```sh
deco-vlan --new-host-key status 192.168.1.20
```

### Firmware updates

A firmware update can remove the access that `deco-vlan` depends on, or change what the script
relies on. Switch automatic updates off with `deco-vlan auto-update HOST off` and update by
hand. After an update, run `status`, and run `install` again if the installation is gone. If
TP-Link closes the access, the Decos behave as stock and both networks are back in the LAN.

## Limits

- `deco-vlan` delivers the networks on VLANs. What they may reach is decided by your router.
- It does not tag the main network.
- It does not give either network to the Ethernet ports of the Decos.
- A Deco that is plugged into another Deco, instead of into a switch, gets neither network.
- Devices on these networks can no longer be found from the LAN by broadcast or mDNS, unless
  your router relays mDNS between the networks.
- If a Deco has no path upstream for a VLAN, the radios of that network stay out of the LAN. The
  devices on them have no network until the path returns.
- The guest network's bandwidth limit and its 6 GHz radio have not been tried.

## Safety

- Nothing is flashed. What persists is a directory of three small files on the Deco's data
  partition and one section in its saved firewall configuration. A factory reset removes both.
- The LAN bridge of the stock firmware is not modified, apart from the radios leaving it.
- Your TP-Link ID password is sent to the Deco over SSH, after the Deco's host key has been
  checked against the recorded one.
- Running commands needs a small runner on the Deco. It lives in RAM, takes its commands from
  files that only root can write, and stops by itself after ten minutes.

## How it works

The mesh already carries the LAN between Decos as VLAN 1 on the backhaul radios. `deco-vlan`
adds a bridge per network on every Deco and puts that network's radios into it. It connects the
bridges with one more VLAN on the same backhaul radios. On the main Deco the VLANs are let
through the built-in Ethernet switch to the uplink port. A watcher keeps all of this in place.

[docs/architecture.md](docs/architecture.md) has the details and lists what the tool relies on.
[docs/research.md](docs/research.md) tells how it was worked out, with the commands to repeat
each step on another model.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
shellcheck src/deco_vlan/payload/*.sh
```

The tests run without a Deco. They check the protocol against a stand-in service, and they
check the watcher's parsers against output captured from a real unit.

## Credits

CPlusPlus17 found and published the way in.
[deco-filepush-disclosure](https://github.com/CPlusPlus17/deco-filepush-disclosure) describes
the SSH service, the protocol behind it and the file read and write calls. The same author's
[deco-iot-vlan](https://github.com/CPlusPlus17/deco-iot-vlan) explores the same goal on the
Deco XE75 Pro, and it pointed to the firewall include as a place to start from at boot.
`deco-vlan` shares no code with either project.

This project is not affiliated with TP-Link.

## License

`deco-vlan` is released into the public domain under the [Unlicense](LICENSE).
