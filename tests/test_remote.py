"""RCON and REST: the clients against tools/fake_rcon.py and tools/fake_rest.py, the
passwords in secrets.toml, and the supervisor's stop steps, warnings and console commands
over them (D-92..D-97). A failure never holds up a stop."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fake_rcon import FakeRcon
from fake_rest import FakeRest
from fastapi.testclient import TestClient

from basecamp import remote
from basecamp.api.app import create_app
from basecamp.models import ServerState, StopAction, StopStep, StopWarning
from basecamp.rcon import RconAuthError, RconClient, RconError, check_host, encode
from basecamp.remote import RemoteConsoles, RemoteError
from basecamp.rest import RestClient, RestError, parse
from basecamp.supervisor import Supervisor, SupervisorError
from tests.conftest import console_text, fake_config, stop_everything, wait_for
from tests.test_api import ADMIN, BASE

PW = "fake-test-password"  # only ever the fakes' password


@pytest.fixture
def fake_rcon():
    with FakeRcon(PW) as fake:
        yield fake


@pytest.fixture
def fake_rest():
    with FakeRest(PW) as fake:
        yield fake


# -- RCON client ----------------------------------------------------------------------------


def test_rcon_command_and_answer(fake_rcon):
    with RconClient("127.0.0.1", fake_rcon.port, PW) as c:
        assert c.command("echo hello there") == "hello there"
        assert c.command("Save") == "ok: Save"
    assert fake_rcon.commands == ["echo hello there", "Save"]
    assert fake_rcon.logins == 1  # the connection is kept between commands


def test_rcon_wrong_password(fake_rcon):
    with pytest.raises(RconAuthError, match="refused the password") as e:
        RconClient("127.0.0.1", fake_rcon.port, "wrong").command("Save")
    assert "wrong" not in str(e.value) and fake_rcon.commands == []


def test_rcon_multi_packet_answer(fake_rcon):
    with RconClient("127.0.0.1", fake_rcon.port, PW) as c:
        assert c.command("big 10000") == "x" * 10000  # 4096 + 4096 + 1808
        assert c.command("big 8192") == "x" * 8192  # ends on a full packet
        assert c.command("echo after") == "after"  # nothing left over in the stream


def test_rcon_answers_with_id_0_like_palworld():
    with FakeRcon(PW, mode="id0") as fake, RconClient("127.0.0.1", fake.port, PW) as c:
        assert c.command("echo pal") == "pal"
        assert c.command("ShowPlayers") == "ok: ShowPlayers"


def test_rcon_slow_reply_times_out():
    with FakeRcon(PW, mode="slow", delay=2) as fake:
        c = RconClient("127.0.0.1", fake.port, PW, timeout=0.5)
        began = time.monotonic()
        with pytest.raises(RconError, match="didn't answer in time"):
            c.command("Save")
        assert time.monotonic() - began < 1.5
        assert fake.commands == ["Save"]  # sent once: a timeout is not retried
        c.close()


def test_rcon_reconnects_after_a_dropped_connection():
    with FakeRcon(PW, mode="drop") as fake, RconClient("127.0.0.1", fake.port, PW) as c:
        assert c.command("echo one") == "one"
        assert c.command("echo two") == "two"  # the kept connection was dead: log in again
        assert fake.logins == 2 and fake.commands == ["echo one", "echo two"]


def test_rcon_nothing_listening():
    with FakeRcon(PW) as fake:
        port = fake.port
    with pytest.raises(RconError, match="isn't listening"):
        RconClient("127.0.0.1", port, PW, timeout=1).command("Save")


def test_rcon_and_rest_are_loopback_only_unless_allowed():
    for host in ("127.0.0.1", "localhost", "::1"):
        check_host(host, allow_remote=False)
    with pytest.raises(RconError, match="isn't this PC"):
        RconClient("192.0.2.10", 25575, PW)
    with pytest.raises(RconError):
        RestClient("example.com", 8212, PW)
    check_host("192.0.2.10", allow_remote=True)


def test_rcon_never_shows_the_password():
    c = RconClient("127.0.0.1", 1, PW)
    assert PW not in repr(c)
    assert PW.encode() in encode(1, 3, PW)  # on the wire only


# -- REST client ----------------------------------------------------------------------------


def test_rest_commands_parse():
    assert parse("announce Server restarting in 5 min") == (
        "POST", "announce", {"message": "Server restarting in 5 min"})
    assert parse("save") == ("POST", "save", {})
    assert parse("shutdown 10 Bye all") == ("POST", "shutdown",
                                            {"waittime": 10, "message": "Bye all"})
    assert parse("shutdown") == ("POST", "shutdown", {"waittime": 1, "message": ""})
    assert parse("INFO") == ("GET", "info", None)
    for bad in ("announce", "shutdown soon", "kick someone", ""):
        with pytest.raises(RestError):
            parse(bad)


def test_rest_calls_with_basic_auth(fake_rest):
    c = RestClient("127.0.0.1", fake_rest.port, PW)
    assert '"servername": "Fake server"' in c.command("info")
    assert c.command("announce Hello world") == ""
    c.command("shutdown 5 Bye")
    assert fake_rest.calls[1:] == [("POST", "announce", {"message": "Hello world"}),
                                   ("POST", "shutdown", {"waittime": 5, "message": "Bye"})]
    assert PW not in repr(c)


def test_rest_wrong_password(fake_rest):
    with pytest.raises(RestError, match="refused the password") as e:
        RestClient("127.0.0.1", fake_rest.port, "wrong").command("save")
    assert "wrong" not in str(e.value) and fake_rest.calls == []


def test_rest_slow_and_dropped():
    with FakeRest(PW, mode="slow", delay=2) as fake:
        began = time.monotonic()
        with pytest.raises(RestError, match="didn't answer in time"):
            RestClient("127.0.0.1", fake.port, PW, timeout=0.5).command("save")
        assert time.monotonic() - began < 1.5
    with FakeRest(PW, mode="drop") as fake, pytest.raises(RestError):
        RestClient("127.0.0.1", fake.port, PW, timeout=2).command("save")


# -- passwords ------------------------------------------------------------------------------


def test_passwords_live_in_secrets_toml(settings):
    remote.set_password(settings, "rcon", 4, PW)
    remote.set_password(settings, "rest", 1, "other \"quoted\" one")
    assert remote.passwords(settings, "rcon") == {4: PW}
    assert remote.passwords(settings, "rest") == {1: 'other "quoted" one'}
    remote.set_password(settings, "rcon", 4, "")
    assert remote.passwords(settings, "rcon") == {}
    assert "[rcon" not in settings.secrets_path.read_text(encoding="utf-8")


def test_send_says_what_is_missing(store, settings):
    consoles = RemoteConsoles(settings)
    server = store.add_server(fake_config("nothing"))
    with pytest.raises(RemoteError, match="no RCON port"):
        consoles.send(server, StopAction.RCON, "Save")
    server = store.add_server(fake_config("noport", rcon_port=25575))
    with pytest.raises(RemoteError, match="no RCON password"):
        consoles.send(server, "rcon", "Save")
    with pytest.raises(RemoteError, match="isn't RCON or REST"):
        consoles.send(server, "stdin", "Save")
    assert consoles.default_channel(server) == "stdin"


# -- through the supervisor -----------------------------------------------------------------


def with_remote(store, settings, name, rcon=None, rest=None, **fields):  # type: ignore[no-untyped-def]
    server = store.add_server(fake_config(
        name, ready_after_s=1, rcon_port=rcon.port if rcon else None,
        rest_port=rest.port if rest else None, **fields))
    if rcon:
        remote.set_password(settings, "rcon", server.id, PW)
    if rest:
        remote.set_password(settings, "rest", server.id, PW)
    return server


def running(sup, server_id):  # type: ignore[no-untyped-def]
    sup.start(server_id)
    wait_for(lambda: sup.state(server_id) is ServerState.RUNNING, 20)


def test_rcon_stop_step_then_ctrl_break(store, settings, sup, fake_rcon):
    recipe = [StopStep(action=StopAction.RCON, command="Save", wait_s=0.5),
              StopStep(action=StopAction.CTRL_BREAK, wait_s=10)]
    server = with_remote(store, settings, "rcon-stop", rcon=fake_rcon, stop_recipe=recipe)
    running(sup, server.id)
    result = sup.stop(server.id)
    assert result.graceful and result.ended_by == "ctrl_break"
    assert fake_rcon.commands == ["Save"]
    assert "(rcon) Save: ok: Save" in console_text(sup, server.id)


def test_rest_shutdown_ends_the_stop(store, settings, sup):
    def game_quits(_wait):  # type: ignore[no-untyped-def]
        sup.send_command(server.id, "quit")  # as the real game exits after /shutdown

    recipe = [StopStep(action=StopAction.REST, command="save", wait_s=0),
              StopStep(action=StopAction.REST, command="shutdown 1 Bye", wait_s=10),
              StopStep(action=StopAction.CTRL_BREAK, wait_s=10)]
    warning = StopWarning(action=StopAction.REST, command="announce Stop in {seconds} s",
                          minutes=[0.02])
    with FakeRest(PW, on_shutdown=game_quits) as fake:
        server = with_remote(store, settings, "rest-stop", rest=fake, stop_recipe=recipe,
                             stop_warning=warning)
        running(sup, server.id)
        result = sup.stop(server.id, warn=True)
        assert result.graceful and result.ended_by == "rest"
        assert [c[1] for c in fake.calls] == ["announce", "save", "shutdown"]
        assert fake.calls[0][2] == {"message": "Stop in 1 s"}


def test_a_failing_channel_never_holds_up_a_stop(store, settings):
    quick = dataclasses.replace(settings, remote_timeout_s=0.5)
    sup = Supervisor(store, quick)
    recipe = [StopStep(action=StopAction.RCON, command="Save", wait_s=30),
              StopStep(action=StopAction.REST, command="save", wait_s=30),
              StopStep(action=StopAction.CTRL_BREAK, wait_s=10)]
    warning = StopWarning(action=StopAction.RCON, command="announce {minutes}", minutes=[1])
    try:
        with FakeRcon(PW, mode="slow", delay=3) as slow, FakeRest("other") as rest:
            server = with_remote(store, quick, "failing", rcon=slow, rest=rest,
                                 stop_recipe=recipe, stop_warning=warning)
            running(sup, server.id)
            began = time.monotonic()
            result = sup.stop(server.id, warn=True)
            assert result.graceful and result.ended_by == "ctrl_break"
            assert time.monotonic() - began < 8  # no 30 s waits, no 1 min countdown
            text = console_text(sup, server.id)
            assert "Skipped warning 'announce 1': RCON failed: RCON didn't answer" in text
            assert "Skipped stop step 'save': REST failed: the REST API refused" in text
            assert PW not in text
    finally:
        stop_everything(sup, store)


def test_console_commands_over_rcon(store, settings, sup):
    with FakeRcon(PW, mode="drop") as fake:
        server = with_remote(store, settings, "console", rcon=fake)
        running(sup, server.id)
        assert sup.remote.default_channel(store.get_server(server.id)) == "rcon"
        sup.send_command(server.id, "big 5000", "rcon")
        sup.send_command(server.id, "echo again", "rcon")  # after the drop: reconnects
        text = console_text(sup, server.id)
        assert "(rcon) big 5000" in text and "x" * 5000 in text and "  again" in text
        remote.set_password(settings, "rcon", server.id, "wrong")
        with pytest.raises(SupervisorError, match="RCON failed: RCON refused the password"):
            sup.send_command(server.id, "echo no", "rcon")


# -- over the API ---------------------------------------------------------------------------


def test_api_sets_passwords_and_never_shows_them(store, settings, fake_rcon):
    web = dataclasses.replace(settings, admin_logins=(ADMIN,))
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    server = store.add_server(fake_config("api", ready_after_s=1, rcon_port=fake_rcon.port))
    try:
        with TestClient(app, base_url=BASE, client=("127.0.0.1", 50124)) as c:
            res = c.put(f"/api/servers/{server.id}/remote", json={"rcon_password": PW})
            assert res.json() == {"channels": ["stdin", "rcon"], "default": "rcon",
                                  "password_set": {"rcon": True, "rest": False}}
            view = c.get(f"/api/servers/{server.id}")
            assert PW not in view.text and view.json()["remote"]["default"] == "rcon"
            assert c.post(f"/api/servers/{server.id}/start").status_code == 200
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["state"] == "running",
                     20)
            res = c.post(f"/api/servers/{server.id}/command", json={"text": "echo hi"})
            assert res.status_code == 204 and fake_rcon.commands == ["echo hi"]
            res = c.post(f"/api/servers/{server.id}/command",
                         json={"text": "status", "channel": "stdin"})
            assert res.status_code == 204 and fake_rcon.commands == ["echo hi"]
            assert c.post(f"/api/servers/{server.id}/command",
                          json={"text": "x", "channel": "telnet"}).status_code == 422
            c.put(f"/api/servers/{server.id}/remote", json={"rcon_password": ""})
            assert c.get(f"/api/servers/{server.id}").json()["remote"]["channels"] == ["stdin"]
            audit = c.get("/api/audit").text
            assert PW not in audit and "rcon.password" in audit
    finally:
        stop_everything(sup, store)
