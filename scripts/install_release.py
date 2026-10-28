"""Check and unpack a Basecamp release for ``install.ps1`` (docs/04-deployment.md).

Standard library only: it runs on the freshly installed embeddable Python, before any of
Basecamp's dependencies are there, so it can't use ``cryptography`` like Basecamp's own
update check does (``basecamp/selfupdate.py``). Instead it carries a small Ed25519 verifier
written from RFC 8032 (verify only; the tests check it against ``cryptography``) (D-80).

    python -I install_release.py --zip basecamp-0.1.0.zip --sig basecamp-0.1.0.zip.sig \\
        --key release_key.pub --root C:\\Basecamp

1. The signature of the whole zip must match the public key (base64 of the raw 32 bytes,
   ``#`` lines are comments: the same format as ``basecamp/release_key.pub``). Nothing in
   the zip is read before that.
2. ``manifest.json`` must name a Basecamp release for this Python (major.minor).
3. It's unpacked to ``versions\\<v>.tmp\\`` (entries that would land outside are refused),
   the files must match the manifest exactly, then the folder becomes ``versions\\<v>\\``.
4. ``launcher.py`` is copied out of the release to ``<root>\\launcher.py`` and
   ``current.txt`` names the version.

If ``current.txt`` already names an unpacked version, Basecamp is installed and updates
itself from the page: only ``launcher.py`` is refreshed from that version, and the zip is
left alone. The exception is ``--min-launcher``: when that version's launcher is older
than the runtime needs (``LAUNCHER_VERSION`` 1 relies on ``PYTHONPATH``, which the
runtime's ``._pth`` file switches off, D-123), the given release must be newer and carry
a new enough launcher, and it's installed the way an update is: the database copied to
``db-backups\\before-<v>.db`` (with ``--data``), the old version kept as ``previous.txt``,
the new one on trial (``trial.txt``), so the launcher rolls back if it isn't healthy.
``--check`` does steps 1 and 2 and changes nothing.

Prints one ``key=value`` line per result for install.ps1 and exits 1 with a message on
any failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path, PurePosixPath

MANIFEST = "manifest.json"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
_LAUNCHER_VERSION = re.compile(r"^LAUNCHER_VERSION = (\d+)", re.M)


class InstallError(Exception):
    pass


# -- Ed25519 verification (RFC 8032, section 5.1) ------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a: tuple, b: tuple) -> tuple:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    aa = (y1 - x1) * (y2 - x2) % _P
    bb = (y1 + x1) * (y2 + x2) % _P
    cc = 2 * t1 * t2 * _D % _P
    dd = 2 * z1 * z2 % _P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, point: tuple) -> tuple:
    out = (0, 1, 1, 0)
    while s:
        if s & 1:
            out = _add(out, point)
        point = _add(point, point)
        s >>= 1
    return out


def _same(a: tuple, b: tuple) -> bool:
    return ((a[0] * b[2] - b[0] * a[2]) % _P == 0
            and (a[1] * b[2] - b[1] * a[2]) % _P == 0)


def _x_from_y(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if x & 1 != sign:
        x = _P - x
    return x


def _point(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _x_from_y(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_x_from_y(_GY, 0), _GY, 1, _x_from_y(_GY, 0) * _GY % _P)  # type: ignore[operator]


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True if ``signature`` is a valid Ed25519 signature of ``message`` by ``public``."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _point(public)
    r = _point(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(),
                       "little") % _L
    return _same(_mul(s, _G), _add(r, _mul(h, a)))


# -- the release ----------------------------------------------------------------------------


def load_public_key(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InstallError(f"Can't read the release public key {path}: {e}") from None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    try:
        raw = base64.b64decode(lines[0], validate=True) if lines else b""
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise InstallError(f"{path} doesn't hold an Ed25519 public key")
    return raw


def verify_zip(zip_path: Path, sig_path: Path, public: bytes) -> bytes:
    data = zip_path.read_bytes()
    signature = sig_path.read_bytes()
    if not ed25519_verify(public, data, signature):
        raise InstallError(f"The signature of {zip_path.name} doesn't match the release key; "
                           "not installing it")
    return data


def read_manifest(zip_path: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read(MANIFEST))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise InstallError(f"The release has no readable {MANIFEST}: {e}") from None
    if not isinstance(manifest, dict) or manifest.get("name") != "basecamp":
        raise InstallError(f"{MANIFEST} isn't a Basecamp manifest")
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION.match(version):
        raise InstallError(f"{MANIFEST} has no usable version: {version!r}")
    here = f"{sys.version_info.major}.{sys.version_info.minor}"
    if manifest.get("python") != here:
        raise InstallError(f"Release {version} is for Python {manifest.get('python')}, "
                           f"this runtime is {here}")
    return manifest


def _skip(rel: PurePosixPath) -> bool:
    return "__pycache__" in rel.parts or rel.suffix == ".pyc"


def file_hashes(root: Path) -> dict[str, str]:
    """Same rule as ``basecamp.selfupdate.file_hashes``."""
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if str(rel) == MANIFEST or _skip(rel):
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def check_tree(root: Path, manifest: dict) -> None:
    want = manifest.get("files")
    if not isinstance(want, dict) or not want:
        raise InstallError(f"{MANIFEST} lists no files")
    have = file_hashes(root)
    for what, names in (("missing", sorted(set(want) - set(have))),
                        ("not in the manifest", sorted(set(have) - set(want))),
                        ("changed", sorted(k for k in set(want) & set(have)
                                           if want[k] != have[k]))):
        if names:
            raise InstallError(f"{len(names)} file(s) {what}, e.g. {names[0]}")
    for name in ("launcher.py", "basecamp/__init__.py"):
        if name not in want:
            raise InstallError(f"The release has no {name}")


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
                raise InstallError(f"The release zip has a bad path: {name!r}")
            target = (base / rel).resolve()
            if not target.is_relative_to(base):
                raise InstallError(f"The release zip has a bad path: {name!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def version_key(text: str) -> tuple:
    """Same order as ``launcher.version_key``; ``text`` must match ``_VERSION``."""
    m = _VERSION.match(text)
    assert m is not None
    pre = tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                for p in m[4].split(".")) if m[4] else ()
    return (int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1, pre)


def launcher_version(path: Path) -> int:
    """``LAUNCHER_VERSION`` in a launcher.py; 0 if it has none or can't be read."""
    try:
        m = _LAUNCHER_VERSION.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return 0
    return int(m[1]) if m else 0


