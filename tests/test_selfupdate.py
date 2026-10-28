"""Phase 6: self-update from (fake) GitHub Releases (R-90..R-94, docs/07-updates.md).

A throwaway Ed25519 key is made per test session; the real release key is never used."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import threading
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from basecamp import selfupdate
from basecamp.api.app import create_app
from basecamp.selfupdate import SelfUpdater, UpdateError, check_tree, parse_version
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor
from basecamp.templates import load_templates
from launcher import LAUNCHER_VERSION
from tests.conftest import REPO, FakeWebhook, wait_for

_spec = importlib.util.spec_from_file_location("build_release",
                                               REPO / "scripts" / "build_release.py")
build_release = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(build_release)  # type: ignore[union-attr]
_fake_spec = importlib.util.spec_from_file_location("fake_releases",
                                                    REPO / "tools" / "fake_releases.py")
fake_releases = importlib.util.module_from_spec(_fake_spec)  # type: ignore[arg-type]
_fake_spec.loader.exec_module(fake_releases)  # type: ignore[union-attr]


@pytest.fixture(scope="session")
def keys(tmp_path_factory) -> tuple[Path, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    path = tmp_path_factory.mktemp("signing") / "test.key"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return path, key


@pytest.fixture(scope="session")
def lib(tmp_path_factory) -> Path:
    """Stands in for the vendored dependencies (pip isn't run in tests)."""
    path = tmp_path_factory.mktemp("lib")
    (path / "somedep").mkdir()
    (path / "somedep" / "__init__.py").write_text("X = 1\n")
    return path


def make_src(tmp: Path, version: str, migration: str | None = None) -> Path:
    src = tmp / f"src-{version}"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(REPO / "basecamp", src / "basecamp", ignore=ignore)
    shutil.copytree(REPO / "migrations", src / "migrations")
    shutil.copytree(REPO / "templates", src / "templates")
    shutil.copy2(REPO / "launcher.py", src / "launcher.py")
    shutil.copy2(REPO / "pyproject.toml", src / "pyproject.toml")
    init = src / "basecamp" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"',
                           init.read_text()))
    if migration is not None:
        (src / "migrations" / "0099_test.sql").write_text(migration)
    return src


class Releases:
    """A fake GitHub serving tools/fake_releases.py's folder."""

    def __init__(self, folder: Path, keys, lib: Path, tmp: Path) -> None:
        self.folder = folder
        self.keys, self.lib, self.tmp = keys, lib, tmp
        self.server = fake_releases.serve(folder)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.list: list[dict] = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, version: str, *, prerelease: bool = False, migration: str | None = None,
                bad_sig: bool = False, **build: object) -> Path:
        src = make_src(self.tmp, version, migration)
        zip_path = build_release.build(version, self.keys[0], self.folder, src, self.lib,
                                       test_key=True, **build)
        if bad_sig:
            sig = zip_path.with_name(zip_path.name + ".sig")
            sig.write_bytes(Ed25519PrivateKey.generate().sign(zip_path.read_bytes()))
        self.list.append({"tag_name": f"v{version}", "body": f"Notes for {version}",
                          "prerelease": prerelease,
                          "assets": [zip_path.name, zip_path.name + ".sig"]})
        (self.folder / "releases.json").write_text(json.dumps(self.list))
        return zip_path

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def releases(tmp_path, keys, lib) -> Iterator[Releases]:
    folder = tmp_path / "releases"
    folder.mkdir()
    r = Releases(folder, keys, lib, tmp_path)
    yield r
    r.close()


@pytest.fixture
def up_settings(settings: Settings, releases: Releases) -> Settings:
    return dataclasses.replace(settings, update_api=releases.url, update_repo="o/r")


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "root"
    (path / "versions").mkdir(parents=True)
    return path


def updater(settings, store, keys, root, **kw) -> SelfUpdater:
    kw.setdefault("launcher", LAUNCHER_VERSION)
    return SelfUpdater(settings, store, kw.pop("notifier", None), kw.pop("stop", None),
                       root=root, public_key=keys[1].public_key(), current="0.1.0", **kw)


# -- pieces ------------------------------------------------------------------------------


