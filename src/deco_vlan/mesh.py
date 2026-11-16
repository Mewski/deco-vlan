"""The units of one Deco mesh: finding them, reading their state, installing and uninstalling."""

import json
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import resources
from typing import Any

from . import __version__
from .device import Deco, DecoError

INSTALL_DIR = "/tp_data/user_data/deco-vlan"
STAGE_DIR = "/tmp/deco-vlan-stage"
STATUS_FILE = "/tmp/deco-vlan.status"
MESH_LIST = "/tmp/sync-server/mesh_dev_list"

# The watcher rewrites its status file at least every 30 s and stamps it with the unit's uptime.
# A stamp older than this, in seconds, means the watcher is no longer running.
STALE = 45

# (model, hardware version) -> firmware versions this has been run on.
TESTED = {("BE63", "2"): ["1.3.5 Build 26071712 Rel. 18941"]}

Connect = Callable[[str], Deco]
Step = Callable[[str], None]


@dataclass(frozen=True)
class Network:
    """One network the watcher manages on a unit: `iot` or `guest`."""

    name: str
    vlan: int
    isolated: bool = False
    uplink: str = ""
    radios: tuple[str, ...] = ()
    members: tuple[str, ...] = ()

    @property
    def connected(self) -> bool:
        """The bridge has a path upstream."""
        return f"{self.uplink}.{self.vlan}" in self.members


@dataclass(frozen=True)
class Status:
    """What the watcher on a unit reports about itself."""

    installed: bool = False
    alive: bool = False
    state: str = ""
    version: str = ""
    uplink: str = ""
    networks: tuple[Network, ...] = ()

    @classmethod
    def parse(cls, text: str, uptime: float) -> "Status":
        """Read the watcher's status file: a line about itself, then a line per network."""
        head, *rest = [_fields(line) for line in text.splitlines()] or [{}]
        uplink = head.get("uplink", "")
        return cls(
            installed=True,
            alive="uptime" in head and uptime - float(head["uptime"]) < STALE,
            state=head.get("state", ""),
            version=head.get("version", ""),
            uplink=uplink,
            networks=tuple(
                Network(
                    name=fields["network"],
                    vlan=int(fields["vlan"]),
                    isolated=fields.get("isolated") == "1",
                    uplink=uplink,
                    radios=_names(fields.get("radios", "")),
                    members=_names(fields.get("members", "")),
                )
                for fields in rest
            ),
        )

    @property
    def ready(self) -> bool:
        """Every network has a path upstream."""
        active = self.alive and self.state == "active"
        return active and all(network.connected for network in self.networks)

    def network(self, name: str) -> Network | None:
        return next((network for network in self.networks if network.name == name), None)


def _fields(line: str) -> dict[str, str]:
    return dict(field.split("=", 1) for field in line.split() if "=" in field)


def _names(text: str) -> tuple[str, ...]:
    return tuple(filter(None, text.split(",")))


@dataclass
class Unit:
    """One Deco of the mesh, with what the mesh says about it."""

    deco: Deco
    main: bool
    level: int  # hops to the main unit
    model: str
    hardware: str
    firmware: str
    wired_backhaul: bool

    @classmethod
    def of(cls, deco: Deco, info: dict[str, Any]) -> "Unit":
        """Build a unit from its entry in the mesh device list."""
        work_mode = json.loads(deco.read_text("/tmp/work_mode") or "{}")
        return cls(
            deco=deco,
            main=info.get("role") == "AP",
            level=work_mode.get("level", 0),
            model=info.get("device_model", "?"),
            hardware=str(info.get("hw_ver", "?")),
            firmware=info.get("fw_version", "?"),
            wired_backhaul=bool(info.get("eth_backhual")),  # the firmware's spelling
        )

    @property
    def ip(self) -> str:
        return self.deco.host

    @property
    def tested(self) -> bool:
        return self.firmware in TESTED.get((self.model, self.hardware), [])

    def status(self) -> Status:
        """State of the watcher on this unit, from the file it publishes."""
        if not self.deco.exists(f"{INSTALL_DIR}/deco-vlan.sh"):
            return Status()
        uptime = float((self.deco.read_text("/proc/uptime") or "0").split()[0])
        return Status.parse(self.deco.read_text(STATUS_FILE) or "", uptime)


@dataclass(frozen=True)
class Problem:
    """A reason not to install on a unit. Only the ones that are not fatal can be forced."""

    unit: Unit
    text: str
    fatal: bool