def backup_db(data: Path, version: str) -> None:
    """``db-backups\\before-<v>.db``, as Basecamp makes before an update (D-55)."""
    db = data / "basecamp.db"
    if not db.is_file():
        return
    (data / "db-backups").mkdir(exist_ok=True)
    target = data / "db-backups" / f"before-{version}.db"
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    os.replace(tmp, target)


def runnable(root: Path, version: str) -> bool:
    return bool(_VERSION.match(version)) and (
        root / "versions" / version / "basecamp" / "__init__.py").is_file()


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def copy_launcher(root: Path, version: str) -> bool:
    """Copy ``launcher.py`` out of a version folder; True if it changed. A running launcher
    keeps the copy it read at its start, so the new one takes effect at its next start."""
    src = root / "versions" / version / "launcher.py"
    dst = root / "launcher.py"
    new = src.read_bytes()
    if dst.is_file() and dst.read_bytes() == new:
        return False
    tmp = dst.with_name("launcher.py.tmp")
    tmp.write_bytes(new)
    os.replace(tmp, dst)
    return True


def current(root: Path) -> str:
    try:
        return (root / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def unpack(zip_path: Path, sig_path: Path, key_path: Path, root: Path) -> str:
    """Check the release and make ``versions\\<v>\\``; its version."""
    verify_zip(zip_path, sig_path, load_public_key(key_path))
    manifest = read_manifest(zip_path)
    version = manifest["version"]
    versions = root / "versions"
    folder = versions / version
    tmp = versions / f"{version}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    if folder.exists():
        # Left by an earlier try that stopped before current.txt: check it again from scratch.
        shutil.rmtree(folder)
    try:
        safe_extract(zip_path, tmp)
        check_tree(tmp, manifest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    os.replace(tmp, folder)
    return version


def install(zip_path: Path | None, sig_path: Path | None, key_path: Path | None,
            root: Path, min_launcher: int = 0, data: Path | None = None) -> dict[str, str]:
    have = current(root)
    if runnable(root, have):
        old = launcher_version(root / "versions" / have / "launcher.py")
        if old >= min_launcher:
            changed = copy_launcher(root, have)
            return {"result": "already-installed", "version": have,
                    "launcher": "updated" if changed else "unchanged"}
        if zip_path is None or sig_path is None or key_path is None:
            raise InstallError(f"Basecamp {have}'s launcher is version {old} and this runtime "
                               f"needs {min_launcher}; a newer release is needed")
        verify_zip(zip_path, sig_path, load_public_key(key_path))
        new = read_manifest(zip_path)["version"]
        if version_key(new) <= version_key(have):
            raise InstallError(f"Basecamp {have}'s launcher is version {old} and this runtime "
                               f"needs {min_launcher}, but the release is {new}, not newer")
    elif zip_path is None or sig_path is None or key_path is None:
        raise InstallError("Basecamp isn't installed here yet and no release was given")
    version = unpack(zip_path, sig_path, key_path, root)
    got = launcher_version(root / "versions" / version / "launcher.py")
    if got < min_launcher:
        shutil.rmtree(root / "versions" / version, ignore_errors=True)
        raise InstallError(f"Release {version}'s launcher is version {got}; this runtime "
                           f"needs {min_launcher}")
    if not runnable(root, have):
        copy_launcher(root, version)
        _write(root / "current.txt", version)
        return {"result": "installed", "version": version, "launcher": "updated"}
    # An upgrade the self-updater can't do (its launcher is too old): done the way the
    # launcher does an update, so a version that isn't healthy rolls back (D-56).
    if data is not None:
        backup_db(data, version)
    _write(root / "previous.txt", have)
    _write(root / "trial.txt", version)
    copy_launcher(root, version)
    _write(root / "current.txt", version)
    return {"result": "upgraded", "from": have, "version": version, "launcher": "updated"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check and unpack a Basecamp release")
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--sig", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--data", type=Path, help="the data folder (for the DB copy)")
    parser.add_argument("--min-launcher", type=int, default=0,
                        help="the oldest LAUNCHER_VERSION this runtime can run")
    parser.add_argument("--check", action="store_true",
                        help="only check the signature and manifest; change nothing")
    args = parser.parse_args(argv)
    try:
        if args.check:
            if not (args.zip and args.sig and args.key):
                raise InstallError("--check needs --zip, --sig and --key")
            verify_zip(args.zip, args.sig, load_public_key(args.key))
            out = {"result": "ok", "version": read_manifest(args.zip)["version"]}
        else:
            if args.root is None:
                raise InstallError("--root is needed")
            out = install(args.zip, args.sig, args.key, args.root, args.min_launcher,
                          args.data)
    except (InstallError, OSError, sqlite3.Error) as e:
        print(f"error={e}")
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
