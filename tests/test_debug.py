"""Read-only debug views (batch 4, item 11): admin only, audited but not in friends'
Activity, only Basecamp's own files, and never a secret."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from basecamp import debug, notify
from basecamp.api.app import create_app
from basecamp.notify import Notifier
from basecamp.selfupdate import SelfUpdater
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import fake_config, stop_everything
from tests.test_api import ADMIN, AS_ADMIN, AS_FRIEND, BASE

# Fake secrets, planted to check they never come back.
WEBHOOK = "https://discord.com/api/webhooks/123456/PlantedWebhookToken-xyz"
RCON = "Planted-Rcon-Pass-77"
REST = "Planted-Rest-Pass-88"
KEYED = "hunter2-keyed"

ENDPOINTS = ("/api/debug", "/api/debug/health", "/api/debug/settings", "/api/debug/errors",
             "/api/debug/logs/basecamp", "/api/debug/servers/{id}/console")


@pytest.fixture
def web(settings: Settings, tmp_path: Path) -> Settings:
    return dataclasses.replace(settings, admin_logins=(ADMIN,), friend_control="anyone",
                               join_host="<tailscale-ip>")


@pytest.fixture
def client(store, web: Settings) -> Iterator[TestClient]:
    sup = Supervisor(store, web)
    with TestClient(create_app(store, sup, web), base_url=BASE,
                    client=("127.0.0.1", 50123)) as c:
        c.sup = sup  # type: ignore[attr-defined]
        yield c
    stop_everything(sup, store)


def plant(c: TestClient, store, web: Settings) -> int:
    """A server with an RCON and a REST password, a webhook, and log lines naming them."""
    server = store.add_server(fake_config("Leaky", "--tick", "0.3", rcon_port=27015,
                                          rest_port=8212))
    notify.set_webhook(web, WEBHOOK)
    for body in ({"rcon_password": RCON}, {"rest_password": REST}):
        assert c.put(f"/api/servers/{server.id}/remote", json=body).status_code == 200
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{stamp},001 INFO    basecamp.x: posting to {WEBHOOK}",
             f"{stamp},002 ERROR   basecamp.rcon: login with {RCON} refused",
             f"{stamp},003 WARNING basecamp.rest: REST password={REST}",
             f"{stamp},004 INFO    basecamp.y: -AdminPassword {KEYED} -port 1",
             f"{stamp},005 INFO    basecamp.z: an ordinary line"]
    web.logs_dir.mkdir(parents=True, exist_ok=True)
    (web.logs_dir / "basecamp.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    console = web.server_log_dir(server.id) / "console.log"
    console.parent.mkdir(parents=True, exist_ok=True)
    console.write_text(f"{stamp} rcon.password is {RCON}\n{stamp} [basecamp] Started\n"
                       f"{stamp} ServerPassword=\"{KEYED}\"\n", encoding="utf-8")
    return server.id


def leaks(text: str) -> list[str]:
    return [s for s in (WEBHOOK, "PlantedWebhookToken", RCON, REST, KEYED) if s in text]


def test_friends_get_403_and_admins_get_it_all(client, store, web):
    sid = plant(client, store, web)
    for path in ENDPOINTS:
        url = path.format(id=sid)
        assert client.get(url, headers=AS_FRIEND).status_code == 403, url
        res = client.get(url, headers=AS_ADMIN)
        assert res.status_code == 200, (url, res.text)
        assert leaks(res.text) == [], url
    assert client.get("/api/debug/logs/basecamp").status_code == 200  # the PC itself


def test_planted_secrets_never_come_back(client, store, web):
    sid = plant(client, store, web)
    out = client.get("/api/debug/logs/basecamp").json()
    text = "\n".join(out["lines"])
    assert leaks(text) == []
    assert "an ordinary line" in text and text.count(debug.HIDDEN) >= 4
    assert "REST password=<hidden>" in text and "-AdminPassword <hidden> -port 1" in text
    console = client.get(f"/api/debug/servers/{sid}/console").json()
    assert leaks(json.dumps(console)) == [] and "[basecamp] Started" in console["lines"][1]
    errors = client.get("/api/debug/errors").json()
    assert [line.split(" ", 3)[2] for line in errors["log"]["lines"]] == ["ERROR", "WARNING"]
    settings = client.get("/api/debug/settings").json()
    assert list(settings["settings"]) == list(debug.SETTINGS)  # the allow-list, exactly
    assert settings["secrets_set"] == {"discord_webhook": True, "server_webhooks": [],
                                       "rcon_passwords": [sid], "rest_passwords": [sid]}
    assert leaks(json.dumps(settings)) == []


def test_a_failed_discord_message_shows_without_its_url(client, store, web):
    from basecamp import activity
    activity.record(store, "notify.failed", None, f"HTTP 404: posting to {WEBHOOK} failed")
    plant(client, store, web)
    errors = client.get("/api/debug/errors").json()
    assert errors["failures"][0]["action"] == "notify.failed"
    assert leaks(json.dumps(errors)) == [] and errors["open_failures"] == 1


def test_lines_since_and_rotated_files(client, web):
    web.logs_dir.mkdir(parents=True, exist_ok=True)
    old = [f"2026-10-28 10:00:{n:02d},000 INFO    a: old {n}" for n in range(30)]
    new = [f"2026-10-29 10:00:{n:02d},000 INFO    a: new {n}" for n in range(30)]
    (web.logs_dir / "basecamp.log.1").write_text("\n".join(old) + "\n", encoding="utf-8")
    (web.logs_dir / "basecamp.log").write_text(
        "\n".join([*new[:5], "Traceback (most recent call last):", "  boom", *new[5:]])
        + "\n", encoding="utf-8")
    out = client.get("/api/debug/logs/basecamp?lines=40").json()
    assert out["files"] == ["basecamp.log", "basecamp.log.1"] and len(out["lines"]) == 40
    assert out["lines"][-1].endswith("new 29") and out["truncated"]
    out = client.get("/api/debug/logs/basecamp?since=2026-10-29T10:00:04").json()
    assert out["lines"][:3] == [new[4], "Traceback (most recent call last):", "  boom"]
    assert len(out["lines"]) == 28 and not out["truncated"]
    out = client.get("/api/debug/logs/basecamp?since=2026-10-29T10:00:04&lines=5").json()
    assert out["lines"] == new[25:] and out["truncated"]  # the newest five
    out = client.get("/api/debug/logs/basecamp?since=2026-10-28T10:00:28&lines=2000").json()
    assert out["lines"][:2] == old[28:] and len(out["lines"]) == 2 + 32
    assert len(client.get("/api/debug/logs/basecamp?lines=99999").json()["lines"]) == 62
    assert client.get("/api/debug/logs/basecamp?since=yesterday").status_code == 400


def test_no_paths_from_the_request(client, store, web):
    for bad in ("..%2F..%2Fsecrets.toml", "secrets", "basecamp.log", "C:%5Cx"):
        res = client.get(f"/api/debug/logs/{bad}")
        assert res.status_code in (404, 422), bad
    assert client.get("/api/debug/servers/999/console").status_code == 404
    sid = store.add_server(fake_config("Quiet")).id
    assert client.get(f"/api/debug/servers/{sid}/console").status_code == 404  # no log yet
    # Without the launcher there is no launcher log to read.
    assert client.get("/api/debug/logs/launcher").status_code == 404


def test_launcher_log_under_the_launcher(store, web, tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "launcher.log").write_text(
        "2026-10-29 10:00:00,000 INFO    Launcher 1 in <root>\n"
        "2026-10-29 10:00:05,000 INFO    0.3.0 is healthy\n", encoding="utf-8")
    sup = Supervisor(store, web)
    updater = SelfUpdater(web, store, Notifier(web, store), root=root, launcher=1)
    with TestClient(create_app(store, sup, web, updater=updater), base_url=BASE,
                    client=("127.0.0.1", 50123)) as c:
        out = c.get("/api/debug/logs/launcher?lines=1").json()
        assert out["lines"] == ["2026-10-29 10:00:05,000 INFO    0.3.0 is healthy"]
        health = c.get("/api/debug/health").json()
        assert health["launcher"]["under_launcher"] and health["launcher"]["launcher_version"] == 1
    sup.detach()


def test_health(client, store):
    store.add_server(fake_config("Idle"))
    health = client.get("/api/debug/health").json()
    for key in ("version", "uptime_s", "launcher", "self_update", "steamcmd", "scheduler",
                "players", "backups", "live_status", "discord", "settings_file",
                "open_failures", "servers"):
        assert key in health, key
    assert health["launcher"]["under_launcher"] is False
    assert health["servers"][0]["state"] == "stopped"
    assert set(health["scheduler"]) == {"running", "every_s", "state"}


def test_each_call_is_audited_but_friends_never_see_it(client):
    client.get("/api/debug/health")
    client.get("/api/debug/settings")
    rows = [(e["who"], e["action"], e["detail"]) for e in client.get("/api/audit").json()]
    assert rows[:2] == [("localhost", "debug", "settings"), ("localhost", "debug", "health")]
    friend = client.get("/api/activity", headers=AS_FRIEND).json()["items"]
    assert not any(i["action"] == "debug" for i in friend)


def test_redactor_unit(web):
    notify.set_webhook(web, WEBHOOK)
    r = debug.Redactor(web, extra=["sekrit-extra"])
    assert r("a sekrit-extra b") == "a <hidden> b"
    assert r(f"x {WEBHOOK} y") == "x <hidden> y"
    assert r("https://discordapp.com/api/webhooks/1/abc") == "<hidden>"
    assert r('{"admin_password": "x1"}') == '{"admin_password": <hidden>}'
    assert r("token: abc def") == "token: <hidden> def"
    assert r("nothing here") == "nothing here"
