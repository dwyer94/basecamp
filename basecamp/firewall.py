"""Firewall indicator per server (batch 1, D-128): read-only.

Games run in the scheduled task's background session, so Windows never shows its "allow
this app?" prompt: a game without an inbound allow rule simply can't be joined. For each
port a server lists, this says whether Windows Firewall lets Tailscale traffic in:

- ``open``: an enabled inbound Allow rule covers the port and protocol, for the server's exe
  or any program, from Tailscale addresses only (``100.64.0.0/10``, ``fd7a:115c:a1e0::/48``
  or narrower), in a profile the Tailscale adapter is on;
- ``too_broad``: allowed, but also from addresses that aren't Tailscale (Any, LocalSubnet,
  another range), or the firewall is off for that profile, or it allows by default;
- ``blocked``: an enabled Block rule covering Tailscale addresses matches (block beats
  allow), or "Block all incoming connections" is on;
- ``missing``: nothing allows it; ``scripts\\add-game-firewall-rule.ps1`` adds the rule;
- ``unknown``: the rules couldn't be read, or the server's exe isn't known.

It says nothing about whether friends can actually join: Tailscale ACLs, the address the
game binds, or Steam networking can still stop them. The rules are read without admin
through the ``HNetCfg.FwPolicy2`` COM object, in a hidden ``powershell -NoProfile``
(about half a second for ~500 rules), and cached for ``CACHE_S``.

Matching works on plain rule lists (``Policy``), so it's tested without the real firewall.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import logging
import ntpath
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

CACHE_S = 60.0
READ_TIMEOUT_S = 30.0
TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
PROFILE_NAMES = {1: "Domain", 2: "Private", 4: "Public"}
_CATEGORY = {"domainauthenticated": 1, "private": 2, "public": 4}
_PROTOCOLS = {"tcp": 6, "udp": 17}
_ANY_PROTOCOL = 256
CREATE_NO_WINDOW = 0x08000000
# Severity for one badge per server: the worst port wins.
ORDER = ("blocked", "missing", "too_broad", "unknown", "open")
# add-game-firewall-rule.ps1 refuses these (D-85): a rule would open the port to any script.
RUNTIMES = {"python", "pythonw", "py", "pyw", "java", "javaw", "node", "powershell", "pwsh",
            "cmd", "wscript", "cscript", "mshta", "rundll32", "regsvr32", "conhost", "bash",
            "wsl", "dotnet", "msiexec", "explorer", "svchost"}


# -- the rules, as plain data --------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    allow: bool
    program: str = ""  # "" = any program
    service: str = ""  # set = only that service (or "*": services only), never a game
    package: str = ""  # set = only that Store app
    owner: str = ""  # a user SID: a Store (packaged) app's per-user rule, never a game
    protocol: int = _ANY_PROTOCOL  # 6 TCP, 17 UDP, 256 any
    ports: str = "*"
    remote: str = "*"
    local: str = "*"  # this PC's addresses it applies to (Tailscale-In: the Tailscale IP)
    profiles: int = 0x7FFFFFFF
    enabled: bool = True


@dataclass(frozen=True)
class Profile:
    on: bool = True
    default_allow: bool = False
    block_all: bool = False


@dataclass
class Policy:
    rules: list[Rule]
    current: int  # bitmask of the active profiles
    profiles: dict[int, Profile] = field(default_factory=dict)
    tailscale: int = 0  # the Tailscale adapter's profile(s); 0 = not found, use ``current``

    @property
    def relevant(self) -> int:
        return self.tailscale or self.current


def parse_policy(data: dict) -> Policy:
    """The reader's JSON (see ``_SCRIPT``) as a Policy."""
    def text(v: object) -> str:
        return "" if v is None else str(v)

    rules = []
    raw = data.get("rules") or []
    for r in [raw] if isinstance(raw, dict) else raw:
        rules.append(Rule(name=text(r.get("name")), allow=r.get("action") == 1,
                          program=text(r.get("program")), service=text(r.get("service")),
                          package=text(r.get("package")), owner=text(r.get("owner")),
                          protocol=int(r.get("protocol") or _ANY_PROTOCOL),
                          ports=text(r.get("ports")) or "*", remote=text(r.get("remote")) or "*",
                          local=text(r.get("local")) or "*",
                          profiles=int(r.get("profiles") or 0)))
    raw_profiles = data.get("profiles") or []
    profiles = {int(p["type"]): Profile(bool(p.get("on")), p.get("inbound") == 1,
                                        bool(p.get("block_all")))
                for p in ([raw_profiles] if isinstance(raw_profiles, dict) else raw_profiles)}
    categories = data.get("tailscale") or []
    if isinstance(categories, str):
        categories = [categories]
    tailscale = 0
    for c in categories:
        tailscale |= _CATEGORY.get(str(c).lower(), 0)
    return Policy(rules, int(data.get("current") or 0), profiles, tailscale)


