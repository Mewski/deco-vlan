"""Root access to one Deco, using nothing but the owner's TP-Link ID.

Every Deco runs an SSH server on port 20001 that accepts the owner of the network: the user name
is the SHA-1 of the TP-Link ID e-mail address, the password is the account password. It gives no
shell, only a forward to `tmpsvr` on the unit's loopback (see tmp.py), and `tmpsvr` can read and
write any file as root. Commands are run by a small script that this module starts through cron.
"""

import base64
import hashlib
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import paramiko

from .tmp import Opcode, RequestError, Session

# paramiko logs a traceback of its own for every failed handshake; the errors are reported here.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

SSH_PORT = 20001
TMPSVR = ("127.0.0.1", 20002)
CONNECT_ATTEMPTS = 4

CRONTAB = "/etc/crontabs/root"
CRON_UPDATE = "/etc/crontabs/cron.update"  # names the users whose crontab crond should reload
RUNNER = "/tmp/deco-vlan-runner"
RUNNER_START = 150  # seconds: crond has to reload the crontab and reach the next full minute
RUNNER_CHECK = 4  # seconds between looks at whether the runner is still there

# A polling command runner, started by one cron tick. It lives in /tmp, so a reboot removes it,
# and it stops by itself when nobody has used it for ten minutes.
RUNNER_SCRIPT = rf"""#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
D={RUNNER}
[ -e $D.pid ] && kill -0 "$(cat $D.pid)" 2>/dev/null && exit 0
echo $$ > $D.pid
idle=0
while [ $idle -lt 600 ]; do
    if [ -f $D.cmd ]; then
        idle=0
        mv $D.cmd $D.run
        sh $D.run > $D.tmp 2>&1
        echo "exit=$?" >> $D.tmp
        mv $D.tmp $D.out
    fi
    [ -e $D.stop ] && break
    idle=$((idle + 1))
    sleep 1
done
rm -f $D.pid $D.stop $D.run $D.sh
"""
CRON_LINE = f"* * * * * (/bin/sh {RUNNER}.sh >/dev/null 2>&1 &)\n"


class DecoError(Exception):
    """Something about a Deco that the user has to know."""


class LoginError(DecoError):
    """Wrong e-mail or password. Ten of these lock a unit for two hours, so callers must stop."""


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class KnownHosts:
    """Host keys, recorded on first contact and required to match afterwards.

    A Deco keeps its host key in RAM and makes a new one at every boot, so a changed key is
    what a reboot looks like, and also what an impostor looks like. The caller decides:
    `accept_changed` replaces the recorded key instead of refusing.
    """

    def __init__(self, path: Path, accept_changed: bool = False) -> None:
        self.path = path
        self.accept_changed = accept_changed
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[str, str]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                match line.split():
                    case [host, kind, key]:
                        self._keys[host] = (kind, key)

    def check(self, host: str, key: paramiko.PKey) -> str | None:
        """Accept or refuse a host key. Returns a note when the key was recorded or replaced."""
        seen = (key.get_name(), key.get_base64())
        known = self._keys.get(host)
        if known == seen:
            return None
        if known is not None and not self.accept_changed:
            raise DecoError(
                f"{host}: host key {fingerprint(key)} differs from the one recorded in "
                f"{self.path}; the password was not sent. A Deco makes a new key at every boot: "
                "if this unit has restarted since the last contact, run the command again with "
                "--new-host-key."
            )
        with self._lock:
            self._keys[host] = seen
            lines = [f"{h} {kind} {key}\n" for h, (kind, key) in sorted(self._keys.items())]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text("".join(lines))
            temporary.replace(self.path)
        verb = "recorded" if known is None else "replaces the recorded one"
        return f"host key {fingerprint(key)} {verb}"


