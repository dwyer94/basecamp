"""'Stopped since 23:40' and 'gave up after 3 tries' (batch 4, item 13; D-142): kept in the
store, so they survive a Basecamp restart, and on the friend card on purpose."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from basecamp.api import views
from basecamp.api.app import create_app
from basecamp.crashes import CrashPolicy
from basecamp.models import ServerState
from basecamp.supervisor import Supervisor
from tests.conftest import fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_FRIEND, BASE


def test_the_friend_card_allow_list_has_them_on_purpose():
    assert views.FRIEND_FIELDS == (
        "id", "name", "state", "uptime_s", "join_address", "players", "cpu_percent",
        "rss_mb", "last_updated", "update_available", "job", "stop_warning", "restart_at",
        "state_since", "gave_up_after")


@pytest.mark.parametrize(("recorded", "state", "expected"), [
    (None, ServerState.STOPPED, (None, None)),
    (("stopped", 100.0, 0), ServerState.STOPPED, (100.0, None)),
    (("crashed", 100.0, 3), ServerState.CRASHED, (100.0, 3)),
    (("crashed", 100.0, 3), ServerState.STOPPED, (100.0, None)),  # Basecamp restarted
    (("running", 100.0, 0), ServerState.STOPPED, (None, None)),  # PC restarted: unknown
    (("stopped", 100.0, 0), ServerState.RUNNING, (None, None)),
    (("crashed", 100.0, 0), ServerState.CRASHED, (100.0, None)),  # a restart is pending
])
def test_state_since_rules(recorded, state, expected):
    assert views.state_since(recorded, state) == expected


def test_stopped_since_survives_a_basecamp_restart(store, settings):
    web = dataclasses.replace(settings, admin_logins=(ADMIN,))
    server = store.add_server(fake_config("Ridge", "--tick", "0.3", ready_after_s=1))
    sup = Supervisor(store, web)
    try:
        with TestClient(create_app(store, sup, web), base_url=BASE,
                        client=("127.0.0.1", 50123)) as c:
            assert c.get("/api/servers").json()[0]["state_since"] is None  # never ran
            sup.start(server.id)
            wait_for(lambda: sup.state(server.id) is ServerState.RUNNING, 20)
            assert c.get("/api/servers").json()[0]["state_since"] is None  # up: uptime
            before = time.time()
            sup.stop(server.id)
            card = c.get("/api/servers", headers=AS_FRIEND).json()[0]
            assert card["state"] == "stopped" and before <= card["state_since"] <= time.time()
            assert card["gave_up_after"] is None
            since = card["state_since"]
    finally:
        stop_everything(sup, store)
    again = Supervisor(store, web)  # a new Basecamp: nothing in memory
    try:
        with TestClient(create_app(store, again, web), base_url=BASE,
                        client=("127.0.0.1", 50123)) as c:
            card = c.get("/api/servers").json()[0]
            assert card["state"] == "stopped" and card["state_since"] == since
    finally:
        again.detach()


def test_gave_up_after_3_tries_until_started_again(store, settings):
    quick = dataclasses.replace(settings, admin_logins=(ADMIN,), crash_restart_delay_s=0.3)
    sup = Supervisor(store, quick)
    CrashPolicy(store, sup, quick)
    server = store.add_server(fake_config("Valley", "--crash-after", "1.5"))
    try:
        with TestClient(create_app(store, sup, quick), base_url=BASE,
                        client=("127.0.0.1", 50123)) as c:
            sup.start(server.id)
            wait_for(lambda: store.server_status(server.id)[2] == 3, timeout=30)
            card = c.get("/api/servers", headers=AS_FRIEND).json()[0]
            assert card["state"] == "crashed" and card["gave_up_after"] == 3
            assert card["restart_at"] is None and card["state_since"] <= time.time()
            assert c.get("/api/servers").json()[0]["gave_up_after"] == 3  # the owner too
            sup.start(server.id)  # Start: the count is over
            assert store.server_status(server.id)[2] == 0
            assert c.get("/api/servers").json()[0]["gave_up_after"] is None
            sup.kill(server.id)
    finally:
        stop_everything(sup, store)


def test_the_row_goes_with_the_server(store):
    server = store.add_server(fake_config("Gone"))
    store.record_state(server.id, "stopped", 5.0)
    store.set_gave_up(server.id, 2)
    assert store.server_status(server.id) == ("stopped", 5.0, 2)
    store.record_state(server.id, "crashed", 6.0)  # keeps the count
    assert store.server_status(server.id) == ("crashed", 6.0, 2)
    store.delete_server(server.id)
    assert store.server_status(server.id) is None