# -- matching --------------------------------------------------------------------------------


def port_matches(spec: str, port: int) -> bool:
    """``*``, ``8211``, ``8211,27015``, ``7777-7780``. Keywords (RPC, IPHTTPS...) never
    match a game port."""
    for part in spec.replace(" ", "").split(","):
        if part in ("*", "any", "Any"):
            return True
        low, sep, high = part.partition("-")
        try:
            if sep and int(low) <= port <= int(high):
                return True
            if not sep and int(part) == port:
                return True
        except ValueError:
            continue  # RPC, RPC-EPMap, Teredo, ...
    return False


def protocol_matches(rule: Rule, protocol: str) -> bool:
    return rule.protocol in (_ANY_PROTOCOL, _PROTOCOLS.get(protocol.lower(), -1))


def _networks(part: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None:
    """One remote address entry as networks; None for a keyword such as LocalSubnet."""
    try:
        if "-" in part:  # 10.0.0.1-10.0.0.9
            first, last = (ipaddress.ip_address(x.strip()) for x in part.split("-", 1))
            return list(ipaddress.summarize_address_range(first, last))  # type: ignore[type-var]
        return [ipaddress.ip_network(part, strict=False)]  # a.b.c.d, /16, /255.255.0.0
    except ValueError:
        return None


def remote_scope(spec: str) -> tuple[bool, bool]:
    """(lets some Tailscale address in, lets some other address in)."""
    tailscale = other = False
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if part.lower() in ("*", "any"):
            return True, True
        nets = _networks(part)
        if nets is None:  # LocalSubnet, Internet, DNS, DefaultGateway, ...
            other = True
            continue
        for net in nets:
            ts = [t for t in TAILSCALE if t.version == net.version]
            if any(net.overlaps(t) for t in ts):
                tailscale = True
            if not any(net.subnet_of(t) for t in ts):  # type: ignore[arg-type]
                other = True
    return tailscale, other


def scope(rule: Rule) -> tuple[bool, bool]:
    """(lets tailnet traffic in, lets other traffic in). Tailnet traffic comes from a
    Tailscale address to this PC's Tailscale address, so a rule limited to the Tailscale IP
    on this side (Tailscale's own "Tailscale-In") is Tailscale-only whatever its remote."""
    remote_ts, remote_other = remote_scope(rule.remote)
    local_ts, local_other = remote_scope(rule.local)
    return remote_ts and local_ts, remote_other and local_other


def norm_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(path.strip().strip('"'))))


@dataclass(frozen=True)
class Target:
    """The program whose rules count: full paths, and for a launcher game whose real exe
    isn't running, its file name under the install folder (``real_exe_name``)."""

    paths: frozenset[str] = frozenset()
    name: str = ""
    under: str = ""

    @property
    def known(self) -> bool:
        return bool(self.paths or (self.name and self.under))

    def matches(self, program: str) -> bool:
        p = norm_path(program)
        if p in self.paths:
            return True
        return bool(self.name and self.under and ntpath.basename(p) == self.name
                    and p.startswith(self.under.rstrip("\\") + "\\"))


def program_matches(rule: Rule, target: Target) -> bool:
    if rule.service or rule.package or rule.owner:  # a service's or a Store app's
        return False
    if not rule.program or rule.program.lower() in ("any", "*"):
        return True
    return target.matches(rule.program)


def _covers(rule: Rule, target: Target, port: int, protocol: str, mask: int) -> bool:
    return (rule.enabled and bool(rule.profiles & mask) and protocol_matches(rule, protocol)
            and port_matches(rule.ports, port) and program_matches(rule, target))


def _names(mask: int) -> str:
    return ", ".join(n for b, n in PROFILE_NAMES.items() if mask & b)


