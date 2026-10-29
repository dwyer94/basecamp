"""Scheduled restarts and updates (batch 2, item 9; D-133), with a clock the test moves."""
# ruff: noqa: F811  (the SteamCMD fixtures are shared with test_steam)

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

from basecamp.models import PlayerQuery, PlayerSource, ServerState
from basecamp.players import Players
from basecamp.schedule import Schedules
from basecamp.steam import Kind
from tests.conftest import REPO, console_text, fake_config, wait_for
from tests.test_steam import (  # noqa: F401
    backups,
    fake_env,
    notifier,
    runner,
    ssup,
    steam_server,
    steam_settings,
    wait_running,
)

sys.path.insert(0, str(REPO / "tools"))
from fake_a2s import FakeA2S

DAY = datetime(2026, 10, 29)


class Clock:
    def __init__(self, at: str) -> None:
        self.set(at)

    def set(self, at: str, day: int = 0) -> None:
        h, m = (int(x) for x in at.split(":"))
        self.now = DAY + timedelta(days=day, hours=h, minutes=m)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def query():
    with FakeA2S(players=0, most=8) as fake:
        yield fake


def scheduler(store, ssup, runner, steam_settings, clock, audits=None):
    players = Players(store, ssup, steam_settings)
    audit = (lambda *a: audits.append(a)) if audits is not None else None
    return Schedules(store, ssup, runner, players, steam_settings, audit=audit, now=clock)


def start(ssup, store, name="s", **fields):
    server = store.add_server(fake_config(name, "--tick", "0.3", **fields))
    ssup.start(server.id)
    wait_for(lambda: ssup.state(server.id) is ServerState.RUNNING, 20)
    return server


def restarted(ssup, sid: int) -> bool:
    return "Stopping" in console_text(ssup, sid)


def test_daily_restart_at_its_time_once_a_day(store, ssup, runner, steam_settings):
    clock = Clock("03:59")
    audits: list = []
    sched = scheduler(store, ssup, runner, steam_settings, clock, audits)
    server = start(ssup, store, restart_at="04:00")
    idle = start(ssup, store, "idle")  # no schedule
    ssup.stop(idle.id)
    assert sched.tick() == []
    clock.set("04:00")
    assert sched.tick() == [(server.id, "restart", "restart")]
    wait_for(lambda: restarted(ssup, server.id), 20)
    wait_running(ssup, server.id)
    assert audits[0][:2] == ("schedule", "schedule.restart")
    clock.set("04:05")
    assert sched.tick() == []  # done today
    again = scheduler(store, ssup, runner, steam_settings, clock)
    assert again.tick() == []  # remembered in schedule.json
    clock.set("04:00", day=1)
    assert again.tick() == [(server.id, "restart", "restart")]


def test_missed_or_stopped_does_nothing(store, ssup, runner, steam_settings):
    clock = Clock("04:30")
    sched = scheduler(store, ssup, runner, steam_settings, clock)
    late = start(ssup, store, "late", restart_at="04:00")
    stopped = store.add_server(fake_config("stopped", restart_at="04:25"))
    assert sorted(sched.tick()) == sorted([(late.id, "restart", "missed"),
                                           (stopped.id, "restart", "not running")])
    assert not restarted(ssup, late.id)


def test_skip_while_players_are_on_and_unknown_goes_ahead(store, ssup, runner,
                                                            steam_settings, query):
    clock = Clock("04:00")
    sched = scheduler(store, ssup, runner, steam_settings, clock)
    on = start(ssup, store, "on", restart_at="04:00", schedule_players="skip",
               players=PlayerQuery(source=PlayerSource.A2S, port=query.port))
    unknown = start(ssup, store, "unknown", restart_at="04:00", schedule_players="skip",
                    players=PlayerQuery(source=PlayerSource.REST))  # no REST password
    query.players = 2
    results = dict(((sid, kind), what) for sid, kind, what in sched.tick())
    assert results[(on.id, "restart")] == "skipped"
    assert results[(unknown.id, "restart")] == "restart"
    assert "2 player(s) on" in console_text(ssup, on.id)
    assert "player count is unknown" in console_text(ssup, unknown.id)
    wait_for(lambda: restarted(ssup, unknown.id), 20)
    assert not restarted(ssup, on.id)


