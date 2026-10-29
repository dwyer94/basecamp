"""Firewall indicator (D-128): matching tested from fake rule lists, plus one read of the
real rules (read-only)."""

from __future__ import annotations

import shutil

import pytest

from basecamp import firewall as fw
from basecamp.firewall import Policy, Profile, Rule, Target

GAME = r"D:\Games\Pal\Pal\Binaries\Win64\PalServer-Win64-Shipping-Cmd.exe"
TS = "100.64.0.0/255.192.0.0"  # how Windows reports 100.64.0.0/10
EVERYWHERE = 0x7FFFFFFF


def policy(*rules: Rule, current: int = 2, tailscale: int = 2, **profiles: Profile) -> Policy:
    states = {1: Profile(), 2: Profile(), 4: Profile()}
    for name, p in profiles.items():
        states[{"domain": 1, "private": 2, "public": 4}[name]] = p
    return Policy(list(rules), current, states, tailscale)


def game() -> Target:
    return fw.target_for(GAME, "", r"D:\Games\Pal")


def allow(name: str = "Pal", **kw) -> Rule:
    kw.setdefault("program", GAME)
    kw.setdefault("protocol", 17)
    kw.setdefault("ports", "8211")
    kw.setdefault("remote", TS)
    return Rule(name=name, allow=True, **kw)


def status(p: Policy, port: int = 8211, protocol: str = "udp", target: Target | None = None):
    return fw.evaluate(p, target or game(), port, protocol)


def test_tailscale_only_rule_for_the_exe_is_open(monkeypatch):
    assert status(policy(allow()))["status"] == "open"
    assert status(policy(allow(remote="100.64.0.0/10")))["status"] == "open"
    assert status(policy(allow(remote="100.101.102.103")))["status"] == "open"  # narrower
    assert status(policy(allow(remote=f"{TS},fd7a:115c:a1e0::/48")))["status"] == "open"
    assert status(policy(allow(program="")))["status"] == "open"  # any program
    # paths: case, environment variables, quotes
    assert status(policy(allow(program=GAME.upper())))["status"] == "open"
    monkeypatch.setenv("GAMEDIR", r"D:\Games\Pal\Pal\Binaries\Win64")
    assert status(policy(allow(program="%GAMEDIR%\\PalServer-Win64-Shipping-Cmd.exe"))
                  )["status"] == "open"
    assert status(policy(allow(program=f'"{GAME}"')))["status"] == "open"
    assert status(policy(allow(protocol=256, ports="*")))["status"] == "open"
    assert status(policy(allow()))["rule"] == "Pal"


@pytest.mark.parametrize("ports, port, hit", [
    ("8211", 8211, True), ("8211,27015", 27015, True), ("7777-7780", 7779, True),
    ("7777-7780", 7781, False), ("*", 1, True), ("RPC,", 8211, False),
    ("RPC-EPMap", 8211, False), ("", 8211, False), ("8211, 8212", 8212, True),
])
def test_port_specs(ports, port, hit):
    assert fw.port_matches(ports, port) is hit


def test_wrong_port_protocol_exe_or_profile_is_missing():
    assert status(policy(allow(ports="8212")))["status"] == "missing"
    assert status(policy(allow(protocol=6)))["status"] == "missing"
    assert status(policy(allow(program=r"D:\Other\server.exe")))["status"] == "missing"
    # A Public-only rule while the Tailscale adapter is on Private doesn't count.
    assert status(policy(allow(profiles=4)))["status"] == "missing"
    assert status(policy(allow(profiles=4), current=6, tailscale=0))["status"] == "open"
    # Disabled rules, and rules scoped to a service or a Store app.
    assert status(policy(allow(enabled=False)))["status"] == "missing"
    assert status(policy(allow(program="", service="Spooler")))["status"] == "missing"
    assert status(policy(allow(program="", service="*")))["status"] == "missing"
    assert status(policy(allow(program="", package="S-1-15-2-1")))["status"] == "missing"
    # A rule that only lets non-Tailscale addresses in still counts as too broad.
    assert status(policy(allow(remote="LocalSubnet")))["status"] == "too_broad"


@pytest.mark.parametrize("remote", ["*", "LocalSubnet", f"{TS},10.0.0.0/8",
                                    "0.0.0.0/0", "100.0.0.0/8", "10.0.0.1-10.0.0.9",
                                    "Internet", "fe80::/64"])
