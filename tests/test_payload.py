"""The watcher's parsers against output captured from a Deco BE63 V2.6, firmware 1.3.5.

The watcher depends on the output format of the firmware's own tools. These tests load the
script as a library and put stand-ins for those tools on the PATH that print the captures.
"""

import os
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = Path(__file__).parents[1] / "src" / "deco_vlan" / "payload" / "deco-vlan.sh"


@pytest.fixture
def run(tmp_path):
    """Run shell code with the watcher's functions defined and stand-in tools on the PATH."""

    def stand_in(name, fixture):
        tool = tmp_path / name
        tool.write_text(f'#!/bin/sh\ncat "{FIXTURES / fixture}"\n')
        tool.chmod(0o755)

    def run(code, **tools):
        for name, fixture in tools.items():
            stand_in(name, fixture)
        result = subprocess.run(
            ["sh", "-c", f'. "{SCRIPT}"\n{code}'],
            env={"DECO_VLAN_LIB": "1", "PATH": f"{tmp_path}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    return run


def test_backhaul_links_of_a_satellite(run):
    assert run("backhaul", hyctl="hyctl-show-satellite.txt").splitlines() == [
        "ath23 norelay enable",
        "ath22 relay enable",
        "ath13 norelay disable",
        "ath03 norelay disable",
        "ath12 relay enable",
        "ath02 relay enable",
        "eth1.4 relay enable",
        "eth1.3 relay enable",
        "eth0.2 relay enable",
        "eth0.1 relay enable",
    ]


def test_satellite_uses_the_one_uplink_the_mesh_broadcasts_on(run):
    out = run('sort_backhaul; echo "$uplink|$children"', hyctl="hyctl-show-satellite.txt")
    assert out == "ath23| ath22 ath12 ath02"


def test_satellite_on_a_cable_uses_the_port_the_mesh_broadcasts_on(run):
    out = run('sort_backhaul; echo "$uplink|$children"', hyctl="hyctl-show-wired.txt")
    assert out == "eth1.4| ath22 ath12 ath02"


def test_main_unit_has_children_and_no_uplink_radio(run):
    out = run('sort_backhaul; echo "$uplink|$children"', hyctl="hyctl-show-main.txt")
    assert out == "| ath22 ath12 ath02"


def test_last_view_stands_when_the_mesh_tool_prints_nothing(run):
    code = 'hyctl() { :; }; children=" ath22" uplink=ath23; links || echo "$uplink|$children"'
    assert run(code) == "ath23| ath22"


def test_satellite_on_a_radio_forgets_its_cable(run):
    out = run('CABLE=eth0.1; links; echo "$uplink|$CABLE"', hyctl="hyctl-show-satellite.txt")
    assert out == "ath23|"


def test_unit_without_an_uplink_radio_uses_the_port_towards_the_gateway(run):
    code = 'wired_uplink() { echo eth0.1; }; links; echo "$uplink"'
    assert run(code, hyctl="hyctl-show-main.txt") == "eth0.1"


def test_cable_stays_known_while_the_gateway_is_not_seen(run):
    code = 'wired_uplink() { return 1; }; CABLE=eth0.1 TICK=1; links; echo "$uplink"'
    assert run(code, hyctl="hyctl-show-main.txt") == "eth0.1"


def test_no_uplink_without_a_radio_or_a_cable(run):
    code = 'wired_uplink() { return 1; }; links; echo "[$uplink]"'
    assert run(code, hyctl="hyctl-show-main.txt") == "[]"


@pytest.mark.parametrize(
    ("vlan", "kind", "mask"),
    [
        ("1", "tagged", "0x20"),
        ("1", "untagged", "0x2"),
        ("4", "untagged", "0x10"),
        ("70", "tagged", "0x22"),
        ("7", "tagged", ""),
    ],
)
def test_switch_members(run, vlan, kind, mask):
    assert run(f"switch_members {vlan} {kind}", ssdk_sh="ssdk-vlan-entry-show.txt") == mask


@pytest.mark.parametrize(("mask", "port"), [("0x2", "1"), ("0x20", "5"), ("0x1", "0")])
def test_port_of(run, mask, port):
    assert run(f"port_of {mask}") == port


@pytest.mark.parametrize(
    ("kinds", "radios"),
    [
        ("iot", "ath08 ath18"),
        ("guest owe_guest", "ath01 ath11 ath21 ath07 ath17"),
        ("nothing", ""),
    ],
)
def test_radios_by_kind_of_network(run, kinds, radios):
    assert run(f"radios {kinds}", uci="uci-show-wireless.txt") == radios


@pytest.mark.parametrize(("text", "role"), [('{"work_mode":"FAP","level":0}', "FAP"), ("", "")])
def test_role(run, tmp_path, text, role):
    (tmp_path / "work_mode").write_text(text)
    assert run(f'WORK_MODE="{tmp_path}/work_mode"; role') == role


def test_each_network_has_its_bridge_vlan_and_radios(run):
    code = """
        IOT_VLAN=70 GUEST_VLAN=80 IOT_RADIOS=ath08 GUEST_RADIOS='ath01 ath11'
        for network in $NETWORKS; do use $network; echo "$BR $VID $ISOLATE $RADIOS"; done
    """
    assert run(code).splitlines() == ["br-iot 70 0 ath08", "br-guest 80 1 ath01 ath11"]


def test_guests_are_not_isolated_when_the_app_allows_local_access(run, tmp_path):
    uci = tmp_path / "uci"
    uci.write_text('#!/bin/sh\n[ "$3" = wifi.guest.host_isolation ] && echo 0\n')
    uci.chmod(0o755)
    assert run('refresh; use guest; echo "$ISOLATE"') == "0"


def test_a_network_without_a_vlan_is_left_alone(run):
    assert run('IOT_VLAN=70; use guest; echo "[$VID]"') == "[]"


def test_vlan_ids_are_read_from_a_configuration_file(run, tmp_path):
    (tmp_path / "config").write_text("IOT_VLAN=70\nGUEST_VLAN=\nVERSION=1\n")
    code = f'echo "[$(configured {tmp_path}/config iot)][$(configured {tmp_path}/config guest)]"'
    assert run(code) == "[70][]"
