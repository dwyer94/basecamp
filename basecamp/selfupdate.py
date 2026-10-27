"""Updating Basecamp itself from GitHub Releases (R-90..R-94, docs/07-updates.md).

The repo is public (D-15), so checks and downloads need no token (R-94). Every
``update_check_h`` hours (and on "Check now") Basecamp asks for the latest release, using the
ETag so a repeat check costs nothing. Pre-releases count only when the beta channel is on.

A newer release is downloaded and checked straight away, so Install is quick:

1. ``basecamp-<v>.zip.sig`` must be a valid Ed25519 signature of the zip by the release key
   (``basecamp/release_key.pub``; the private key never leaves the dev PC).
2. The zip is unpacked to ``<root>\\versions\\<v>.tmp\\``; its ``manifest.json`` must name the
   same version, a launcher and Python this PC has, and the SHA-256 of every file, with
   nothing missing or extra.
3. Its migrations are run against a copy of the DB, so a release whose migrations fail is
   refused before anything changes.

Then the folder is renamed to ``versions\\<v>\\``, the dashboard shows a banner with the release
notes and Discord gets one message. **Install** (or the auto-install hour, off by default)
makes Basecamp exit; on the way out it copies the DB to ``<data>\\db-backups\\before-<v>.db``,
writes ``pending.txt`` and exits with code 3. The launcher (``launcher.py``) does the swap,
the health check and, if it fails, the rollback. Game servers keep running throughout
(R-93): they live under their own host processes, and the new version adopts them.

``<root>`` is the launcher's folder, passed in ``BASECAMP_ROOT``. Without it (a dev run with
``python -m basecamp``) updates are still checked and downloaded to ``<data>\\updates\\``, but
Install is refused.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from basecamp import __version__, db
from basecamp.db import Store
from basecamp.settings import Settings

log = logging.getLogger(__name__)

EXIT_UPDATE = 3  # tells the launcher: pending.txt names the version to start
ROOT_ENV = "BASECAMP_ROOT"
LAUNCHER_ENV = "BASECAMP_LAUNCHER"  # the launcher's version number (min_launcher)
PUBLIC_KEY_FILE = Path(__file__).resolve().parent / "release_key.pub"
MANIFEST = "manifest.json"
MAX_DOWNLOAD = 300 * 1024 * 1024
MAX_NOTES = 20_000
DB_BACKUPS_KEEP = 5
_TIMEOUT_S = 30
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class UpdateError(Exception):
    """A release that can't be installed; the message is shown on the page."""


# -- versions -----------------------------------------------------------------------------


def parse_version(text: str) -> tuple:
    """A sortable key for ``1.2.3`` or ``v1.2.3-beta.1`` (a pre-release sorts before its
    release). ValueError if it isn't one."""
    m = _VERSION.match(text.strip())
    if not m:
        raise ValueError(f"Not a version: {text!r}")
    pre = m[4]
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) \
        if pre else ()
    return (int(m[1]), int(m[2]), int(m[3]), 0 if pre else 1, parts)


def is_newer(candidate: str, current: str) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


# -- signature and manifest ---------------------------------------------------------------


