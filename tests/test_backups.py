"""Save backups (R-50..R-53): make, list, prune, restore with a safety backup, schedule."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from basecamp.backups import BackupError, Backups, check_name, save_folder
from basecamp.models import NotifyEvent, ServerState
from tests.conftest import fake_config, wait_for


def make_save(folder: Path, world: str = "day 1") -> Path:
    (folder / "world").mkdir(parents=True, exist_ok=True)
    (folder / "world" / "level.sav").write_text(world, encoding="utf-8")
    (folder / "players.json").write_text('{"players": 2}', encoding="utf-8")
    return folder


@pytest.fixture
def saves(tmp_path: Path) -> Path:
    return make_save(tmp_path / "game" / "Saved")


@pytest.fixture
def backups(store, sup, settings) -> Backups:
    return Backups(store, sup, settings)


def test_backup_restore_round_trip_with_safety_backup(store, sup, backups, saves, settings):
    server = store.add_server(fake_config("alpha", save_dir=str(saves)))

    first = backups.make(server, "manual")
    assert first.files == 2 and first.reason == "manual"
    zipped = settings.server_backup_dir(server.id) / first.name
    with zipfile.ZipFile(zipped) as zf:
        assert sorted(zf.namelist()) == ["players.json", "world/level.sav"]
    assert not list(settings.server_backup_dir(server.id).glob("*.tmp"))

    (saves / "world" / "level.sav").write_text("day 9, everything burned", encoding="utf-8")
    (saves / "junk.tmp").write_text("x", encoding="utf-8")

    safety = backups.restore(server, first.name)

    assert (saves / "world" / "level.sav").read_text(encoding="utf-8") == "day 1"
    assert not (saves / "junk.tmp").exists()  # a restore is the backup, not a merge
    assert safety is not None and safety.reason == "pre-restore"
    assert [b.name for b in backups.list(server.id)] == [safety.name, first.name]
    # Nothing left over next to the save folder.
    assert sorted(p.name for p in saves.parent.iterdir()) == ["Saved"]
    # The safety backup has the burned world, so the restore can be undone.
    backups.restore(server, safety.name)
    assert "burned" in (saves / "world" / "level.sav").read_text(encoding="utf-8")
    assert "Restored" in "\n".join(x.text for x in sup.console(server.id).lines())


def test_restore_needs_the_server_stopped(store, sup, backups, saves):
    server = store.add_server(fake_config("bravo", save_dir=str(saves)))
    backup = backups.make(server, "manual")  # a running server can be backed up
    sup.start(server.id)
    wait_for(lambda: sup.state(server.id) is ServerState.RUNNING)
    backups.make(server, "manual")

    with pytest.raises(BackupError, match="Stop bravo"):
        backups.restore(server, backup.name)

    sup.stop(server.id)
    backups.restore(server, backup.name)


def test_retention_keeps_the_newest(store, backups, saves, settings):
    server = store.add_server(fake_config("charlie", save_dir=str(saves), backup_keep=3))
    made = [backups.make(server, "manual").name for _ in range(5)]
    assert len(set(made)) == 5  # same second: -2, -3... suffixes

    removed = backups.prune(server)

    assert len(removed) == 2
    kept = [b.name for b in backups.list(server.id)]
    assert len(kept) == 3 and set(kept) | set(removed) == set(made)
    assert len(list(settings.server_backup_dir(server.id).iterdir())) == 3


def test_missing_or_unset_save_folder_is_a_clear_error(store, backups, tmp_path):
    unset = store.add_server(fake_config("delta"))
    with pytest.raises(BackupError, match="No save folder"):
        backups.make(unset, "manual")
    missing = store.add_server(fake_config("echo", save_dir=str(tmp_path / "nope")))
    with pytest.raises(BackupError, match="not found"):
        backups.make(missing, "manual")


def test_relative_save_dir_is_under_the_install_folder(tmp_path):
    config = fake_config("foxtrot", install_dir=str(tmp_path / "game"), save_dir="Saved")
    assert save_folder(config) == tmp_path / "game" / "Saved"  # type: ignore[arg-type]


def test_backup_names_cannot_reach_outside_the_folder(store, backups, saves):
    server = store.add_server(fake_config("golf", save_dir=str(saves)))
    for bad in ("..\\..\\basecamp.db", "../x.zip", "C:\\x.zip", "20260101-000000-a.zip\\..",
                "notes.txt"):
        with pytest.raises(BackupError):
            check_name(bad)
        with pytest.raises(BackupError):
            backups.restore(server, bad)


def test_zip_with_unsafe_paths_is_refused(store, backups, saves, settings):
    server = store.add_server(fake_config("hotel", save_dir=str(saves)))
    folder = settings.server_backup_dir(server.id)
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "20260101-000000-manual.zip", "w") as zf:
        zf.writestr("../../evil.txt", "x")

    with pytest.raises(BackupError, match="damaged"):
        backups.restore(server, "20260101-000000-manual.zip")

    assert not (saves.parent.parent / "evil.txt").exists()
    assert (saves / "world" / "level.sav").read_text(encoding="utf-8") == "day 1"


def test_schedule_backs_up_running_servers_and_reports_failures(store, sup, settings, saves,
                                                                 tmp_path):
    reports = []
    backups = Backups(store, sup, settings, lambda s, ev, text: reports.append((s.id, ev, text)))
    every = 0.5 / 3600  # half a second
    good = store.add_server(fake_config("india", save_dir=str(saves), backup_every_h=every,
                                        backup_keep=2))
    broken = store.add_server(fake_config("juliet", save_dir=str(tmp_path / "gone"),
                                          backup_every_h=every))
    off = store.add_server(fake_config("kilo", save_dir=str(saves)))

    assert backups.run_due() == []  # nothing running yet
    for server in (good, broken, off):
        sup.start(server.id)
    wait_for(lambda: all(sup.state(s.id) is ServerState.RUNNING for s in (good, broken, off)))

    assert backups.run_due() == [good.id]
    assert backups.run_due() == []  # not due again yet
    assert [(sid, ev) for sid, ev, _ in reports] == [(broken.id, NotifyEvent.BACKUP_FAILED)]
    time.sleep(0.6)
    backups.run_due()
    backups.run_due()
    time.sleep(0.6)
    backups.run_due()
    assert len(backups.list(good.id)) == 2  # pruned to backup_keep
    assert backups.list(off.id) == []
    assert len(reports) == 3  # one per interval, not one per check