class Deco:
    """An authenticated session with one unit."""

    def __init__(self, host: str, email: str, password: str, known_hosts: KnownHosts) -> None:
        self.host = host
        self._sequence = int(time.time()) % 100000
        self._transport = self._connect(host)
        try:
            self.host_key_note = known_hosts.check(host, self._transport.get_remote_server_key())
            self._session = Session(self._login(email, password))
        except BaseException:
            self._transport.close()
            raise

    @staticmethod
    def _connect(host: str) -> paramiko.Transport:
        """Open the SSH transport.

        The Deco's server has been seen to reset connections that arrive in quick succession,
        so a failed handshake is tried again. No login has been attempted at that point.
        """
        error: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                transport = paramiko.Transport(socket.create_connection((host, SSH_PORT), 8))
                transport.start_client(timeout=10)
            except (OSError, EOFError, paramiko.SSHException) as e:
                error = e
                time.sleep(2 + 2 * attempt)
            else:
                return transport
        raise DecoError(f"{host}: cannot open an SSH session on port {SSH_PORT}: {error}")

    def _login(self, email: str, password: str) -> paramiko.Channel:
        """Log in as the owner and open the forward to `tmpsvr`."""
        user = hashlib.sha1(email.encode(), usedforsecurity=False).hexdigest()
        try:
            self._transport.auth_password(user, password, fallback=False)
            return self._transport.open_channel(
                "direct-tcpip", TMPSVR, ("127.0.0.1", 0), timeout=10
            )
        except paramiko.AuthenticationException as e:
            raise LoginError(f"{self.host}: TP-Link ID rejected") from e
        except paramiko.SSHException as e:
            raise DecoError(f"{self.host}: SSH session failed: {e}") from e

    def close(self) -> None:
        self._transport.close()

    def call(self, opcode: int, params: dict[str, Any] | None = None) -> Any:
        """One of the Deco app's requests; see `tmp.Opcode`."""
        return self._session.call(opcode, params)

    def read(self, path: str) -> bytes | None:
        """Contents of a file, or None if it does not exist or is a directory."""
        try:
            result = self.call(Opcode.READ_FILE, {"file": path})
        except RequestError:
            return None
        data: str | None = result.get("data")
        return None if data is None else data.encode("latin-1")

    def read_text(self, path: str) -> str | None:
        data = self.read(path)
        return None if data is None else data.decode("utf-8", "replace")

    def exists(self, path: str) -> bool:
        return self.read(path) is not None

    def write(self, path: str, text: str, verify: bool = True) -> None:
        """Write an ASCII file.

        The service reports success whatever happens, so the file is read back, unless
        something on the unit is about to consume it.
        """
        text.encode("ascii")
        self.call(Opcode.WRITE_FILE, {"file": path, "content": text})
        if verify and self.read(path) != text.encode():
            raise DecoError(f"{self.host}: writing {path} failed")

    def start_runner(self) -> None:
        """Start the command runner, which can take two minutes."""
        if self.exists(f"{RUNNER}.pid"):
            return
        self.write(f"{RUNNER}.sh", RUNNER_SCRIPT)
        self.write(CRONTAB, (self.read_text(CRONTAB) or "") + CRON_LINE)
        self.write(CRON_UPDATE, "root\n")
        try:
            deadline = time.monotonic() + RUNNER_START
            while not self.exists(f"{RUNNER}.pid"):
                if time.monotonic() > deadline:
                    raise DecoError(f"{self.host}: the command runner did not start")
                time.sleep(3)
        finally:
            # The firmware may have edited the crontab meanwhile; take out only our line.
            self.write(CRONTAB, (self.read_text(CRONTAB) or "").replace(CRON_LINE, ""))
            self.write(CRON_UPDATE, "root\n")

    def stop_runner(self) -> None:
        if self.exists(f"{RUNNER}.pid"):
            self.write(f"{RUNNER}.stop", "\n", verify=False)

    def run(self, script: str, timeout: float = 60) -> tuple[int, str]:
        """Run shell commands as root. Returns the exit status and the output."""
        self.start_runner()
        self._sequence += 1
        tag = f"run={self._sequence}"
        self.write(f"{RUNNER}.cmd", f"echo {tag}\n{script}\n", verify=False)
        deadline = time.monotonic() + timeout
        checked = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.6)
            if time.monotonic() - checked > RUNNER_CHECK:
                checked = time.monotonic()
                # The runner may have reached its idle limit as the command arrived.
                if self.exists(f"{RUNNER}.cmd") and not self.exists(f"{RUNNER}.pid"):
                    self.start_runner()
                    deadline = time.monotonic() + timeout
            out = self.read_text(f"{RUNNER}.out") or ""
            # The runner appends "exit=N" as the last line. The command's own output may end
            # without a newline, so only the end of the file is looked at.
            if out.startswith(f"{tag}\n") and out.endswith("\n") and "exit=" in out[-12:]:
                body, _, status = out.removeprefix(f"{tag}\n").rpartition("exit=")
                return int(status), body
        raise DecoError(f"{self.host}: command did not finish within {timeout} s")
