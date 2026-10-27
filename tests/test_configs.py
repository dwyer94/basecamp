"""Phase 5 config editor: the path guard (R-43), encodings and line endings, the copies
kept on save (R-41), restore, and the API around them (R-40, R-42)."""

from __future__ import annotations

import _winapi
import dataclasses
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from basecamp import configs
from basecamp.api.app import create_app
from basecamp.configs import ConfigFiles, NotAllowed
from basecamp.models import ServerConfig
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import fake_config, stop_everything, wait_for

BASE = "http://127.0.0.1:8150"
PALWORLD_LINE = ("OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,"
                 + ",".join(f"Setting{i}={i}.000000" for i in range(400))
                 + ',ServerName="Basecamp test",CrossplayPlatforms=(Steam,Xbox,PS5,Mac))')


@pytest.fixture
def game(tmp_path: Path) -> Path:
    """A fake install folder with a few config files and a neighbour that isn't listed."""
    root = tmp_path / "game"
    (root / "cfg").mkdir(parents=True)
    (root / "cfg" / "server.json").write_bytes(b'{\r\n  "name": "test"\r\n}\r\n')
    (root / "cfg" / "Game.ini").write_bytes(
        b"[/Script/Pal.PalGameWorldSettings]\r\n" + PALWORLD_LINE.encode() + b"\r\n")
    (root / "cfg" / "secret.ini").write_text("not listed")
    (root / "cfg" / "folder").mkdir()
    return root


def make_server(store, game: Path, files: list[str], name: str = "g"):  # type: ignore[no-untyped-def]
    return store.add_server(ServerConfig(name=name, exe_path=str(game / "server.exe"),
                                         install_dir=str(game), config_files=files))


@pytest.fixture
def server(store, game):  # type: ignore[no-untyped-def]
    return make_server(store, game, ["cfg\\server.json", str(game / "cfg" / "Game.ini"),
                                     "cfg\\folder", "cfg\\missing.ini"])


# -- the guard (R-43) -----------------------------------------------------------------------


def test_listed_files_pass_relative_absolute_and_any_case(server, game, settings):
    real = (game / "cfg" / "server.json").resolve()
    assert configs.check(server, "cfg\\server.json", settings) == real
    assert configs.check(server, str(game / "cfg" / "server.json"), settings) == real
    assert configs.check(server, str(game / "CFG" / "SERVER.JSON"), settings) == real
    assert configs.check(server, "cfg/./x/../server.json", settings) == real
    assert configs.check(server, "cfg\\missing.ini", settings).name == "missing.ini"


@pytest.mark.parametrize("path", [
    "cfg\\secret.ini",                      # same folder, not listed
    "..\\..\\Windows\\win.ini",
    "cfg\\..\\..\\outside.txt",
    "C:\\Windows\\win.ini",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\.\\C:\\Windows\\win.ini",
    "cfg\\server.json:secret",              # alternate data stream
    "cfg\\server.json::$DATA",
    "cfg\\server.json\0.txt",
    "",
    "cfg",
])
def test_anything_else_is_refused(server, settings, path):
    with pytest.raises(NotAllowed):
        configs.check(server, path, settings)


def test_a_listed_folder_is_refused(server, settings):
    with pytest.raises(NotAllowed, match="folder"):
        configs.check(server, "cfg\\folder", settings)


def test_a_junction_to_elsewhere_is_refused(store, game, tmp_path, settings):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "server.json").write_text("{}")
    _winapi.CreateJunction(str(outside), str(game / "linked"))
    # Listed through the junction: the entry resolves outside, but it is the listed file.
    s = make_server(store, game, ["linked\\server.json"])
    assert configs.check(s, "linked\\server.json", settings) == (outside / "server.json").resolve()
    # The real file behind it is reachable by its real name (same file), and nothing else is.
    assert configs.check(s, str(outside / "server.json"), settings)
    with pytest.raises(NotAllowed):
        configs.check(s, "linked\\other.json", settings)
    # A path through the junction to a file that isn't listed.
    s2 = make_server(store, game, ["cfg\\server.json"], name="g2")
    with pytest.raises(NotAllowed):
        configs.check(s2, "linked\\server.json", settings)


def test_a_symlinked_file_is_refused(store, game, tmp_path, settings):
    target = tmp_path / "elsewhere.ini"
    target.write_text("x")
    link = game / "cfg" / "link.ini"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks need Developer Mode or admin on Windows")
    s = make_server(store, game, ["cfg\\link.ini"])
    with pytest.raises(NotAllowed, match="link"):
        configs.check(s, "cfg\\link.ini", settings)