def discover(host: str, connect: Connect, only: Iterable[str] = ()) -> list[Unit]:
    """Connect to every unit of the mesh that `host` belongs to; the main unit comes first.

    Every unit keeps the list of all mesh members, so any one of them will do as `host`.
    """
    only = set(only)
    first = connect(host)
    listing = first.read_text(MESH_LIST)
    if not listing:
        raise DecoError(f"{host}: no mesh device list; has this Deco finished starting?")
    members = [
        info
        for info in json.loads(listing).values()
        if info.get("ip") and (not only or info["ip"] in only)
    ]

    def unit(info: dict[str, Any]) -> Unit:
        return Unit.of(first if info["ip"] == host else connect(info["ip"]), info)

    # The first connection has shown that the password is right, so the rest can go in parallel
    # without risking a row of rejected logins.
    with ThreadPoolExecutor(max(len(members), 1)) as pool:
        units = list(pool.map(unit, members))
    if not any(unit.deco is first for unit in units):
        first.close()
    return sorted(units, key=lambda unit: (not unit.main, unit.level, unit.ip))


def problems(units: Iterable[Unit]) -> list[Problem]:
    """Check every unit against what the watcher needs and what has been tested."""
    found = []
    for unit in units:
        mode = re.search(r"option mode '(\w+)'", unit.deco.read_text("/etc/config/sysmode") or "")
        if not mode or mode.group(1) != "AP":
            found.append(Problem(unit, "not in access point mode", fatal=True))
        if not unit.tested:
            text = f"{unit.model} v{unit.hardware} with firmware {unit.firmware} is untested"
            found.append(Problem(unit, text, fatal=False))
        if unit.wired_backhaul and not unit.main:
            text = "wired backhaul is not supported; its networks would be cut off"
            found.append(Problem(unit, text, fatal=False))
    return found


def install(
    units: list[Unit], iot: int | None, guest: int | None, step: Step = lambda _: None
) -> None:
    """Install on every unit and wait for each watcher to find its uplink.

    `iot` and `guest` are the VLAN IDs; a network without one is left in the LAN. The main unit
    goes first, then the satellites outwards, so that the path upstream exists when a
    satellite's radios are moved.
    """
    payload = resources.files("deco_vlan") / "payload"
    files = {
        "deco-vlan.sh": (payload / "deco-vlan.sh").read_text(),
        "boot.sh": (payload / "boot.sh").read_text(),
        "config": f"IOT_VLAN={iot or ''}\nGUEST_VLAN={guest or ''}\nVERSION={__version__}\n",
    }
    step("Starting a command runner on each unit")
    _start_runners(units)
    for unit in units:
        step(f"Installing on {unit.ip}")
        _run(unit, f"rm -rf {STAGE_DIR}; mkdir -p {STAGE_DIR}")
        for name, text in files.items():
            unit.deco.write(f"{STAGE_DIR}/{name}", text)
        _run(unit, f"sh {STAGE_DIR}/deco-vlan.sh install; rc=$?; rm -rf {STAGE_DIR}; exit $rc")
    for unit in units:
        step(f"Waiting for {unit.ip}")
        _wait_ready(unit)
        unit.deco.stop_runner()


def uninstall(units: list[Unit], step: Step = lambda _: None) -> list[Unit]:
    """Uninstall from every unit, outermost first. Returns the units it was removed from."""
    step("Starting a command runner on each unit")
    _start_runners(units)
    removed = []
    for unit in reversed(units):
        if unit.deco.exists(f"{INSTALL_DIR}/deco-vlan.sh"):
            step(f"Uninstalling from {unit.ip}")
            _run(unit, f"sh {INSTALL_DIR}/deco-vlan.sh uninstall")
            removed.append(unit)
        unit.deco.stop_runner()
    return removed


def _start_runners(units: list[Unit]) -> None:
    with ThreadPoolExecutor(len(units)) as pool:
        list(pool.map(lambda unit: unit.deco.start_runner(), units))


def _run(unit: Unit, script: str) -> None:
    code, output = unit.deco.run(script, timeout=120)
    if code != 0:
        raise DecoError(f"{unit.ip}: `{script}` failed: {output.strip() or f'exit {code}'}")


def _wait_ready(unit: Unit, seconds: float = 40) -> None:
    deadline = time.monotonic() + seconds
    while not unit.status().ready and time.monotonic() < deadline:
        time.sleep(2)
