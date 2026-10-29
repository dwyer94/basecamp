"""New game from a template in one flow (batch 2, item 6; D-130), against
tools/fake_steamcmd.py and tools/fake_server.py."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from basecamp import setup as setup_mod
from basecamp import templates
from basecamp.api.app import create_app
from basecamp.models import ServerKind
from basecamp.setup import Setups
from basecamp.supervisor import Supervisor
from tests.conftest import (
    FAKE_SERVER,
    PYTHON,
    REPO,
    fake_config,
    free_port,
    stop_everything,
    wait_for,
)
from tests.test_api import ADMIN, AS_FRIEND, BASE

FAKE_STEAMCMD = REPO / "tools" / "fake_steamcmd.py"
APP_ID = 2394010
CONFIG = "Config\\Settings.ini"


# -- writing one value ---------------------------------------------------------------------


def test_ini_set_changes_adds_and_makes_sections():
    text = "[A]\nOwnerId=\nName = x\n\n[B]\nOwnerId=keep\n"
    out = setup_mod.ini_set(text, "a", "ownerid", "123")
    assert out == "[A]\nOwnerId=123\nName = x\n\n[B]\nOwnerId=keep\n"
    assert setup_mod.ini_get(out, "A", "OwnerId") == "123"
    assert setup_mod.ini_get(out, "B", "OwnerId") == "keep"
    out = setup_mod.ini_set(text, "A", "Password", "p w")
    assert out == "[A]\nOwnerId=\nName = x\nPassword=p w\n\n[B]\nOwnerId=keep\n"
    out = setup_mod.ini_set(text, "/Script/New", "OwnerId", "9")
    assert out.endswith("OwnerId=keep\n\n[/Script/New]\nOwnerId=9\n")
    assert setup_mod.ini_set("", "S", "K", "v") == "[S]\nK=v\n"
    assert setup_mod.ini_set("x=1\n[S]\n", "", "y", "2") == "x=1\ny=2\n[S]\n"
    assert setup_mod.ini_get("[S]\nK=v\n", "T", "K") is None


def test_json_set_keeps_types_and_indent():
    text = '{\n    "Name": "a",\n    "Max": 4,\n    "On": true,\n    "Deep": {"Id": ""}\n}\n'
    out = setup_mod.json_set(text, "Deep.Id", "77")
    assert json.loads(out)["Deep"]["Id"] == "77"
    assert '\n    "Name"' in out
    assert json.loads(setup_mod.json_set(text, "Max", "8"))["Max"] == 8
    assert json.loads(setup_mod.json_set(text, "On", "false"))["On"] is False
    with pytest.raises(setup_mod.SetupError, match="number"):
        setup_mod.json_set(text, "Max", "lots")
    assert json.loads(setup_mod.json_set("", "A.B", "c")) == {"A": {"B": "c"}}
    assert setup_mod.json_get(out, "Deep.Id") == "77"
    with pytest.raises(setup_mod.SetupError, match="one line"):
        setup_mod.check_value("Id", "a\nb")


def test_required_values_must_name_a_config_file():
    base = {"id": "g", "name": "G", "kind": "custom", "exe": "g.exe",
            "ports": [{"port": 1, "protocol": "udp"}], "config_files": ["a.ini", "b.json"]}
    ok = templates.Template.model_validate(
        {**base, "required_values": [{"file": "A.INI", "section": "S", "key": "K",
                                      "label": "K"}]})
    assert ok.required_values[0].key == "K"
    for bad, why in (({"file": "c.ini", "key": "K", "label": "K"}, "isn't one of"),
                     ({"file": "b.json", "section": "S", "key": "K", "label": "K"}, "section"),
                     ({"file": "a.ini", "key": "K=1", "label": "K"}, "key"),
                     ({"file": "a.ini", "key": "K", "label": "K", "typo": 1}, "typo")):
        with pytest.raises(ValueError, match=why):
            templates.Template.model_validate({**base, "required_values": [bad]})
    dragonwilds = templates.load_file(REPO / "templates" / "dragonwilds.json")
    assert [v.key for v in dragonwilds.required_values] == ["OwnerId"]


# -- the whole flow over the API -----------------------------------------------------------


def write_template(settings, port: int) -> None:
    args = subprocess.list2cmdline(["-I", str(FAKE_SERVER), "--name", "Fake", "--port",
                                    str(port), "--write-config", CONFIG])
    template = {
        "id": "fake-game", "name": "Fake game", "kind": "steam", "steam_app_id": APP_ID,
        "steam_installdir": "FakeGame", "exe": str(PYTHON), "args": args, "working_dir": ".",
        "ports": [{"port": port, "protocol": "udp", "label": "game"}],
        "ready_pattern": "Server ready", "ready_after_s": 20,
        "stop_recipe": [{"action": "stdin", "command": "quit", "wait_s": 10}],
        "config_files": [CONFIG],
        "required_values": [{"file": CONFIG, "section": "/Script/Fake.Settings",
                             "key": "OwnerId", "label": "Your player ID",
                             "hint": "In the game's settings"}],
    }
    folder = settings.data_templates_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake-game.json").write_text(json.dumps(template), encoding="utf-8")


@pytest.fixture(autouse=True)
def fake_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKE_STEAMCMD_DELAY", "0.02")
    monkeypatch.setenv("FAKE_STEAMCMD_STATE", str(tmp_path / "selfupdated"))
    monkeypatch.delenv("FAKE_STEAMCMD_MODE", raising=False)


@pytest.fixture
def web(settings, tmp_path):
    steamcmd = tmp_path / "SteamCmd" / "steamcmd.py"  # its steamapps\common is the default
    steamcmd.parent.mkdir()
    steamcmd.write_bytes(FAKE_STEAMCMD.read_bytes())
    return dataclasses.replace(settings, admin_logins=(ADMIN,), join_host="<tailscale-ip>",
                               steamcmd_path=str(steamcmd), setup_config_wait_s=5)


@pytest.fixture
def client(store, web) -> Iterator[TestClient]:
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
        c.sup = sup  # type: ignore[attr-defined]
        c.app_ = app  # type: ignore[attr-defined]
        yield c
    app.state.steam.close()
    stop_everything(sup, store)


def wait_step(c: TestClient, sid: int, step: str, status: str = "waiting",
              timeout: float = 60) -> dict:
    view: dict = {}

    def there() -> bool:
        nonlocal view
        view = c.get(f"/api/setups/{sid}").json()
        assert view["status"] != "failed" or status == "failed", view["message"]
        return view["step"] == step and view["status"] == status

    wait_for(there, timeout=timeout, interval=0.2)
    return view


def test_new_game_flow_end_to_end(client, web):
    write_template(web, free_port())
    assert client.post("/api/setups", json={"template_id": "fake-game"},
                       headers=AS_FRIEND).status_code == 403
    res = client.post("/api/setups", json={"template_id": "fake-game", "name": "My game"})
    assert res.status_code == 201, res.text
    sid = res.json()["server_id"]
    server = client.get(f"/api/servers/{sid}").json()["config"]
    install = Path(web.steamcmd_exe).parent / "steamapps" / "common" / "FakeGame"
    assert server["install_dir"] == str(install) and server["kind"] == "steam"

    view = wait_step(client, sid, "values")
    assert [s["state"] for s in view["steps"]] == ["done", "done", "current", "todo", "todo"]
    assert view["values"] == [{"n": 0, "label": "Your player ID", "hint": "In the game's settings",
                               "file": "Settings.ini", "key": "OwnerId",
                               "section": "/Script/Fake.Settings", "current": "", "error": ""}]
    assert client.get(f"/api/servers/{sid}").json()["state"] == "stopped"
    config = install / CONFIG
    assert config.read_bytes() == b"[/Script/Fake.Settings]\r\nServerName=Fake\r\nOwnerId=\r\n"
    assert any(s["server_id"] == sid for s in client.get("/api/setups").json())

    assert client.post(f"/api/setups/{sid}/values", json={"values": {"0": " "}}).status_code \
        == 409
    res = client.post(f"/api/setups/{sid}/values", json={"values": {"0": "12345"}})
    assert res.status_code == 200, res.text
    assert res.json()["step"] == "firewall" and res.json()["values"][0]["current"] == "12345"
    assert config.read_bytes() == \
        b"[/Script/Fake.Settings]\r\nServerName=Fake\r\nOwnerId=12345\r\n"
    copies = client.get(f"/api/servers/{sid}/configs/0").json()["copies"]
    assert len(copies) == 1 and copies[0]["reason"] == "save"  # the config editor's copy

    assert client.post(f"/api/setups/{sid}/next").json()["step"] == "ready"
    res = client.post(f"/api/setups/{sid}/finish?start=true")
    assert res.status_code == 200, res.text
    assert client.get(f"/api/setups/{sid}").status_code == 404
    wait_for(lambda: client.get(f"/api/servers/{sid}").json()["state"] == "running", 20)
    actions = [e["action"] for e in client.get("/api/audit").json()]
    for action in ("server.add", "setup.start", "install", "setup.values", "setup.next",
                   "setup.finish"):
        assert action in actions
    values = next(e for e in client.get("/api/audit").json() if e["action"] == "setup.values")
    assert values["detail"] == "OwnerId"  # the key, never the value


def test_a_failed_install_stops_there_and_tries_again(client, web, monkeypatch):
    write_template(web, free_port())
    monkeypatch.setenv("FAKE_STEAMCMD_MODE", "fail")
    sid = client.post("/api/setups", json={"template_id": "fake-game"}).json()["server_id"]
    view = wait_step(client, sid, "install", "failed")
    assert "Install failed" in view["message"] and "Disk write failure" in view["message"]
    assert client.post(f"/api/setups/{sid}/values", json={"values": {"0": "1"}}).status_code \
        == 409
    monkeypatch.delenv("FAKE_STEAMCMD_MODE")
    # The failed run left a manifest-less folder; the fake writes nothing, so it's empty.
    res = client.post(f"/api/setups/{sid}/retry")
    assert res.status_code == 200, res.text
    wait_step(client, sid, "values")
    assert client.post(f"/api/setups/{sid}/retry").status_code == 409  # nothing failed


def test_refuses_a_folder_with_something_else_in_it(client, web, tmp_path):
    write_template(web, free_port())
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.txt").write_text("x")
    res = client.post("/api/setups", json={"template_id": "fake-game",
                                           "install_dir": str(other)})
    assert res.status_code == 409 and "isn't empty" in res.json()["detail"]
    assert client.get("/api/servers").json() == []
    assert client.post("/api/setups", json={"template_id": "nope"}).status_code == 404


def test_an_installed_game_with_its_config_skips_to_the_values(client, web, tmp_path):
    write_template(web, free_port())
    install = tmp_path / "ready"
    (install / "Config").mkdir(parents=True)
    (install / CONFIG).write_text("[/Script/Fake.Settings]\nOwnerId=old\n")
    manifest = install / "steamapps" / f"appmanifest_{APP_ID}.acf"
    manifest.parent.mkdir()
    manifest.write_text(f'"AppState"\n{{\n\t"appid"\t\t"{APP_ID}"\n\t"buildid"\t\t"7"\n}}\n')
    sid = client.post("/api/setups", json={"template_id": "fake-game",
                                           "install_dir": str(install)}).json()["server_id"]
    view = wait_step(client, sid, "values", timeout=10)
    assert view["values"][0]["current"] == "old"
    assert "no first start" in view["message"]


def test_a_running_step_is_failed_after_a_restart(store, settings, tmp_path):
    server = store.add_server(fake_config("s", kind=ServerKind.STEAM, steam_app_id=1,
                                          install_dir=str(tmp_path / "x")))
    store.add_setup(server.id, "t", "install", "running", [], "owner")
    sup = Supervisor(store, settings)
    try:
        Setups(store, sup, None, None, settings)  # type: ignore[arg-type]
        setup = store.get_setup(server.id)
        assert setup.status == "failed" and "restarted" in setup.message
        store.delete_server(server.id)
        with pytest.raises(KeyError):
            store.get_setup(server.id)  # goes with the server
    finally:
        stop_everything(sup, store)