def evaluate(policy: Policy, target: Target, port: int, protocol: str) -> dict:
    """One port's status: ``{"status", "rule", "detail"}``."""
    if not target.known:
        return {"status": "unknown", "rule": "", "detail": "The server's exe isn't known."}
    mask = policy.relevant
    active = [b for b in PROFILE_NAMES if mask & b]
    if not active:
        return {"status": "unknown", "rule": "", "detail": "No active network profile."}
    states = {b: policy.profiles.get(b, Profile()) for b in active}
    on = sum(b for b in active if states[b].on)
    off = sum(b for b in active if not states[b].on)
    for b in active:
        if states[b].on and states[b].block_all:
            return {"status": "blocked", "rule": "Block all incoming connections",
                    "detail": f"Windows blocks every incoming connection on the "
                              f"{PROFILE_NAMES[b]} profile."}
    for rule in policy.rules:
        if not rule.allow and _covers(rule, target, port, protocol, on) and scope(rule)[0]:
            return {"status": "blocked", "rule": rule.name,
                    "detail": f"Block rule '{rule.name}' (remote {rule.remote})."}
    if off:
        return {"status": "too_broad", "rule": "",
                "detail": f"The firewall is off for {_names(off)}: every port is open."}
    if any(states[b].default_allow for b in active):
        return {"status": "too_broad", "rule": "",
                "detail": "Incoming connections are allowed by default."}
    broad, tailscale_only = [], []
    for rule in policy.rules:
        if rule.allow and _covers(rule, target, port, protocol, on):
            ts, other = scope(rule)
            if other:
                broad.append(rule)
            elif ts:
                tailscale_only.append(rule)
    if broad:
        r = broad[0]
        return {"status": "too_broad", "rule": r.name,
                "detail": f"Rule '{r.name}' allows remote {r.remote}, not only Tailscale"
                          + (f" (and {len(broad) - 1} more)" if len(broad) > 1 else "") + "."}
    if tailscale_only:
        r = tailscale_only[0]
        return {"status": "open", "rule": r.name,
                "detail": f"Rule '{r.name}' allows remote {r.remote}"
                          + (f" to {r.local}" if r.local != "*" else "") + "."}
    return {"status": "missing", "rule": "", "detail": "No rule allows it."}


# -- one server ------------------------------------------------------------------------------


def target_for(exe_path: str, real_exe_name: str, install_dir: str,
               running_exe: str = "") -> Target:
    """The tracked process's exe when it runs; else the configured exe, or for a launcher
    game (``real_exe_name``) that name anywhere under the install folder."""
    if running_exe:
        return Target(frozenset({norm_path(running_exe)}))
    if real_exe_name.strip():
        under = install_dir.strip() or (ntpath.dirname(exe_path) if exe_path.strip() else "")
        return Target(name=ntpath.normcase(real_exe_name.strip()),
                      under=norm_path(under) if under else "")
    if exe_path.strip():
        return Target(frozenset({norm_path(exe_path)}))
    return Target()


def find_exe(name: str, folder: str, max_depth: int = 6) -> str:
    """The first ``name`` under ``folder`` (for the command to copy), or ""."""
    root = Path(folder)
    if not name or not root.is_dir():
        return ""
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base >= max_depth:
            dirnames.clear()
        for f in filenames:
            if f.lower() == name.lower():
                return str(Path(dirpath) / f)
    return ""


def command(exe: str, ports: list[int], protocol: str) -> str:
    quoted = exe.replace("'", "''")
    return (f"scripts\\add-game-firewall-rule.ps1 -Exe '{quoted}' "
            f"-Port {','.join(str(p) for p in ports)} -Protocol {protocol.upper()}")


def server_status(policy: Policy | None, error: str, ports: list[tuple[int, str, str]],
                  target: Target, rule_exe: Callable[[], str]) -> dict:
    """Every port of one server, the worst status, and the commands for missing ports.

    ``ports`` is ``(port, protocol, label)``; ``rule_exe`` gives the full exe path for the
    command ("" if not known), asked only when a port is missing."""
    rows = []
    for port, protocol, label in ports:
        if policy is None:
            result = {"status": "unknown", "rule": "",
                      "detail": f"The firewall rules couldn't be read: {error}"}
        else:
            result = evaluate(policy, target, port, protocol)
        rows.append({"port": port, "protocol": protocol, "label": label, **result})
    commands, note, exe = [], "", ""
    missing = [r for r in rows if r["status"] == "missing"]
    if missing:
        exe = rule_exe()
        base = ntpath.splitext(ntpath.basename(exe))[0].lower()
        if not exe:
            note = ("The real exe's full path isn't known until the server runs; start it "
                    "once, then check again for the command.")
        elif base in RUNTIMES:
            note = (f"{ntpath.basename(exe)} is a script runtime: "
                    "add-game-firewall-rule.ps1 won't add a rule for it (D-85).")
        else:
            for protocol in ("udp", "tcp"):
                ps = sorted({r["port"] for r in missing if r["protocol"] == protocol})
                for i in range(0, len(ps), 10):  # the script takes up to 10 ports
                    commands.append(command(exe, ps[i:i + 10], protocol))
    statuses = [r["status"] for r in rows]
    worst = next((s for s in ORDER if s in statuses), "unknown")
    return {"status": worst, "ports": rows, "commands": commands, "note": note, "exe": exe}