def test_the_data_folder_is_refused(store, game, settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "secrets.toml").write_text("[discord]\n")
    s = make_server(store, game, [str(settings.data_dir / "secrets.toml")])
    with pytest.raises(NotAllowed, match="data folder"):
        configs.check(s, str(settings.data_dir / "secrets.toml"), settings)


def test_relative_entries_need_an_install_folder(store, settings):
    s = store.add_server(ServerConfig(name="x", exe_path="server.exe",
                                      config_files=["a.ini"]))
    with pytest.raises(NotAllowed):
        configs.check(s, "a.ini", settings)


# -- encodings and line endings ------------------------------------------------------------


@pytest.mark.parametrize(("data", "label"), [
    (b"a=1\r\nb=2\r\n", "UTF-8, CRLF"),
    (b"a=1\nb=2\n", "UTF-8, LF"),
    (b"\xef\xbb\xbfa=\xc3\xa9\r\n", "UTF-8 with BOM, CRLF"),
    ("\ufeffa=é\r\nb=2\r\n".encode("utf-16-le"), "UTF-16 LE with BOM, CRLF"),
    ("\ufeffa=1\n".encode("utf-16-be"), "UTF-16 BE with BOM, LF"),
    ("a=1\r\nname=x\r\n".encode("utf-16-le"), "UTF-16 LE, CRLF"),
    (b"name=caf\xe9\r\n", "Windows-1252, CRLF"),
    (b"", "UTF-8, CRLF"),
    (PALWORLD_LINE.encode(), "UTF-8, CRLF"),
])
def test_unchanged_text_saves_byte_for_byte(data, label):
    decoded = configs.decode(data)
    assert decoded.label() == label
    page_text = decoded.text.replace("\r\n", "\n")  # what the browser sends back
    assert configs.encode(page_text, decoded) == data


def test_edits_keep_the_encoding_and_line_endings():
    data = "\ufeff[S]\r\nName=Old\r\n".encode("utf-16-le")
    decoded = configs.decode(data)
    new = configs.encode("[S]\nName=Café\n", decoded)
    assert new == "\ufeff[S]\r\nName=Café\r\n".encode("utf-16-le")


def test_characters_the_code_page_cant_hold_are_refused():
    decoded = configs.decode(b"name=caf\xe9\r\n")
    with pytest.raises(configs.ConfigError, match="encoding"):
        configs.encode("name=\u2603\n", decoded)


# -- saving, copies, restore ---------------------------------------------------------------


def test_save_keeps_a_copy_and_checks_json(server, game, settings):
    files = ConfigFiles(settings)
    first = files.read(server, 0)
    assert first["text"] == '{\n  "name": "test"\n}\n'
    with pytest.raises(configs.ConfigError, match="Not valid JSON"):
        files.save(server, 0, '{"name": ', first["sha256"])
    assert (game / "cfg" / "server.json").read_bytes() == b'{\r\n  "name": "test"\r\n}\r\n'
    result = files.save(server, 0, '{\n  "name": "new"\n}\n', first["sha256"])
    assert (game / "cfg" / "server.json").read_bytes() == b'{\r\n  "name": "new"\r\n}\r\n'
    copies = files.copies(server, 0)
    assert [c.name for c in copies] == [result["copy"]]
    assert files.read_copy(server, 0, result["copy"])["text"] == first["text"]
    assert not list((game / "cfg").glob("*.basecamp-tmp"))


def test_a_file_changed_on_disk_is_a_conflict(server, game, settings):
    files = ConfigFiles(settings)
    loaded = files.read(server, 0)
    (game / "cfg" / "server.json").write_text('{"name": "game wrote this"}')
    with pytest.raises(configs.Conflict):
        files.save(server, 0, "{}", loaded["sha256"])
    files.save(server, 0, "{}", None)  # the admin chose to overwrite
    assert (game / "cfg" / "server.json").read_text() == "{}"


def test_only_the_newest_copies_are_kept(server, settings):
    files = ConfigFiles(dataclasses.replace(settings, config_copies_keep=3))
    for i in range(6):
        files.save(server, 1, f"[S]\nN={i}\n", None)
    copies = files.copies(server, 1)
    assert len(copies) == 3
    assert files.read_copy(server, 1, copies[0].name)["text"] == "[S]\nN=4\n"  # newest first


