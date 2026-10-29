"""SteamCMD installs and updates (R-11, R-30..R-32) against tools/fake_steamcmd.py."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from basecamp import notify
from basecamp.backups import BackupError, Backups
from basecamp.models import ServerKind, ServerState, StopAction, StopWarning
from basecamp.notify import Notifier
from basecamp.settings import Settings
from basecamp.steam import (
    Kind,
    SteamError,
    SteamRunner,
    branch_build,
    check_command,
    installed_build,
    parse_app_info,
    steam_command,
)
from basecamp.supervisor import Supervisor
from tests.conftest import REPO, console_text, fake_config, stop_everything, wait_for

FAKE_STEAMCMD = REPO / "tools" / "fake_steamcmd.py"
APP_ID = 2394010


@pytest.fixture
def steam_settings(settings: Settings) -> Settings:
    return dataclasses.replace(settings, steamcmd_path=str(FAKE_STEAMCMD))


@pytest.fixture(autouse=True)
def fake_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKE_STEAMCMD_DELAY", "0.02")
    monkeypatch.setenv("FAKE_STEAMCMD_STATE", str(tmp_path / "selfupdated"))
    monkeypatch.delenv("FAKE_STEAMCMD_MODE", raising=False)


@pytest.fixture
def ssup(store, steam_settings) -> Iterator[Supervisor]:
    sup = Supervisor(store, steam_settings)
    yield sup
    stop_everything(sup, store)


@pytest.fixture
def notifier(store, steam_settings, webhook) -> Iterator[Notifier]:
    notify.set_webhook(steam_settings, webhook.url)
    n = Notifier(steam_settings, store)
    yield n
    n.close()


@pytest.fixture
def backups(store, ssup, steam_settings) -> Backups:
    return Backups(store, ssup, steam_settings)


@pytest.fixture
def runner(store, ssup, steam_settings, backups, notifier) -> Iterator[SteamRunner]:
    r = SteamRunner(store, ssup, steam_settings, backups, notifier)
    yield r
    r.close()


def steam_server(store, tmp_path: Path, name: str = "steamy", *, installed: bool = True,
                 **fields):
    install = tmp_path / name
    save = install / "Saved"
    if installed:
        save.mkdir(parents=True)
        (save / "world.sav").write_text("day 1", encoding="utf-8")
        manifest = install / "steamapps" / f"appmanifest_{APP_ID}.acf"
        manifest.parent.mkdir()
        manifest.write_text(f'"AppState"\n{{\n\t"appid"\t\t"{APP_ID}"\n'
                            '\t"buildid"\t\t"1500"\n}\n', encoding="utf-8")
    values = dict(kind=ServerKind.STEAM, steam_app_id=APP_ID, install_dir=str(install),
                  save_dir="Saved")
    values.update(fields)
    return store.add_server(fake_config(name, **values))


def wait_running(sup: Supervisor, server_id: int) -> None:
    wait_for(lambda: sup.state(server_id) is ServerState.RUNNING, timeout=20)


def no_fake_steamcmd_left() -> bool:
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if any("fake_steamcmd" in part for part in proc.info["cmdline"] or ()):
                return False
        except psutil.Error:
            continue
    return True


def test_command_and_build_id(store, steam_settings, tmp_path):
    server = steam_server(store, tmp_path, steam_beta="public-test", steam_validate=True)
    cmd = steam_command(steam_settings, server)
    assert cmd[-10:] == ["+force_install_dir", server.install_dir, "+login", "anonymous",
                         "+app_update", str(APP_ID), "-beta", "public-test", "validate",
                         "+quit"]
    assert cmd.index("+force_install_dir") < cmd.index("+login")
    assert installed_build(server) == "1500"
    other = store.update_server(server.id, server.model_copy(update={"steam_app_id": 99}))
    assert installed_build(other) is None  # only that app's appmanifest counts
    manifest = Path(server.install_dir) / "steamapps" / f"appmanifest_{APP_ID}.acf"
    manifest.write_text(manifest.read_text().replace('"1500"', '"0"'))
    assert installed_build(server) is None  # mid-download, real SteamCMD writes buildid 0


def test_update_backs_up_stops_updates_and_starts_again(store, ssup, runner, backups,
                                                        tmp_path, webhook):
    server = steam_server(store, tmp_path)
    states: list[ServerState] = []
    ssup.add_listener(lambda _id, _old, new: states.append(new))
    ssup.start(server.id)
    wait_running(ssup, server.id)
    states.clear()

    job = runner.enqueue(server.id, Kind.UPDATE, "owner")
    assert runner.wait_idle(30)
    wait_running(ssup, server.id)

    run = store.get_steam_run(job.run_id)
    assert run.result == "ok", run.message
    assert (run.build_before, run.build_after) == ("1500", "1501")
    assert run.was_running and run.started_again
    assert run.backup and [b.name for b in backups.list(server.id)] == [run.backup]
    assert states[:3] == [ServerState.STOPPING, ServerState.UPDATING, ServerState.STOPPED]
    assert ServerState.STARTING in states
    text = console_text(ssup, server.id)
    assert "progress: 43.25" in text and "fully installed" in text
    assert "updated to build 1501" in text
    view = runner.view(store.get_server(server.id))
    assert view["build_id"] == "1501" and not view["failed"] and view["job"] is None
    wait_for(lambda: len(webhook.texts()) >= 2, timeout=10)
    texts = webhook.texts()
    assert any("Updating **steamy**" in t for t in texts)
    assert any("updated to build 1501" in t for t in texts)
    assert not any("stopped" in t for t in texts)  # an update isn't a "stopped" message
    log_text = runner.log_text(server.id, job.run_id)
    assert "+app_update" in log_text and "exit code 0" in log_text


def test_failed_update_leaves_it_stopped_marked_and_keeps_the_backup(
        store, ssup, runner, backups, tmp_path, webhook):
    server = steam_server(store, tmp_path, steam_beta="fail")  # the fake fails on "fail"
    ssup.start(server.id)
    wait_running(ssup, server.id)

    job = runner.enqueue(server.id, Kind.UPDATE, "owner")
    assert runner.wait_idle(30)

    assert ssup.state(server.id) is ServerState.STOPPED
    run = store.get_steam_run(job.run_id)
    assert run.result == "failed" and not run.started_again
    assert "Disk write failure" in run.message and run.backup in run.message
    assert [b.name for b in backups.list(server.id)] == [run.backup]
    view = runner.view(store.get_server(server.id))
    assert view["failed"] and view["last"]["result"] == "failed"
    assert view["build_id"] == "1500"
    wait_for(lambda: any("failed" in t for t in webhook.texts()), timeout=10)


def test_self_update_is_retried_once(store, ssup, runner, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STEAMCMD_MODE", "selfupdate")
    server = steam_server(store, tmp_path)
    job = runner.enqueue(server.id, Kind.UPDATE, "owner")
    assert runner.wait_idle(30)
    run = store.get_steam_run(job.run_id)
    assert run.result == "ok", run.message
    text = console_text(ssup, server.id)
    assert "launching Steamcmd" in text and "updated itself; running it again" in text
    assert ssup.state(server.id) is ServerState.STOPPED  # it wasn't running before


def test_hang_is_killed_after_the_timeout(store, ssup, steam_settings, backups, tmp_path,
                                          monkeypatch):
    monkeypatch.setenv("FAKE_STEAMCMD_MODE", "hang")
    quick = dataclasses.replace(steam_settings, steamcmd_timeout_s=1.5)
    runner = SteamRunner(store, ssup, quick, backups)
    try:
        server = steam_server(store, tmp_path)
        job = runner.enqueue(server.id, Kind.UPDATE, "owner")
        assert runner.wait_idle(30)
        run = store.get_steam_run(job.run_id)
        assert run.result == "failed" and "longer than" in run.message
        wait_for(no_fake_steamcmd_left, timeout=10)
    finally:
        runner.close()


def test_one_at_a_time_queue_and_cancel(store, ssup, runner, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STEAMCMD_MODE", "hang")
    first = steam_server(store, tmp_path, "first")
    second = steam_server(store, tmp_path, "second")
    third = steam_server(store, tmp_path, "third")
    job1 = runner.enqueue(first.id, Kind.UPDATE, "owner")
    job2 = runner.enqueue(second.id, Kind.UPDATE, "owner")
    job3 = runner.enqueue(third.id, Kind.UPDATE, "owner")
    with pytest.raises(SteamError, match="already has"):
        runner.enqueue(first.id, Kind.UPDATE, "owner")

    wait_for(lambda: (j := runner.job(first.id)) is not None and j.progress == 43.25,
             timeout=15)
    assert [j["position"] for j in runner.jobs()] == [0, 1, 2]
    assert runner.busy(first.id) and not runner.busy(second.id)  # queued isn't busy
    assert runner.job(second.id).phase.value == "queued"
    assert runner.cancel(third.id)  # off the queue: nothing changed
    assert store.get_steam_run(job3.run_id).result == "cancelled"

    assert runner.cancel(first.id)  # kills the running SteamCMD
    wait_for(lambda: runner.job(first.id) is None, timeout=15)
    run1 = store.get_steam_run(job1.run_id)
    assert run1.result == "failed" and "half updated" in run1.message
    # The second one runs next (and hangs too); cancel it as well.
    wait_for(lambda: runner.busy(second.id), timeout=15)
    runner.cancel(second.id)
    assert runner.wait_idle(15)
    assert store.get_steam_run(job2.run_id).result in ("failed", "cancelled")
    wait_for(no_fake_steamcmd_left, timeout=10)


def test_first_install_into_an_empty_folder(store, ssup, runner, tmp_path):
    server = steam_server(store, tmp_path, "fresh", installed=False)
    assert runner.view(server)["build_id"] is None
    job = runner.enqueue(server.id, Kind.INSTALL, "owner")
    assert runner.wait_idle(30)
    run = store.get_steam_run(job.run_id)
    assert run.result == "ok", run.message
    assert installed_build(server) == "1000"
    assert ssup.state(server.id) is ServerState.STOPPED  # an install doesn't start it
    assert not run.backup

    with pytest.raises(SteamError, match="isn't empty"):
        runner.enqueue(server.id, Kind.INSTALL, "owner")


def test_install_refused_while_running_and_for_custom_servers(store, ssup, runner, tmp_path):
    server = steam_server(store, tmp_path, "busy", installed=False)
    ssup.start(server.id)
    wait_running(ssup, server.id)
    with pytest.raises(SteamError, match="Stop busy"):
        runner.enqueue(server.id, Kind.INSTALL, "owner")
    custom = store.add_server(fake_config("custom"))
    with pytest.raises(SteamError, match="Steam servers"):
        runner.enqueue(custom.id, Kind.UPDATE, "owner")
    no_folder = steam_server(store, tmp_path, "nofolder", installed=False)
    with pytest.raises(SteamError, match="Use Install"):
        runner.enqueue(no_folder.id, Kind.UPDATE, "owner")


def test_backup_failure_stops_the_update_with_nothing_changed(store, ssup, runner, backups,
                                                              tmp_path, monkeypatch):
    server = steam_server(store, tmp_path)
    ssup.start(server.id)
    wait_running(ssup, server.id)

    def broken(*_args, **_kwargs):
        raise BackupError("can't read world.sav")

    monkeypatch.setattr(backups, "make", broken)
    job = runner.enqueue(server.id, Kind.UPDATE, "owner")
    assert runner.wait_idle(30)
    run = store.get_steam_run(job.run_id)
    assert run.result == "failed" and "nothing was changed" in run.message
    assert ssup.state(server.id) is ServerState.RUNNING  # never stopped
    assert installed_build(server) == "1500"


def test_runs_left_over_from_a_restart_are_marked_interrupted(store, ssup, steam_settings,
                                                              backups, tmp_path):
    server = steam_server(store, tmp_path)
    run_id = store.add_steam_run(server.id, "update", "owner")
    store.update_steam_run(run_id, result="running", started_at=True)
    runner = SteamRunner(store, ssup, steam_settings, backups)
    try:
        assert store.get_steam_run(run_id).result == "interrupted"
        assert runner.view(server)["failed"]
    finally:
        runner.close()


def test_missing_steamcmd_fails_clearly(store, ssup, steam_settings, backups, tmp_path):
    missing = dataclasses.replace(steam_settings, steamcmd_path=str(tmp_path / "nope.exe"))
    runner = SteamRunner(store, ssup, missing, backups)
    try:
        server = steam_server(store, tmp_path)
        assert runner.view(server)["steamcmd_found"] is False
        job = runner.enqueue(server.id, Kind.UPDATE, "owner")
        assert runner.wait_idle(30)
        assert "SteamCMD not found" in store.get_steam_run(job.run_id).message
    finally:
        runner.close()


def test_update_over_the_api(store, steam_settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from basecamp.api.app import create_app
    from tests.test_api import ADMIN, AS_FRIEND, BASE

    monkeypatch.setenv("FAKE_STEAMCMD_DELAY", "0.3")
    web = dataclasses.replace(steam_settings, admin_logins=(ADMIN,))
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    server = steam_server(store, tmp_path)
    custom = store.add_server(fake_config("custom"))
    try:
        with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
            assert c.post(f"/api/servers/{server.id}/update", headers=AS_FRIEND).status_code == 403
            assert c.post(f"/api/servers/{custom.id}/update").status_code == 409
            res = c.post(f"/api/servers/{server.id}/update")
            assert res.status_code == 202, res.text
            run_id = res.json()["run_id"]
            wait_for(lambda: c.get(f"/api/servers/{server.id}").json()["steam"]["job"]
                     and app.state.steam.busy(server.id), timeout=10)
            view = c.get(f"/api/servers/{server.id}").json()
            assert view["steam"]["build_id"] == "1500"
            assert c.post(f"/api/servers/{server.id}/start").status_code == 409  # busy
            assert c.get("/api/steam").json()["jobs"][0]["run_id"] == run_id
            assert app.state.steam.wait_idle(30)

            view = c.get(f"/api/servers/{server.id}").json()["steam"]
            assert view["build_id"] == "1501" and view["last"]["result"] == "ok"
            log_res = c.get(f"/api/servers/{server.id}/steam/runs/{run_id}/log")
            assert log_res.status_code == 200 and "fully installed" in log_res.text
            assert c.get(f"/api/servers/{custom.id}/steam/runs/{run_id}/log").status_code == 404
            assert c.post(f"/api/servers/{server.id}/steam/cancel").status_code == 409
            actions = [e["action"] for e in c.get("/api/audit").json()]
            assert actions.count("update") == 2 and "steam.cancel" in actions
            assert "steam" not in c.get("/api/servers", headers=AS_FRIEND).json()[0]
    finally:
        app.state.steam.close()
        sup.detach()


# -- build checks and the update-available badge (R-34, D-65) ------------------------------


APP_INFO = """AppID : 2394010, change number : 1/0, last change : Mon Oct 26 10:00:00 2026
"2394010"
{
	"common" { "name" "Palworld \\"Dedicated\\" Server" }
	"depots"
	{
		"branches"
		{
			"Public" { "buildid" "20000001" "description" "a {brace} in a string" }
			"beta" { "buildid" "20000002" }
			"broken" { "timeupdated" "1" }
		}
	}
}
"""


def test_parse_app_info():
    info = parse_app_info("Loading Steam API...OK\n" + APP_INFO, APP_ID)
    assert info["common"]["name"] == 'Palworld \\"Dedicated\\" Server'
    assert branch_build(info, "public") == "20000001"
    assert branch_build(info, "beta") == "20000002"
    assert branch_build(info, "broken") is None and branch_build(info, "nope") is None
    assert parse_app_info(APP_INFO, 99) is None and branch_build(None, "public") is None
    assert check_command(Settings.load(Path("x"), steamcmd_path="C:\\s\\steamcmd.exe"),
                         APP_ID) == ["C:\\s\\steamcmd.exe", "+login", "anonymous",
                                     "+app_info_update", "1", "+app_info_print",
                                     str(APP_ID), "+quit"]


def test_check_finds_an_update_and_never_counts_as_busy(store, ssup, runner, tmp_path,
                                                        monkeypatch, steam_settings):
    server = steam_server(store, tmp_path)  # build 1500; the fake says Steam has 1001
    view = runner.view(server)
    assert view["latest"] is None and view["update_available"] is None
    assert runner.check(server.id, "me") is True
    assert runner.check(server.id) is False  # one queued at a time
    assert runner.job(server.id) is None and not runner.busy(server.id)
    assert runner.wait_idle(30)
    view = runner.view(server)
    assert view["latest"]["build_id"] == "1001" and view["latest"]["branch"] == "public"
    assert view["update_available"] is True and not view["checking"]

    monkeypatch.setenv("FAKE_STEAMCMD_LATEST", "1500")
    runner.check(server.id)
    assert runner.wait_idle(30)
    assert runner.view(server)["update_available"] is False
    # Kept across a Basecamp restart.
    again = SteamRunner(store, ssup, steam_settings, Backups(store, ssup, steam_settings))
    try:
        assert again.view(server)["latest"]["build_id"] == "1500"
    finally:
        again.close()


@pytest.mark.parametrize("how", ["branch", "steamcmd fails"])
def test_a_failed_check_is_unknown_not_an_error(store, runner, tmp_path, monkeypatch, how):
    if how == "branch":  # the fake lists public and beta only
        server = steam_server(store, tmp_path, steam_beta="fail")
    else:
        server = steam_server(store, tmp_path)
        monkeypatch.setenv("FAKE_STEAMCMD_MODE", "fail")
    runner.check(server.id)
    assert runner.wait_idle(30)
    view = runner.view(server)
    assert view["latest"]["build_id"] is None and view["latest"]["error"]
    assert view["update_available"] is None
    assert view["last"] is None  # a check isn't an install or update run
    # Another branch or app makes the old answer irrelevant.
    other = store.update_server(server.id, server.model_copy(update={"steam_beta": "beta"}))
    assert runner.view(other)["latest"] is None


def test_check_due_only_for_installed_servers_and_when_old(store, runner, tmp_path,
                                                           steam_settings):
    installed = steam_server(store, tmp_path, "inst")
    steam_server(store, tmp_path, "empty", installed=False)
    store.add_server(fake_config("custom"))
    assert runner.check_due() == [installed.id]
    assert runner.wait_idle(30)
    assert runner.check_due() == []  # checked just now
    object.__setattr__(steam_settings, "steam_check_h", 0.0)
    assert runner.check_due() == []  # 0 = only by hand


def test_check_over_the_api(store, steam_settings, tmp_path):
    from fastapi.testclient import TestClient

    from basecamp.api.app import create_app
    from tests.test_api import ADMIN, AS_FRIEND, BASE

    web = dataclasses.replace(steam_settings, admin_logins=(ADMIN,))
    sup = Supervisor(store, web)
    app = create_app(store, sup, web)
    server = steam_server(store, tmp_path)
    custom = store.add_server(fake_config("custom"))
    try:
        with TestClient(app, base_url=BASE, client=("127.0.0.1", 50123)) as c:
            path = f"/api/servers/{server.id}/steam/check"
            assert c.post(path, headers=AS_FRIEND).status_code == 403
            assert c.post(f"/api/servers/{custom.id}/steam/check").status_code == 409
            assert c.post(path).status_code == 202
            assert app.state.steam.wait_idle(30)
            steam = c.get(f"/api/servers/{server.id}").json()["steam"]
            assert steam["update_available"] is True and steam["latest"]["build_id"] == "1001"
            assert "steam.check" in [e["action"] for e in c.get("/api/audit").json()]
    finally:
        app.state.steam.close()
        sup.detach()


def test_cancelling_an_update_countdown_leaves_it_running(store, ssup, runner, tmp_path):
    """Cancel during the update's stop warning (batch 4, item 12): the run ends cancelled
    with nothing changed, and the server keeps running."""
    warning = StopWarning(action=StopAction.STDIN, command="say update in {minutes}",
                          minutes=[1])
    server = steam_server(store, tmp_path, stop_warning=warning, ready_after_s=1)
    ssup.start(server.id)
    wait_running(ssup, server.id)
    pid = ssup.status(server.id).pid
    runner.enqueue(server.id, Kind.UPDATE, "pal@example.com")
    wait_for(lambda: ssup.counting_down(server.id), timeout=20)
    assert ssup.cancel_countdown(server.id) == "update"
    assert runner.wait_idle(30)
    run = store.last_steam_run(server.id)
    assert run.result == "cancelled" and "during the warning" in run.message
    assert ssup.state(server.id) is ServerState.RUNNING and ssup.status(server.id).pid == pid
    assert runner.job(server.id) is None


def test_update_warns_players_and_cancel_during_the_warning(store, ssup, runner, tmp_path):
    warning = StopWarning(action=StopAction.STDIN, command="say update in {minutes}",
                          minutes=[1])
    server = steam_server(store, tmp_path, stop_warning=warning, ready_after_s=1)
    ssup.start(server.id)
    wait_running(ssup, server.id)
    runner.enqueue(server.id, Kind.UPDATE, "me")
    wait_for(lambda: ssup.counting_down(server.id), timeout=20)
    assert runner.view(server)["job"]["phase"] == "warning"
    assert ssup.status(server.id).warning["kind"] == "update"
    wait_for(lambda: "say update in 1" in console_text(ssup, server.id))
    assert runner.cancel(server.id)
    assert runner.wait_idle(30)
    assert ssup.state(server.id) is ServerState.RUNNING  # left running, nothing changed
    assert store.last_steam_run(server.id).result == "cancelled"

    runner.enqueue(server.id, Kind.UPDATE, "me")
    wait_for(lambda: ssup.counting_down(server.id), timeout=20)
    assert ssup.stop_now(server.id)  # skip the rest: the update goes ahead
    assert runner.wait_idle(60)
    assert store.last_steam_run(server.id).result == "ok"
    wait_running(ssup, server.id)
