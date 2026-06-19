"""
li_connect — brainless connection to a LI-6800 over link-local IPv6 (Windows).

Build order (each block is independently testable):
  Step 1  discovery        find_li6800_candidates()
  Step 2  local key        ensure_local_key(), key_auth_works()
  Step 3  install/identity  (added next)
  Step 4  orchestration     (added next)
  Step 5  persistence/use   (added next)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

USER = "licor"
DEFAULT_KEY = Path.home() / ".ssh" / "id_ed25519"


def _run(cmd: List[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- #
# Step 1: discovery
# --------------------------------------------------------------------------- #
def find_wired_iface(interface_hint: str = "Ethernet") -> Tuple[int, str]:
    """Return (ifindex, name) of the first *connected* interface matching hint."""
    r = _run(["netsh", "interface", "ipv6", "show", "interfaces"])
    if r.returncode != 0:
        raise RuntimeError(f"netsh failed: {r.stderr or r.stdout}")
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*(\d+)\s+\d+\s+\d+\s+(\w+)\s+(.+?)\s*$", line)
        if not m:
            continue
        idx, state, name = int(m.group(1)), m.group(2).lower(), m.group(3).strip()
        if interface_hint.lower() in name.lower() and state == "connected":
            return idx, name
    raise RuntimeError(
        f"No *connected* interface matching '{interface_hint}'. "
        "Is the cable plugged in and the link up?"
    )


def _provoke_discovery(ifindex: int) -> None:
    """Kick IPv6 neighbor discovery so the NDP cache populates."""
    _run(["ping", "-6", "-n", "2", "-w", "1000", f"ff02::1%{ifindex}"])


def _neighbors(ifindex: int) -> List[Tuple[str, str]]:
    r = _run(["netsh", "interface", "ipv6", "show", "neighbors", f"interface={ifindex}"])
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower().startswith("fe80::"):
            out.append((parts[0], parts[-1].lower()))
    return out


def _ping_ok(addr: str, ifindex: int, timeout_ms: int = 1000) -> bool:
    return _run(
        ["ping", "-6", "-n", "1", "-w", str(timeout_ms), f"{addr}%{ifindex}"]
    ).returncode == 0


def find_li6800_candidates(
    interface_hint: str = "Ethernet", verify: bool = True
) -> Tuple[int, str, List[str]]:
    """
    Discover reachable link-local IPv6 neighbors on the wired interface.

    Returns (ifindex, ifname, [addresses])  — addresses are bare (no scope id).
    Build the ssh target as f"{USER}@[{addr}%{ifindex}]".
    """
    idx, name = find_wired_iface(interface_hint)
    _provoke_discovery(idx)
    time.sleep(1.5)

    seen = []
    for addr, state in _neighbors(idx):
        if state in ("unreachable", "incomplete"):
            continue
        if addr not in seen:
            seen.append(addr)

    addrs = sorted(seen)
    if verify:
        addrs = [a for a in addrs if _ping_ok(a, idx)]
    return idx, name, addrs


# --------------------------------------------------------------------------- #
# Step 2: local key
# --------------------------------------------------------------------------- #
def ensure_local_key(key_path: Path = DEFAULT_KEY) -> Path:
    """Make sure an ed25519 keypair (no passphrase) exists; create it if not."""
    key_path = Path(key_path)
    pub = key_path.with_suffix(".pub")
    if key_path.exists() and pub.exists():
        return key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    r = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path),
              "-C", "li-control"])
    if r.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {r.stderr or r.stdout}")
    return key_path


def ssh_target(addr: str, ifindex: int, user: str = USER) -> str:
    return f"{user}@[{addr}%{ifindex}]"


def key_auth_works(addr: str, ifindex: int, key_path: Path = DEFAULT_KEY,
                   user: str = USER, timeout: int = 8) -> bool:
    """True if passwordless (key) SSH already works to this address."""
    target = ssh_target(addr, ifindex, user)
    try:
        r = _run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", f"ConnectTimeout={timeout}", "-i", str(key_path), target, "true"],
            timeout=timeout + 5,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


# --------------------------------------------------------------------------- #
# Step 3: paramiko transport (handles link-local scope id), install, identity
# --------------------------------------------------------------------------- #
import socket  # noqa: E402

import paramiko  # noqa: E402

REMOTE_IDENTITY = "hostname; uname -srm; ls /home/licor/apps 2>/dev/null | tr '\\n' ' '"


def _paramiko_connect(addr: str, ifindex: int, *, password: str | None = None,
                      key_path: Path | None = None, user: str = USER,
                      timeout: float = 10.0) -> "paramiko.SSHClient":
    """
    Open a paramiko SSH client to a link-local IPv6 address.

    The scope id can't ride along in the hostname reliably, so we build the
    AF_INET6 socket ourselves with scope_id in the sockaddr tuple and hand it
    to paramiko via the `sock` argument.
    """
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((addr, 22, 0, ifindex))  # (host, port, flowinfo, scope_id)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=addr, sock=sock, username=user,
        password=password,
        key_filename=str(key_path) if key_path else None,
        look_for_keys=False, allow_agent=False, timeout=timeout,
    )
    return client


def _exec(client: "paramiko.SSHClient", cmd: str, timeout: float = 15.0) -> str:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if stdout.channel.recv_exit_status() != 0 and err:
        return f"{out}\n[stderr] {err}".strip()
    return out


def install_public_key(addr: str, ifindex: int, password: str,
                       key_path: Path = DEFAULT_KEY, user: str = USER) -> str:
    """Append our public key to the device's authorized_keys (idempotent)."""
    pub = Path(key_path).with_suffix(".pub").read_text().strip()
    if "'" in pub:  # our keys never contain quotes; guard anyway
        raise RuntimeError("public key contains a single quote; aborting.")
    client = _paramiko_connect(addr, ifindex, password=password, user=user)
    try:
        cmd = (
            "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF '{pub}' ~/.ssh/authorized_keys "
            f"|| echo '{pub}' >> ~/.ssh/authorized_keys; "
            "echo INSTALLED"
        )
        out = _exec(client, cmd)
        if "INSTALLED" not in out:
            raise RuntimeError(f"key install failed: {out}")
        return out
    finally:
        client.close()


