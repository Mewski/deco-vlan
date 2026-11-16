"""Put the IoT and guest Wi-Fi networks of a TP-Link Deco mesh on their own VLANs.

For Decos that run in access point mode.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, mesh
from .device import Deco, DecoError, KnownHosts, LoginError
from .mesh import Connect, Status, Step, Unit
from .tmp import Opcode, ProtocolError, RequestError

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
KNOWN_HOSTS = CONFIG_HOME / "deco-vlan" / "known_hosts"
NETWORKS = {"iot": "IoT", "guest": "Guest"}

app = typer.Typer(
    help=__doc__,
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
out = Console(highlight=False)
err = Console(highlight=False, stderr=True)


@dataclass
class Login:
    """The options every command shares: who logs in, and how host keys are treated."""

    email: str | None
    password_file: Path | None
    new_host_key: bool

    def connector(self) -> Connect:
        """Ask for whatever is missing, once, and return a function that opens a Deco."""
        email = self.email or typer.prompt("TP-Link ID e-mail", err=True)
        if self.password_file:
            password = self.password_file.read_text().strip("\r\n")
        else:
            password = typer.prompt("TP-Link ID password", hide_input=True, err=True)
        known_hosts = KnownHosts(KNOWN_HOSTS, accept_changed=self.new_host_key)

        def connect(host: str) -> Deco:
            deco = Deco(host, email, password, known_hosts)
            if deco.host_key_note:
                err.print(f"[dim]{host}: {deco.host_key_note}[/]")
            return deco

        return connect


class Switch(str, Enum):
    ON = "on"
    OFF = "off"


def show_version(wanted: bool) -> None:
    if wanted:
        out.print(__version__)
        raise typer.Exit


def vlan(network: str) -> Any:
    return typer.Option(
        min=2,
        max=4094,
        metavar="VLAN",
        help=f"VLAN ID for the {network} network.",
        show_default=False,
    )


Email = Annotated[
    str | None,
    typer.Option(
        envvar="DECO_VLAN_EMAIL",
        help="TP-Link ID e-mail. Asked for when missing.",
        show_default=False,
    ),
]
PasswordFile = Annotated[
    Path | None,
    typer.Option(
        envvar="DECO_VLAN_PASSWORD_FILE",
        exists=True,
        dir_okay=False,
        help="File holding the TP-Link ID password. Asked for when missing.",
        show_default=False,
    ),
]
NewHostKey = Annotated[
    bool,
    typer.Option(
        "--new-host-key", help="Accept a changed host key. A Deco makes a new one at every boot."
    ),
]
Version = Annotated[
    bool,
    typer.Option(
        "--version", callback=show_version, is_eager=True, help="Show the version and exit."
    ),
]
AnyHost = Annotated[
    str, typer.Argument(metavar="HOST", help="Address of any Deco of the mesh.", show_default=False)
]
OneHost = Annotated[
    str, typer.Argument(metavar="HOST", help="Address of the Deco.", show_default=False)
]
Only = Annotated[
    list[str] | None,
    typer.Option(metavar="ADDRESS", help="Limit to this unit. Repeatable.", show_default=False),
]
Force = Annotated[bool, typer.Option("--force", help="Install on untested models and firmware.")]


@app.callback()
def options(
    ctx: typer.Context,
    email: Email = None,
    password_file: PasswordFile = None,
    new_host_key: NewHostKey = False,
    version: Version = False,  # noqa: ARG001, handled by its callback
) -> None:
    ctx.obj = Login(email, password_file, new_host_key)


@app.command()
def status(ctx: typer.Context, host: AnyHost, only: Only = None) -> None:
    """Show every unit of the mesh and what its watcher is doing."""
    show(discover(ctx, host, only))


@app.command()
def install(  # noqa: PLR0913, PLR0917, one parameter per command line option
    ctx: typer.Context,
    host: AnyHost,
    iot: Annotated[int | None, vlan("IoT")] = None,
    guest: Annotated[int | None, vlan("guest")] = None,
    force: Force = False,
    only: Only = None,
) -> None:
    """Install on every unit of the mesh, or change an installation.

    A network that is given no VLAN ID stays in, or returns to, the LAN.
    """
    if iot is None and guest is None:
        fail("give at least one of --iot and --guest")
    if iot == guest:
        fail("the two networks need different VLAN IDs")
    units = discover(ctx, host, only)
    check(units, force)
    with progress() as step:
        mesh.install(units, iot, guest, step)
    show(units)


@app.command()
def uninstall(ctx: typer.Context, host: AnyHost, only: Only = None) -> None:
    """Remove from every unit. Both networks return to the LAN."""
    units = discover(ctx, host, only)
    with progress() as step:
        removed = mesh.uninstall(units, step)
    for unit in units:
        out.print(f"{unit.ip}  {'uninstalled' if unit in removed else '[dim]was not installed[/]'}")


@app.command()
def auto_update(
    ctx: typer.Context,
    host: AnyHost,
    state: Annotated[
        Switch | None, typer.Argument(help="Leave out to show the setting.", show_default=False)
    ] = None,
) -> None:
    """Show or set automatic firmware updates, a setting of the whole mesh."""
    deco = next(unit.deco for unit in discover(ctx, host, None) if unit.main)
    setting = deco.call(Opcode.AUTO_UPDATE_GET)
    if state:
        setting = {
            "enable_auto_update": state is Switch.ON,
            "time_begin": int(setting["time_begin"]),
            "time_end": int(setting["time_end"]),
        }
        deco.call(Opcode.AUTO_UPDATE_SET, setting)
        setting = deco.call(Opcode.AUTO_UPDATE_GET)
    enabled = setting["enable_auto_update"]
    out.print(f"Automatic firmware updates are [bold]{'on' if enabled else 'off'}[/].")


@app.command()
def run(
    ctx: typer.Context,
    host: OneHost,
    command: Annotated[list[str], typer.Argument(help="Shell command.", show_default=False)],
    timeout: Annotated[int, typer.Option(help="Seconds to wait for the command.")] = 60,
) -> None:
    """Run a shell command as root on one unit."""
    connect = ctx.obj.connector()
    with err.status("Starting a command runner, which can take two minutes..."):
        deco = connect(host)
        deco.start_runner()
    code, output = deco.run(" ".join(command), timeout)
    sys.stdout.write(output)
    raise typer.Exit(code)


@app.command()
def read(
    ctx: typer.Context,
    host: OneHost,
    path: Annotated[str, typer.Argument(help="File on the Deco.", show_default=False)],
) -> None:
    """Print a file from one unit."""
    data = ctx.obj.connector()(host).read(path)
    if data is None:
        fail(f"{host}: cannot read {path}")
    sys.stdout.buffer.write(data)


def discover(ctx: typer.Context, host: str, only: list[str] | None) -> list[Unit]:
    connect = ctx.obj.connector()
    with err.status("Connecting to the mesh..."):
        return mesh.discover(host, connect, only or ())


def check(units: list[Unit], force: bool) -> None:
    """Report what speaks against installing, and stop unless it may be overridden."""
    problems = mesh.problems(units)
    fatal = any(problem.fatal for problem in problems)
    for problem in problems:
        label = "[yellow]warning[/]" if force and not problem.fatal else "[red]refusing[/]"
        err.print(f"{label}  {problem.unit.ip}: {escape(problem.text)}")
    if fatal:
        fail("nothing was installed")
    if problems and not force:
        fail("nothing was installed; --force overrides the above")


@contextmanager
def progress() -> Iterator[Step]:
    """Show a spinner whose text follows the steps of a long operation."""
    with err.status("") as spinner:
        yield lambda text: spinner.update(f"{text}...")


def show(units: list[Unit]) -> None:
    """Print one row per unit, with a column for every network that is set up somewhere."""
    with err.status("Reading the state of each unit..."):
        states = [unit.status() for unit in units]
    shown = {name: ids for name in NETWORKS if (ids := vlan_ids(states, name))}
    table = Table(box=None, header_style="bold", pad_edge=False, padding=(0, 2, 0, 0))
    for column in ("Unit", "Role", "Device", "Watcher", "Uplink"):
        table.add_column(column, no_wrap=True)
    for name, ids in shown.items():
        table.add_column(f"{NETWORKS[name]} {ids}", no_wrap=True)
    for unit, state in zip(units, states, strict=True):
        cells = [unit.ip, role(unit), device(unit), watcher(state), uplink(state)]
        table.add_row(*cells, *(radios(state, name) for name in shown))
    out.print(table)


def vlan_ids(states: list[Status], name: str) -> str:
    """Give the VLAN ID a network has across the units, or all of them if the units differ."""
    ids = {network.vlan for state in states if (network := state.network(name))}
    return "/".join(map(str, sorted(ids)))


def role(unit: Unit) -> str:
    """Say where the unit sits in the mesh: the main unit, or so many wireless hops from it."""
    if unit.main:
        return "main"
    text = f"{unit.level} hop{'' if unit.level == 1 else 's'}"
    return text + (" [yellow](wired)[/]" if unit.wired_backhaul else "")


def uplink(state: Status) -> str:
    if not state.alive:
        return ""
    return state.uplink or "[red]none[/]"


def device(unit: Unit) -> str:
    text = f"{unit.model} v{unit.hardware} {unit.firmware.split()[0]}"
    return text if unit.tested else f"{text} [yellow](untested)[/]"


def watcher(state: Status) -> str:
    if not state.installed:
        return "[dim]not installed[/]"
    if not state.alive:
        return "[red]not running[/]"
    text = f"[green]{state.state}[/]" if state.state == "active" else f"[yellow]{state.state}[/]"
    if state.version != __version__:
        text += f" [yellow](version {escape(state.version)})[/]"
    return text


def radios(state: Status, name: str) -> str:
    """List the radios of a network: green in the VLAN bridge, red when still in the LAN."""
    network = state.network(name)
    if not state.alive:
        return ""
    if network is None:
        return "[yellow]not set up[/]"
    if not network.radios:
        return "[dim]switched off[/]"
    text = " ".join(
        f"[green]{radio}[/]" if radio in network.members else f"[red]{radio} (in the LAN)[/]"
        for radio in network.radios
    )
    if name == "guest" and not network.isolated:
        text += " [yellow](local access)[/]"
    return text


def fail(message: str) -> NoReturn:
    err.print(f"[red]error[/]  {escape(message)}")
    sys.exit(1)


def main() -> None:
    try:
        app()
    except LoginError as e:
        fail(f"{e}. Stopping: ten rejected logins lock a unit for two hours.")
    except (DecoError, ProtocolError, RequestError, OSError) as e:
        fail(str(e))
