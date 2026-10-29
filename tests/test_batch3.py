"""Batch 3 backend (D-134..D-137): the Activity feed and its red count, the friend views
key by key, friend Update and quick commands, the read-only friend console, last updated."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from basecamp import activity, steam, templates
from basecamp.api import views
from basecamp.api.app import _friend_line, create_app
from basecamp.console import ConsoleLine, LineSource
from basecamp.models import ServerKind, ServerState
from basecamp.notify import Notifier
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import REPO, fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_ADMIN, AS_FRIEND, BASE, FRIEND, SERVE, ws_connect

FAKE_STEAMCMD = REPO / "tools" / "fake_steamcmd.py"
APP_ID = 2394010
QUICK_TEMPLATE = {
    "id": "quicky", "name": "Quicky", "kind": "custom", "exe": "fake.exe",
    "ports": [{"port": 7777, "protocol": "udp", "label": "game"}],
    "quick_commands": [
        {"label": "Save", "command": "save", "channel": "stdin", "friend": True},
        {"label": "Say…", "command": "say {message}", "channel": "stdin"},
        {"label": "Quit", "command": "quit", "channel": "stdin"},
    ],
}


def make_client(store, settings: Settings, **overrides) -> TestClient:
    web = dataclasses.replace(settings, admin_logins=(ADMIN,), join_host="<tailscale-ip>",
                              steamcmd_path=str(FAKE_STEAMCMD), **overrides)
    sup = Supervisor(store, web)
    c = TestClient(create_app(store, sup, web), base_url=BASE, client=("127.0.0.1", 50123))
    c.sup = sup  # type: ignore[attr-defined]
    return c


@pytest.fixture
def client(store, settings) -> Iterator[TestClient]:
    with make_client(store, settings) as c:
        yield c
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


@pytest.fixture
def anyone(store, settings, monkeypatch, tmp_path) -> Iterator[TestClient]:
    monkeypatch.setenv("FAKE_STEAMCMD_DELAY", "0.02")
    monkeypatch.setenv("FAKE_STEAMCMD_STATE", str(tmp_path / "selfupdated"))
    with make_client(store, settings, friend_control="anyone", friend_control_gap_s=0) as c:
        yield c
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def running(c: TestClient, store, name: str = "fake", **fields):
    server = store.add_server(fake_config(name, "--tick", "0.3", **fields))
    c.sup.start(server.id)  # type: ignore[attr-defined]
    wait_for(lambda: c.sup.state(server.id) is ServerState.RUNNING, 20)  # type: ignore[attr-defined]
    return server


def steam_install(tmp_path: Path, name: str, last_updated: int | None = None) -> Path:
    install = tmp_path / name
    (install / "Saved").mkdir(parents=True)
    (install / "Saved" / "world.sav").write_text("day 1", encoding="utf-8")
    manifest = install / "steamapps" / f"appmanifest_{APP_ID}.acf"
    manifest.parent.mkdir()
    extra = f'\t"LastUpdated"\t\t"{last_updated}"\n' if last_updated else ""
    manifest.write_text(f'"AppState"\n{{\n\t"appid"\t\t"{APP_ID}"\n\t"buildid"\t\t"1500"\n'
                        f"{extra}}}\n", encoding="utf-8")
    return install


# -- Activity: failures, dismiss, clearing (D-134) --------------------------------------


def test_failures_count_until_dismissed_or_running_again(client, store):
    server = store.add_server(fake_config("crashy"))
    activity.record(store, "crash.gave_up", server.id, "Crashed 3 times")
    activity.record(store, "notify.failed", None, "HTTP 404: crashy crashed")
    activity.record(store, "steam.update.failed", server.id, "SteamCMD failed")
    assert client.get("/api/me").json()["failures"] == 3
    feed = client.get("/api/activity").json()
    assert feed["open_failures"] == 3
    assert [i["action"] for i in feed["items"]][:3] == [
        "steam.update.failed", "notify.failed", "crash.gave_up"]
    assert all(i["failure"] and i["open"] for i in feed["items"][:3])

    # Running normally again clears the crash, not the update or Discord.
    client.sup.start(server.id)  # type: ignore[attr-defined]
    wait_for(lambda: client.sup.state(server.id) is ServerState.RUNNING, 20)  # type: ignore[attr-defined]
    wait_for(lambda: client.get("/api/activity").json()["open_failures"] == 2)
    gave_up = next(i for i in client.get("/api/activity").json()["items"]
                   if i["action"] == "crash.gave_up")
    assert not gave_up["open"] and gave_up["cleared_by"] == "basecamp"

    # A good update clears the failed one; Discord only on Dismiss.
    activity.record(store, "steam.update.ok", server.id, "updated")
    assert client.get("/api/activity").json()["open_failures"] == 1
    discord = next(i for i in client.get("/api/activity").json()["items"]
                   if i["action"] == "notify.failed")
    assert client.post(f"/api/activity/{discord['id']}/dismiss").json() == {"open_failures": 0}
    assert client.post(f"/api/activity/{discord['id']}/dismiss").status_code == 404
    activity.record(store, "backup.failed", server.id, "disk full")
    res = client.post("/api/activity/dismiss", headers=AS_ADMIN).json()
    assert res == {"dismissed": 1, "open_failures": 0}
    cleared = next(i for i in client.get("/api/activity").json()["items"]
                   if i["action"] == "backup.failed")
    assert cleared["cleared_by"] == ADMIN


def test_status_websocket_carries_the_count_for_admins_only(client, store):
    activity.record(store, "notify.failed", None, "HTTP 404")
    with ws_connect(client, "/ws/status") as ws:
        msg = ws.receive_json()
    assert msg["failures"] == 1 and set(msg["host"]) == {"ram_free_mb", "ram_total_mb",
                                                         "boot_time"}
    with ws_connect(client, "/ws/status", headers=AS_FRIEND) as ws:
        msg = ws.receive_json()
    assert set(msg) == {"type", "servers", "host"}
    assert set(msg["host"]) == {"ram_free_mb", "ram_total_mb"}


def test_crash_and_discord_failures_are_written(store, settings, webhook):
    server = store.add_server(fake_config("crashy"))
    from basecamp.crashes import CrashPolicy
    sup = Supervisor(store, settings)
    try:
        CrashPolicy(store, sup, dataclasses.replace(settings, crash_limit=1), None)
        sup.start(server.id)
        wait_for(lambda: sup.state(server.id) is ServerState.RUNNING, 20)
        sup.send_command(server.id, "crash")
        wait_for(lambda: any(e.action == "crash.gave_up" for e in store.list_audit()), 20)
    finally:
        stop_everything(sup, store)
    webhook.answers.append((404, {"message": "Unknown Webhook"}))
    notifier = Notifier(dataclasses.replace(settings, notify_retry_s=()), store)
    try:
        notifier._deliver(type("M", (), {"url": webhook.url, "text": ":boom: **x** crashed",
                                         "tries": 0})())
    finally:
        notifier.close()
    entry = store.list_audit(1)[0]
    assert entry.action == "notify.failed" and entry.server_id is None
    assert "x crashed" in entry.detail and webhook.url not in entry.detail


# -- friend views, key by key (D-30, D-135) ---------------------------------------------


def test_friend_card_and_detail_keys(client, store, tmp_path):
    install = steam_install(tmp_path, "game", last_updated=1761700000)
    server = running(client, store, "game", kind=ServerKind.STEAM, steam_app_id=APP_ID,
                     install_dir=str(install), save_dir="Saved", notes="secret notes",
                     restart_at="05:00", template_id="palworld")
    client.post(f"/api/servers/{server.id}/backups")
    card = client.get("/api/servers", headers=AS_FRIEND).json()[0]
    assert tuple(card) == views.FRIEND_FIELDS
    assert card["last_updated"] == {"at": 1761700000.0, "source": "steam"}
    detail = client.get(f"/api/servers/{server.id}", headers=AS_FRIEND).json()
    assert tuple(detail) == views.FRIEND_DETAIL_FIELDS
    assert tuple(detail["schedule"]) == views.FRIEND_SCHEDULE_FIELDS
    assert detail["schedule"]["restart_at"] == "05:00"
    assert detail["build_id"] == "1500" and detail["last_backup"] is not None
    # Palworld's template: friends get Save and Players by number, never the commands.
    assert detail["quick_commands"] == [{"n": 0, "label": "Save"}, {"n": 1, "label": "Players"}]
    text = json.dumps(detail)
    for secret in ("secret notes", str(install).replace("\\", "\\\\"), "fake_server", "Saved"):
        assert secret not in text
    admin = client.get(f"/api/servers/{server.id}").json()
    assert [q["command"] for q in admin["quick_commands"]] == [
        "save", "players", "announce {message}"]
    assert admin["last_updated"]["source"] == "steam"


def test_friend_job_view_keys():
    job = {"kind": "update", "phase": "steamcmd", "progress": 40.0, "position": 0,
           "who": ADMIN, "run_id": 3}
    card = views._job({"job": job})
    assert tuple(card) == views.FRIEND_JOB_FIELDS and "who" not in card


def test_friend_activity_is_filtered_on_the_server(client, store):
    server = store.add_server(fake_config("game"))
    store.add_audit(ADMIN, "settings", None, "friend_control")
    store.add_audit(ADMIN, "config.save", server.id, "Game.ini")
    store.add_audit(ADMIN, "template.add", None, "valheim")
    store.add_audit(ADMIN, "backup.restore", server.id, "x.zip")
    store.add_audit(ADMIN, "update.install", None, "0.2 -> 0.3")
    activity.record(store, "notify.failed", None, "HTTP 404")
    store.add_audit(ADMIN, "console", server.id, "say hi")
    store.add_audit(ADMIN, "restart", server.id, "game")
    store.add_audit(FRIEND, "friend.stop", server.id, "game")
    activity.record(store, "crash", server.id, "exit code 3 at C:\\games\\x")
    items = client.get("/api/activity", headers=AS_FRIEND).json()
    assert set(items) == {"items"}  # no red count for friends
    rows = items["items"]
    assert all(tuple(r) == activity.FRIEND_ITEM_FIELDS for r in rows)
    assert [r["action"] for r in rows] == ["crash", "friend.stop", "restart", "console"]
    who = {r["action"]: r["who"] for r in rows}
    assert who == {"crash": "basecamp", "friend.stop": FRIEND, "restart": "owner",
                   "console": "owner"}
    detail = {r["action"]: r["detail"] for r in rows}
    assert detail == {"crash": "", "friend.stop": "", "restart": "", "console": "say hi"}
    assert ADMIN not in json.dumps(items)
    history = client.get(f"/api/activity?server_id={server.id}&failures=true",
                         headers=AS_FRIEND).json()["items"]
    assert [r["action"] for r in history] == ["crash"]


def test_owner_only_routes_refuse_friends(client, store):
    server = store.add_server(fake_config("game"))
    for method, path in [("get", f"/api/servers/{server.id}/backups"),
                         ("get", f"/api/servers/{server.id}/backups/x.zip/download"),
                         ("get", "/api/audit"), ("post", "/api/activity/dismiss"),
                         ("get", "/api/templates"), ("get", "/api/update"),
                         ("post", f"/api/servers/{server.id}/kill")]:
        assert client.request(method, path, headers=AS_FRIEND).status_code == 403, path


# -- friend Update and quick commands (D-135, D-137) --------------------------------------


def test_friend_update_needs_the_permission_and_is_audited(store, settings, monkeypatch,
                                                           tmp_path):
    install = steam_install(tmp_path, "steamy")
    server = store.add_server(fake_config("steamy", kind=ServerKind.STEAM,
                                          steam_app_id=APP_ID, install_dir=str(install),
                                          save_dir="Saved"))
    custom = store.add_server(fake_config("custom"))
    with make_client(store, settings) as c:
        assert c.post(f"/api/friend/servers/{server.id}/update",
                      headers=AS_FRIEND).status_code == 403
    stop_everything(c.sup, store)  # type: ignore[attr-defined]
    monkeypatch.setenv("FAKE_STEAMCMD_DELAY", "0.02")
    monkeypatch.setenv("FAKE_STEAMCMD_STATE", str(tmp_path / "selfupdated"))
    with make_client(store, settings, friend_control="anyone") as c:
        assert c.post(f"/api/friend/servers/{custom.id}/update",
                      headers=AS_FRIEND).status_code == 409
        res = c.post(f"/api/friend/servers/{server.id}/update", headers=AS_FRIEND)
        assert res.status_code == 202, res.text
        # One friend click per server per gap (60 s by default), Update included.
        assert c.post(f"/api/friend/servers/{server.id}/update",
                      headers=AS_FRIEND).status_code in (409, 429)
        wait_for(lambda: any(e.action == "steam.update.ok" for e in store.list_audit()), 30)
        entries = [(e.who, e.action) for e in store.list_audit()]
        assert (FRIEND, "friend.update") in entries
        assert store.last_ok_steam_run(server.id).who == FRIEND
        # The save was backed up first, as for the admin's Update.
        assert list(settings.server_backup_dir(server.id).glob("*pre-update*.zip"))
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def test_friend_quick_commands_come_from_the_template(anyone, store, settings):
    folder = settings.data_templates_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "quicky.json").write_text(json.dumps(QUICK_TEMPLATE), encoding="utf-8")
    server = store.add_server(fake_config("quick", template_id="quicky"))
    sid = server.id
    assert anyone.post(f"/api/friend/servers/{sid}/quick/0",
                       headers=AS_FRIEND).status_code == 409  # not running
    anyone.sup.start(sid)  # type: ignore[attr-defined]
    wait_for(lambda: anyone.sup.state(sid) is ServerState.RUNNING, 20)  # type: ignore[attr-defined]
    for n in (1, 2, 3, -1):  # {message}, not for friends, out of range
        assert anyone.post(f"/api/friend/servers/{sid}/quick/{n}",
                           headers=AS_FRIEND).status_code in (403, 404, 422), n
    res = anyone.post(f"/api/friend/servers/{sid}/quick/0", headers=AS_FRIEND)
    assert res.status_code == 202, res.text
    assert anyone.post(f"/api/friend/servers/{sid}/quick/0",
                       headers=AS_FRIEND).status_code == 429
    assert (FRIEND, "friend.command", "save") in [
        (e.who, e.action, e.detail) for e in store.list_audit()]
    assert anyone.sup.state(sid) is ServerState.RUNNING  # type: ignore[attr-defined]


def test_quick_command_with_a_message_cant_be_for_friends():
    bad = {**QUICK_TEMPLATE, "quick_commands": [
        {"label": "Say", "command": "say {message}", "friend": True}]}
    with pytest.raises(ValueError, match="message"):
        templates.parse(json.dumps(bad))
    assert templates.parse(json.dumps(QUICK_TEMPLATE)).quick_commands[0].friend


# -- the friend console: read-only, earlier lines, no paths (D-135) -----------------------


def test_friend_console_is_read_only_with_the_backlog(client, store):
    server = running(client, store, "talky")
    con = client.sup.console(server.id)  # type: ignore[attr-defined]
    con.system(f"Started {server.exe_path} {server.args}")
    con.write("SteamCMD at C:\\steam", LineSource.STEAM)
    with ws_connect(client, f"/ws/servers/{server.id}/console", headers=AS_FRIEND) as ws:
        first = ws.receive_json()
        assert first["type"] == "lines" and first["lines"]
        texts = [x["text"] for x in first["lines"]]
        assert not any(x["source"] == "steam" for x in first["lines"])
        assert "Started <hidden> <hidden>" in texts
        ws.send_json({"type": "command", "text": "quit"})
        assert ws.receive_json() == {"type": "error",
                                     "message": "The console is read-only for friends"}
    assert client.sup.state(server.id) is ServerState.RUNNING  # type: ignore[attr-defined]
    assert not any(e.action == "console" for e in store.list_audit())


def test_friend_line_blanks_every_copy_case_blind():
    line = ConsoleLine(1, 0.0, LineSource.SYS, r"C:\Games\X and c:\games\x\save")
    assert _friend_line(line, [r"C:\Games\X"])["text"] == "<hidden> and <hidden>\\save"
    assert _friend_line(ConsoleLine(2, 0.0, LineSource.STEAM, "x"), []) is None


# -- last updated (D-136) ---------------------------------------------------------------


def test_last_updated_from_the_manifest_else_basecamp(store, tmp_path):
    install = steam_install(tmp_path, "a", last_updated=1761000000)
    server = store.add_server(fake_config("a", kind=ServerKind.STEAM, steam_app_id=APP_ID,
                                          install_dir=str(install)))
    assert steam.last_updated(server, store) == {"at": 1761000000.0, "source": "steam"}
    plain = store.add_server(fake_config("b"))
    assert steam.last_updated(plain, store) is None
    run = store.add_steam_run(plain.id, "update", ADMIN)
    store.update_steam_run(run, result="ok", ended_at=True)
    found = steam.last_updated(plain, store)
    assert found is not None and found["source"] == "basecamp" and found["at"] > 0


def test_one_page_for_everyone_and_nothing_from_the_internet(client):
    page = client.get("/", headers=SERVE).text
    assert "/static/app.js" in page and page == client.get("/").text
    web = Path(__file__).resolve().parent.parent / "basecamp" / "web"
    for name in ("index.html", "style.css", "app.js", "pages.js", "common.js"):
        text = (web / name).read_text(encoding="utf-8")
        assert "googleapis" not in text and "gstatic" not in text and "confirm(" not in text, name
    font = client.get("/static/fonts/instrument-sans/InstrumentSans-Variable.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"
    assert font.headers["content-type"] == "font/woff2"
    for family in ("bricolage-grotesque", "instrument-sans", "jetbrains-mono"):
        assert "SIL Open Font License" in (web / "fonts" / family / "OFL.txt").read_text(
            encoding="utf-8")