def test_versions_sort():
    order = ["0.1.0", "0.2.0-beta.1", "0.2.0-beta.2", "0.2.0", "0.10.0", "1.0.0"]
    assert sorted(order, key=parse_version) == order
    assert parse_version("v1.2.3") == parse_version("1.2.3")
    assert selfupdate.is_newer("0.2.0", "0.1.0")
    assert not selfupdate.is_newer("0.1.0", "0.1.0")
    assert not selfupdate.is_newer("junk", "0.1.0")
    with pytest.raises(ValueError):
        parse_version("1.2")


def test_build_makes_a_signed_zip_with_a_manifest(tmp_path, keys, lib):
    zip_path = build_release.build("0.2.0", keys[0], tmp_path / "out",
                                   make_src(tmp_path, "0.2.0"), lib, test_key=True)
    data = zip_path.read_bytes()
    sig = zip_path.with_name(zip_path.name + ".sig").read_bytes()
    selfupdate.verify_signature(data, sig, keys[1].public_key())
    with pytest.raises(UpdateError, match="signature"):
        selfupdate.verify_signature(data + b"x", sig, keys[1].public_key())
    with pytest.raises(UpdateError, match="signature"):
        selfupdate.verify_signature(data, sig, Ed25519PrivateKey.generate().public_key())
    out = tmp_path / "unpacked"
    selfupdate.safe_extract(zip_path, out)
    manifest = check_tree(out, "0.2.0")
    assert manifest["min_launcher"] == build_release.MIN_LAUNCHER == 2
    assert manifest["python"] == "3.13"
    assert "basecamp/selfupdate.py" in manifest["files"]
    assert "lib/somedep/__init__.py" in manifest["files"]
    assert "migrations/0001_init.sql" in manifest["files"]
    assert "launcher.py" in manifest["files"]
    assert not any("__pycache__" in name for name in manifest["files"])
    # The test build ships the test key's public key.
    shipped = selfupdate.load_public_key(out / "basecamp" / "release_key.pub")
    assert selfupdate.public_key_text(shipped) == selfupdate.public_key_text(keys[1].public_key())
    # Running it writes bytecode; that doesn't count as an extra file.
    (out / "basecamp" / "__pycache__").mkdir()
    (out / "basecamp" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"")
    check_tree(out, "0.2.0")


def test_build_of_the_repo_ships_every_built_in_template(tmp_path, keys, lib):
    version = build_release.source_version(REPO)
    zip_path = build_release.build(version, keys[0], tmp_path / "out", REPO, lib,
                                   test_key=True)
    out = tmp_path / "unpacked"
    selfupdate.safe_extract(zip_path, out)
    manifest = check_tree(out, version)
    repo_templates = sorted(p.name for p in (REPO / "templates").glob("*.json"))
    assert repo_templates
    assert sorted(name.removeprefix("templates/") for name in manifest["files"]
                  if name.startswith("templates/")) == repo_templates
    for name in repo_templates:
        assert (out / "templates" / name).read_bytes() == \
            (REPO / "templates" / name).read_bytes()
    # The installed copy finds them where settings.TEMPLATES_DIR points: next to basecamp\.
    loaded = load_templates(out / "templates", tmp_path / "no-data-templates")
    assert loaded.errors == []
    assert {f"{t}.json" for t in loaded.templates} == set(repo_templates)


def test_build_refuses_a_key_that_isnt_the_repos(tmp_path, keys, lib):
    src = make_src(tmp_path, "0.2.0")
    other = Ed25519PrivateKey.generate().public_key()
    (src / "basecamp" / "release_key.pub").write_text(selfupdate.public_key_text(other))
    with pytest.raises(SystemExit, match="doesn't match"):
        build_release.build("0.2.0", keys[0], tmp_path / "out", src, lib)
    with pytest.raises(SystemExit, match=r"says 0\.2\.0"):
        build_release.build("0.3.0", keys[0], tmp_path / "out", src, lib, test_key=True)


def test_check_tree_catches_changed_missing_and_extra_files(tmp_path, keys, lib):
    zip_path = build_release.build("0.2.0", keys[0], tmp_path / "out",
                                   make_src(tmp_path, "0.2.0"), lib, test_key=True)
    for change, message in (
        (lambda d: (d / "basecamp" / "main.py").write_text("evil"), "changed"),
        (lambda d: (d / "basecamp" / "main.py").unlink(), "missing"),
        (lambda d: (d / "basecamp" / "extra.py").write_text(""), "not in the manifest"),
    ):
        out = tmp_path / f"u{message}"
        selfupdate.safe_extract(zip_path, out)
        change(out)
        with pytest.raises(UpdateError, match=message):
            check_tree(out, "0.2.0")
    out = tmp_path / "wrongversion"
    selfupdate.safe_extract(zip_path, out)
    with pytest.raises(UpdateError, match=r"manifest\.json says"):
        check_tree(out, "0.3.0")


