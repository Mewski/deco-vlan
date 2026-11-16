"""Mesh discovery and status parsing against files of the kind a Deco keeps."""

import json

from deco_vlan import mesh
from deco_vlan.mesh import Status

FIRMWARE = "1.3.5 Build 26071712 Rel. 18941"

MESH_LIST = {
    "id-satellite-far": {"ip": "192.168.1.23", "role": "RE"},
    "id-main": {"ip": "192.168.1.20", "role": "AP"},
    "id-satellite-near": {"ip": "192.168.1.22", "role": "RE", "eth_backhual": True},
    "id-not-yet-addressed": {"role": "RE"},
}
LEVELS = {"192.168.1.20": 0, "192.168.1.22": 1, "192.168.1.23": 2}


class FakeDeco:
    """Serves the files `mesh` reads from a unit."""

    def __init__(self, host, files=None):
        self.host = host
        members = {
            key: {"device_model": "BE63", "hw_ver": "2", "fw_version": FIRMWARE, **info}
            for key, info in MESH_LIST.items()
        }
        self.files = {
            mesh.MESH_LIST: json.dumps(members),
            "/tmp/work_mode": json.dumps({"level": LEVELS[host]}),
            "/etc/config/sysmode": "config config 'sysmode'\n    option mode 'AP'\n",
            **(files or {}),
        }

    def read_text(self, path):
        return self.files.get(path)

    def exists(self, path):
        return path in self.files

    def close(self):
        pass


def test_discover_finds_every_addressed_unit_main_first():
    units = mesh.discover("192.168.1.23", FakeDeco)
    assert [u.ip for u in units] == ["192.168.1.20", "192.168.1.22", "192.168.1.23"]
    assert [u.main for u in units] == [True, False, False]
    assert [u.level for u in units] == [0, 1, 2]
    assert all(u.tested for u in units)


def test_discover_can_be_limited():
    units = mesh.discover("192.168.1.23", FakeDeco, only=["192.168.1.20"])
    assert [u.ip for u in units] == ["192.168.1.20"]


def test_healthy_mesh_has_only_the_wired_satellite_as_problem():
    units = mesh.discover("192.168.1.20", FakeDeco)
    assert [(p.unit.ip, p.fatal) for p in mesh.problems(units)] == [("192.168.1.22", False)]


def test_router_mode_is_a_fatal_problem():
    router = {"/etc/config/sysmode": "config config 'sysmode'\n    option mode 'Router'\n"}
    units = mesh.discover("192.168.1.20", lambda host: FakeDeco(host, router), ["192.168.1.20"])
    assert [(p.text, p.fatal) for p in mesh.problems(units)] == [("not in access point mode", True)]


def test_untested_firmware_is_flagged():
    units = mesh.discover("192.168.1.20", FakeDeco, only=["192.168.1.20"])
    units[0].firmware = "9.9.9"
    assert not units[0].tested
    assert [p.fatal for p in mesh.problems(units)] == [False]


STATUS = """\
version=0.2.0 pid=2167 uptime=1000.50 state=active role=RE mode=AP uplink=ath23
network=iot vlan=70 isolated=0 radios=ath08 members=ath02.70,ath08,ath12.70,ath22.70,ath23.70
network=guest vlan=80 isolated=1 radios=ath01,ath11 members=ath01,ath02.80,ath11,ath23.80
"""


def test_status_of_a_running_watcher():
    status = Status.parse(STATUS, uptime=1010.0)
    assert status.ready
    assert status.version == "0.2.0"
    assert status.uplink == "ath23"
    assert [network.name for network in status.networks] == ["iot", "guest"]


def test_status_per_network():
    status = Status.parse(STATUS, uptime=1010.0)
    iot, guest = status.network("iot"), status.network("guest")
    assert (iot.vlan, iot.isolated, iot.radios) == (70, False, ("ath08",))
    assert (guest.vlan, guest.isolated, guest.radios) == (80, True, ("ath01", "ath11"))
    assert iot.connected
    assert guest.connected


def test_status_goes_stale_when_the_watcher_stops_writing():
    status = Status.parse(STATUS, uptime=1100.0)
    assert not status.alive
    assert not status.ready


def test_network_that_is_switched_off_is_still_connected():
    text = STATUS.replace("radios=ath01,ath11 members=ath01,ath02.80,ath11,", "radios= members=")
    guest = Status.parse(text, uptime=1010.0).network("guest")
    assert guest.radios == ()
    assert guest.connected


def test_one_network_without_a_path_upstream_is_not_ready():
    status = Status.parse(STATUS.replace(",ath23.80", ""), uptime=1010.0)
    assert status.network("iot").connected
    assert not status.network("guest").connected
    assert status.alive
    assert not status.ready


def test_network_that_is_not_set_up():
    text = "\n".join(STATUS.splitlines()[:2])
    assert Status.parse(text, uptime=1010.0).network("guest") is None


def test_status_without_a_file():
    assert Status.parse("", uptime=50.0) == Status(installed=True)


def test_unit_without_the_script_is_not_installed():
    unit = mesh.discover("192.168.1.20", FakeDeco, only=["192.168.1.20"])[0]
    assert unit.status() == Status()


def test_installed_unit_reports_its_watcher():
    files = {
        f"{mesh.INSTALL_DIR}/deco-vlan.sh": "",
        mesh.STATUS_FILE: STATUS,
        "/proc/uptime": "1010.00 4000.00\n",
    }
    unit = mesh.discover("192.168.1.20", lambda host: FakeDeco(host, files), ["192.168.1.20"])[0]
    assert unit.status().ready