def test_non_tailscale_remotes_are_too_broad(remote):
    result = status(policy(allow(remote=remote)))
    assert result["status"] == "too_broad" and remote.split(",")[-1] in result["detail"]


def test_broad_beats_a_tailscale_rule_and_says_which():
    result = status(policy(allow("Narrow"), allow("Steam prompt", remote="*", ports="*")))
    assert result["status"] == "too_broad" and result["rule"] == "Steam prompt"


def test_block_rules():
    block = Rule(name="Block Pal", allow=False, program=GAME, protocol=17, ports="*")
    result = status(policy(allow(), block))
    assert result["status"] == "blocked" and result["rule"] == "Block Pal"
    by_port = Rule(name="No 8211", allow=False, protocol=256, ports="8211")
    assert status(policy(allow(), by_port))["rule"] == "No 8211"
    # A block that doesn't reach Tailscale addresses doesn't block friends.
    lan_only = Rule(name="LAN", allow=False, program=GAME, remote="192.168.0.0/16")
    assert status(policy(allow(), lan_only))["status"] == "open"
    off_profile = Rule(name="Public", allow=False, program=GAME, profiles=4)
    assert status(policy(allow(), off_profile))["status"] == "open"
    shields_up = policy(allow(), private=Profile(block_all=True))
    assert status(shields_up)["status"] == "blocked"
    assert status(shields_up)["rule"] == "Block all incoming connections"


def test_firewall_off_or_default_allow_is_too_broad():
    off = status(policy(allow(), private=Profile(on=False)))
    assert off["status"] == "too_broad" and "off for Private" in off["detail"]
    assert status(policy(private=Profile(default_allow=True)))["status"] == "too_broad"
    # Off for a profile Tailscale isn't on doesn't matter here...
    assert status(policy(allow(), public=Profile(on=False)))["status"] == "open"
    # ...but is reported when it's active.
    notes = fw.profile_notes(policy(current=6, public=Profile(on=False)))
    assert any("off for the Public profile" in n for n in notes)


def test_unknown_exe_or_unreadable_rules():
    assert status(policy(allow()), target=Target())["status"] == "unknown"
    out = fw.server_status(None, "access denied", [(8211, "udp", "game")], game(),
                           lambda: GAME)
    assert out["status"] == "unknown" and "access denied" in out["ports"][0]["detail"]


def test_launcher_game_matches_its_real_exe_under_the_install_folder():
    real = fw.target_for(r"D:\Games\DW\RSDragonwildsServer.exe",
                         "RSDragonwildsServer-Win64-Shipping.exe", r"D:\Games\DW")
    shipping = r"D:\Games\DW\RSDragonwilds\Binaries\Win64\RSDragonwildsServer-Win64-Shipping.exe"
    assert status(policy(allow(program=shipping)), target=real)["status"] == "open"
    # The launcher's own rule doesn't let the real server's traffic in,
    assert status(policy(allow(program=r"D:\Games\DW\RSDragonwildsServer.exe")),
                  target=real)["status"] == "missing"
    # and the same name elsewhere isn't this game.
    assert status(policy(allow(program=r"E:\Copy\RSDragonwildsServer-Win64-Shipping.exe")),
                  target=real)["status"] == "missing"
    # Once it runs, its tracked process's exe is what counts.
    running = fw.target_for("", "x", "", running_exe=shipping)
    assert status(policy(allow(program=shipping)), target=running)["status"] == "open"


def test_server_status_worst_wins_and_commands_for_missing_ports():
    ports = [(8211, "udp", "game"), (27015, "udp", "query"), (25575, "tcp", "rcon"),
             (8212, "tcp", "rest")]
    out = fw.server_status(policy(allow()), "", ports, game(), lambda: GAME)
    assert [r["status"] for r in out["ports"]] == ["open", "missing", "missing", "missing"]
    assert out["status"] == "missing"
    assert out["commands"] == [
        f"scripts\\add-game-firewall-rule.ps1 -Exe '{GAME}' -Port 27015 -Protocol UDP",
        f"scripts\\add-game-firewall-rule.ps1 -Exe '{GAME}' -Port 8212,25575 -Protocol TCP"]
    many = [(p, "udp", "") for p in range(30000, 30012)]
    out = fw.server_status(policy(), "", many, game(), lambda: r"D:\it's\game.exe")
    assert [c.count(",") for c in out["commands"]] == [9, 1]
    assert "-Exe 'D:\\it''s\\game.exe'" in out["commands"][0]
    runtime = fw.server_status(policy(), "", ports, game(), lambda: r"C:\Py\python.exe")
    assert runtime["commands"] == [] and "script runtime" in runtime["note"]
    unknown = fw.server_status(policy(), "", ports, game(), lambda: "")
    assert unknown["commands"] == [] and "start it once" in unknown["note"]
    asked = []
    fw.server_status(policy(allow(ports="*", protocol=256)), "", ports, game(),
                     lambda: asked.append(1) or GAME)
    assert asked == []  # the exe is only looked for when a port is missing


