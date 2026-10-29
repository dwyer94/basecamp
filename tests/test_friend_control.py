"""Friends may start, stop and restart (batch 2, item 7; D-131): off by default, a list of
logins or anyone, nothing else, audited by login, rate-limited, stop warnings kept."""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from basecamp.api import views
from basecamp.api.app import create_app
from basecamp.models import ServerState, StopAction, StopWarning
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_FRIEND, BASE, FRIEND, SERVE

OTHER = {**SERVE, "tailscale-user-login": "stranger@example.com"}


def make_client(store, settings: Settings, **overrides) -> TestClient:
    web = dataclasses.replace(settings, admin_logins=(ADMIN,), join_host="<tailscale-ip>",
                              **overrides)
    sup = Supervisor(store, web)
    c = TestClient(create_app(store, sup, web), base_url=BASE, client=("127.0.0.1", 50123))
    c.sup = sup  # type: ignore[attr-defined]
    c.web = web  # type: ignore[attr-defined]
    return c


@pytest.fixture
def anyone(store, settings) -> Iterator[TestClient]:
    with make_client(store, settings, friend_control="anyone", friend_control_gap_s=0) as c:
        yield c
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def add(store, name="fake", **fields):
    return store.add_server(fake_config(name, "--tick", "0.3", **fields))


def state(c: TestClient, sid: int) -> str:
    return c.sup.state(sid).value  # type: ignore[attr-defined]


def test_off_by_default(store, settings):
    server = add(store)
    with make_client(store, settings) as c:
        assert c.get("/api/me", headers=AS_FRIEND).json()["can_control"] is False
        assert c.get("/api/me").json()["can_control"] is True  # the admin
        for action in ("start", "stop", "restart", "update"):
            res = c.post(f"/api/friend/servers/{server.id}/{action}", headers=AS_FRIEND)
            assert res.status_code == 403
        assert state(c, server.id) == "stopped"
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def test_anyone_can_start_stop_restart_and_it_is_audited(anyone, store):
    server = add(store)
    sid = server.id
    assert anyone.get("/api/me", headers=SERVE).json()["can_control"] is True
    res = anyone.post(f"/api/friend/servers/{sid}/start", headers=SERVE)  # no login
    assert res.status_code == 202, res.text
    wait_for(lambda: state(anyone, sid) == "running", 20)
    assert anyone.post(f"/api/friend/servers/{sid}/start", headers=AS_FRIEND).status_code == 409
    res = anyone.post(f"/api/friend/servers/{sid}/restart", headers=AS_FRIEND)
    assert res.status_code == 202, res.text
    wait_for(lambda: any(e["action"] == "friend.restart"
                         for e in anyone.get("/api/audit").json()))
    with anyone.sup.hold(sid):  # type: ignore[attr-defined]  # the restart is done
        pass
    wait_for(lambda: state(anyone, sid) == "running", 20)
    assert anyone.post(f"/api/friend/servers/{sid}/stop", headers=AS_FRIEND).status_code == 202
    wait_for(lambda: state(anyone, sid) == "stopped", 20)

    entries = [(e["who"], e["action"]) for e in anyone.get("/api/audit").json()]
    assert ("unknown", "friend.start") in entries
    assert (FRIEND, "friend.restart") in entries and (FRIEND, "friend.stop") in entries

    # Still nothing else: kill, Stop now, config, console, delete, update, passwords.
    for method, path in (("post", f"/api/servers/{sid}/kill"),
                         ("post", f"/api/servers/{sid}/stop?now=true"),
                         ("post", f"/api/servers/{sid}/start"),
                         ("get", f"/api/servers/{sid}/configs"),
                         ("get", f"/api/servers/{sid}/console"),
                         ("delete", f"/api/servers/{sid}"),
                         ("post", f"/api/servers/{sid}/update"),
                         ("put", f"/api/servers/{sid}/remote"),
                         ("get", "/api/settings")):
        assert anyone.request(method, path, headers=AS_FRIEND,
                              json={}).status_code == 403, path
    # The friend JSON stays the allow-list.
    for item in anyone.get("/api/servers", headers=AS_FRIEND).json():
        assert tuple(item) == views.FRIEND_FIELDS