def test_restore_puts_a_copy_back_and_keeps_the_current_one(server, game, settings):
    files = ConfigFiles(settings)
    original = (game / "cfg" / "Game.ini").read_bytes()
    saved = files.save(server, 1, "[S]\nbroken\n", None)
    result = files.restore(server, 1, saved["copy"])
    assert (game / "cfg" / "Game.ini").read_bytes() == original
    assert files.read_copy(server, 1, result["copy"])["text"] == "[S]\nbroken\n"
    assert len(files.copies(server, 1)) == 2


@pytest.mark.parametrize("name", ["..\\..\\x.ini", "../server.json", "20261027-120000.ini",
                                  "C:\\Windows\\win.ini", "x"])
def test_restore_takes_only_copy_names(server, settings, name):
    with pytest.raises(NotAllowed):
        ConfigFiles(settings).restore(server, 1, name)


def test_long_palworld_line_survives(server, game, settings):
    files = ConfigFiles(settings)
    loaded = files.read(server, 1)
    assert PALWORLD_LINE in loaded["text"].split("\n")
    edited = loaded["text"].replace('ServerName="Basecamp test"', 'ServerName="Renamed"')
    files.save(server, 1, edited, loaded["sha256"])
    text = (game / "cfg" / "Game.ini").read_bytes().decode()
    assert text.count("\r\n") == 2
    assert PALWORLD_LINE.replace("Basecamp test", "Renamed") in text


def test_missing_file_can_be_created_if_its_folder_exists(server, game, settings):
    files = ConfigFiles(settings)
    assert not files.read(server, 3)["exists"]
    result = files.save(server, 3, "a=1\n", "")
    assert result["copy"] is None
    assert (game / "cfg" / "missing.ini").read_bytes() == b"a=1\r\n"


# -- API -------------------------------------------------------------------------------------


@pytest.fixture
def client(store, settings) -> Iterator[TestClient]:
    web = dataclasses.replace(settings, admin_logins=("owner@example.com",))
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
        c.sup = sup  # type: ignore[attr-defined]
        yield c
    stop_everything(sup, store)


def test_api_list_read_save_restore_and_audit(client, server, game):
    files = client.get(f"/api/servers/{server.id}/configs").json()["files"]
    assert [f["name"] for f in files] == ["server.json", "Game.ini", "folder", "missing.ini"]
    assert "folder" in files[2]["error"] and files[3]["exists"] is False
    loaded = client.get(f"/api/servers/{server.id}/configs/0").json()
    assert loaded["encoding"] == "UTF-8, CRLF" and loaded["running"] is False
    bad = client.put(f"/api/servers/{server.id}/configs/0",
                     json={"text": "{nope", "base_sha256": loaded["sha256"]})
    assert bad.status_code == 400 and "JSON" in bad.json()["detail"]
    ok = client.put(f"/api/servers/{server.id}/configs/0",
                    json={"text": '{"a": 1}', "base_sha256": loaded["sha256"]})
    assert ok.status_code == 200, ok.text
    stale = client.put(f"/api/servers/{server.id}/configs/0",
                       json={"text": '{"a": 2}', "base_sha256": loaded["sha256"]})
    assert stale.status_code == 409
    copy = ok.json()["copy"]
    seen = client.get(f"/api/servers/{server.id}/configs/0/copies/{copy}").json()
    assert seen["text"] == loaded["text"]
    restored = client.post(f"/api/servers/{server.id}/configs/0/copies/{copy}/restore")
    assert restored.status_code == 200
    assert (game / "cfg" / "server.json").read_bytes() == b'{\r\n  "name": "test"\r\n}\r\n'
    actions = [e["action"] for e in client.get(f"/api/audit?server_id={server.id}").json()]
    assert actions.count("config.read") == 2
    assert {"config.save", "config.restore"} <= set(actions)


def test_api_bad_json_save_is_refused_and_audited(client, server):
    loaded = client.get(f"/api/servers/{server.id}/configs/0").json()
    res = client.put(f"/api/servers/{server.id}/configs/0",
                     json={"text": '{"name": ', "base_sha256": loaded["sha256"]})
    assert res.status_code == 400 and "Not valid JSON" in res.json()["detail"]
    entry = client.get(f"/api/audit?server_id={server.id}").json()[0]
    assert entry["action"] == "config.invalid"
    assert entry["detail"].startswith("server.json: Not valid JSON")