def probe_identity(addr: str, ifindex: int, *, password: str | None = None,
                   key_path: Path | None = DEFAULT_KEY, user: str = USER) -> str:
    """Read hostname / kernel / app list to identify the unit."""
    client = _paramiko_connect(addr, ifindex, password=password,
                               key_path=(None if password else key_path), user=user)
    try:
        return _exec(client, REMOTE_IDENTITY)
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# Step 4: persistence + one-call orchestration
# --------------------------------------------------------------------------- #
CONFIG = Path(__file__).with_name("li6800_config.json")


def _load_choice(interface_hint: str) -> Optional[str]:
    try:
        d = json.loads(CONFIG.read_text(encoding="utf-8"))
        if d.get("interface_hint") == interface_hint:
            return d.get("addr")
    except Exception:
        pass
    return None


def _save_choice(addr: str, interface_hint: str, user: str, hostname: str = "") -> None:
    CONFIG.write_text(
        json.dumps({"addr": addr, "interface_hint": interface_hint,
                    "user": user, "hostname": hostname}, indent=2),
        encoding="utf-8",
    )


def _prompt_choice(addrs: List[str], ident: Dict[str, str], idx: int) -> Optional[str]:
    print("\nMultiple LI-6800s reachable — choose yours:")
    for i, a in enumerate(addrs, 1):
        host = (ident.get(a, "") or "").splitlines()[:1]
        print(f"  [{i}] {a}%{idx}   {host[0] if host else ''}")
    try:
        sel = input("Enter number (or blank to cancel): ").strip()
    except EOFError:
        return None
    if sel.isdigit() and 1 <= int(sel) <= len(addrs):
        return addrs[int(sel) - 1]
    return None