def test_delay_until_they_leave_then_after_the_limit(store, ssup, runner, steam_settings,
                                                     query):
    clock = Clock("04:00")
    sched = scheduler(store, ssup, runner, steam_settings, clock)
    server = start(ssup, store, "d", restart_at="04:00", schedule_players="delay",
                   players=PlayerQuery(source=PlayerSource.A2S, port=query.port))
    query.players = 1
    assert sched.tick() == [(server.id, "restart", "waiting")]
    assert sched.view(store.get_server(server.id))["waiting"]["restart"]
    clock.set("04:02")
    assert sched.tick() == []  # asks again every 5 min
    clock.set("04:05")
    assert sched.tick() == [(server.id, "restart", "waiting")]
    query.players = 0
    clock.set("04:10")
    assert sched.tick() == [(server.id, "restart", "restart")]
    wait_for(lambda: restarted(ssup, server.id), 20)

    # Players who never leave: it goes ahead after the limit, even past midnight.
    other = start(ssup, store, "late", restart_at="23:30", schedule_players="delay",
                  players=PlayerQuery(source=PlayerSource.A2S, port=query.port))
    query.players = 3
    clock.set("23:30")
    assert sched.tick() == [(other.id, "restart", "waiting")]
    clock.set("01:31", day=1)
    assert sched.tick() == [(other.id, "restart", "restart")]
    assert "still on after 120 min" in console_text(ssup, other.id)


def test_scheduled_update_only_when_the_badge_says_so(store, ssup, runner, steam_settings,
                                                      tmp_path, monkeypatch):
    clock = Clock("05:00")
    audits: list = []
    sched = scheduler(store, ssup, runner, steam_settings, clock, audits)
    server = steam_server(store, tmp_path, update_at="05:00")  # installed build 1500
    monkeypatch.setenv("FAKE_STEAMCMD_LATEST", "1500")
    runner.check(server.id)
    assert runner.wait_idle(30)
    assert sched.tick() == [(server.id, "update", "no update")]

    monkeypatch.setenv("FAKE_STEAMCMD_LATEST", "1501")
    runner.check(server.id)
    assert runner.wait_idle(30)
    clock.set("05:00", day=1)
    assert sched.tick() == [(server.id, "update", "update")]
    assert runner.wait_idle(30)
    run = store.last_steam_run(server.id)
    assert run.kind == Kind.UPDATE.value and run.who == "schedule" and run.result == "ok"
    assert any(a[1] == "schedule.update" for a in audits)


def test_a_cancelled_scheduled_restart_counts_as_done_that_day(store, ssup, runner,
                                                               steam_settings):
    """Cancel during its countdown (batch 4, item 12): it keeps running, and the schedule
    doesn't try again until tomorrow."""
    from basecamp.models import StopAction, StopWarning

    clock = Clock("04:00")
    sched = scheduler(store, ssup, runner, steam_settings, clock)
    warning = StopWarning(action=StopAction.STDIN, command="say {minutes}", minutes=[1])
    server = start(ssup, store, restart_at="04:00", stop_warning=warning, ready_after_s=1)
    assert sched.tick() == [(server.id, "restart", "restart")]
    wait_for(lambda: ssup.counting_down(server.id), 20)
    assert ssup.cancel_countdown(server.id) == "restart"
    wait_for(lambda: "Restart cancelled" in console_text(ssup, server.id))
    with ssup.hold(server.id):  # the restart thread is done
        pass
    assert ssup.state(server.id) is ServerState.RUNNING
    clock.set("04:05")
    assert sched.tick() == []  # done for today
    clock.set("04:00", day=1)
    assert sched.tick() == [(server.id, "restart", "restart")]
