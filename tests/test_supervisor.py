"""The supervisor against real fake-server processes (Phase 1 done-criteria)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import psutil
import pytest

from basecamp.models import Port, Protocol, ServerState, StopAction, StopStep, StopWarning
from basecamp.supervisor import InvalidState, PortInUse, Supervisor
from tests.conftest import console_text, fake_config, free_port, wait_for

QUIT_RECIPE = [
    StopStep(action=StopAction.STDIN, command="save", wait_s=0.5),
    StopStep(action=StopAction.STDIN, command="quit", wait_s=5),
]


def start_and_wait(sup: Supervisor, server_id: int) -> int:
    pid = sup.start(server_id).pid
    wait_for(lambda: sup.state(server_id) is ServerState.RUNNING)
    return pid


def test_start_runs_and_stop_is_graceful(sup, store, settings):
    server = store.add_server(fake_config("alpha", stop_recipe=QUIT_RECIPE))
    states = []
    sup.add_listener(lambda sid, old, new: states.append(new))

    pid = start_and_wait(sup, server.id)
    status = sup.status(server.id)
    assert status.pid == pid and status.has_console and not status.adopted
    saved = json.loads(settings.state_path.read_text())
    assert saved[str(server.id)]["pid"] == pid

    result = sup.stop(server.id)

    assert result.graceful and not result.killed and result.ended_by == "stdin"
    assert sup.state(server.id) is ServerState.STOPPED
    assert not psutil.pid_exists(pid)
    text = console_text(sup, server.id)
    assert "World saved" in text and "Goodbye" in text
    assert states == [ServerState.STARTING, ServerState.RUNNING, ServerState.STOPPING,
                      ServerState.STOPPED]
    assert json.loads(settings.state_path.read_text()) == {}
    log_file = settings.server_log_dir(server.id) / "console.log"
    assert "Goodbye" in log_file.read_text(encoding="utf-8")


def test_default_recipe_uses_ctrl_break(sup, store):
    server = store.add_server(fake_config("bravo"))
    start_and_wait(sup, server.id)

    result = sup.stop(server.id)

    assert result.graceful and result.ended_by == "ctrl_break"
    assert "Ctrl+Break received" in console_text(sup, server.id)


def test_restart_starts_a_new_process(sup, store):
    server = store.add_server(fake_config("charlie", stop_recipe=QUIT_RECIPE))
    first = start_and_wait(sup, server.id)

    second = sup.restart(server.id).pid
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)

    assert second != first and not psutil.pid_exists(first)
    assert psutil.pid_exists(second)


def test_concurrent_restarts_are_serialised(sup, store):
    server = store.add_server(fake_config("delta", stop_recipe=QUIT_RECIPE))
    start_and_wait(sup, server.id)
    errors = []

    def restart():
        try:
            sup.restart(server.id)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=restart) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)
    fakes = [p for p in psutil.process_iter(["cmdline"])
             if p.info["cmdline"] and "delta" in p.info["cmdline"]]
    assert len(fakes) == 1


def test_start_twice_is_refused(sup, store):
    server = store.add_server(fake_config("echo"))
    sup.start(server.id)
    with pytest.raises(InvalidState):
        sup.start(server.id)


def test_send_command_goes_to_stdin(sup, store):
    server = store.add_server(fake_config("foxtrot"))
    start_and_wait(sup, server.id)

    sup.send_command(server.id, "say hello there")

    wait_for(lambda: "hello there" in console_text(sup, server.id))


def test_hung_server_is_killed(sup, store):
    recipe = [
        StopStep(action=StopAction.STDIN, command="quit", wait_s=0.5),
        StopStep(action=StopAction.CTRL_BREAK, wait_s=0.5),
    ]
    server = store.add_server(fake_config("golf", "--hang-on-stop", stop_recipe=recipe))
    pid = start_and_wait(sup, server.id)

    started = time.monotonic()
    result = sup.stop(server.id)

    assert result.killed and not result.graceful and result.ended_by == "kill"
    assert time.monotonic() - started < 8
    assert sup.state(server.id) is ServerState.STOPPED
    assert not psutil.pid_exists(pid)
    assert "ignoring Ctrl+Break" in console_text(sup, server.id)


def test_kill_ends_a_stuck_stop(sup, store):
    recipe = [StopStep(action=StopAction.STDIN, command="quit", wait_s=30)]
    server = store.add_server(fake_config("hotel", "--hang-on-stop", stop_recipe=recipe))
    start_and_wait(sup, server.id)
    results = []
    stopper = threading.Thread(target=lambda: results.append(sup.stop(server.id)))
    stopper.start()
    wait_for(lambda: sup.state(server.id) is ServerState.STOPPING)

    sup.kill(server.id)
    stopper.join(10)

    assert sup.state(server.id) is ServerState.STOPPED
    assert results and results[0].killed and results[0].ended_by == "kill"


def test_unexpected_exit_is_a_crash(sup, store):
    server = store.add_server(fake_config("india"))
    start_and_wait(sup, server.id)

    sup.send_command(server.id, "crash")

    wait_for(lambda: sup.state(server.id) is ServerState.CRASHED)
    assert sup.status(server.id).last_exit_code == 3
    start_and_wait(sup, server.id)  # a crashed server can be started again


def test_ready_after_timeout_without_ready_line(sup, store):
    server = store.add_server(fake_config("juliet", ready_pattern="", ready_after_s=0.5))
    sup.start(server.id)
    assert sup.state(server.id) is ServerState.STARTING
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING, timeout=5)


def test_duplicate_port_of_running_server_is_refused(sup, store):
    port = free_port()
    first = store.add_server(fake_config("kilo", port=port))
    second = store.add_server(fake_config("lima", port=port))
    start_and_wait(sup, first.id)

    with pytest.raises(PortInUse) as err:
        sup.start(second.id)

    assert err.value.ports == [f"udp/{port}"]
    assert sup.state(second.id) is ServerState.STOPPED
    assert "port in use" in console_text(sup, second.id)


def test_duplicate_port_is_refused_before_the_game_binds(sup, store):
    port = free_port()
    first = store.add_server(fake_config("mike", "--ready-delay", "3", port=port))
    second = store.add_server(fake_config("november", port=port))
    sup.start(first.id)  # still Starting

    with pytest.raises(PortInUse):
        sup.start(second.id)


@pytest.mark.parametrize("protocol", [Protocol.UDP, Protocol.TCP])
def test_port_held_by_another_program_is_refused(sup, store, protocol):
    kind = socket.SOCK_DGRAM if protocol is Protocol.UDP else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as other:
        other.bind(("127.0.0.1", 0))
        if protocol is Protocol.TCP:
            other.listen()
        port = other.getsockname()[1]
        server = store.add_server(fake_config("oscar", port=port, protocol=protocol))
        with pytest.raises(PortInUse):
            sup.start(server.id)


def test_low_ram_warns_but_starts(sup, store):
    server = store.add_server(fake_config("papa", min_free_ram_mb=10**9))

    result = sup.start(server.id)

    assert result.warnings and "Low free RAM" in result.warnings[0]
    assert "Warning: Low free RAM" in console_text(sup, server.id)
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)


def test_launcher_game_tracks_the_real_exe(sup, store):
    server = store.add_server(fake_config("quebec", "--spawn-child", real_exe_name="python.exe",
                                          stop_recipe=QUIT_RECIPE))
    launcher_pid = sup.start(server.id).pid

    wait_for(lambda: sup.status(server.id).pid != launcher_pid)
    real_pid = sup.status(server.id).pid
    wait_for(lambda: not psutil.pid_exists(launcher_pid))  # the launcher has exited
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)
    time.sleep(0.5)
    assert sup.state(server.id) is ServerState.RUNNING  # launcher exit wasn't a crash

    result = sup.stop(server.id)  # the child shares the launcher's stdin pipe

    assert result.graceful and result.ended_by == "stdin"
    assert not psutil.pid_exists(real_pid)


def test_log_file_source_is_tailed(sup, store, tmp_path):
    log_file = tmp_path / "game.log"
    log_file.write_text("old line from last run\n", encoding="utf-8")
    server = store.add_server(fake_config("romeo", "--log-file", str(log_file),
                                          log_source="file", log_file=str(log_file)))
    start_and_wait(sup, server.id)  # Running came from the ready line in the file

    sup.send_command(server.id, "say from the file")

    wait_for(lambda: "from the file" in console_text(sup, server.id))
    assert "old line from last run" not in console_text(sup, server.id)


def test_start_on_boot_in_order(sup, store):
    late = store.add_server(fake_config("sierra", start_on_boot=True, boot_order=2))
    store.add_server(fake_config("tango", start_on_boot=False))
    early = store.add_server(fake_config("uniform", start_on_boot=True, boot_order=1))
    order = []
    sup.add_listener(lambda sid, old, new: order.append(sid) if new is ServerState.STARTING
                     else None)

    results = sup.start_on_boot(gap_s=0.3)

    assert list(results) == [early.id, late.id]
    assert order == [early.id, late.id]
    assert sup.status(late.id).started_at - sup.status(early.id).started_at >= 0.3


def test_stale_state_entry_is_not_adopted_and_reported_as_crash(sup, store, settings):
    server = store.add_server(fake_config("victor"))
    me = psutil.Process(os.getpid())
    settings.state_path.write_text(json.dumps({str(server.id): {
        "pid": me.pid, "exe": me.exe(), "create_time": me.create_time() - 100,
        "started_at": time.time(),
    }}))

    assert sup.adopt_orphans() == []
    # The game vanished while Basecamp was down, with no host to say why: a crash.
    assert sup.state(server.id) is ServerState.CRASHED
    assert json.loads(settings.state_path.read_text()) == {}


def test_ports_model_rejects_bad_port():
    with pytest.raises(ValueError):
        Port(port=70000)


def test_ctrl_c_step_reaches_the_game(sup, store):
    # The host starts games with Ctrl+C on, though it was itself started in a new process
    # group (which turns Ctrl+C off for it and, without care, for its children): D-120.
    recipe = [StopStep(action=StopAction.CTRL_C, wait_s=10)]
    server = store.add_server(fake_config("ctrlc", stop_recipe=recipe))
    pid = start_and_wait(sup, server.id)

    result = sup.stop(server.id)

    assert result.graceful and not result.killed and result.ended_by == "ctrl_c"
    assert not psutil.pid_exists(pid)
    text = console_text(sup, server.id)
    assert "Sending Ctrl+C" in text and "Ctrl+C received" in text and "World saved" in text


def test_wait_save_waits_for_the_games_autosave(sup, store, tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    recipe = [StopStep(action=StopAction.WAIT_SAVE, wait_s=20),
              StopStep(action=StopAction.CTRL_C, wait_s=10)]
    server = store.add_server(fake_config("autosave", "--autosave", "5", "--save-dir", str(saves),
                                          save_dir=str(saves), stop_recipe=recipe))
    start_and_wait(sup, server.id)
    wait_for(lambda: (saves / "world.sav").exists())
    first = (saves / "world.sav").stat().st_mtime_ns

    result = sup.stop(server.id)

    assert result.graceful and result.ended_by == "ctrl_c"
    assert (saves / "world.sav").stat().st_mtime_ns > first  # stopped after a new save
    text = console_text(sup, server.id)
    assert "Waiting up to 20 s for the game to save" in text and "Saved: world.sav" in text


def test_wait_save_goes_on_after_its_time(sup, store, tmp_path):
    recipe = [StopStep(action=StopAction.WAIT_SAVE, wait_s=1),
              StopStep(action=StopAction.CTRL_C, wait_s=10)]
    server = store.add_server(fake_config("nosave", save_dir=str(tmp_path), stop_recipe=recipe))
    start_and_wait(sup, server.id)

    started = time.monotonic()
    result = sup.stop(server.id)

    assert result.graceful and result.ended_by == "ctrl_c"
    assert time.monotonic() - started < 8
    assert "No save in 1 s; going on" in console_text(sup, server.id)


def test_wait_save_without_a_save_folder_is_skipped(sup, store):
    recipe = [StopStep(action=StopAction.WAIT_SAVE, wait_s=30),
              StopStep(action=StopAction.CTRL_BREAK, wait_s=10)]
    server = store.add_server(fake_config("nofolder", stop_recipe=recipe))
    start_and_wait(sup, server.id)

    started = time.monotonic()
    result = sup.stop(server.id)

    assert result.graceful and result.ended_by == "ctrl_break"
    assert time.monotonic() - started < 8
    assert "Skipped waiting for a save: no save folder (not set)" in console_text(sup, server.id)


def test_console_event_and_wait_steps_need_no_command():
    for action in (StopAction.CTRL_BREAK, StopAction.CTRL_C, StopAction.WAIT_SAVE):
        assert StopStep(action=action).command == ""
        with pytest.raises(ValueError):
            StopWarning(action=action, command="x", minutes=[1])
    with pytest.raises(ValueError):
        StopStep(action=StopAction.STDIN)
