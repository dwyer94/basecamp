"""Crash auto-restart (R-05): restart, give up after 3 in 10 minutes, Discord, cancel."""

from __future__ import annotations

import dataclasses
import time

import pytest

from basecamp import notify
from basecamp.crashes import CrashPolicy
from basecamp.models import NotifyEvent, ServerState
from basecamp.notify import Notifier
from basecamp.supervisor import Supervisor
from tests.conftest import console_text, fake_config, wait_for


@pytest.fixture
def quick(settings):
    return dataclasses.replace(settings, crash_restart_delay_s=0.3, notify_retry_s=(0.1,))


@pytest.fixture
def qsup(store, quick):
    sup = Supervisor(store, quick)
    yield sup
    for server in store.list_servers():
        if sup.state(server.id) is not ServerState.STOPPED:
            sup.kill(server.id)
    sup.detach()


def test_crashing_server_is_restarted_twice_then_left_crashed(store, qsup, quick, webhook):
    notify.set_webhook(quick, webhook.url)
    notifier = Notifier(quick, store)
    qsup.add_listener(notifier.on_state)
    policy = CrashPolicy(store, qsup, quick, notifier)
    starts = []
    qsup.add_listener(lambda sid, old, new: starts.append(new) if new is ServerState.STARTING
                      else None)
    # Crashes 1.5 s after every start.
    server = store.add_server(fake_config("alpha", "--crash-after", "1.5"))

    qsup.start(server.id)

    wait_for(lambda: any("gave up" in t or "crashed 3 times" in t for t in webhook.texts()),
             timeout=30)
    assert qsup.state(server.id) is ServerState.CRASHED
    assert len(starts) == 3  # the first start and two restarts
    time.sleep(1)  # and no fourth
    assert len(starts) == 3 and policy.restart_at(server.id) is None
    texts = webhook.texts()
    assert sum("crashed (exit code 3). Restarting" in t for t in texts) == 2
    assert "crashed 3 times in 10 minutes" in texts[-1]
    assert sum("is up" in t for t in texts) == 3
    out = console_text(qsup, server.id)
    assert "restarting in 0.3 s (crash 1 of 3" in out and "not restarting" in out

    # Press Start: it gets its retries back.
    qsup.start(server.id)
    wait_for(lambda: policy.restart_at(server.id) is not None, timeout=10)
    notifier.close()


def test_stop_cancels_a_pending_restart(store, settings):
    slow = dataclasses.replace(settings, crash_restart_delay_s=3)
    sup = Supervisor(store, slow)
    policy = CrashPolicy(store, sup, slow)
    server = store.add_server(fake_config("bravo"))
    try:
        sup.start(server.id)
        wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)
        sup.send_command(server.id, "crash")
        wait_for(lambda: policy.restart_at(server.id) is not None)

        assert policy.cancel(server.id)

        time.sleep(3.5)
        assert sup.state(server.id) is ServerState.CRASHED
        assert "restart cancelled" in console_text(sup, server.id)
    finally:
        sup.detach()


def test_manual_start_during_the_wait_wins(store, qsup, quick):
    slow = dataclasses.replace(quick, crash_restart_delay_s=1.5)
    sup = Supervisor(store, slow)
    policy = CrashPolicy(store, sup, slow)
    server = store.add_server(fake_config("charlie"))
    try:
        sup.start(server.id)
        wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)
        sup.send_command(server.id, "crash")
        wait_for(lambda: policy.restart_at(server.id) is not None)
        pid = sup.start(server.id).pid  # by hand, before the timer

        time.sleep(2)
        assert sup.state(server.id) is ServerState.RUNNING and sup.status(server.id).pid == pid
    finally:
        sup.kill(server.id)
        sup.detach()


def test_restart_that_cannot_start_gives_up(store, qsup, quick, webhook, tmp_path):
    notify.set_webhook(quick, webhook.url)
    notifier = Notifier(quick, store)
    CrashPolicy(store, qsup, quick, notifier)
    server = store.add_server(fake_config("delta", notify_off=[NotifyEvent.CRASHED]))
    qsup.start(server.id)
    wait_for(lambda: qsup.state(server.id) is ServerState.RUNNING)
    # The exe vanishes (an update gone wrong, a moved folder) and then it crashes.
    store.update_server(server.id, server.model_copy(update={"exe_path": str(tmp_path / "x.exe")}))
    qsup.send_command(server.id, "crash")

    wait_for(lambda: webhook.texts(), timeout=10)
    assert "couldn't be restarted" in webhook.texts()[0]  # "crashed" is off for it
    assert qsup.state(server.id) is ServerState.CRASHED
    notifier.close()