def profile_notes(policy: Policy | None) -> list[str]:
    """The firewall being off (or blocking everything) for an active profile."""
    if policy is None:
        return []
    notes = []
    for b, name in PROFILE_NAMES.items():
        if not policy.current & b:
            continue
        p = policy.profiles.get(b, Profile())
        if not p.on:
            notes.append(f"Windows Firewall is off for the {name} profile, which is active.")
        elif p.block_all:
            notes.append(f"'Block all incoming connections' is on for the {name} profile.")
    if policy.tailscale:
        notes.append(f"The Tailscale adapter is on the {_names(policy.tailscale)} profile.")
    else:
        notes.append("No Tailscale adapter found; rules are checked for every active "
                     f"profile ({_names(policy.current) or 'none'}).")
    return notes


# -- reading the real rules --------------------------------------------------------------

# Enabled inbound rules only. LocalAppPackageId and LocalUserOwner are INetFwRule3's
# (Windows 8 and later).
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$p = New-Object -ComObject HNetCfg.FwPolicy2
$ts = @()
try {
  $ts = @(Get-NetConnectionProfile | Where-Object { $_.InterfaceAlias -like '*Tailscale*' } |
          ForEach-Object { "$($_.NetworkCategory)" })
} catch { }
$rules = @(foreach ($r in $p.Rules) {
  if ($r.Direction -ne 1 -or -not $r.Enabled) { continue }
  $pkg = $null; $owner = $null
  try { $pkg = $r.LocalAppPackageId; $owner = $r.LocalUserOwner } catch { }
  [ordered]@{ name = $r.Name; action = $r.Action; program = $r.ApplicationName;
              service = $r.ServiceName; package = $pkg; owner = $owner; protocol = $r.Protocol;
              ports = $r.LocalPorts; remote = $r.RemoteAddresses; local = $r.LocalAddresses;
              profiles = $r.Profiles }
})
$profiles = @(foreach ($t in 1, 2, 4) {
  [ordered]@{ type = $t; on = $p.FirewallEnabled($t); inbound = $p.DefaultInboundAction($t);
              block_all = $p.BlockAllInboundTraffic($t) }
})
[ordered]@{ current = $p.CurrentProfileTypes; tailscale = $ts; profiles = $profiles;
            rules = $rules } | ConvertTo-Json -Depth 4 -Compress
"""


def powershell() -> str:
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def read_policy(timeout: float = READ_TIMEOUT_S) -> Policy:
    """The real firewall's inbound rules and profiles. RuntimeError with the reason if they
    can't be read. Changes nothing."""
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"PowerShell didn't run: {e}") from None
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not out:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError(f"PowerShell exited with {proc.returncode}: "
                           f"{err[0] if err else 'no output'}")
    try:
        return parse_policy(json.loads(out.lstrip("\ufeff")))
    except (ValueError, TypeError, KeyError) as e:
        raise RuntimeError(f"Unexpected answer from PowerShell: {e}") from None


class Firewall:
    """The rules, read at most once per ``cache_s`` unless asked to check again."""

    def __init__(self, cache_s: float = CACHE_S, reader=read_policy) -> None:  # type: ignore[no-untyped-def]
        self.cache_s = cache_s
        self._reader = reader
        self._lock = threading.Lock()
        self._policy: Policy | None = None
        self._error = ""
        self._at = 0.0  # time.time() of the last read
        self._took_s = 0.0

    def get(self, force: bool = False) -> tuple[Policy | None, str, float, float]:
        """(policy or None, error, read at, seconds the read took)."""
        with self._lock:
            if force or not self._at or time.time() - self._at >= self.cache_s:
                started = time.monotonic()
                try:
                    self._policy, self._error = self._reader(), ""
                except RuntimeError as e:
                    self._policy, self._error = None, str(e)
                    log.warning("Firewall rules not read: %s", e)
                self._took_s = time.monotonic() - started
                self._at = time.time()
            return self._policy, self._error, self._at, self._took_s


def running_exe(pid: int | None) -> str:
    if not pid:
        return ""
    with contextlib.suppress(psutil.Error):
        return psutil.Process(pid).exe()
    return ""
