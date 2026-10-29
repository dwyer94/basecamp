"""Game templates (R-13): the built-in files, data-folder overrides, validation messages."""

from __future__ import annotations

import json
from pathlib import Path

from basecamp.settings import TEMPLATES_DIR
from basecamp.templates import (
    draft,
    folder_name,
    list_library,
    load_templates,
    manifests_for,
    read_manifests,
    resolve_path,
)

OWNER_GAMES = {"palworld", "enshrouded", "v-rising", "dragonwilds", "windrose"}


def write(folder: Path, name: str, data: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (folder / name).write_text(text, encoding="utf-8")


def minimal(**fields) -> dict:
    return {"id": "mygame", "name": "My Game", "steam_app_id": 1, "exe": "server.exe",
            "ports": [{"port": 7777, "protocol": "udp", "label": "game"}], **fields}


def test_built_in_templates_are_valid():
    loaded = load_templates(TEMPLATES_DIR, Path("missing"))
    assert loaded.errors == []
    assert set(loaded.templates) >= OWNER_GAMES
    for t in loaded.templates.values():
        assert t.source == "built-in"
        assert t.template.docs, f"{t.template.id}: say where the facts came from"
        assert t.template.join_port_label, t.template.id


def test_data_folder_adds_and_overrides(tmp_path):
    write(tmp_path, "mygame.json", minimal())
    write(tmp_path, "palworld.json", minimal(id="palworld", name="Palworld (mine)"))
    loaded = load_templates(TEMPLATES_DIR, tmp_path)
    assert loaded.get("mygame").source == "data folder"
    assert not loaded.get("mygame").overrides
    mine = loaded.get("palworld")
    assert mine.source == "data folder" and mine.overrides
    assert mine.template.name == "Palworld (mine)"


def test_bad_templates_are_reported_not_fatal(tmp_path):
    write(tmp_path, "good.json", minimal(id="good"))
    write(tmp_path, "typo.json", minimal(id="typo", prots=[]))
    write(tmp_path, "noapp.json", minimal(id="noapp", steam_app_id=None))
    write(tmp_path, "label.json", minimal(id="label", join_port_label="query"))
    write(tmp_path, "unv.json", minimal(id="unv", unverified=["exe", "colour"]))
    write(tmp_path, "renamed.json", minimal(id="other"))
    write(tmp_path, "list.json", "[]")
    loaded = load_templates(Path("missing"), tmp_path)
    assert set(loaded.templates) == {"good"}
    errors = {e["file"].split(": ")[1]: e["error"] for e in loaded.errors}
    assert errors["typo.json"] == "prots: Extra inputs are not permitted"
    assert "a steam template needs steam_app_id" in errors["noapp.json"]
    assert "join_port_label 'query' is not one of the port labels ['game']" in errors["label.json"]
    assert "unverified lists unknown fields: colour" in errors["unv.json"]
    assert errors["renamed.json"] == "file name must match the id: rename it to other.json"
    assert errors["list.json"] == "must be a JSON object"


def test_paths_resolve_against_the_install_folder(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert resolve_path(r"D:\Games\X", r"Saved\Config") == r"D:\Games\X\Saved\Config"
    assert resolve_path(r"D:\Games\X", r"%LOCALAPPDATA%\X\Saved") == \
        r"C:\Users\someone\AppData\Local\X\Saved"
    assert resolve_path(r"D:\Games\X", r"E:\saves") == r"E:\saves"
    assert resolve_path("", r"Saved\Config") == r"Saved\Config"
    assert resolve_path(r"D:\Games\X", "") == ""


def test_draft_picks_the_join_port_by_label():
    from basecamp.templates import Template

    t = Template.model_validate(minimal(ports=[
        {"port": 1000, "label": "game"}, {"port": 1001, "label": "query"}],
        join_port_label="query"))
    assert draft(t, r"D:\X")["join_port"] == 1001


def test_read_manifests(tmp_path):
    write(tmp_path / "steamapps", "appmanifest_2394010.acf",
          '"AppState"\n{\n "appid" "2394010"\n "name" "Palworld Dedicated Server"\n'
          ' "buildid" "999"\n "InstalledDepots"\n {\n  "2394012"\n  {\n'
          '   "manifest" "1"\n  }\n }\n}\n')
    write(tmp_path / "steamapps", "appmanifest_bad.acf", '"AppState" { "appid" "x" }')
    [manifest] = read_manifests(tmp_path)
    assert (manifest.app_id, manifest.name, manifest.build_id) == (
        2394010, "Palworld Dedicated Server", "999")


def test_stop_warnings_are_off_until_the_owner_turns_them_on(tmp_path):
    loaded = load_templates(TEMPLATES_DIR, tmp_path)
    for game, action in (("palworld", "rest"), ("v-rising", "rcon")):
        template = loaded.get(game).template
        assert template.stop_warning.action == action
        assert template.stop_warning.command.startswith("announce ")
        assert template.stop_warning.minutes == []  # the owner turns it on per server
        assert draft(template, r"C:\x")["stop_warning"]["command"].startswith("announce ")
    assert draft(loaded.get("enshrouded").template, "")["stop_warning"] is None


def test_steam_installdir_is_a_plain_folder_name(tmp_path):
    for i, bad in enumerate(["..", "a\b", "a/b", "C:x", " lead", "trail.", "x" * 101]):
        write(tmp_path, f"bad{i}.json", minimal(id=f"bad{i}", steam_installdir=bad))
    write(tmp_path, "ok.json", minimal(id="ok", steam_installdir="My Game Dedicated Server"))
    loaded = load_templates(Path("missing"), tmp_path)
    assert set(loaded.templates) == {"ok"}
    assert len(loaded.errors) == 7


def test_built_in_steam_folders():
    loaded = load_templates(TEMPLATES_DIR, Path("missing"))
    dirs = {k: t.template.steam_installdir for k, t in loaded.templates.items()}
    assert dirs["palworld"] == "PalServer"
    assert dirs["enshrouded"] == "EnshroudedServer"
    assert dirs["dragonwilds"] == "RuneScape Dragonwilds Dedicated Server"
    assert folder_name(loaded.get("palworld").template) == "PalServer"
    assert folder_name(None, 'My: "Game" ') == "My   Game"


def acf(folder: Path, app_id: int, installdir: str, build: str = "7") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"appmanifest_{app_id}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"App {app_id}"\n'
        f'\t"buildid"\t\t"{build}"\n\t"installdir"\t\t"{installdir}"\n}}\n')


def test_library_listing_and_manifests_for(tmp_path):
    steamcmd = tmp_path / "SteamCmd" / "steamcmd.exe"
    assert list_library(steamcmd, [])["error"].startswith("SteamCMD isn't at")
    steamcmd.parent.mkdir()
    steamcmd.write_bytes(b"")
    res = list_library(steamcmd, [])
    assert res["error"] == "" and not res["exists"] and res["folders"] == []
    common = steamcmd.parent / "steamapps" / "common"
    (common / "PalServer").mkdir(parents=True)
    (common / "Forced").mkdir()
    acf(common / "Forced" / "steamapps", 99, "Forced")  # +force_install_dir
    (common / "Loose").mkdir()
    (common / "notes.txt").write_text("not a folder")
    acf(steamcmd.parent / "steamapps", 2394010, "PalServer", "123")
    taken = [("Palworld", str(common / "palserver") + "\\")]  # case and slash don't matter
    res = list_library(steamcmd, taken)
    assert res["exists"]
    rows = {f["name"]: f for f in res["folders"]}
    assert list(rows) == ["Forced", "Loose", "PalServer"]
    assert rows["PalServer"]["app_id"] == 2394010 and rows["PalServer"]["used_by"] == "Palworld"
    assert rows["Forced"]["app_id"] == 99 and rows["Loose"]["app_id"] is None
    assert rows["Loose"]["used_by"] == ""
    # R-15 import and the installed build find the library's manifest too.
    found = manifests_for(common / "PalServer")
    assert [(m.app_id, m.build_id) for m in found] == [(2394010, "123")]
    assert manifests_for(common / "Loose") == []
    assert manifests_for(tmp_path) == []


def test_library_skips_links(tmp_path):
    import subprocess

    steamcmd = tmp_path / "SteamCmd" / "steamcmd.exe"
    common = steamcmd.parent / "steamapps" / "common"
    common.mkdir(parents=True)
    steamcmd.write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(common / "Junction"), str(outside)],
                   check=True, capture_output=True)
    assert list_library(steamcmd, [])["folders"] == []