def connect(
    interface_hint: str = "Ethernet",
    prefer: Optional[str] = None,
    *,
    user: str = USER,
    key_path: Path = DEFAULT_KEY,
    password: Optional[str] = None,
    remember: bool = True,
    interactive: bool = True,
) -> str:
    """
    Brainless connect. Returns an ssh target string 'user@[fe80::..%idx]'.

    - Repeat runs: if the remembered unit is reachable and key-auth already
      works, returns instantly with no prompts.
    - First run: prompts once for the SSH password (getpass), then keeps only the
      units that actually accept it — if exactly one does, it is auto-selected
      with no prompt; otherwise you pick among the ones that authenticated.
      Installs the key on the chosen unit only.

    `prefer` is a substring matched against the address or hostname to
    auto-select a unit without prompting.
    """
    idx, name, addrs = find_li6800_candidates(interface_hint)
    if not addrs:
        raise RuntimeError(
            "No reachable LI-6800 found on "
            f"'{interface_hint}'. Is it powered on, booted, and cabled to this segment?"
        )
    ensure_local_key(key_path)
    remembered = _load_choice(interface_hint)

    def ready(a: str) -> bool:
        return key_auth_works(a, idx, key_path, user)

    # --- try to pick a unit without needing the password ---
    pick: Optional[str] = None
    if prefer:
        m = [a for a in addrs if prefer.lower() in a.lower()]
        if len(m) == 1:
            pick = m[0]
    if pick is None and remembered in addrs:
        pick = remembered
    if pick is None and len(addrs) == 1:
        pick = addrs[0]

    # fast path: chosen unit already passwordless -> done, no prompts
    if pick is not None and ready(pick):
        if remember:
            _save_choice(pick, interface_hint, user)
        return ssh_target(pick, idx, user)

    # --- need the password (to identify and/or install the key) ---
    if password is None:
        if not interactive:
            raise RuntimeError(
                "Key auth not set up and no password provided "
                "(call connect(password=...) or run interactively)."
            )
        import getpass
        password = getpass.getpass(
            f"LI-6800 SSH password for '{user}' (one-time, to install key): "
        )

    # if we still don't have a pick, identify candidates and keep only the ones
    # that actually accept this password — empirically that alone usually picks
    # the right unit (an OUI/vendor guess is unreliable: the real LI-6800 may
    # carry a locally-administered MAC while other LI-COR-OUI devices reject it).
    if pick is None:
        ident: Dict[str, str] = {}
        authed: List[str] = []
        for a in addrs:
            try:
                ident[a] = probe_identity(a, idx, password=password, user=user)
                authed.append(a)
            except paramiko.AuthenticationException:
                ident[a] = "<password rejected>"
            except Exception as e:  # unreachable / no sshd / not a LICOR
                ident[a] = f"<no identity: {type(e).__name__}>"

        if not authed:
            listing = "\n".join(f"  {a}%{idx}  {ident[a]}" for a in addrs)
            raise RuntimeError(
                f"No discovered unit accepted the password for '{user}'. Check the "
                "password (LI-6800 factory default is 'licor') or it was changed on "
                "the unit.\n" + listing
            )

        # prefer narrows within the units that authenticated
        if prefer:
            m = [a for a in authed if prefer.lower() in (a + ident[a]).lower()]
            if len(m) == 1:
                pick = m[0]

        # exactly one unit accepts the password -> brainless auto-select
        if pick is None and len(authed) == 1:
            pick = authed[0]

        # still ambiguous: prompt, but only among the units that authenticated
        if pick is None and interactive:
            pick = _prompt_choice(authed, ident, idx)

        if pick is None:
            listing = "\n".join(
                f"  {a}%{idx}  {(ident[a].splitlines() or [''])[0]}" for a in authed
            )
            raise RuntimeError(
                "Multiple units accepted the password; re-run with "
                "prefer='<address-or-hostname substring>':\n" + listing
            )

    # ensure the key is installed on the chosen unit
    if not ready(pick):
        try:
            install_public_key(pick, idx, password, key_path, user)
        except paramiko.AuthenticationException:
            raise RuntimeError(f"Password rejected by the LI-6800 at {pick}.")
        if not ready(pick):
            raise RuntimeError(
                f"Installed key on {pick} but passwordless SSH still fails — "
                "check the device's sshd / authorized_keys permissions."
            )
    if remember:
        try:
            host = probe_identity(pick, idx, key_path=key_path, user=user).splitlines()[:1]
            _save_choice(pick, interface_hint, user, host[0] if host else "")
        except Exception:
            _save_choice(pick, interface_hint, user)
    return ssh_target(pick, idx, user)


