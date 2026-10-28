"""Build and sign a release zip (docs/07-updates.md). ``release.ps1`` runs this; so does the
dev PC's end-to-end test, with a throwaway key.

    python scripts/build_release.py 1.3.0 --key %USERPROFILE%\\.basecamp-signing\\release.key

Makes ``dist\\basecamp-<v>.zip`` and ``dist\\basecamp-<v>.zip.sig``. The zip holds the
``basecamp\\`` package, ``migrations\\``, ``templates\\``, ``launcher.py``, ``lib\\`` (the
dependencies as Windows wheels for the target Python, so the host PC never needs pip) and
``manifest.json`` with the SHA-256 of every file. The signature is Ed25519 over the whole
zip. The private key is read from its file and never printed or copied.

By default the key must match the repo's ``basecamp/release_key.pub``. ``--test-key`` puts
the signing key's public key in the zip instead (test builds only: a PC with the real
public key refuses such a zip).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from basecamp import selfupdate  # noqa: E402

PYTHON = "3.13"
# Launcher 1 sets PYTHONPATH, which the runtime's ._pth file switches off (D-123).
MIN_LAUNCHER = 2
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def source_version(src: Path) -> str:
    text = (src / "basecamp" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("No __version__ in basecamp/__init__.py")
    return m[1]


def load_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} isn't an Ed25519 key")
    return key


def copy_sources(src: Path, stage: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(src / "basecamp", stage / "basecamp", ignore=ignore)
    (stage / "migrations").mkdir()
    for path in sorted((src / "migrations").glob("*.sql")):
        shutil.copy2(path, stage / "migrations" / path.name)
    (stage / "templates").mkdir()
    for path in sorted((src / "templates").glob("*.json")):
        shutil.copy2(path, stage / "templates" / path.name)
    for name in ("launcher.py", "CHANGES.md"):
        if (src / name).is_file():
            shutil.copy2(src / name, stage / name)


def vendor(src: Path, lib: Path, python: str) -> None:
    with (src / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check",
                    "--target", str(lib), "--only-binary=:all:", "--platform", "win_amd64",
                    "--implementation", "cp", "--python-version", python, "--no-compile",
                    *deps], check=True)
    shutil.rmtree(lib / "bin", ignore_errors=True)  # console scripts, unused


def write_zip(stage: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    tmp.replace(target)


def build(version: str, key_path: Path, out: Path, src: Path = REPO, lib: Path | None = None,
          test_key: bool = False, python: str = PYTHON,
          min_launcher: int = MIN_LAUNCHER) -> Path:
    selfupdate.parse_version(version)  # ValueError if it isn't one
    if source_version(src) != version:
        raise SystemExit(f"basecamp/__init__.py says {source_version(src)}, not {version}")
    key = load_key(key_path)
    public = selfupdate.public_key_text(key.public_key())  # type: ignore[arg-type]
    shipped = selfupdate.load_public_key(src / "basecamp" / "release_key.pub")
    if not test_key and (shipped is None or selfupdate.public_key_text(shipped) != public):
        raise SystemExit("The signing key doesn't match basecamp/release_key.pub; "
                         "run scripts/new-signing-key.ps1 or use the right key")
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"basecamp-{version}.zip"
    with tempfile.TemporaryDirectory(prefix="basecamp-build-") as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        copy_sources(src, stage)
        if test_key:
            (stage / "basecamp" / "release_key.pub").write_text(
                "# TEST BUILD key, not the real release key\n" + public + "\n",
                encoding="utf-8", newline="\n")
        if lib is not None:
            shutil.copytree(lib, stage / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        else:
            vendor(src, stage / "lib", python)
        manifest = selfupdate.make_manifest(stage, version, min_launcher, python)
        (stage / selfupdate.MANIFEST).write_text(
            json.dumps(manifest, indent=1), encoding="utf-8", newline="\n")
        write_zip(stage, target)
    data = target.read_bytes()
    signature = key.sign(data)
    target.with_name(target.name + ".sig").write_bytes(signature)
    selfupdate.verify_signature(data, signature, key.public_key())
    print(f"{target}  {len(data) // 1024} KB  sha256 {hashlib.sha256(data).hexdigest()}")
    print(f"{target}.sig  (public key {public[:8]}..., signature "
          f"{base64.b64encode(signature).decode()[:12]}...)")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and sign a Basecamp release zip")
    parser.add_argument("version")
    parser.add_argument("--key", type=Path, required=True, help="the Ed25519 private key file")
    parser.add_argument("--out", type=Path, default=REPO / "dist")
    parser.add_argument("--src", type=Path, default=REPO, help="the source tree to package")
    parser.add_argument("--lib", type=Path, help="use this lib folder instead of pip")
    parser.add_argument("--python", default=PYTHON)
    parser.add_argument("--min-launcher", type=int, default=MIN_LAUNCHER)
    parser.add_argument("--test-key", action="store_true",
                        help="test build: ship the signing key's public key in the zip")
    args = parser.parse_args()
    build(args.version, args.key, args.out, args.src, args.lib, args.test_key, args.python,
          args.min_launcher)


if __name__ == "__main__":
    main()
