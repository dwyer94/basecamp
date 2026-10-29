"""settings.toml picked up without a restart (D-127): what applies live, what waits for a
restart, a broken file, and admin_logins taking effect on the next request."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from basecamp.api.app import create_app
from basecamp.db import Store
from basecamp.settings import RESTART_SETTINGS, Settings, SettingsFile
from basecamp.supervisor import Supervisor
from tests.conftest import stop_everything

ADMIN = "boss@example.com"
AS_FRIEND = {"x-forwarded-for": "100.64.0.7", "host": "box.example.ts.net",
             "tailscale-user-login": ADMIN}


def write(data: Path, text: str) -> None:
    path = data / "settings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(text, encoding="utf-8")
    if path.stat().st_mtime_ns == before:  # same tick: make the change visible
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))


def test_reload_applies_live_keys_and_holds_restart_ones(tmp_path):
    data = tmp_path / "data"
    write(data, '[basecamp]\njoin_host = "a"\nport = 8150\n')
    s = Settings.load(data, low_ram_mb=0)  # an override the file doesn't mention
    seen = []
    watch = SettingsFile(s, lambda who, r: seen.append((who, r)), every_s=0)
    write(data, '[basecamp]\njoin_host = "b"\nport = 8151\nadmin_logins = ["x@y"]\n'
                'crash_limit = 5\n')
    result = watch.check()
    assert sorted(result["applied"]) == ["admin_logins", "crash_limit", "join_host"]
    assert result["restart"] == ["port"]
    assert s.join_host == "b" and s.admin_logins == ("x@y",) and s.crash_limit == 5
    assert s.port == 8150 and s.low_ram_mb == 0
    assert watch.view()["restart_pending"] == [{"key": "port", "now": "8150", "after": "8151"}]
    assert seen == [("settings.toml", result)]
    assert watch.check() is None  # unchanged since

    write(data, '[basecamp]\nport = 8150\n')  # keys taken out go back to their defaults
    result = watch.reload("me")
    assert sorted(result["applied"]) == ["admin_logins", "crash_limit", "join_host"]
    assert s.join_host == "" and s.admin_logins == () and s.crash_limit == 3
    assert watch.view()["restart_pending"] == []


@pytest.mark.parametrize("text, error", [
    ("[basecamp\nport = 1", "isn't valid TOML"),
    ('[basecamp]\nprot = 1\n', "Unknown settings: prot"),
    ('[basecamp]\nport = "8150x"\n', "port should be int"),
    ('[basecamp]\ndiscord_live_status = 1\n', "discord_live_status should be bool"),
    ('[basecamp]\njoin_host = 5\n', "join_host should be str"),
])
def test_a_bad_file_keeps_the_old_settings(tmp_path, text, error):
    data = tmp_path / "data"
    write(data, '[basecamp]\njoin_host = "a"\n')
    s = Settings.load(data)
    watch = SettingsFile(s, every_s=0)
    write(data, text)
    result = watch.check()
    assert error in result["error"][0] and error in watch.view()["error"]
    assert s.join_host == "a" and s.port == 8150
    write(data, '[basecamp]\njoin_host = "c"\ncrash_window_s = 60\n')  # int for a float
    assert watch.check()["applied"] == ["join_host", "crash_window_s"]
    assert watch.error == "" and s.crash_window_s == 60.0


def test_page_settings_saves_are_not_reported_as_reloads(tmp_path):
    data = tmp_path / "data"
    s = Settings.load(data)
    seen = []
    watch = SettingsFile(s, lambda who, r: seen.append(r), every_s=0)
    s.save_page_settings({"join_host": "100.1.2.3"})
    assert watch.check() == {"applied": [], "restart": []}


def test_restart_settings_are_the_ones_read_once():
    assert set(RESTART_SETTINGS) == {"host", "port", "migrations_dir", "console_lines",
                                     "console_log_bytes", "console_log_backups"}


def test_admin_logins_apply_on_the_next_request(tmp_path):
    data = tmp_path / "data"
    write(data, "[basecamp]\nadmin_logins = []\n")
    settings = Settings.load(data, low_ram_mb=0)
    store = Store.open(settings.db_path, settings.migrations_dir)
    sup = Supervisor(store, settings)
    app = create_app(store, sup, settings, settings_check_s=0)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8150", client=("127.0.0.1", 5)) as c:
            assert not c.get("/api/me", headers=AS_FRIEND).json()["is_admin"]
            write(data, f'[basecamp]\nadmin_logins = ["{ADMIN}"]\n')
            assert c.get("/api/me", headers=AS_FRIEND).json()["is_admin"]
            assert c.get("/api/settings", headers=AS_FRIEND).status_code == 200

            write(data, '[basecamp]\nadmin_logins = ["' + ADMIN + '"]\nport = 9999\n')
            res = c.post("/api/settings/reload").json()
            assert res["reloaded"] == {"applied": [], "restart": ["port"]}
            assert res["file"]["restart_pending"][0]["key"] == "port"

            write(data, "[basecamp\n")
            res = c.post("/api/settings/reload").json()
            assert "isn't valid TOML" in res["file"]["error"]
            assert c.get("/api/me", headers=AS_FRIEND).json()["is_admin"]  # kept

            write(data, "[basecamp]\n")
            assert not c.get("/api/me", headers=AS_FRIEND).json()["is_admin"]
            assert c.post("/api/settings/reload", headers=AS_FRIEND).status_code == 403
        details = [(e.who, e.detail) for e in store.list_audit()
                   if e.action == "settings.reload"]
        assert ("settings.toml", "applied admin_logins") in details
        assert ("localhost", "after a restart port") in details
        assert any(d.startswith("not applied: settings.toml isn't valid TOML")
                   for _, d in details)
        assert all(ADMIN not in d for _, d in details)  # names, never values
    finally:
        stop_everything(sup, store)
        store.close()


def test_open_status_socket_closes_when_the_login_loses_admin(tmp_path):
    data = tmp_path / "data"
    write(data, f'[basecamp]\nadmin_logins = ["{ADMIN}"]\n')
    settings = Settings.load(data, low_ram_mb=0)
    store = Store.open(settings.db_path, settings.migrations_dir)
    sup = Supervisor(store, settings)
    app = create_app(store, sup, settings, settings_check_s=0)
    headers = {**AS_FRIEND, "host": "127.0.0.1:8150"}
    try:
        with (TestClient(app, base_url="http://127.0.0.1:8150", client=("127.0.0.1", 5)) as c,
              c.websocket_connect("/ws/status", headers=headers) as ws):
            assert ws.receive_json()["type"] == "servers"
            write(data, "[basecamp]\n")
            c.get("/api/me")  # any request picks the change up
            app.state.hub.poke()
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401
    finally:
        stop_everything(sup, store)
        store.close()


def test_a_restart_key_put_back_is_not_reported(tmp_path):
    data = tmp_path / "data"
    write(data, "[basecamp]\nport = 8150\n")
    s = Settings.load(data)
    watch = SettingsFile(s, every_s=0)
    write(data, "[basecamp]\nport = 8151\n")
    assert watch.check()["restart"] == ["port"]
    write(data, "[basecamp]\n")  # back to the default, which is what runs
    assert watch.check() == {"applied": [], "restart": []}
    assert watch.view()["restart_pending"] == []