def test_login_list_lets_only_those_logins_in(store, settings):
    server = add(store)
    with make_client(store, settings, friend_control="logins",
                     friend_control_logins=(FRIEND.upper(),)) as c:
        assert c.get("/api/me", headers=AS_FRIEND).json()["can_control"] is True
        for headers in (OTHER, SERVE):  # another login, and no login at all (Q-02)
            assert c.get("/api/me", headers=headers).json()["can_control"] is False
            assert c.post(f"/api/friend/servers/{server.id}/start",
                          headers=headers).status_code == 403
        assert c.post(f"/api/friend/servers/{server.id}/start",
                      headers=AS_FRIEND).status_code == 202
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def test_one_click_per_server_per_gap(store, settings):
    a, b = add(store, "a"), add(store, "b")
    with make_client(store, settings, friend_control="anyone", friend_control_gap_s=60) as c:
        assert c.post(f"/api/friend/servers/{a.id}/start", headers=AS_FRIEND).status_code == 202
        wait_for(lambda: state(c, a.id) == "running", 20)
        res = c.post(f"/api/friend/servers/{a.id}/restart", headers=OTHER)
        assert res.status_code == 429 and "try again in" in res.json()["detail"]
        assert c.post(f"/api/friend/servers/{b.id}/start", headers=OTHER).status_code == 202
        assert c.post(f"/api/servers/{a.id}/restart").status_code == 202  # admins aren't held
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def test_cross_site_requests_are_refused(anyone, store):
    server = add(store)
    res = anyone.post(f"/api/friend/servers/{server.id}/start",
                      headers={**AS_FRIEND, "origin": "https://evil.example"})
    assert res.status_code == 403
    res = anyone.post(f"/api/friend/servers/{server.id}/start",
                      headers={**AS_FRIEND, "host": "evil.example"})
    assert res.status_code == 400
    assert state(anyone, server.id) == "stopped"


def test_a_friend_stop_warns_players_first(anyone, store):
    warning = StopWarning(action=StopAction.STDIN, command="say Stopping in {seconds} s",
                          minutes=[0.05])
    server = add(store, stop_warning=warning)
    anyone.sup.start(server.id)  # type: ignore[attr-defined]
    wait_for(lambda: state(anyone, server.id) == "running", 20)
    res = anyone.post(f"/api/friend/servers/{server.id}/stop", headers=AS_FRIEND)
    assert res.status_code == 202 and res.json()["warning_s"] == pytest.approx(3)
    wait_for(lambda: anyone.sup.counting_down(server.id))  # type: ignore[attr-defined]
    res = anyone.post(f"/api/friend/servers/{server.id}/restart", headers=AS_FRIEND)
    assert res.status_code == 409  # already counting down
    wait_for(lambda: anyone.sup.state(server.id) is ServerState.STOPPED, 20)  # type: ignore[attr-defined]
    text = "\n".join(x.text for x in anyone.sup.console(server.id).lines())  # type: ignore[attr-defined]
    assert "Stopping in 3 s" in text


def test_settings_page_saves_the_friend_permission(store, settings):
    with make_client(store, settings) as c:
        res = c.put("/api/settings", json={"friend_control": "logins",
                                           "friend_control_logins": [" pal@example.com ",
                                                                     "pal@example.com",
                                                                     "b@github"]})
        assert res.status_code == 200, res.text
        assert res.json()["friend_control_logins"] == ["pal@example.com", "b@github"]
        saved = tomllib.loads((c.web.data_dir / "settings.toml").read_text())  # type: ignore[attr-defined]
        assert saved["basecamp"]["friend_control"] == "logins"
        assert saved["basecamp"]["friend_control_logins"] == ["pal@example.com", "b@github"]
        assert c.put("/api/settings", json={"friend_control": "everyone"}).status_code == 422
        assert c.put("/api/settings", json={"friend_control": "anyone"},
                     headers=AS_FRIEND).status_code == 403
    reread = Settings.read_file(settings.data_dir)
    assert reread.friend_control_logins == ("pal@example.com", "b@github")
    with pytest.raises(ValueError, match="friend_control"):
        Settings.load(settings.data_dir, friend_control="everyone")
    stop_everything(c.sup, store)  # type: ignore[attr-defined]
