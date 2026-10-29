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
from basecamp.supervisor import Supervisor, SupervisorError
from tests.conftest import console_text, fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, AS_FRIEND, BASE, FRIEND

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



# -- cancel during the countdown (batch 4, item 12) -----------------------------------------


@pytest.mark.parametrize("kind", ["stop", "restart"])
def test_cancel_calls_off_a_stop_or_restart_and_it_keeps_running(store, sup, kind):
    server = warned(store, minutes=[1])
    start_running(sup, server.id)
    pid = sup.status(server.id).pid
    t, result = in_thread(getattr(sup, kind), server.id, warn=True)
    wait_for(lambda: sup.counting_down(server.id))
    assert sup.cancel_countdown(server.id) == kind
    t.join(20)
    assert not t.is_alive()
    if kind == "stop":
        assert result[0].ended_by == "cancelled" and not result[0].killed
    else:
        assert result == [None]
    assert sup.state(server.id) is ServerState.RUNNING and sup.status(server.id).pid == pid
    assert not sup.counting_down(server.id)
    assert f"{kind.capitalize()} cancelled; the server keeps running" in console_text(
        sup, server.id)
    with pytest.raises(SupervisorError, match="Too late"):  # nothing left to cancel
        sup.cancel_countdown(server.id)


def test_cancel_is_refused_once_the_stop_has_begun(store, sup):
    server = warned(store, minutes=[1])
    start_running(sup, server.id)
    t, result = in_thread(sup.stop, server.id, warn=True)
    wait_for(lambda: sup.counting_down(server.id))
    assert sup.stop_now(server.id)
    with pytest.raises(SupervisorError, match="already going ahead"):
        sup.cancel_countdown(server.id)  # Stop now won
    t.join(20)
    assert result[0].graceful and sup.state(server.id) is ServerState.STOPPED


def test_cancel_racing_the_end_of_the_countdown_has_one_winner(store, sup):
    """Cancel at the moment the countdown runs out: either it is honoured (still Running)
    or refused (and the stop went ahead), never both or neither."""
    for attempt in range(3):
        server = warned(store, f"race{attempt}", minutes=[0.02])  # 1.2 s
        start_running(sup, server.id)
        t, result = in_thread(sup.stop, server.id, warn=True)
        wait_for(lambda sid=server.id: sup.counting_down(sid))
        stop_at = sup.status(server.id).warning["stop_at"]
        time.sleep(max(0.0, stop_at - time.time() - 0.01))
        try:
            sup.cancel_countdown(server.id)
            won = True
        except SupervisorError:
            won = False
        t.join(20)
        if won:
            assert result[0].ended_by == "cancelled"
            assert sup.state(server.id) is ServerState.RUNNING
            sup.stop(server.id)
        else:
            assert result[0].graceful and sup.state(server.id) is ServerState.STOPPED



def test_api_cancel_by_the_owner_and_by_friends(store, settings, webhook):
    from basecamp import notify
    from tests.test_api import SERVE

    web = dataclasses.replace(settings, admin_logins=(ADMIN,), friend_control="off",
                              friend_control_gap_s=60)
    notify.set_webhook(web, webhook.url)
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    server = warned(store, minutes=[1])
    sid = server.id
    cancel = f"/api/servers/{sid}/countdown/cancel"
    friend_cancel = f"/api/friend/servers/{sid}/countdown/cancel"

    def countdown(c, path=f"/api/servers/{sid}/restart"):  # type: ignore[no-untyped-def]
        assert c.post(path).status_code == 202
        wait_for(lambda: sup.counting_down(sid))

    def after_cancel():  # type: ignore[no-untyped-def]
        with sup.hold(sid):  # the restart thread has seen it
            pass
        assert sup.state(sid) is ServerState.RUNNING

    try:
        with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
            assert c.post(f"/api/servers/{sid}/start").status_code == 200
            wait_for(lambda: sup.state(sid) is ServerState.RUNNING, 20)
            res = c.post(cancel)
            assert res.status_code == 409 and "Too late" in res.json()["detail"]

            countdown(c)
            assert c.post(cancel, headers=AS_FRIEND).status_code == 403  # admin route
            assert c.post(friend_cancel, headers=AS_FRIEND).status_code == 403  # controls off
            assert c.post(cancel, headers={"origin": "http://evil.example"}).status_code == 403
            res = c.post(cancel)
            assert res.status_code == 200 and res.json() == {"cancelled": "restart"}
            after_cancel()
            wait_for(lambda: any("restart of **warned** was cancelled" in t
                                 for t in webhook.texts()), 10)

            assert c.put("/api/settings", json={"friend_control": "anyone"}).status_code == 200
            countdown(c, f"/api/servers/{sid}/stop")  # the owner's stop; a friend may cancel
            res = c.post(friend_cancel, headers=AS_FRIEND)
            assert res.status_code == 200 and res.json() == {"cancelled": "stop"}
            after_cancel()
            countdown(c)
            res = c.post(friend_cancel, headers=AS_FRIEND)  # one per gap, its own clock
            assert res.status_code == 429
            assert c.post(cancel).status_code == 200  # the owner isn't held
            after_cancel()
            res = c.post(friend_cancel, headers=AS_FRIEND)
            assert res.status_code == 409  # nothing counting down: refused before the gap

            rows = [(e["who"], e["action"], e["detail"]) for e in c.get("/api/audit").json()]
            assert ("localhost", "countdown.cancel", "restart") in rows
            assert (FRIEND, "friend.cancel", "stop") in rows
            feed = c.get("/api/activity", headers=AS_FRIEND).json()["items"]
            seen = [(i["who"], i["action"]) for i in feed if "cancel" in i["action"]]
            assert seen == [("owner", "countdown.cancel"), (FRIEND, "friend.cancel"),
                            ("owner", "countdown.cancel")]
            assert c.get("/api/activity", headers=SERVE).status_code == 200
    finally:
        stop_everything(sup, store)