def load_public_key(path: Path = PUBLIC_KEY_FILE) -> Ed25519PublicKey | None:
    """The release public key: base64 of the raw 32 bytes, ``#`` lines are comments."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(lines[0], validate=True))
    except ValueError:
        log.error("%s doesn't hold an Ed25519 public key", path)
        return None


def public_key_text(key: Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def verify_signature(data: bytes, signature: bytes, key: Ed25519PublicKey | None) -> None:
    if key is None:
        raise UpdateError("This Basecamp has no release key, so it can't check updates")
    try:
        key.verify(signature, data)
    except InvalidSignature:
        raise UpdateError("The release's signature doesn't match; not installing it") from None


def _skip(rel: PurePosixPath) -> bool:
    return "__pycache__" in rel.parts or rel.suffix == ".pyc"


def file_hashes(root: Path) -> dict[str, str]:
    """SHA-256 of every file under ``root`` by ``/`` path, except the manifest and bytecode
    (Python writes ``__pycache__`` next to the code once it runs)."""
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if str(rel) == MANIFEST or _skip(rel):
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def make_manifest(root: Path, version: str, min_launcher: int = 1,
                  python: str | None = None) -> dict:
    return {"name": "basecamp", "version": version, "min_launcher": min_launcher,
            "python": python or f"{sys.version_info.major}.{sys.version_info.minor}",
            "built_at": _now(), "files": file_hashes(root)}


def check_tree(root: Path, version: str) -> dict:
    """The unpacked release's manifest, after checking it names ``version`` and that the
    files match it exactly."""
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UpdateError(f"The release has no readable manifest.json: {e}") from None
    if not isinstance(manifest, dict) or manifest.get("name") != "basecamp":
        raise UpdateError("manifest.json isn't a Basecamp manifest")
    if manifest.get("version") != version:
        raise UpdateError(f"manifest.json says {manifest.get('version')!r}, the release "
                          f"says {version!r}")
    want = manifest.get("files")
    if not isinstance(want, dict) or not want:
        raise UpdateError("manifest.json lists no files")
    have = file_hashes(root)
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    changed = sorted(k for k in set(want) & set(have) if want[k] != have[k])
    for what, names in (("missing", missing), ("not in the manifest", extra),
                        ("changed", changed)):
        if names:
            raise UpdateError(f"{len(names)} file(s) {what}, e.g. {names[0]}")
    return manifest


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Unpack, refusing any entry that would land outside ``dest``."""
    dest.mkdir(parents=True)
    base = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            rel = PurePosixPath(name)
            if (name.startswith(("/", "\\")) or "\\" in name or ":" in name
                    or ".." in rel.parts or rel.is_absolute()):
                raise UpdateError(f"The release zip has a bad path: {name!r}")
            target = (base / rel).resolve()
            if not target.is_relative_to(base):
                raise UpdateError(f"The release zip has a bad path: {name!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


# -- the database -------------------------------------------------------------------------


def backup_db(db_path: Path, dest_dir: Path, name: str, keep: int = DB_BACKUPS_KEEP) -> Path:
    """A consistent copy of the DB (SQLite's backup API, so WAL contents are included) as
    ``dest_dir/<name>.db``; the newest ``keep`` copies are kept."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{name}.db"
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, target)
    copies = sorted(dest_dir.glob("before-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in copies[keep:]:
        with contextlib.suppress(OSError):
            old.unlink()
    return target


def try_migrations(db_path: Path, migrations_dir: Path) -> list[int]:
    """Run a release's migrations on a throwaway copy of the DB; UpdateError if they fail."""
    with tempfile.TemporaryDirectory(prefix="basecamp-migrate-") as tmp:
        copy = Path(tmp) / "copy.db"
        if db_path.is_file():
            backup_db(db_path, Path(tmp), "copy", keep=99)
        conn = sqlite3.connect(copy, isolation_level=None)
        try:
            return db.migrate(conn, migrations_dir)
        except Exception as e:
            raise UpdateError(f"The release's database migrations fail: {e}") from None
        finally:
            conn.close()


# -- talking to GitHub ----------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _check_url(url: str) -> str:
    """https only, except to this PC (the dev PC's fake release server)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and parts.hostname:
        with contextlib.suppress(ValueError):
            if parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback:
                return url
    raise UpdateError(f"Refusing to fetch {url!r}: https only")


def _request(url: str, accept: str, etag: str = "") -> urllib.request.Request:
    headers = {"Accept": accept, "User-Agent": f"Basecamp/{__version__}"}
    if etag:
        headers["If-None-Match"] = etag
    return urllib.request.Request(_check_url(url), headers=headers)


def _pick(data: Any, beta: bool) -> dict | None:
    """The newest usable release from GitHub's answer (one release, or a list)."""
    releases = data if isinstance(data, list) else [data]
    best, best_key = None, None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if rel.get("prerelease") and not beta:
            continue
        try:
            key = parse_version(str(rel.get("tag_name", "")))
        except ValueError:
            continue
        if best_key is None or key > best_key:
            best, best_key = rel, key
    if best is None:
        return None
    version = str(best["tag_name"]).removeprefix("v")
    assets = {a.get("name"): a.get("browser_download_url") for a in best.get("assets") or []
              if isinstance(a, dict)}
    return {"version": version, "tag": best["tag_name"],
            "name": str(best.get("name") or best["tag_name"]),
            "notes": str(best.get("body") or "")[:MAX_NOTES],
            "prerelease": bool(best.get("prerelease")),
            "published_at": str(best.get("published_at") or ""),
            "html_url": str(best.get("html_url") or ""),
            "zip_url": assets.get(f"basecamp-{version}.zip") or "",
            "sig_url": assets.get(f"basecamp-{version}.zip.sig") or ""}


def _download(url: str, target: Path, limit: int = MAX_DOWNLOAD) -> None:
    req = _request(url, "application/octet-stream")
    size = 0
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as res, target.open("wb") as out:
        _check_url(res.geturl())  # where a redirect ended up
        while chunk := res.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise UpdateError(f"{url} is bigger than {limit // (1024 * 1024)} MB")
            out.write(chunk)


# -- the service ----------------------------------------------------------------------------


class SelfUpdater:
    """Checks, downloads and stages releases; Install hands over to the launcher.

    State the page shows is kept in ``<data>\\selfupdate.json`` (the DB isn't used, so a
    rollback that restores the DB doesn't forget what happened)."""

    def __init__(self, settings: Settings, store: Store | None = None, notifier: Any = None,
                 stop: threading.Event | None = None, *, root: Path | str | None = None,
                 launcher: int | None = None, public_key: Ed25519PublicKey | None = None,
                 current: str = __version__) -> None:
        self._settings = settings
        self._store = store
        self._notifier = notifier
        self._stop = stop or threading.Event()
        if root is None:
            root = os.environ.get(ROOT_ENV) or None
        self.root = Path(root) if root else None
        if launcher is None:
            with contextlib.suppress(ValueError):
                launcher = int(os.environ.get(LAUNCHER_ENV, "") or 0) or None
        self.launcher = launcher
        self._key = public_key if public_key is not None else load_public_key()
        self.current = current
        self.exit_code = 0
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "idle"  # idle, checking, downloading, ready, failed, installing
        self._error = ""
        self._ready_dir: Path | None = None
        self._state = self._load()
        if self._state.get("ready") and self._state["ready"].get("version"):
            path = Path(self._state["ready"].get("path", ""))
            if path.is_dir() and is_newer(self._state["ready"]["version"], self.current):
                self._ready_dir = path
                self._status = "ready"

    # -- persisted state ---------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self._settings.data_dir / "selfupdate.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @property
    def staging_dir(self) -> Path:
        return self.root / "versions" if self.root else self._settings.data_dir / "updates"

    # -- background loop ---------------------------------------------------------------

    def start(self) -> None:
        self.report_launcher_results()
        self._thread = threading.Thread(target=self._loop, name="selfupdate", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing.set()

    def _loop(self) -> None:
        if self._closing.wait(2 if self._state.get("installing") else 10):
            return
        while not self._closing.is_set():
            if self._state.get("installing"):
                self._confirm_update()
            every_h = self._settings.update_check_h
            last = self._state.get("last_check_ts", 0.0)
            if every_h > 0 and time.time() - last >= every_h * 3600:
                self.check()
            self.maybe_auto_install()
            if self._closing.wait(2 if self._state.get("installing") else 60):
                return

    # -- checking ------------------------------------------------------------------------

    def _release_url(self) -> str:
        api = self._settings.update_api.rstrip("/")
        repo = self._settings.update_repo
        if self._settings.update_beta:
            return f"{api}/repos/{repo}/releases?per_page=20"
        return f"{api}/repos/{repo}/releases/latest"

    def check_async(self) -> bool:
        """Check now on a thread; False if a check is already running."""
        if self._busy.locked():
            return False
        threading.Thread(target=self.check, name="selfupdate check", daemon=True).start()
        return True

    def check(self) -> dict:
        """Ask GitHub, and download and stage a newer release. Never raises."""
        if not self._busy.acquire(blocking=False):
            return self.view()
        try:
            self._set("checking")
            try:
                release = self._fetch()
                self._state["last_check"] = _now()
                self._state["last_check_ts"] = time.time()
                self._state["latest"] = release
                self._save()
                if release and is_newer(release["version"], self.current):
                    self._notify_found(release)
                    if not (self._ready_dir and self._state.get("ready", {}).get("version")
                            == release["version"]):
                        self._set("downloading")
                        self._stage(release)
                    self._set("ready")
                else:
                    self._set("idle")
            except UpdateError as e:
                self._fail(str(e))
            except (OSError, ValueError, urllib.error.URLError) as e:
                self._fail(f"Update check failed: {e}")
            except Exception as e:  # never kill the loop
                log.exception("Update check failed")
                self._fail(f"Update check failed: {e}")
            return self.view()
        finally:
            self._busy.release()

    def _fetch(self) -> dict | None:
        url = self._release_url()
        cache = self._state.get("etag", {})
        etag = cache.get("etag", "") if cache.get("url") == url else ""
        req = _request(url, "application/vnd.github+json", etag)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as res:
                data = json.loads(res.read(5 * 1024 * 1024))
                new_etag = res.headers.get("ETag", "")
        except urllib.error.HTTPError as e:
            if e.code == 304 and etag:
                log.debug("Release check: not modified")
                return self._state.get("latest")
            if e.code == 404:
                return None  # no releases yet
            raise UpdateError(f"GitHub answered {e.code} {e.reason}") from None
        release = _pick(data, self._settings.update_beta)
        self._state["etag"] = {"url": url, "etag": new_etag}
        return release

    def _stage(self, release: dict) -> None:
        version = release["version"]
        if not release["zip_url"] or not release["sig_url"]:
            raise UpdateError(f"Release {version} has no basecamp-{version}.zip and .sig")
        staging = self.staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        final = staging / version
        tmp = staging / f"{version}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix="basecamp-dl-", dir=staging) as dl:
            zip_path = Path(dl) / f"basecamp-{version}.zip"
            sig_path = Path(dl) / "zip.sig"
            _download(release["sig_url"], sig_path, limit=4096)
            _download(release["zip_url"], zip_path)
            verify_signature(zip_path.read_bytes(), sig_path.read_bytes(), self._key)
            try:
                safe_extract(zip_path, tmp)
                manifest = check_tree(tmp, version)
                self._check_manifest(manifest)
                try_migrations(self._settings.db_path, tmp / "migrations")
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        if final.exists():
            # A folder left from an earlier try: replace it unless it's the running version.
            if version == self.current:
                raise UpdateError(f"{version} is the running version")
            shutil.rmtree(final)
        os.replace(tmp, final)
        log.info("Release %s downloaded, checked and unpacked to %s", version, final)
        with self._lock:
            self._ready_dir = final
            self._state["ready"] = {"version": version, "path": str(final)}
            self._save()

    def _check_manifest(self, manifest: dict) -> None:
        need = manifest.get("min_launcher", 1)
        if self.launcher is not None and isinstance(need, int) and need > self.launcher:
            raise UpdateError(f"This update needs launcher {need} (this PC has "
                              f"{self.launcher}); it needs a manual reinstall")
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        if manifest.get("python") != python:
            raise UpdateError(f"This update is built for Python {manifest.get('python')}, "
                              f"this PC runs {python}; it needs a manual reinstall")

    def _set(self, status: str) -> None:
        with self._lock:
            self._status = status
            if status != "failed":
                self._error = ""

    def _fail(self, error: str) -> None:
        log.warning("%s", error)
        with self._lock:
            self._status = "failed"
            self._error = error

    def _notify_found(self, release: dict) -> None:
        notified = self._state.setdefault("notified", [])
        if release["version"] in notified:
            return
        notified.append(release["version"])
        del notified[:-20]
        self._save()
        self._post(f":package: Basecamp **{release['version']}** is out (this PC runs "
                   f"{self.current}). Install it from the dashboard.")

    def _post(self, text: str) -> None:
        if self._notifier is not None:
            self._notifier.post(text)

    # -- installing ----------------------------------------------------------------------

    def install(self, who: str = "") -> str:
        """Start the hand-over to the launcher: Basecamp exits, backs up the DB on the way
        out and the launcher starts the new version. UpdateError if it can't."""
        with self._lock:
            if self.root is None or self.launcher is None:
                raise UpdateError("Basecamp isn't running under the launcher, so it can't "
                                  "install updates here")
            if self._status == "installing":
                raise UpdateError("Already installing")
            ready = self._state.get("ready") or {}
            version = ready.get("version")
            if self._status != "ready" or not version or self._ready_dir is None:
                raise UpdateError("No update is downloaded and checked yet")
            if not (self._ready_dir / "basecamp" / "__init__.py").is_file():
                raise UpdateError(f"The unpacked {version} is gone; check again")
            self._status = "installing"
            self._state["installing"] = {"from": self.current, "to": version, "by": who,
                                         "at": _now()}
            self._save()
        log.info("Installing Basecamp %s (asked by %s); exiting for the launcher", version,
                 who or "auto-install")
        self.exit_code = EXIT_UPDATE
        self._stop.set()
        return version

    def maybe_auto_install(self, now: datetime | None = None) -> bool:
        hour = self._settings.update_auto_hour
        if hour < 0 or self._status != "ready" or self.root is None or self.launcher is None:
            return False
        now = now or datetime.now()
        if now.hour != hour:
            return False
        version = (self._state.get("ready") or {}).get("version")
        if version and version in self._state.get("failed_versions", []):
            return False  # it rolled back once; only a person installs it again
        try:
            self.install("auto-install")
        except UpdateError as e:
            log.warning("Auto-install: %s", e)
            return False
        if self._store is not None:
            self._store.add_audit("auto-install", "update.install", None,
                                  f"{self.current} -> {version}")
        return True

    def finish_exit(self) -> int:
        """Called last as Basecamp exits: for an update, back up the DB and write
        ``pending.txt``. Returns the process exit code (3 = start the new version; 0 if the
        backup failed, so the launcher just starts this version again)."""
        if self.exit_code != EXIT_UPDATE or self.root is None:
            return self.exit_code
        target = (self._state.get("installing") or {}).get("to", "")
        try:
            backup_db(self._settings.db_path, self._settings.data_dir / "db-backups",
                      f"before-{target}")
            pending = self.root / "pending.txt"
            tmp = pending.with_name("pending.txt.tmp")
            tmp.write_text(target, encoding="utf-8")
            os.replace(tmp, pending)
        except (OSError, sqlite3.Error) as e:
            log.error("Not updating: the DB backup failed: %s", e)
            self._state.pop("installing", None)
            self._state["install_error"] = f"Not installed: the database backup failed: {e}"
            self._save()
            return 0
        log.info("DB backed up; launcher will start %s", target)
        return EXIT_UPDATE

    def report_launcher_results(self) -> None:
        """At start: say whether the last install worked, and pick up the launcher's
        update-failed.json after a rollback (R-92)."""
        installing = self._state.get("installing")
        if installing and installing.get("to") != self.current:
            self._state.pop("installing")
            if not self._failure_file_for(installing.get("to")):
                self._state["install_error"] = (
                    f"The update to {installing.get('to')} didn't happen; see the launcher's "
                    "log")
        self._confirm_update()
        failure = self.failure()
        if failure and failure.get("id") not in self._state.get("reported", []):
            self._state.setdefault("reported", []).append(failure.get("id"))
            del self._state["reported"][:-20]
            bad = self._state.setdefault("failed_versions", [])
            if failure.get("to") and failure["to"] not in bad:
                bad.append(failure["to"])
            self._post(f":warning: Basecamp **{failure.get('to')}** wasn't healthy "
                       f"({failure.get('reason')}), so it rolled back to "
                       f"{failure.get('from')}")
        self._save()

    def _confirm_update(self) -> None:
        """This version was just installed: say so once the launcher has taken it off trial
        (its health check passed). Until then it may still be rolled back."""
        installing = self._state.get("installing")
        if not installing or installing.get("to") != self.current:
            return
        if self.root is not None and self._read_root("trial.txt") == self.current:
            return
        self._state.pop("installing")
        log.info("Updated from %s to %s", installing.get("from"), self.current)
        self._state["last_update"] = {**installing, "done_at": _now()}
        self._post(f":white_check_mark: Basecamp updated from {installing.get('from')}"
                   f" to **{self.current}**")
        self._state.pop("ready", None)
        self._ready_dir = None
        if self._status == "ready":
            self._status = "idle"
        self._save()

    def _read_root(self, name: str) -> str:
        try:
            return (self.root / name).read_text(encoding="utf-8").strip()  # type: ignore[operator]
        except OSError:
            return ""

    def _failure_file_for(self, version: str | None) -> bool:
        failure = self.failure()
        return bool(failure and failure.get("to") == version)

    def failure(self) -> dict | None:
        """The launcher's last rollback note, unless dismissed."""
        if self.root is None:
            return None
        try:
            data = json.loads((self.root / "update-failed.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("id") == self._state.get("dismissed"):
            return None
        return data

    def dismiss(self) -> None:
        with self._lock:
            failure = self.failure()
            if failure:
                self._state["dismissed"] = failure.get("id")
            self._state.pop("install_error", None)
            self._save()

    # -- what the page shows -------------------------------------------------------------

    def view(self) -> dict:
        with self._lock:
            latest = self._state.get("latest")
            ready = self._state.get("ready") or {}
            available = bool(latest and is_newer(latest["version"], self.current))
            return {
                "current": self.current,
                "status": self._status,
                "error": self._error,
                "install_error": self._state.get("install_error", ""),
                "latest": latest,
                "available": available,
                "ready": self._status == "ready" and bool(ready.get("version")),
                "ready_version": ready.get("version", ""),
                "last_check": self._state.get("last_check", ""),
                "can_install": self.root is not None and self.launcher is not None,
                "launcher": self.launcher,
                "has_key": self._key is not None,
                "beta": self._settings.update_beta,
                "auto_hour": self._settings.update_auto_hour,
                "check_h": self._settings.update_check_h,
                "failure": self.failure(),
                "failed_versions": list(self._state.get("failed_versions", [])),
                "last_update": self._state.get("last_update"),
            }