@pytest.mark.parametrize("path", [
    "/configs/9", "/configs/-1", "/configs/..%2F..%2Fsecret.ini", "/configs/cfg%5Csecret.ini",
    "/configs/0/copies/..%5C..%5Cserver.json", "/configs/0/copies/%2E%2E",
])
def test_api_refuses_anything_off_the_list(client, server, path):
    res = client.get(f"/api/servers/{server.id}{path}")
    assert res.status_code in (403, 404, 422), (path, res.status_code)
    assert "not listed" not in res.text


def test_api_folder_entry_is_refused_and_audited(client, server):
    res = client.get(f"/api/servers/{server.id}/configs/2")
    assert res.status_code == 403
    audit = client.get(f"/api/audit?server_id={server.id}").json()
    assert audit[0]["action"] == "config.refused"


def test_api_is_admin_only(client, server):
    friend = {"x-forwarded-for": "100.64.0.7", "host": "box.example.ts.net",
              "tailscale-user-login": "pal@example.com"}
    assert client.get(f"/api/servers/{server.id}/configs", headers=friend).status_code == 403
    assert client.get(f"/api/servers/{server.id}/configs/0", headers=friend).status_code == 403
    assert client.put(f"/api/servers/{server.id}/configs/0", headers=friend,
                      json={"text": "{}"}).status_code == 403
    assert client.get("/api/settings", headers=friend).status_code == 403


def test_api_says_running_so_the_page_offers_restart(client, store, tmp_path):
    cfg = tmp_path / "fake.json"
    cfg.write_text("{}")
    config = fake_config("cfgfake", "--tick", "0.3", config_files=[str(cfg)])
    s = client.post("/api/servers", json=config.model_dump(mode="json")).json()
    assert client.post(f"/api/servers/{s['id']}/start").status_code == 200
    wait_for(lambda: client.get(f"/api/servers/{s['id']}").json()["state"] == "running", 20)
    loaded = client.get(f"/api/servers/{s['id']}/configs/0").json()
    saved = client.put(f"/api/servers/{s['id']}/configs/0",
                       json={"text": '{"x": 1}', "base_sha256": loaded["sha256"]}).json()
    assert saved["running"] is True
    assert client.post(f"/api/servers/{s['id']}/restart").status_code == 202


# -- settings page ---------------------------------------------------------------------------


def test_settings_page_saves_to_toml_and_applies_live(client, store, game, settings):
    toml = settings.data_dir / "settings.toml"
    toml.write_text('# my notes\n[basecamp]\nadmin_logins = ["owner@example.com"]  # me\n'
                    'join_host = "old"\n\n[other]\nx = 1\n', encoding="utf-8")
    make_server(store, game, [], name="b1")
    res = client.put("/api/settings", json={
        "join_host": "100.64.0.1", "steamcmd_path": 'C:\\Steam Cmd\\steamcmd.exe',
        "default_backup_every_h": 6, "default_backup_keep": 4,
        "apply_backup_defaults": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body["changed"]) == {"join_host", "steamcmd_path", "default_backup_every_h",
                                    "default_backup_keep"}
    assert body["servers_updated"] == ["b1"]
    assert client.get("/api/me").json()["join_host"] == "100.64.0.1"
    text = toml.read_text(encoding="utf-8")
    assert "# my notes" in text and "# me" in text and "[other]\nx = 1" in text
    reloaded = Settings.load(settings.data_dir)
    assert reloaded.join_host == "100.64.0.1"
    assert reloaded.steamcmd_path == "C:\\Steam Cmd\\steamcmd.exe"
    assert reloaded.default_backup_keep == 4
    b1 = store.list_servers()[0]
    assert (b1.backup_every_h, b1.backup_keep) == (6, 4)
    audit = client.get("/api/audit").json()
    assert audit[1]["action"] == "settings"
    assert "100.64.0.1" not in json.dumps(audit)  # names only


@pytest.mark.parametrize("body", [
    {"join_host": "http://evil"}, {"join_host": "a b"}, {"steamcmd_path": "a\nb"},
    {"default_backup_keep": 0}, {"default_backup_every_h": -1},
])
def test_settings_page_validates(client, body):
    assert client.put("/api/settings", json=body).status_code == 422


def test_settings_page_masks_the_webhook(client, settings):
    from basecamp import notify
    url = "https://discord.com/api/webhooks/123/abcdefghijklmnop"
    notify.set_webhook(settings, url)
    text = client.get("/api/settings").text
    assert "abcdefghijklmnop" not in text and "abcd" in text