# --------------------------------------------------------------------------- #
# Step 5: high-level instrument handle (wraps the file-based command protocol)
# --------------------------------------------------------------------------- #
class LiCor:
    """
    Thin wrapper over the RemoteEnvMeasure file protocol.

      li = LiCor.connect()                  # discovers + sets up key + picks unit
      ack = li.measure(co2_r=400, tair=25)  # set conditions, log one record
    """

    REMOTE_DIR = "/home/licor/apps/dynamic"

    def __init__(self, target: str, key_path: Path = DEFAULT_KEY,
                 workdir: Optional[Path] = None):
        self.target = target              # 'licor@[fe80::..%6]'
        self.key_path = Path(key_path)
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.remote_cmd = f"{self.REMOTE_DIR}/remote_cmd.json"
        self.remote_tmp = f"{self.REMOTE_DIR}/remote_cmd.json.tmp"
        self.remote_ack = f"{self.REMOTE_DIR}/remote_ack.json"

    @classmethod
    def connect(cls, **kw) -> "LiCor":
        key_path = kw.get("key_path", DEFAULT_KEY)
        return cls(connect(**kw), key_path=key_path)

    # -- low-level ssh/scp using the installed key ------------------------- #
    def _ssh(self, *args: str) -> subprocess.CompletedProcess:
        return _run(["ssh", "-o", "BatchMode=yes",
                     "-o", "StrictHostKeyChecking=accept-new",
                     "-i", str(self.key_path), self.target, *args])

    def _scp(self, src: str, dst: str) -> subprocess.CompletedProcess:
        return _run(["scp", "-o", "BatchMode=yes",
                     "-o", "StrictHostKeyChecking=accept-new",
                     "-i", str(self.key_path), src, dst])

    def ping(self) -> bool:
        return self._ssh("true").returncode == 0

    def send_and_wait_ack(self, cmd: Dict[str, Any], *, timeout_s: float = 120.0,
                          poll_s: float = 0.5,
                          pickup_timeout_s: float = 10.0) -> Dict[str, Any]:
        cmd_id = cmd.get("cmd_id") or str(uuid.uuid4())
        cmd = dict(cmd, cmd_id=cmd_id)

        local_cmd = self.workdir / "remote_cmd.json"
        local_ack = self.workdir / "remote_ack.json"
        local_cmd.write_text(json.dumps(cmd, indent=2), encoding="utf-8")

        r1 = self._scp(str(local_cmd), f"{self.target}:{self.remote_tmp}")
        if r1.returncode != 0:
            raise RuntimeError(f"SCP upload failed:\n{r1.stderr}")
        r2 = self._ssh(f"mv {self.remote_tmp} {self.remote_cmd}")
        if r2.returncode != 0:
            raise RuntimeError(f"SSH mv failed:\n{r2.stderr}")

        t0 = time.time()
        last_err = ""
        picked_up = False
        while time.time() - t0 < timeout_s:
            # Liveness: RemoteEnvMeasure deletes the command file the instant it
            # reads it. If the file never disappears, the BP isn't running on the
            # console — fail fast with a clear message instead of blocking for the
            # full timeout. (The file is consumed before any setpoint/wait, so this
            # stays accurate even for long wait_for_* commands.)
            if not picked_up:
                chk = self._ssh(f"test -e {self.remote_cmd} && echo PRESENT || echo GONE")
                if "GONE" in chk.stdout:
                    picked_up = True
                elif "PRESENT" in chk.stdout and time.time() - t0 > pickup_timeout_s:
                    raise RuntimeError(
                        f"The LI-6800 did not pick up the command within "
                        f"{pickup_timeout_s:.0f}s ({self.remote_cmd} is still there). "
                        "Start RemoteEnvMeasure.py as a Background Program on the "
                        "console (Bluestem > Background Programs), then try again."
                    )

            r3 = self._scp(f"{self.target}:{self.remote_ack}", str(local_ack))
            if r3.returncode == 0:
                try:
                    ack = json.loads(local_ack.read_text(encoding="utf-8"))
                    if ack.get("cmd_id") == cmd_id:
                        return ack
                except Exception as e:
                    last_err = f"ACK parse error: {e!r}"
            else:
                last_err = r3.stderr.strip()
            time.sleep(poll_s)
        raise TimeoutError(f"Timed out waiting for ack cmd_id={cmd_id}. Last: {last_err}")

    def bp_running(self, settle_s: float = 6.0) -> bool:
        """True if the RemoteEnvMeasure background program is consuming commands.

        Drops a harmless no-op command and checks whether the console picks it up
        (the BP deletes remote_cmd.json the moment it reads it). Returns False if
        the file is still sitting there after settle_s — i.e. the BP isn't running.
        Use this for a quick readiness check before read()/measure().
        """
        probe = json.dumps(
            {"action": "ping", "log": False, "wait_s": 0, "cmd_id": str(uuid.uuid4())},
            indent=2,
        )
        local = self.workdir / "remote_cmd.json"
        local.write_text(probe, encoding="utf-8")
        if self._scp(str(local), f"{self.target}:{self.remote_tmp}").returncode != 0:
            return False
        if self._ssh(f"mv {self.remote_tmp} {self.remote_cmd}").returncode != 0:
            return False
        t0 = time.time()
        while time.time() - t0 < settle_s:
            chk = self._ssh(f"test -e {self.remote_cmd} && echo PRESENT || echo GONE")
            if "GONE" in chk.stdout:
                return True
            time.sleep(0.5)
        return False

    def read(self, timeout_s: float = 30.0) -> Dict[str, Any]:
        """Report the instrument's current live values without changing anything.

        Sends a command with no setpoints, no logging, and no wait, then returns
        the ack's 'meas' dict: CO2_r, CO2_s, H2O_r, H2O_s (mmol/mol), Tchamber,
        Tleaf (deg C), RHcham (%), PPFD_in (umol m-2 s-1). Safe to call any time —
        it never toggles a controller or writes a log record. Requires the
        RemoteEnvMeasure background program to be running on the console.
        """
        ack = self.send_and_wait_ack(
            {"action": "read", "wait_s": 0, "log": False, "wait_for_co2": False},
            timeout_s=timeout_s,
        )
        return ack.get("meas", {})

    def measure(self, *, co2_r=None, qin=None, flow=None, tair=None, rh_air=None,
                fan_rpm=None, pressure=None, wait_for_co2=False, co2_tol=20,
                wait_s=10, log=True, **extra) -> Dict[str, Any]:
        cmd: Dict[str, Any] = {"action": "measure", "wait_for_co2": wait_for_co2,
                               "co2_tol": co2_tol, "wait_s": wait_s, "log": log}
        for k, v in dict(co2_r=co2_r, qin=qin, flow=flow, tair=tair, rh_air=rh_air,
                         fan_rpm=fan_rpm, pressure=pressure).items():
            if v is not None:
                cmd[k] = v
        cmd.update(extra)
        return self.send_and_wait_ack(cmd)


if __name__ == "__main__":
    import sys

    idx, name, addrs = find_li6800_candidates()
    print(f"interface: {name} (ifindex {idx})")
    print(f"reachable link-local neighbors: {addrs}")
    keyp = ensure_local_key()
    print(f"local key: {keyp}  (pub exists: {keyp.with_suffix('.pub').exists()})")
    for a in addrs:
        print(f"  key-auth to {a}%{idx}: {key_auth_works(a, idx)}")

    if "--probe-transport" in sys.argv and addrs:
        a = addrs[0]
        print(f"\n[transport test] paramiko -> {a}%{idx} with a bogus password")
        try:
            _paramiko_connect(a, idx, password="definitely-not-the-password").close()
            print("  unexpected: connected with bogus password?!")
        except paramiko.AuthenticationException:
            print("  OK: reached SSH server, auth rejected (transport works)")
        except Exception as e:
            print(f"  TRANSPORT PROBLEM: {type(e).__name__}: {e}")