def test_parse_policy_from_the_readers_json():
    data = {"current": 6, "tailscale": "Private",
            "profiles": [{"type": 2, "on": True, "inbound": 0, "block_all": False},
                         {"type": 4, "on": False, "inbound": 0, "block_all": False}],
            "rules": {"name": "One", "action": 1, "program": None, "service": None,
                      "package": "", "protocol": 17, "ports": "8211", "remote": "*",
                      "profiles": 2147483647}}
    p = fw.parse_policy(data)
    assert p.current == 6 and p.tailscale == 2 and p.relevant == 2
    assert p.rules == [Rule(name="One", allow=True, protocol=17, ports="8211")]
    assert p.profiles[4].on is False


def test_cache_and_check_again():
    reads = []

    def reader():
        reads.append(1)
        if len(reads) == 2:
            raise RuntimeError("COM said no")
        return policy(allow())

    f = fw.Firewall(cache_s=60, reader=reader)
    assert f.get()[0] is not None and f.get()[0] is not None and len(reads) == 1
    p, error, _, _ = f.get(force=True)
    assert p is None and error == "COM said no"
    assert f.get(force=True)[0] is not None and len(reads) == 3


def test_find_exe(tmp_path):
    deep = tmp_path / "Game" / "Binaries" / "Win64"
    deep.mkdir(parents=True)
    (deep / "Game-Win64-Shipping.exe").write_bytes(b"")
    assert fw.find_exe("game-win64-shipping.exe", str(tmp_path)) == str(
        deep / "Game-Win64-Shipping.exe")
    assert fw.find_exe("nope.exe", str(tmp_path)) == ""
    assert fw.find_exe("x.exe", str(tmp_path / "missing")) == ""


@pytest.mark.skipif(not shutil.which("powershell"), reason="needs Windows PowerShell")
def test_reads_the_real_rules_without_changing_them():
    """Integration: the real firewall, read-only (no rule is added or changed)."""
    p = fw.read_policy()
    assert p.current and set(p.profiles) == {1, 2, 4}
    assert p.rules, "an enabled inbound rule list is never empty on Windows"
    assert all(isinstance(r.allow, bool) for r in p.rules)


def test_rules_limited_to_the_tailscale_address_on_this_side():
    """Tailscale's own Tailscale-In: any program, any port, remote Any, but only to this
    PC's Tailscale IP, so only tailnet traffic, on the profiles it lists (Domain, Private)."""
    ts_in = Rule(name="Tailscale-In", allow=True, remote="*",
                 local="100.101.102.103/255.255.255.255", profiles=3)
    ts6 = Rule(name="Tailscale-In", allow=True, remote="*", local="fd7a:115c:a1e0::1/128",
               profiles=3)
    result = status(policy(ts_in, ts6))
    assert result["status"] == "open" and "to 100.101.102.103" in result["detail"]
    # On a Public Tailscale adapter it doesn't apply, so the game needs its own rule.
    assert status(policy(ts_in, current=4, tailscale=4))["status"] == "missing"
    # A rule for the LAN address only lets no tailnet traffic in, but opens it to the LAN.
    lan = allow(remote="*", local="192.168.50.10")
    assert status(policy(lan))["status"] == "too_broad"
    assert status(policy(Rule(name="LAN block", allow=False, local="192.168.50.10"),
                         allow()))["status"] == "open"


def test_store_app_rules_with_an_owner_dont_count():
    store_app = Rule(name="Xbox", allow=True, owner="S-1-5-21-1-2-3-1001", profiles=3)
    assert status(policy(store_app))["status"] == "missing"
