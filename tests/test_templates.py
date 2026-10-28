"""Game templates (R-13): the built-in files, data-folder overrides, validation messages."""

from __future__ import annotations

import json
from pathlib import Path

from basecamp.settings import TEMPLATES_DIR
from basecamp.templates import draft, load_templates, read_manifests, resolve_path

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
