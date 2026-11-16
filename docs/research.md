# Research

A record of the steps, in order, with the commands that produced each finding. It is meant for
anyone who wants to repeat the work on another Deco model or firmware. The hardware was a mesh
of Deco BE63 V2.6 on firmware 1.3.5, in access point mode behind a VLAN-aware switch.

## Starting point

- TP-Link's support states that in access point mode the guest and IoT networks cannot be given
  a VLAN ID. In router mode the firmware isolates the guest network internally with VLAN 591.
- There is no OpenWrt port for the BE63. The model exists in three hardware revisions with
  separate firmware. V1.6 is a Qualcomm IPQ9574 design and V2.6 is an IPQ5332 design. V3.6 was
  not looked at.
- Opening the case was not needed.

## Reading the firmware

TP-Link's download page lists the images for every revision.

The V1.6 image is not encrypted, and `UBI#` headers are visible in it. Cutting the file from the
first header and unpacking it gives the root file system.

    ubireader_extract_images -o imgs ubi.img        # volumes: kernel, ubi_rootfs
    unsquashfs -d rootfs imgs/ubi.img/*ubi_rootfs*

The V2.6 and V3.6 images are encrypted. Apart from a short header, the file is indistinguishable
from random data. They were not decrypted. Once there was access to a running unit, its files
were read directly.

The V1.6 root file system showed three things, all of which later held on the V2.6 units.

- `/etc/init.d/iot_network` contains the VLAN handling for the IoT network, commented out. It
  sources a helper, `/lib/iot_network/iot_eth.sh`, that is not in the image.
- Dropbear is started on port 20001 and authenticates against the cloud account.
- The Lua code of the app handlers is shipped as Lua 5.1 bytecode, and `strings` shows the
  handler and field names.

## Getting in

The entry is described in CPlusPlus17's
[deco-filepush-disclosure](https://github.com/CPlusPlus17/deco-filepush-disclosure), shown there
on a Deco XE75 Pro. It consists of SSH on port 20001 with the TP-Link ID, a forward to `tmpsvr`,
and the two opcodes that read and write files as root. On the BE63 it worked unchanged at the
first attempt.

    deco-vlan read 192.168.1.20 /proc/version

Several things were found on the way.

- The SSH banner announces 10 login attempts and a 120 minute lock. Every tool written here
  stops at the first rejected login.
- All units share one RSA host key, offered only as `ssh-rsa` (SHA-1). The ECDSA key is per
  unit, and a reboot showed that it is generated anew at every boot.
- A request body larger than 8156 bytes has to be split. Each fragment goes in its own `PUSH`
  frame with the total length, the CRC-32 of the whole body and the fragment's offset. The
  service acknowledges each with the running byte count and answers after the last one. A 30 kB
  file written this way read back identical.
- Binary files read back intact, which made it possible to pull `/usr/bin/tmpsvr` off a unit.
- A request for a file that does not exist fails with a Lua error. A request for a directory
  succeeds without any contents.

## The app's own calls

`tmpsvr` contains the table of everything the Deco app can ask for. The script in the
[appendix](#appendix-listing-the-opcodes-of-tmpsvr) lists it from the binary. There are 508
entries in the V1.6 image and 612 on the live V2.6 units, and no opcode has a different meaning
in the two.

The first use was switching off automatic firmware updates, so that the units would not change
under the work. The table gives `0x427E` (`admin.mobile_app.cloud`, `auto_upgrade`, `read`) and
`0x427F` (`write`). Calling the read handler shows the fields.

    {"enable_auto_update": true, "time_begin": "180", "time_end": "300"}

The write handler wants the two times as numbers. With strings it fails with a Lua error and
changes nothing. The setting belongs to the mesh. It was written on the main unit and appeared
in `/etc/config/auto_upgrade` on every unit within a minute.

## Running commands

File read and write are enough to run commands through cron, as
[architecture.md](architecture.md#running-commands) describes. With a root shell the rest is
ordinary Linux work.

    deco-vlan run 192.168.1.21 'brctl show; cat /proc/net/vlan/config; hyctl show'

## How the mesh moves traffic

| Command | What it showed |
|---|---|
| `brctl show` | One bridge, `br-lan`, holds every client radio including the IoT radio `ath08`. |
| `cat /proc/net/vlan/config` | The backhaul radios are in the bridge as `ath*.1`, so the LAN crosses the backhaul tagged with VLAN 1. |
| `cat /proc/net/dev` | On a satellite almost all traffic is on `ath23` (6 GHz station), so the backhaul is wireless. |
| `iwconfig ath03`, `ath13`, `ath23` | A satellite is associated to its parent on two bands at once. |
| `hyctl show` | It shows which backhaul radio faces the parent, and which one the mesh is using. |
| `ssdk_sh vlan entry show` | The Ethernet switch has one VLAN per port towards the CPU. |
| `ssdk_sh portVlan ingress get 1` | The mode is `SECURE`, so unknown VLANs are dropped at the port. |
| `grep bridge /tmp/run/hostapd-ath08.conf` | `hostapd` does not name a bridge for the IoT radio. |

The question that decided whether the project was possible was whether a VLAN other than 1 would
cross the wireless backhaul. It does, with nothing to enable.

## Prototype by hand

The IoT VLAN (70) was tagged on the switch port of the main unit. On the main unit, the VLAN was
let through the Ethernet switch, and the router answered.

    ssdk_sh vlan entry create 70
    ssdk_sh vlan member add 70 1 tagged
    ssdk_sh vlan member add 70 5 tagged
    vconfig add eth0 70; ifconfig eth0.70 up
    arping -D -I eth0.70 -c 3 192.168.70.1

A bridge followed on each unit along the path, joined over the 6 GHz backhaul.

    brctl addbr br-iot
    vconfig add ath22 70; brctl addif br-iot ath22.70      # towards children
    vconfig add ath23 70; brctl addif br-iot ath23.70      # towards the parent
    brctl addif br-iot eth0.70                             # main unit only

From a satellite two wireless hops out, `arping -D -I br-iot 192.168.70.1` was answered in 8 ms.
The IoT radio was moved last.

    brctl delif br-lan ath08; brctl addif br-iot ath08

### A test client that disturbs nothing

A Linux laptop whose Wi-Fi card allows two station interfaces can join the IoT SSID on a second
interface while staying on the main SSID. The second interface is pinned to the BSSID of the
unit under test and gets its own routing table, so nothing else on the laptop uses it.

    iw dev wlan0 interface add wlan0iot type managed
    nmcli con add type wifi ifname wlan0iot con-name iot-test ssid "My IoT" \
        wifi.bssid AA:BB:CC:DD:EE:FF wifi-sec.key-mgmt wpa-psk wifi-sec.psk "..." \
        ipv4.route-table 7070 ipv6.route-table 7070 connection.autoconnect no
    ip rule add priority 7070 oif wlan0iot table 7070
    nmcli con up iot-test
    ping -I wlan0iot 1.1.1.1

It received an address from the IoT network's DHCP server and reached the internet over IPv4 and
IPv6. It could not reach the LAN, as the router's rules intended.

## From prototype to something that stays

Each of these was a problem with the prototype and decided part of the design.

### Clients keep their old address

Moving the radio under a connected device leaves it with an address from the wrong network. A
deauthentication makes most devices ask again, and an authoritative DHCP server refuses the old
address, which restarts the device's DHCP. At the cutover all devices but one had a new address
within about two minutes. One device kept sending ARP requests for its old gateway and no DHCP
request, also after a second deauthentication. Left alone, it asked for a new address within
twenty minutes. Taking the radio down for 25 seconds instead of deauthenticating was tried
earlier and only made the device roam to another Deco.

### Parallel backhaul links

The prototype used the 6 GHz link only. Adding the VLAN on every link would loop, since a
satellite talks to its parent on two bands. Two answers were considered. One was spanning tree
on the new bridge, and the other was following the uplink the mesh has already chosen. Following
the mesh won. It needs no timers, it keeps the traffic on the link the mesh considers best, and
one uplink per unit cannot form a loop. `hyctl show` marks that uplink.

### The stock firmware undoes it

The answer is a watcher that repairs once a second instead of a one-time setup. It handles the
radios first in every pass.

### Reboots

`/etc` is a RAM disk and the root file system is read-only. A search of the init scripts found
nothing that runs a file from the writable partition. The firewall's `config include` does,
which CPlusPlus17's [deco-iot-vlan](https://github.com/CPlusPlus17/deco-iot-vlan) pointed out.
Its path is part of the saved settings, and the file it names is sourced at every firewall
start. This was confirmed by installing on one satellite and rebooting it. The watcher was
running again, started from the include, 30 seconds before the IoT radio came up.

### Mixed states

With some units converted and others not, a device that roams changes network without noticing.
The installer therefore always works on the whole mesh, main unit first.

## The guest network

The guest network was added after the IoT network worked, with the same method. It gets a bridge
and a VLAN of its own.

### Stock behaviour

The guest radios are the `wireless` sections of type `guest` (`ath01`, `ath11`, and `ath21` on
6 GHz), plus `owe_guest` for open networks. They exist only while the guest network is switched
on, and they are members of `br-lan` like everything else. The firmware's guest VLAN (591 by
default) is for router mode only. `set_guest_vlan` in `/lib/guest-eth/guest-vlan.sh` returns at
once in access point mode.

The stock firmware keeps guests apart in two ways. The radios run with `ap_isolate=1`, so
clients of one radio cannot reach each other. And `hyfi_bridging` puts the guest radios in a
class of their own, which is the `class` column of `hyctl show`. TP-Link describes the effect as
guests being allowed to talk to the gateway only. The module keeps the gateway's address in
`/proc/hyfi_filter/gw_mac`, next to the switches `full_isolate` and `local_isolate`. The app
calls this "Allow Local Access", and the setting is `wifi.guest.host_isolation`. The stock
filter itself was not tested.

### Keeping guests apart in a bridge of their own

The second mechanism belongs to `br-lan`. In a bridge of their own, guests on different Decos
could reach each other. The Linux bridge has what is needed. A port can be marked isolated, and
the bridge does not forward between two isolated ports. With every port isolated except the one
towards upstream, a guest reaches the router and nothing else. This takes no filter rules and
does not depend on knowing the router's address.

### Testing isolation with one client

A second guest was imitated on another unit with a virtual Ethernet pair, one end in that unit's
`br-guest`. An ARP request from it for the real guest's address got no answer, while one for the
router's address did. The same probe from `br-iot`, where no port is isolated, was answered by
IoT devices on other units. So the probe does reach clients when nothing stops it.

    ip link add gt0 type veth peer name gt1
    ifconfig gt0 up; ifconfig gt1 up; brctl addif br-guest gt0
    arping -I gt1 -c 3 192.168.80.123        # the other guest: no reply
    arping -D -I gt1 -c 3 192.168.80.1       # the router: reply

### Switching it on

The app's wireless settings are opcode `0x4009` to read and `0x4008` to write. The guest network
is the `guest` object of `band2_4` and `band5_1`, and its SSID and password are base64-encoded.
Writing those objects back with `enable` set switched it on.

## Satellites on a cable

A satellite that is plugged into the wired network uses the cable as its backhaul. No satellite
of this mesh was on a cable, so the stock side was read from the firmware.

- `awnd` decides about the cable. Its messages speak of activating Ethernet backhaul and of
  disconnecting the other backhauls, or keeping them. It writes `{"status":1}` to
  `/tmp/eth_runtime_info`, which reads `{"status":0}` on every unit here.
- `apsd` avoids loops in `br-lan`. Its messages speak of picking one broadcast port among the
  ports that do not relay. `/etc/apsd.json` names the Ethernet ports next to the backhaul
  radios.
- `hyctl show` lists the four Ethernet ports as type `relay` on every unit, the main unit's
  uplink port included. What it shows for a satellite on a cable is not known.

The watcher therefore does not depend on how the firmware marks a cable. A unit whose mesh
software broadcasts over a radio towards its parent is on wireless backhaul. Any other unit
uses the Ethernet port behind which the gateway is, which is what the main unit always did.

The Ethernet path was tried on a satellite without a cable. The watcher's code ran there with
the gateway said to be behind `eth0.1`. It took the radio uplink out of both bridges, created
both VLANs in the switch with port 1 and the CPU port as tagged members, and put `eth0.70` and
`eth0.80` into the bridges, with only `eth0.80` not isolated. The real watcher then put
everything back within a second, and it removed the VLANs from the switch. No radio moved.

## What was measured

The first seven rows were measured with the IoT network alone, the first six on the earliest
version of the watcher. The watcher has since been reorganised so that its parsers can be
tested, and extended to the guest network. What it does for the IoT network is unchanged.

| Test | Result |
|---|---|
| Install on the whole mesh | It took 75 s, and every unit was active. |
| IoT devices after install | All but one had an address in the IoT network within about two minutes, and the last within twenty. |
| IoT radio moved back to `br-lan` by hand, VLAN interfaces deleted | It was repaired in under 3 s. |
| 6 GHz uplink of a satellite taken down | The VLAN moved to the 2.4 GHz uplink within 3 s, and the router stayed reachable. It was back on 6 GHz within 5 s of the link returning. |
| Satellite rebooted | It was back after 2 min 20 s with the watcher running and the VLAN in place. |
| `wifi reload` on a satellite with no settings changed | Nothing was undone. |
| Guest network switched on through the app's own call, which reloads Wi-Fi on every unit | The IoT radio stayed in the VLAN bridge, and the watcher had nothing to repair. |
| Guest network added to a running IoT installation | No IoT radio moved, and no IoT device was disconnected. |
| Client on the guest SSID of a satellite | It got an address and reached the internet over IPv4 and IPv6 through the guest VLAN. Nothing in the LAN or the IoT network was reachable. |
| A second guest, imitated on another unit's guest bridge | The first guest did not answer, and the router did. |
| The same probe on the IoT bridge, where ports are not isolated | IoT devices on other units answered. |
| Guest VLAN taken away, then given back with another ID, then with the first | The radios went back to `br-lan` and the bridge was gone. Then the members and the switch entry existed for the new ID only. The IoT network was untouched each time. |
| Watcher CPU use with both networks | It was about 1 % of the four cores, on the main unit and on a satellite. |
| Version 0.3.0 installed over 0.2.0 on the whole mesh | Every watcher took over what was there. None logged a change. |
| Version 0.3.0: uplink VLAN interface of a satellite deleted by hand | It was back within 2 s. |
| Version 0.3.0: 6 GHz uplink of a satellite taken down, then brought back | The VLANs moved to the 2.4 GHz uplink within 3 s, and back within 3 s of the link returning. |

## Not done

- The main unit has not been rebooted with deco-vlan installed.
- No firmware update has been applied with deco-vlan installed.
- No satellite has been on a real cable. It is not known what the firmware does with the
  wireless links then, nor whether the gateway is always seen behind the Ethernet port.
- A Deco plugged into another Deco is not handled. The upstream Deco would have to let the
  VLANs through to that port.
- Four parts of the guest network have not been tried. They are the 6 GHz radio, the bandwidth
  limit, the captive portal some models offer, and "Allow Local Access" switched on.
- `uninstall` has not been run since the guest network was added.

## Appendix: listing the opcodes of `tmpsvr`

`tmpsvr` holds a table of {opcode, module, form, operation}, four 8-byte words per entry. The
last three are pointers to strings such as `admin.mobile_app.cloud`, `auto_upgrade` and `write`.
The binary is position independent, so the pointers are not in the file as plain numbers. They
are in its list of relative relocations, which `readelf` from binutils prints. The script works
for 64-bit ARM builds.

```sh
deco-vlan read 192.168.1.20 /usr/bin/tmpsvr > tmpsvr
python3 opcodes.py tmpsvr
```

```python
import struct
import subprocess
import sys


def readelf(option, path):
    command = ["readelf", option, "--wide", "--use-dynamic", path]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.splitlines()


def table(path):
    data = open(path, "rb").read()
    # Loadable segments as (address, file offset, size).
    segments = [
        (int(fields[2], 16), int(fields[1], 16), int(fields[4], 16))
        for fields in map(str.split, readelf("--program-headers", path))
        if fields and fields[0] == "LOAD"
    ]

    def offset(address):
        for start, file_offset, size in segments:
            if start <= address < start + size:
                return file_offset + address - start

    def string(address):
        at = offset(address)
        if at is None:
            return None
        text = data[at : data.find(b"\0", at)]
        return text.decode() if 0 < len(text) < 80 and all(32 <= c < 127 for c in text) else None

    # Slot address -> the address the slot will point to once the binary is loaded.
    pointers = {
        int(fields[0], 16): int(fields[3], 16)
        for fields in map(str.split, readelf("--relocs", path))
        if len(fields) >= 4 and "RELATIVE" in fields[2]
    }

    entries = {}
    for slot, target in pointers.items():
        module = string(target)
        if module and module.startswith("admin.") and {slot + 8, slot + 16} <= pointers.keys():
            at = offset(slot - 8)
            if at is not None:
                (opcode,) = struct.unpack_from("<Q", data, at)
                if 0 < opcode < 0x10000:
                    entries[opcode] = (
                        module,
                        string(pointers[slot + 8]),
                        string(pointers[slot + 16]),
                    )
    return entries


for opcode, (module, form, operation) in sorted(table(sys.argv[1]).items()):
    print(f"{opcode:#06x}  {module}  {form}  {operation}")
```
