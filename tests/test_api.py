"""Phase 2 web API: admin vs friend, the friend fields (R-70, R-71), actions, console and
status WebSockets, templates and import (R-13, R-15), audit log (R-83)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from basecamp.api import views
from basecamp.api.app import create_app
from basecamp.models import ServerState
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import fake_config, wait_for

ADMIN = "owner@example.com"
FRIEND = "pal@example.com"
JOIN_HOST = "<tailscale-ip>"
BASE = "http://127.0.0.1:8150"
SERVE = {"x-forwarded-for": "100.64.0.7", "host": "box.example.ts.net"}
AS_ADMIN = {**SERVE, "tailscale-user-login": ADMIN}
AS_FRIEND = {**SERVE, "tailscale-user-login": FRIEND}
# Things that must never reach a friend (R-71, R-82).
ADMIN_ONLY = ("config", "exe_path", "args", "install_dir", "pid", "discord_line", "notes",
              "rcon_port", "save_dir", "has_console", "last_exit_code")


@pytest.fixture
def web_settings(settings: Settings) -> Settings:
    return dataclasses.replace(settings, join_host=JOIN_HOST, admin_logins=(ADMIN,))


@pytest.fixture
def client(store, web_settings) -> Iterator[TestClient]:
    sup = Supervisor(store, web_settings)
    app = create_app(store, sup, web_settings)
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
        c.sup = sup  # type: ignore[attr-defined]
        yield c
    for server in store.list_servers():
        if sup.state(server.id).value in ("starting", "running", "stopping"):
            sup.kill(server.id)
    sup.detach()


def add_fake(client: TestClient, name: str = "fake", **fields) -> dict:
    config = fake_config(name, "--tick", "0.3", **fields).model_dump(mode="json")
    res = client.post("/api/servers", json=config)
    assert res.status_code == 201, res.text
    return res.json()


def ws_connect(client: TestClient, path: str, headers: dict | None = None):
    # The test client's WebSocket always says Host: testserver; a browser sends ours.
    return client.websocket_connect(path, headers={"host": "127.0.0.1:8150", **(headers or {})})


def all_keys(obj) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= all_keys(v)
    return keys


# -- who gets what -------------------------------------------------------------------


def test_local_and_listed_logins_are_admin_others_friends(client):
    assert client.get("/api/me").json()["is_admin"]  # this PC
    assert client.get("/api/me", headers=AS_ADMIN).json()["is_admin"]
    me = client.get("/api/me", headers=AS_FRIEND).json()
    assert me == {"login": FRIEND, "name": "", "is_admin": False, "version": me["version"]}


def test_friend_json_has_only_friend_fields(client):
    server = add_fake(client, notes="secret notes", save_dir=r"C:\saves", rcon_port=25575)
    client.post(f"/api/servers/{server['id']}/start")
    wait_for(lambda: client.get(f"/api/servers/{server['id']}").json()["state"] == "running")

    for res in (client.get("/api/servers", headers=AS_FRIEND),
                client.get(f"/api/servers/{server['id']}", headers=AS_FRIEND)):
        data = res.json()
        items = data if isinstance(data, list) else [data]
        for item in items:
            assert tuple(item) == views.FRIEND_FIELDS
        assert not all_keys(data) & set(ADMIN_ONLY)
        assert "secret notes" not in res.text and "fake_server" not in res.text

    friend = client.get("/api/servers", headers=AS_FRIEND).json()[0]
    assert friend["join_address"] == f"{JOIN_HOST}:{server['config']['join_port']}"
    assert friend["state"] == "running" and friend["uptime_s"] is not None

    admin = client.get("/api/servers", headers=AS_ADMIN).json()[0]
    assert admin["config"]["notes"] == "secret notes" and admin["pid"]
    assert admin["discord_line"] == f"fake is up \u2192 {friend['join_address']}"


def test_friend_status_websocket_is_trimmed_too(client):
    add_fake(client, notes="secret notes")
    with ws_connect(client, "/ws/status", headers=AS_FRIEND) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "servers"
    assert [tuple(s) for s in msg["servers"]] == [views.FRIEND_FIELDS]
    assert "secret notes" not in json.dumps(msg)
    with ws_connect(client, "/ws/status") as ws:
        assert "config" in ws.receive_json()["servers"][0]


def test_friend_cannot_do_admin_things(client):
    server = add_fake(client)
    sid = server["id"]
    config = server["config"]
    for method, path, body in [
        ("post", f"/api/servers/{sid}/start", None),
        ("post", f"/api/servers/{sid}/stop", None),
        ("post", f"/api/servers/{sid}/restart", None),
        ("post", f"/api/servers/{sid}/kill", None),
        ("post", f"/api/servers/{sid}/command", {"text": "save"}),
        ("get", f"/api/servers/{sid}/console", None),
        ("post", "/api/servers", config),
        ("put", f"/api/servers/{sid}", config),
        ("delete", f"/api/servers/{sid}", None),
        ("get", "/api/templates", None),
        ("post", "/api/drafts", {"template_id": "palworld"}),
        ("get", "/api/audit", None),
    ]:
        res = client.request(method, path, json=body, headers=AS_FRIEND)
        assert res.status_code == 403, (method, path, res.text)
    assert client.app.state is not None
    assert client.sup.state(sid) is ServerState.STOPPED  # type: ignore[attr-defined]
    with pytest.raises(WebSocketDisconnect) as e, \
            ws_connect(client, f"/ws/servers/{sid}/console", headers=AS_FRIEND):
        pass
    assert e.value.code == 4403


def test_spoofed_header_from_outside_loopback_is_a_friend(store, web_settings):
    sup = Supervisor(store, web_settings)
    app = create_app(store, sup, web_settings)
    try:
        with TestClient(app, base_url=BASE, client=("100.64.0.9", 40000)) as c:
            me = c.get("/api/me", headers={"tailscale-user-login": ADMIN}).json()
            assert not me["is_admin"] and me["login"] == ""
            assert not c.get("/api/me").json()["is_admin"]  # not local either
            assert c.post("/api/servers/1/start",
                          headers={"tailscale-user-login": ADMIN}).status_code == 403
            assert "Game servers" in c.get("/").text  # the friend page
    finally:
        sup.detach()


def test_tagged_device_through_serve_is_a_friend(client):
    # Serve sends no login for tagged devices; the request still comes from loopback.
    assert not client.get("/api/me", headers=SERVE).json()["is_admin"]
    assert "Game servers" in client.get("/", headers=SERVE).text
    assert "app.js" in client.get("/").text  # the admin page for this PC


def test_cross_site_and_rebinding_requests_are_refused(client):
    server = add_fake(client)
    res = client.post(f"/api/servers/{server['id']}/start",
                      headers={"origin": "https://evil.example"})
    assert res.status_code == 403
    assert client.get("/api/me", headers={"host": "evil.example:8150"}).status_code == 400
    with pytest.raises(WebSocketDisconnect) as e, \
            ws_connect(client, f"/ws/servers/{server['id']}/console",
                                     headers={"origin": "https://evil.example"}):
        pass
    assert e.value.code == 4403
    assert client.sup.state(server["id"]) is ServerState.STOPPED  # type: ignore[attr-defined]
    ok = client.post(f"/api/servers/{server['id']}/start", headers={"origin": BASE})
    assert ok.status_code == 200


# -- servers and actions ----------------------------------------------------------------


def test_start_console_command_stop_and_audit(client):
    server = add_fake(client)
    sid = server["id"]

    res = client.post(f"/api/servers/{sid}/start", headers=AS_ADMIN)
    assert res.status_code == 200 and res.json()["pid"]
    with ws_connect(client, f"/ws/servers/{sid}/console") as ws:
        lines = ws.receive_json()
        assert lines["type"] == "lines"
        seen = [x["text"] for x in lines["lines"]]
        while not any("Server ready" in t for t in seen):
            seen.append(ws.receive_json()["text"])
        ws.send_json({"type": "command", "text": "say hi from the web"})
        while not any(t == "hi from the web" for t in seen):
            seen.append(ws.receive_json()["text"])
    assert "say hi from the web" in seen  # echoed as a sent command

    # Reconnecting with ?after= replays only what's newer.
    last = client.get(f"/api/servers/{sid}/console").json()[-1]["seq"]
    with ws_connect(client, f"/ws/servers/{sid}/console?after={last}") as ws:
        assert ws.receive_json()["lines"] == []

    assert client.post(f"/api/servers/{sid}/stop").status_code == 202
    wait_for(lambda: client.get(f"/api/servers/{sid}").json()["state"] == "stopped", 30)

    audit = client.get("/api/audit").json()
    assert [(a["who"], a["action"]) for a in reversed(audit)] == [
        ("localhost", "server.add"), (ADMIN, "start"), ("localhost", "console"),
        ("localhost", "stop")]
    assert audit[1]["detail"] == "say hi from the web"


def test_command_over_rest_and_errors(client):
    server = add_fake(client)
    sid = server["id"]
    res = client.post(f"/api/servers/{sid}/command", json={"text": "save"})
    assert res.status_code == 409 and "isn't running" in res.json()["detail"]
    assert client.post("/api/servers/999/start").status_code == 404


def test_duplicate_port_is_refused_at_start(client):
    first = add_fake(client, "one")
    port = first["config"]["join_port"]
    second = add_fake(client, "two", port=port)
    assert client.post(f"/api/servers/{first['id']}/start").status_code == 200
    res = client.post(f"/api/servers/{second['id']}/start")
    assert res.status_code == 409 and "in use" in res.json()["detail"]


def test_add_edit_delete(client):
    server = add_fake(client)
    sid = server["id"]
    assert client.post("/api/servers", json=server["config"]).status_code == 409  # same name
    bad = {**server["config"], "ports": [{"port": 70000}]}
    res = client.post("/api/servers", json={**bad, "name": "other"})
    assert res.status_code == 422

    edited = {**server["config"], "notes": "edited", "start_on_boot": True}
    res = client.put(f"/api/servers/{sid}", json=edited)
    assert res.status_code == 200 and res.json()["config"]["notes"] == "edited"

    client.post(f"/api/servers/{sid}/start")
    assert client.delete(f"/api/servers/{sid}").status_code == 409  # running
    client.sup.kill(sid)  # type: ignore[attr-defined]
    wait_for(lambda: client.get(f"/api/servers/{sid}").json()["state"] == "stopped")
    assert client.delete(f"/api/servers/{sid}").status_code == 204
    assert client.get("/api/servers").json() == []
    actions = [a["action"] for a in client.get("/api/audit").json()]
    assert actions[0] == "server.delete" and "server.update" in actions


# -- templates and import ------------------------------------------------------------


def test_templates_list_sources_and_errors(client, web_settings):
    folder = web_settings.data_templates_dir
    folder.mkdir(parents=True)
    builtin = json.loads((web_settings.templates_dir / "palworld.json").read_text("utf-8"))
    (folder / "palworld.json").write_text(json.dumps({**builtin, "args": "-port=9999"}))
    (folder / "broken.json").write_text('{"id": "broken", "name": ')

    res = client.get("/api/templates").json()
    by_id = {t["id"]: t for t in res["templates"]}
    assert {"palworld", "enshrouded", "v-rising", "dragonwilds", "windrose"} <= set(by_id)
    assert by_id["palworld"]["source"] == "data folder" and by_id["palworld"]["overrides"]
    assert by_id["palworld"]["args"] == "-port=9999"
    assert by_id["enshrouded"]["source"] == "built-in"
    assert res["errors"] == [{"file": "data folder: broken.json",
                              "error": "not valid JSON: line 1 column 26: Expecting value"}]


def test_draft_from_template_then_save(client, tmp_path):
    install = tmp_path / "Palworld"
    install.mkdir()
    res = client.post("/api/drafts", json={"template_id": "palworld",
                                           "install_dir": str(install)}).json()
    draft = res["draft"]
    assert draft["exe_path"] == str(install / "PalServer.exe")
    assert draft["config_files"] == [
        str(install / "Pal" / "Saved" / "Config" / "WindowsServer" / "PalWorldSettings.ini")]
    assert draft["join_port"] == 8211 and draft["template_id"] == "palworld"
    assert any("doesn't exist" in m for m in res["messages"])
    assert "real_exe_name" in res["unverified"]
    saved = client.post("/api/servers", json=draft)
    assert saved.status_code == 201 and saved.json()["config"]["template_id"] == "palworld"


def test_import_existing_install_reads_appmanifest(client, tmp_path):
    install = tmp_path / "Enshrouded"
    (install / "steamapps").mkdir(parents=True)
    (install / "enshrouded_server.exe").write_bytes(b"")
    (install / "steamapps" / "appmanifest_2278520.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"2278520"\n\t"name"\t\t"Enshrouded Dedicated Server"\n'
        '\t"buildid"\t\t"12345678"\n\t"UserConfig"\n\t{\n\t\t"language"\t\t"english"\n\t}\n}\n')
    res = client.post("/api/drafts", json={"install_dir": str(install)}).json()
    assert res["draft"]["template_id"] == "enshrouded"
    assert res["draft"]["steam_app_id"] == 2278520
    assert res["messages"][0].startswith("Found Enshrouded Dedicated Server (app 2278520, "
                                         "build 12345678)")

    unknown = tmp_path / "Other"
    (unknown / "steamapps").mkdir(parents=True)
    (unknown / "steamapps" / "appmanifest_42.acf").write_text('"AppState" { "appid" "42" }')
    res = client.post("/api/drafts", json={"install_dir": str(unknown)}).json()
    assert res["draft"]["steam_app_id"] == 42 and "no template" in res["messages"][0]

    empty = client.post("/api/drafts", json={"install_dir": str(tmp_path)})
    assert empty.status_code == 400 and "appmanifest" in empty.json()["detail"]
    missing = client.post("/api/drafts", json={"install_dir": str(tmp_path / "nope")})
    assert missing.status_code == 400


def test_pages_and_health(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/healthz").json()["ok"]
    assert "Basecamp" in client.get("/").text
