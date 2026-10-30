"""See what friends see (batch 4, item 14; D-143): the owner's own requests are served as a
friend's while the preview cookie is set, it only ever takes rights away, and never
applies to a friend."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from basecamp import auth
from basecamp.api import views
from basecamp.api.app import create_app
from basecamp.models import ServerState
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from tests.conftest import fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_ADMIN, AS_FRIEND, BASE, ws_connect

PREVIEW = {"cookie": f"{auth.VIEW_COOKIE}=friend"}


def make(store, settings: Settings, **overrides) -> TestClient:
    web = dataclasses.replace(settings, admin_logins=(ADMIN,), join_host="<tailscale-ip>",
                              friend_control_gap_s=0, **overrides)
    sup = Supervisor(store, web)
    c = TestClient(create_app(store, sup, web), base_url=BASE, client=("127.0.0.1", 50123))
    c.sup = sup  # type: ignore[attr-defined]
    return c


@pytest.fixture
def client(store, settings) -> Iterator[TestClient]:
    with make(store, settings, friend_control="off") as c:
        yield c
    stop_everything(c.sup, store)  # type: ignore[attr-defined]


def test_identify_only_takes_rights_away(settings):
    web = dataclasses.replace(settings, admin_logins=(ADMIN,))
    cookie = f"other=1; {auth.VIEW_COOKIE}=friend"
    admin = auth.identify("127.0.0.1", {**AS_ADMIN, "cookie": cookie}, web)
    assert (admin.is_admin, admin.preview, admin.login) == (False, True, ADMIN)
    local = auth.identify("127.0.0.1", {"host": "127.0.0.1:8150", "cookie": cookie}, web)
    assert (local.is_admin, local.preview) == (False, True)
    friend = auth.identify("127.0.0.1", {**AS_FRIEND, "cookie": cookie}, web)
    assert (friend.is_admin, friend.preview) == (False, False)  # a friend stays a friend
    for other in (f"{auth.VIEW_COOKIE}=owner", f"{auth.VIEW_COOKIE}=admin", "bad;;=", ""):
        who = auth.identify("127.0.0.1", {**AS_ADMIN, "cookie": other}, web)
        assert who.is_admin and not who.preview, other


def test_the_owner_is_served_the_friend_json(client, store):
    server = store.add_server(fake_config("Valley"))
    res = client.post("/api/view", json={"as": "friend"})
    assert res.status_code == 200 and "httponly" in res.headers["set-cookie"].lower()
    assert "samesite=strict" in res.headers["set-cookie"].lower()
    me = client.get("/api/me").json()  # the cookie is kept by the client from here on
    assert me["is_admin"] is False and me["can_control"] is False
    assert me["preview"] == {"friend_control": "off", "listed": 0}
    assert "join_host" not in me and "failures" not in me
    assert tuple(client.get("/api/servers").json()[0]) == views.FRIEND_FIELDS
    detail = client.get(f"/api/servers/{server.id}").json()
    assert tuple(detail) == views.FRIEND_DETAIL_FIELDS
    with ws_connect(client, "/ws/status", PREVIEW) as ws:
        msg = ws.receive_json()
        assert [tuple(s) for s in msg["servers"]] == [views.FRIEND_FIELDS]
        assert "failures" not in msg and "boot_time" not in msg["host"]
    for method, path in (("GET", "/api/settings"), ("GET", "/api/debug/health"),
                         ("GET", "/api/audit"), ("POST", f"/api/servers/{server.id}/start"),
                         ("POST", "/api/view")):
        body = {"as": "friend"} if path == "/api/view" else None
        assert client.request(method, path, json=body).status_code == 403, path
    assert client.post(f"/api/friend/servers/{server.id}/start").status_code == 403

    res = client.post("/api/view", json={"as": "owner"})  # back, even as a "friend"
    assert res.status_code == 200
    me = client.get("/api/me").json()
    assert me["is_admin"] is True and "preview" not in me
    rows = [(e["who"], e["action"]) for e in client.get("/api/audit").json()]
    assert rows[:2] == [("localhost", "view.owner"), ("localhost", "view.friend")]
    assert not any(i["action"].startswith("view.") for i in
                   client.get("/api/activity", headers=AS_FRIEND).json()["items"])


def test_friends_cannot_turn_it_on_and_cross_site_is_refused(client):
    assert client.post("/api/view", json={"as": "friend"}, headers=AS_FRIEND).status_code == 403
    assert client.post("/api/view", json={"as": "owner"}, headers=AS_FRIEND).status_code == 200
    res = client.post("/api/view", json={"as": "friend"},
                      headers={"origin": "http://evil.example"})
    assert res.status_code == 403 and "set-cookie" not in res.headers
    assert client.post("/api/view", json={"as": "nobody"}).status_code == 422


@pytest.mark.parametrize(("mode", "logins", "can"), [
    ("off", (), False), ("anyone", (), True),
    ("logins", ("pal@example.com",), True),  # shown as a listed friend
    ("logins", (), False),  # nobody listed: read-only
])
def test_friend_controls_follow_the_setting(store, settings, mode, logins, can):
    with make(store, settings, friend_control=mode, friend_control_logins=logins) as c:
        me = c.get("/api/me", headers=PREVIEW).json()
        assert me["can_control"] is can
        assert me["preview"] == {"friend_control": mode, "listed": len(logins)}
    c.sup.detach()  # type: ignore[attr-defined]


def test_friend_actions_while_previewing_never_name_the_owner(store, settings):
    with make(store, settings, friend_control="anyone") as c:
        server = store.add_server(fake_config("Ridge", "--tick", "0.3", ready_after_s=1))
        try:
            res = c.post(f"/api/friend/servers/{server.id}/start", headers={**AS_ADMIN,
                                                                             **PREVIEW})
            assert res.status_code == 202, res.text
            wait_for(lambda: c.sup.state(server.id) is ServerState.RUNNING, 20)  # type: ignore[attr-defined]
            rows = [(e["who"], e["action"]) for e in c.get("/api/audit").json()]
            assert ("owner (preview)", "friend.start") in rows
            feed = c.get("/api/activity", headers=AS_FRIEND).json()["items"]
            assert [i["who"] for i in feed if i["action"] == "friend.start"] == [
                "owner (preview)"]
            assert ADMIN not in str(feed)
        finally:
            stop_everything(c.sup, store)  # type: ignore[attr-defined]
