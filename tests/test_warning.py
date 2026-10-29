"""The warning to players before a stop, restart or update (R-25, D-61)."""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from basecamp.api.app import create_app
from basecamp.models import ServerState, StopAction, StopWarning
from basecamp.supervisor import Supervisor
from tests.conftest import console_text, fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_FRIEND, BASE

WARN = "say Server stops in {minutes} min ({seconds} s) {not a field}"


def warned(store, name: str = "warned", minutes=(0.05, 0.02), **fields):  # type: ignore[no-untyped-def]
    warning = StopWarning(action=StopAction.STDIN, command=WARN, minutes=list(minutes))
    return store.add_server(fake_config(name, "--tick", "0.3", stop_warning=warning,
                                        ready_after_s=1, **fields))


def start_running(sup: Supervisor, server_id: int) -> None:
    sup.start(server_id)
    wait_for(lambda: sup.state(server_id) is ServerState.RUNNING, timeout=20)


def in_thread(fn, *args, **kwargs) -> tuple[threading.Thread, list]:  # type: ignore[no-untyped-def]
    out: list = []
    t = threading.Thread(target=lambda: out.append(fn(*args, **kwargs)), daemon=True)
    t.start()
    return t, out


def test_warning_model():
    w = StopWarning(action=StopAction.RCON, command="announce {minutes} min", minutes=[1, 5, 5])
    assert w.minutes == [5, 1] and w.lead_s == 300
    assert w.text(5) == "announce 5 min" and w.text(0.5) == "announce 0.5 min"
    assert StopWarning(command=WARN, minutes=[2]).text(2) == (
        "say Server stops in 2 min (120 s) {not a field}")
    assert StopWarning().minutes == []  # off
    for bad in ({"action": "ctrl_break", "command": "x", "minutes": [1]},
                {"command": "", "minutes": [1]}, {"command": "x", "minutes": [0]},
                {"command": "x", "minutes": [61]}):
        with pytest.raises(ValidationError):
            StopWarning(**bad)


def test_stop_warns_at_each_minute_then_stops(store, sup):
    server = warned(store)
    start_running(sup, server.id)
    began = time.monotonic()
    changes: list[int] = []
    sup.add_change_listener(changes.append)
    t, result = in_thread(sup.stop, server.id, warn=True)
    wait_for(lambda: sup.counting_down(server.id))
    status = sup.status(server.id)
    assert status.state is ServerState.RUNNING  # players keep playing meanwhile
    assert status.warning["kind"] == "stop"
    assert 2 < status.warning["stop_at"] - time.time() <= 3.1
    t.join(30)
    assert result[0].graceful and time.monotonic() - began >= 2.9
    assert sup.state(server.id) is ServerState.STOPPED and sup.status(server.id).warning is None
    text = console_text(sup, server.id)
    assert "Stopping in 0.05 min; warning players" in text
    assert "say Server stops in 0.05 min (3 s) {not a field}" in text
    assert "say Server stops in 0.02 min (1 s)" in text
    assert changes == [server.id, server.id]  # countdown on, off


def test_no_warning_unless_asked_or_running(store, sup):
    server = warned(store, minutes=[1])
    start_running(sup, server.id)
    began = time.monotonic()
    sup.stop(server.id)  # warn=False: tests, stop_all, crash handling
    assert time.monotonic() - began < 10
    assert "warning players" not in console_text(sup, server.id)


def test_stop_now_skips_the_rest(store, sup):
    server = warned(store, minutes=[1])
    start_running(sup, server.id)
    t, result = in_thread(sup.stop, server.id, warn=True)
    wait_for(lambda: sup.counting_down(server.id))
    wait_for(lambda: "say Server stops in 1 min" in console_text(sup, server.id))
    with pytest.raises(Exception, match="about to stop"):
        sup.start(server.id)  # refused at once, not stuck behind the countdown
    assert sup.stop_now(server.id)
    t.join(20)
    assert result[0].graceful and sup.state(server.id) is ServerState.STOPPED
    assert "the rest of the warning is skipped" in console_text(sup, server.id)
    assert not sup.stop_now(server.id)


def test_kill_during_a_restart_countdown_leaves_it_down(store, sup):
    server = warned(store, minutes=[1])
    start_running(sup, server.id)
    t, result = in_thread(sup.restart, server.id, warn=True)
    wait_for(lambda: sup.counting_down(server.id))
    assert sup.status(server.id).warning["kind"] == "restart"
    sup.kill(server.id)
    t.join(20)
    assert result == [None]  # not started again
    assert sup.state(server.id) is ServerState.STOPPED
    assert not sup.counting_down(server.id)


def test_a_warning_that_cant_be_sent_doesnt_hold_up_the_stop(store, sup):
    warning = StopWarning(action=StopAction.RCON, command="broadcast {minutes}", minutes=[1])
    server = store.add_server(fake_config("norcon", stop_warning=warning, ready_after_s=1))
    start_running(sup, server.id)
    began = time.monotonic()
    assert sup.stop(server.id, warn=True).graceful
    assert time.monotonic() - began < 10
    text = console_text(sup, server.id)
    assert "Skipped warning 'broadcast 1': RCON failed: no RCON port" in text
    assert "Couldn't warn players; the stop goes ahead now" in text


def test_stop_warning_is_saved_and_round_trips(store):
    server = warned(store, minutes=[5, 1])
    again = store.get_server(server.id)
    assert again.stop_warning == server.stop_warning and again.stop_warning.minutes == [5, 1]
    plain = store.add_server(fake_config("plain"))
    assert store.get_server(plain.id).stop_warning is None


# -- over the API --------------------------------------------------------------------------


def test_api_stop_counts_down_and_stop_now_skips(store, settings):
    web = dataclasses.replace(settings, admin_logins=(ADMIN,))
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    server = warned(store, minutes=[1])
    try:
        with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
            assert c.post(f"/api/servers/{server.id}/start").status_code == 200
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["state"] == "running", 20)
            assert c.post(f"/api/servers/{server.id}/stop").status_code == 202
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["stop_warning"])
            view = c.get(f"/api/servers/{server.id}").json()
            assert view["stop_warning"]["kind"] == "stop" and view["state"] == "running"
            assert view["config"]["stop_warning"]["minutes"] == [1]
            friend = c.get("/api/servers", headers=AS_FRIEND).json()[0]  # the countdown only
            assert friend["stop_warning"] == {"kind": "stop",
                                              "stop_at": view["stop_warning"]["stop_at"]}
            assert c.post(f"/api/servers/{server.id}/stop").status_code == 409
            assert c.post(f"/api/servers/{server.id}/restart").status_code == 409
            assert c.post(f"/api/servers/{server.id}/stop?now=true",
                          headers=AS_FRIEND).status_code == 403
            res = c.post(f"/api/servers/{server.id}/stop?now=true")
            assert res.status_code == 202 and res.json()["skipped_warning"]
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["state"] == "stopped",
                     20)
            actions = [e["action"] for e in c.get("/api/audit").json()]
            assert "stop" in actions and "stop.now" in actions

            # Stop with now=true and no countdown: no warning at all.
            assert c.post(f"/api/servers/{server.id}/start").status_code == 200
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["state"] == "running", 20)
            began = time.monotonic()
            assert c.post(f"/api/servers/{server.id}/stop?now=true").status_code == 202
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["state"] == "stopped",
                     20)
            assert time.monotonic() - began < 15
            details = [e["detail"] for e in c.get("/api/audit").json() if e["action"] == "stop"]
            assert details[0].endswith("(no warning)")
    finally:
        stop_everything(sup, store)

