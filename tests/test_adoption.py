"""R-04 / R-04a: games outlive Basecamp, and a new Basecamp reattaches to their hosts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

import psutil
import pytest

from basecamp import hostclient
from basecamp.db import Store
from basecamp.hostclient import HostClient, HostError, IncompatibleHost
from basecamp.models import Protocol, ServerState
from basecamp.supervisor import NoConsole, Supervisor
from tests.conftest import REPO, console_text, fake_config, free_port, wait_for


def _is_host(proc: psutil.Process) -> bool:
    try:
        return "--spec" in proc.cmdline()
    except psutil.Error:
        return False


def _kill_basecamp(proc: subprocess.Popen) -> None:
    """Hard-kill Basecamp (TerminateProcess, no cleanup), sparing the hosts it started.

    With a venv, sys.executable is a small launcher whose child is the real interpreter,
    so kill the launcher and its direct children, but never a host.
    """
    root = psutil.Process(proc.pid)
    victims = [c for c in root.children() if not _is_host(c)] + [root]
    for p in victims:
        p.kill()
    psutil.wait_procs(victims, timeout=10)
    proc.wait(10)


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as res:
            return json.loads(res.read())["ok"]
    except OSError:
        return False


def _saved(settings, server_id: int) -> dict | None:
    try:
        return json.loads(settings.state_path.read_text())[str(server_id)]
    except (OSError, ValueError, KeyError):
        return None


def test_reattaches_after_basecamp_is_killed(settings):
    store = Store.open(settings.db_path, settings.migrations_dir)
    server = store.add_server(fake_config("adoptee", "--tick", "0.3", start_on_boot=True))
    store.close()
    web_port = free_port(Protocol.TCP)  # not 8150: a dev server may be running
    (settings.data_dir / "settings.toml").write_text(
        f"[basecamp]\nlow_ram_mb = 0\nport = {web_port}\n")

    env = {**os.environ, "BASECAMP_DATA_DIR": str(settings.data_dir)}
    basecamp = subprocess.Popen([sys.executable, "-m", "basecamp"], cwd=REPO, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    game_pid = None
    try:
        wait_for(lambda: (_saved(settings, server.id) or {}).get("state") == "running",
                 timeout=30)
        game_pid = _saved(settings, server.id)["pid"]
        # The web server is up too, on the port from settings.toml.
        wait_for(lambda: _healthy(web_port), timeout=15)
        _kill_basecamp(basecamp)

        time.sleep(1.0)
        assert psutil.pid_exists(game_pid)  # R-04a: Basecamp dying doesn't stop the game

        store = Store.open(settings.db_path, settings.migrations_dir)
        sup = Supervisor(store, settings)
        try:
            assert sup.adopt_orphans() == [server.id]
            status = sup.status(server.id)
            assert status.state is ServerState.RUNNING
            assert status.pid == game_pid and status.adopted and status.has_console
            # Output from before the restart comes back from the host's buffer...
            wait_for(lambda: "Server ready" in console_text(sup, server.id))
            assert "Basecamp reattached" in console_text(sup, server.id)
            # ...and stdin works again.
            sup.send_command(server.id, "say hello after the restart")
            wait_for(lambda: "hello after the restart" in console_text(sup, server.id))
            assert sup.start_on_boot(gap_s=0) == {server.id: "already running"}

            result = sup.stop(server.id)

            assert result.graceful and result.ended_by == "ctrl_break"
            assert sup.state(server.id) is ServerState.STOPPED
            assert not psutil.pid_exists(game_pid)
            log = (settings.server_log_dir(server.id) / "console.log").read_text("utf-8")
            assert "Server ready" in log and "hello after the restart" in log
            assert "Goodbye" in log
        finally:
            sup.detach()
            store.close()
    finally:
        if basecamp.poll() is None:
            basecamp.kill()
        if game_pid and psutil.pid_exists(game_pid):
            psutil.Process(game_pid).kill()


def test_exit_while_basecamp_is_down_is_reported_as_crash(sup, store, settings):
    server = store.add_server(fake_config("crasher", "--crash-after", "1.5"))
    first = Supervisor(store, settings)
    first.start(server.id)
    wait_for(lambda: first.state(server.id) is ServerState.RUNNING)
    first.detach()  # Basecamp is "down"
    wait_for(lambda: (settings.host_dir(server.id) / "exit.json").exists())
    crashes = []
    sup.add_listener(lambda sid, old, new: crashes.append(sid)
                     if new is ServerState.CRASHED else None)

    assert sup.adopt_orphans() == []

    assert sup.state(server.id) is ServerState.CRASHED
    assert sup.status(server.id).last_exit_code == 3
    assert crashes == [server.id]
    assert "Crashed while Basecamp was down (code 3)" in console_text(sup, server.id)
    assert not (settings.host_dir(server.id) / "exit.json").exists()  # collected once


def test_stop_finished_while_basecamp_is_down_is_not_a_crash(sup, store, settings):
    server = store.add_server(fake_config("stopper"))
    first = Supervisor(store, settings)
    first.start(server.id)
    wait_for(lambda: first.state(server.id) is ServerState.RUNNING)
    entry = _saved(settings, server.id)["host"]
    first.detach()
    client = HostClient(entry["port"], entry["token"])
    client.mark_stopping()  # a stop was under way when Basecamp went down
    client.send_line("quit")
    wait_for(lambda: (settings.host_dir(server.id) / "exit.json").exists())

    assert sup.adopt_orphans() == []
    assert sup.state(server.id) is ServerState.STOPPED


def test_host_dies_while_basecamp_is_down_game_is_adopted_bare(sup, store, settings):
    server = store.add_server(fake_config("bare", "--tick", "0.2"))
    first = Supervisor(store, settings)
    game_pid = first.start(server.id).pid
    wait_for(lambda: first.state(server.id) is ServerState.RUNNING)
    host_pid = _saved(settings, server.id)["host"]["pid"]
    first.detach()
    psutil.Process(host_pid).kill()
    time.sleep(0.5)
    assert psutil.pid_exists(game_pid)  # the game survives losing its pipes

    assert sup.adopt_orphans() == [server.id]
    status = sup.status(server.id)
    assert status.pid == game_pid and not status.has_console
    with pytest.raises(NoConsole):
        sup.send_command(server.id, "save")

    result = sup.stop(server.id)  # Ctrl+Break through the winctrl helper

    assert result.graceful and result.ended_by == "ctrl_break"
    assert not psutil.pid_exists(game_pid)


def test_host_dies_while_basecamp_runs(sup, store, settings):
    server = store.add_server(fake_config("lost", "--tick", "0.2"))
    game_pid = sup.start(server.id).pid
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)

    psutil.Process(_saved(settings, server.id)["host"]["pid"]).kill()

    wait_for(lambda: not sup.status(server.id).has_console)
    assert sup.state(server.id) is ServerState.RUNNING
    assert "Lost the server's host" in console_text(sup, server.id)
    assert "host" not in _saved(settings, server.id)
    result = sup.stop(server.id)
    assert result.graceful and not psutil.pid_exists(game_pid)


def test_adopted_server_that_dies_is_a_crash(sup, store, settings):
    server = store.add_server(fake_config("orphan"))
    first = Supervisor(store, settings)
    pid = first.start(server.id).pid
    wait_for(lambda: first.state(server.id) is ServerState.RUNNING)
    first.detach()

    assert sup.adopt_orphans() == [server.id]
    psutil.Process(pid).kill()

    wait_for(lambda: sup.state(server.id) is ServerState.CRASHED)
    assert json.loads(settings.state_path.read_text()) == {}


def test_host_channel_needs_the_token_and_negotiates_protocol(sup, store, settings,
                                                              monkeypatch):
    server = store.add_server(fake_config("channel"))
    sup.start(server.id)
    entry = _saved(settings, server.id)["host"]

    with pytest.raises(HostError):
        HostClient(entry["port"], "not-the-token").hello()

    # A newer Basecamp offering a higher protocol gets the host's own version back.
    monkeypatch.setattr(hostclient, "MAX_HOST_PROTOCOL", 99)
    assert HostClient(entry["port"], entry["token"]).hello() == 1
    # A Basecamp that no longer supports protocol 1 refuses the host (falls back to bare).
    monkeypatch.setattr(hostclient, "MIN_HOST_PROTOCOL", 2)
    with pytest.raises(IncompatibleHost):
        HostClient(entry["port"], entry["token"]).hello()