@pytest.mark.parametrize("name", ["../evil.py", "/abs.py", "C:/x.py", "a\\..\\..\\b.py"])
def test_safe_extract_refuses_paths_outside(tmp_path, name):
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ok.txt", "fine")
        zf.writestr(name, "evil")
    with pytest.raises(UpdateError, match="bad path"):
        selfupdate.safe_extract(zip_path, tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_backup_db_is_a_consistent_copy_and_keeps_five(tmp_path, store, settings):
    store.add_audit("me", "something")
    dest = tmp_path / "db-backups"
    for i in range(7):
        copy = selfupdate.backup_db(settings.db_path, dest, f"before-0.{i}.0")
        os.utime(copy, (1000 + i, 1000 + i))
    names = sorted(p.name for p in dest.glob("*.db"))
    assert len(names) == 5 and "before-0.0.0.db" not in names
    conn = sqlite3.connect(dest / "before-0.6.0.db")
    assert conn.execute("SELECT action FROM audit_log").fetchall() == [("something",)]
    conn.close()


def test_failing_migrations_are_found_on_a_copy(tmp_path, store, settings):
    migrations = tmp_path / "migrations"
    shutil.copytree(settings.migrations_dir, migrations)
    (migrations / "0099_bad.sql").write_text("ALTER TABLE nope ADD COLUMN x INTEGER;")
    with pytest.raises(UpdateError, match="migrations fail"):
        selfupdate.try_migrations(settings.db_path, migrations)
    (migrations / "0099_bad.sql").write_text("CREATE TABLE fine (x INTEGER);")
    assert selfupdate.try_migrations(settings.db_path, migrations) == [99]
    # The real DB is untouched.
    assert not store._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'fine'").fetchone()


def test_downloads_only_over_https_or_to_this_pc():
    for url in ("http://example.com/x", "ftp://127.0.0.1/x", "file:///C:/x"):
        with pytest.raises(UpdateError, match="https only"):
            selfupdate._check_url(url)
    for url in ("https://api.github.com/x", "http://127.0.0.1:1/x", "http://localhost/x"):
        assert selfupdate._check_url(url) == url


# -- the service -------------------------------------------------------------------------


def test_finds_downloads_and_stages_a_newer_release(up_settings, store, keys, root, releases,
                                                     webhook: FakeWebhook):
    from basecamp.notify import Notifier, set_webhook

    set_webhook(up_settings, webhook.url)
    notifier = Notifier(up_settings, store)
    u = updater(up_settings, store, keys, root, notifier=notifier)
    assert u.check()["status"] == "idle"  # no releases yet (404)
    releases.publish("0.1.0")
    view = u.check()
    assert view["status"] == "idle" and not view["available"]
    releases.publish("0.2.0")
    view = u.check()
    assert view["status"] == "ready", view["error"]
    assert view["ready_version"] == "0.2.0" and view["latest"]["notes"] == "Notes for 0.2.0"
    folder = root / "versions" / "0.2.0"
    assert (folder / "basecamp" / "__init__.py").is_file()
    assert not list((root / "versions").glob("*.tmp"))
    check_tree(folder, "0.2.0")
    wait_for(lambda: webhook.texts())
    assert "0.2.0" in webhook.texts()[0] and "is out" in webhook.texts()[0]
    # Checking again: no second download, no second Discord message.
    marker = folder / "marker"
    marker.write_text("x")
    assert u.check()["status"] == "ready"
    assert marker.exists()
    # A new SelfUpdater (Basecamp restarted) remembers it's ready.
    again = updater(up_settings, store, keys, root)
    assert again.view()["ready"]
    notifier.close()
    assert len(webhook.texts()) == 1


def test_repeat_checks_use_the_etag(up_settings, store, keys, root, releases, monkeypatch):
    releases.publish("0.1.0")
    sent = []
    real = selfupdate._request

    def spy(url, accept, etag=""):
        sent.append(etag)
        return real(url, accept, etag)

    monkeypatch.setattr(selfupdate, "_request", spy)
    u = updater(up_settings, store, keys, root)
    u.check()
    u.check()
    assert sent[0] == "" and sent[1].startswith('"')
    assert u.view()["latest"]["version"] == "0.1.0"  # kept from the first answer (304)


def test_pre_releases_only_on_the_beta_channel(up_settings, store, keys, root, releases):
    releases.publish("0.2.0-beta.1", prerelease=True)
    u = updater(up_settings, store, keys, root)
    assert u.check()["status"] == "idle"
    beta = dataclasses.replace(up_settings, update_beta=True)
    view = updater(beta, store, keys, root).check()
    assert view["ready_version"] == "0.2.0-beta.1", view["error"]


def test_a_bad_signature_is_refused_and_nothing_is_unpacked(up_settings, store, keys, root,
                                                            releases):
    releases.publish("0.2.0", bad_sig=True)
    view = updater(up_settings, store, keys, root).check()
    assert view["status"] == "failed" and "signature" in view["error"]
    assert view["available"] and not view["ready"]
    assert list((root / "versions").iterdir()) == []


def test_no_release_key_means_no_update(up_settings, store, keys, root, releases):
    releases.publish("0.2.0")
    u = SelfUpdater(up_settings, store, root=root, launcher=LAUNCHER_VERSION, current="0.1.0",
                    public_key=None)
    u._key = None
    view = u.check()
    assert view["status"] == "failed" and "no release key" in view["error"]


def test_launcher_1_is_told_to_reinstall(up_settings, store, keys, root, releases):
    # Releases need launcher 2, the one for the isolated runtime (D-123): a 0.2.0 install
    # can't update itself into it.
    releases.publish("0.2.0")
    view = updater(up_settings, store, keys, root, launcher=1).check()
    assert "needs launcher 2" in view["error"] and "manual reinstall" in view["error"]


def test_refuses_a_newer_launcher_other_python_or_bad_migrations(up_settings, store, keys,
                                                                 root, releases):
    releases.publish("0.2.0", min_launcher=LAUNCHER_VERSION + 1)
    view = updater(up_settings, store, keys, root).check()
    assert f"needs launcher {LAUNCHER_VERSION + 1}" in view["error"]
    releases.publish("0.3.0", python="3.99")
    assert "Python 3.99" in updater(up_settings, store, keys, root).check()["error"]
    releases.publish("0.4.0", migration="DROP TABLE nope;")
    assert "migrations fail" in updater(up_settings, store, keys, root).check()["error"]
    assert list((root / "versions").iterdir()) == []


def test_install_hands_over_to_the_launcher(up_settings, store, keys, root, releases):
    releases.publish("0.2.0")
    stop = threading.Event()
    u = updater(up_settings, store, keys, root, stop=stop)
    with pytest.raises(UpdateError, match="No update"):
        u.install("me")
    u.check()
    assert u.install("me") == "0.2.0"
    assert stop.is_set() and u.exit_code == selfupdate.EXIT_UPDATE
    with pytest.raises(UpdateError, match="Already"):
        u.install("me")
    store.add_audit("me", "last thing before exit")
    assert u.finish_exit() == selfupdate.EXIT_UPDATE
    assert (root / "pending.txt").read_text() == "0.2.0"
    copy = up_settings.data_dir / "db-backups" / "before-0.2.0.db"
    conn = sqlite3.connect(copy)
    assert ("last thing before exit",) in conn.execute("SELECT action FROM audit_log")
    conn.close()


def test_install_needs_the_launcher(up_settings, store, keys, root, releases):
    releases.publish("0.2.0")
    u = SelfUpdater(up_settings, store, root=None, launcher=None, current="0.1.0",
                    public_key=keys[1].public_key())
    assert u.check()["ready"]  # staged in <data>\updates
    assert (up_settings.data_dir / "updates" / "0.2.0").is_dir()
    assert not u.view()["can_install"]
    with pytest.raises(UpdateError, match="launcher"):
        u.install("me")


def test_failed_db_backup_cancels_the_update(up_settings, store, keys, root, releases,
                                             monkeypatch):
    releases.publish("0.2.0")
    u = updater(up_settings, store, keys, root)
    u.check()
    u.install("me")

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(selfupdate, "backup_db", broken)
    assert u.finish_exit() == 0
    assert not (root / "pending.txt").exists()
    assert "database backup failed" in updater(up_settings, store, keys, root).view()[
        "install_error"]


def test_reports_the_update_and_a_rollback(up_settings, store, keys, root, releases,
                                           webhook: FakeWebhook):
    from basecamp.notify import Notifier, set_webhook

    set_webhook(up_settings, webhook.url)
    releases.publish("0.2.0")
    u = updater(up_settings, store, keys, root)
    u.check()
    u.install("me")
    u.finish_exit()
    # The launcher started 0.2.0: it says so once.
    notifier = Notifier(up_settings, store)
    new = SelfUpdater(up_settings, store, notifier, root=root, launcher=LAUNCHER_VERSION,
                      current="0.2.0",
                      public_key=keys[1].public_key())
    (root / "trial.txt").write_text("0.2.0")  # health check not passed yet
    new.report_launcher_results()
    assert new.view()["last_update"] is None
    (root / "trial.txt").unlink()  # the launcher kept it
    new._confirm_update()
    view = new.view()
    assert view["last_update"]["from"] == "0.1.0" and not view["ready"]
    wait_for(lambda: any("updated from 0.1.0" in t for t in webhook.texts()))
    # Next: 0.3.0 fails its health check and the launcher rolls back to 0.2.0.
    (root / "update-failed.json").write_text(json.dumps(
        {"id": "x1", "from": "0.2.0", "to": "0.3.0", "reason": "it exited with code 1",
         "db_restored": True}))
    new._state["installing"] = {"from": "0.2.0", "to": "0.3.0"}
    new.report_launcher_results()
    view = new.view()
    assert view["failure"]["to"] == "0.3.0" and view["failed_versions"] == ["0.3.0"]
    assert not view["install_error"]
    wait_for(lambda: any("rolled back" in t for t in webhook.texts()))
    new.report_launcher_results()  # posted once only
    new.dismiss()
    assert new.view()["failure"] is None
    notifier.close()
    assert sum("rolled back" in t for t in webhook.texts()) == 1


def test_auto_install_at_its_hour_but_never_a_version_that_rolled_back(
        up_settings, store, keys, root, releases):
    releases.publish("0.2.0")
    off = updater(up_settings, store, keys, root)
    off.check()
    assert not off.maybe_auto_install(datetime(2026, 1, 1, 4))  # off by default
    at4 = dataclasses.replace(up_settings, update_auto_hour=4)
    u = updater(at4, store, keys, root)
    assert not u.maybe_auto_install(datetime(2026, 1, 1, 3))
    u._state["failed_versions"] = ["0.2.0"]
    assert not u.maybe_auto_install(datetime(2026, 1, 1, 4))
    u._state["failed_versions"] = []
    assert u.maybe_auto_install(datetime(2026, 1, 1, 4))
    assert u.exit_code == selfupdate.EXIT_UPDATE
    assert store.list_audit()[0].action == "update.install"


# -- the API -------------------------------------------------------------------------------


def test_update_api_is_admin_only_and_audited(up_settings, store, keys, root, releases):
    releases.publish("0.2.0")
    u = updater(up_settings, store, keys, root)
    sup = Supervisor(store, up_settings)
    app = create_app(store, sup, up_settings, updater=u)
    friend = {"x-forwarded-for": "100.64.0.7", "host": "box.example.ts.net",
              "tailscale-user-login": "pal@example.com"}
    with TestClient(app, base_url="http://127.0.0.1:8150", client=("127.0.0.1", 5)) as c:
        for method, path in (("GET", "/api/update"), ("POST", "/api/update/check"),
                             ("POST", "/api/update/install"), ("POST", "/api/update/dismiss")):
            assert c.request(method, path, headers=friend).status_code == 403
        assert c.post("/api/update/install").status_code == 409  # nothing ready yet
        assert c.post("/api/update/check").status_code == 202
        wait_for(lambda: c.get("/api/update").json()["status"] == "ready")
        assert c.get("/api/settings").json()["update"]["ready_version"] == "0.2.0"
        res = c.put("/api/settings", json={"update_beta": True, "update_auto_hour": 5})
        assert res.status_code == 200 and res.json()["update_auto_hour"] == 5
        assert c.put("/api/settings", json={"update_auto_hour": 24}).status_code == 422
        assert c.post("/api/update/install").status_code == 202
        assert u.exit_code == selfupdate.EXIT_UPDATE
    sup.detach()
    actions = [e.action for e in store.list_audit()]
    assert {"update.check", "update.refused", "update.install", "settings"} <= set(actions)
