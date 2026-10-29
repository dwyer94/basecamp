from __future__ import annotations

import shutil

import pytest
from pydantic import ValidationError

from basecamp import db
from basecamp.models import (
    LogSource,
    Port,
    Protocol,
    ServerConfig,
    ServerKind,
    StopAction,
    StopStep,
)
from basecamp.settings import MIGRATIONS_DIR, Settings


def full_config(**overrides) -> ServerConfig:
    values = dict(
        name="Palworld",
        kind=ServerKind.STEAM,
        install_dir=r"C:\GameServers\Palworld",
        exe_path=r"C:\GameServers\Palworld\PalServer.exe",
        args="-useperfthreads -NoAsyncLoadingThread",
        ports=[Port(port=8211, protocol=Protocol.UDP, label="game"),
               Port(port=25575, protocol=Protocol.TCP, label="rcon")],
        join_port=8211,
        start_on_boot=True,
        steam_app_id=2394010,
        config_files=[r"Pal\Saved\Config\WindowsServer\PalWorldSettings.ini"],
        save_dir=r"Pal\Saved\SaveGames",
        rcon_port=25575,
        real_exe_name="PalServer-Win64-Shipping-Cmd.exe",
        ready_pattern="Running Palworld dedicated server",
        stop_recipe=[StopStep(action=StopAction.RCON, command="Save", wait_s=10),
                     StopStep(action=StopAction.RCON, command="Shutdown 1", wait_s=60),
                     StopStep(action=StopAction.CTRL_BREAK, wait_s=30)],
    )
    values.update(overrides)
    return ServerConfig(**values)


def test_round_trip(store):
    config = full_config()

    server = store.add_server(config)

    assert server.id > 0
    assert server.model_dump(exclude={"id", "created_at", "updated_at"}) == config.model_dump()
    assert store.list_servers() == [server]


def test_update_replaces_ports_and_recipe(store):
    server = store.add_server(full_config())

    updated = store.update_server(server.id, full_config(
        ports=[Port(port=9000)], stop_recipe=[], start_on_boot=False))

    assert updated.ports == [Port(port=9000)]
    assert updated.stop_recipe == []
    assert updated.effective_stop_recipe()[0].action is StopAction.CTRL_BREAK
    assert not updated.start_on_boot


def test_delete_cascades(store):
    server = store.add_server(full_config())
    store.delete_server(server.id)

    with pytest.raises(KeyError):
        store.get_server(server.id)
    with pytest.raises(KeyError):
        store.delete_server(server.id)


def test_names_are_unique(store):
    store.add_server(full_config())
    with pytest.raises(Exception, match="UNIQUE"):
        store.add_server(full_config())
    assert len(store.list_servers()) == 1  # the failed insert left nothing behind


def test_validation():
    with pytest.raises(ValidationError):
        StopStep(action=StopAction.STDIN)  # needs a command
    with pytest.raises(ValidationError):
        full_config(log_source=LogSource.FILE, log_file="")
    with pytest.raises(ValidationError):
        full_config(kind="ftp")


def test_migrations_are_applied_once_and_new_ones_later(tmp_path):
    migrations = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, migrations)
    conn = db.connect(tmp_path / "x.db")

    assert db.migrate(conn, migrations) == [1, 2, 3, 4, 5, 6, 7]
    assert db.migrate(conn, migrations) == []

    (migrations / "0099_add_column.sql").write_text(
        "-- example\nALTER TABLE servers ADD COLUMN color TEXT NOT NULL DEFAULT 'blue';\n")
    assert db.migrate(conn, migrations) == [99]
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(servers)")]
    assert "color" in cols

    # An older Basecamp (rolled back) still opens the newer DB.
    (migrations / "0099_add_column.sql").unlink()
    assert db.migrate(conn, migrations) == []
    conn.close()


def test_failed_migration_rolls_back(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_bad.sql").write_text(
        "CREATE TABLE a (x INTEGER);\nTHIS IS NOT SQL;\n")
    conn = db.connect(tmp_path / "x.db")

    with pytest.raises(Exception, match="syntax"):
        db.migrate(conn, migrations)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" not in tables
    conn.close()


def test_bad_migration_name(tmp_path):
    (tmp_path / "init.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError):
        db.list_migrations(tmp_path)


def test_settings_toml_overrides(tmp_path):
    (tmp_path / "settings.toml").write_text("[basecamp]\nboot_gap_s = 3\nport = 8151\n")

    settings = Settings.load(tmp_path)

    assert settings.boot_gap_s == 3 and settings.port == 8151
    assert settings.db_path == tmp_path.resolve() / "basecamp.db"
    with pytest.raises(ValueError, match="nope"):
        Settings.load(tmp_path, nope=1)
