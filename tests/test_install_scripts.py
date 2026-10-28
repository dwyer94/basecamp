"""Phase 7 install helpers: ``scripts/install_release.py`` (the installer's release check, with
its own Ed25519 verifier) and the PowerShell scripts' dry-run modes."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from basecamp import selfupdate

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import install_release as ir  # noqa: E402

PY = f"{sys.version_info.major}.{sys.version_info.minor}"


def raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)


# -- Ed25519 ------------------------------------------------------------------------------


def test_rfc8032_test_vector_1() -> None:
    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
                        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert ir.ed25519_verify(public, b"", sig)
    assert not ir.ed25519_verify(public, b"x", sig)


def test_agrees_with_cryptography() -> None:
    for i in range(25):
        key = Ed25519PrivateKey.generate()
        msg = os.urandom(i * 53)
        sig = key.sign(msg)
        public = raw_public(key)
        assert ir.ed25519_verify(public, msg, sig)
        assert not ir.ed25519_verify(public, msg + b"\0", sig)
        flipped = bytearray(sig)
        flipped[i % 64] ^= 1 << (i % 8)
        assert not ir.ed25519_verify(public, msg, bytes(flipped))
        other = raw_public(Ed25519PrivateKey.generate())
        assert not ir.ed25519_verify(other, msg, sig)


def test_refuses_malleable_and_malformed_signatures() -> None:
    key = Ed25519PrivateKey.generate()
    public = raw_public(key)
    sig = key.sign(b"release")
    s = int.from_bytes(sig[32:], "little") + ir._L  # same point, s not reduced
    assert not ir.ed25519_verify(public, b"release", sig[:32] + s.to_bytes(32, "little"))
    assert not ir.ed25519_verify(public, b"release", sig[:63])
    assert not ir.ed25519_verify(public[:31], b"release", sig)
    assert not ir.ed25519_verify(b"\xff" * 32, b"release", sig)  # not a point


# -- releases -----------------------------------------------------------------------------


def make_release(tmp: Path, version: str = "0.1.0", python: str = PY, extra: dict | None = None,
                 tamper: bool = False) -> tuple[Path, Path, Path]:
    """A small signed release zip like build_release.py makes, plus its key file."""
    stage = tmp / f"stage-{version}"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "basecamp").mkdir(parents=True)
    (stage / "basecamp" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (stage / "launcher.py").write_text(f"# launcher for {version}\n")
    (stage / "lib").mkdir()
    (stage / "lib" / "dep.py").write_text("x = 1\n")
    manifest = selfupdate.make_manifest(stage, version, 1, python)
    (stage / "manifest.json").write_text(json.dumps(manifest))
    if tamper:
        (stage / "lib" / "dep.py").write_text("x = 2\n")
    zip_path = tmp / f"basecamp-{version}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(stage).as_posix())
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    key = Ed25519PrivateKey.generate()
    sig = zip_path.with_name(zip_path.name + ".sig")
    sig.write_bytes(key.sign(zip_path.read_bytes()))
    pub = tmp / "release_key.pub"
    pub.write_text("# test key\n" + base64.b64encode(raw_public(key)).decode() + "\n")
    return zip_path, sig, pub


def test_installs_a_good_release(tmp_path: Path) -> None:
    zip_path, sig, pub = make_release(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    out = ir.install(zip_path, sig, pub, root)
    assert out == {"result": "installed", "version": "0.1.0", "launcher": "updated"}
    assert (root / "current.txt").read_text() == "0.1.0"
    assert (root / "launcher.py").read_text() == "# launcher for 0.1.0\n"
    assert (root / "versions" / "0.1.0" / "lib" / "dep.py").is_file()
    assert not (root / "versions" / "0.1.0.tmp").exists()
    # Same rule as Basecamp's own check, so an update check later agrees.
    selfupdate.check_tree(root / "versions" / "0.1.0", "0.1.0")


def test_installed_already_only_refreshes_the_launcher(tmp_path: Path) -> None:
    zip_path, sig, pub = make_release(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    ir.install(zip_path, sig, pub, root)
    assert ir.install(None, None, None, root)["launcher"] == "unchanged"
    (root / "launcher.py").write_text("old launcher")
    newer, nsig, npub = make_release(tmp_path, "0.2.0")
    out = ir.install(newer, nsig, npub, root)
    assert out == {"result": "already-installed", "version": "0.1.0", "launcher": "updated"}
    assert (root / "launcher.py").read_text() == "# launcher for 0.1.0\n"
    assert not (root / "versions" / "0.2.0").exists()  # Basecamp updates itself


def test_bad_signature_unpacks_nothing(tmp_path: Path) -> None:
    zip_path, sig, pub = make_release(tmp_path)
    data = bytearray(sig.read_bytes())
    data[0] ^= 1
    sig.write_bytes(bytes(data))
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ir.InstallError, match="signature"):
        ir.install(zip_path, sig, pub, root)
    assert not (root / "versions").exists()
    assert not (root / "current.txt").exists()


@pytest.mark.parametrize("case, match", [
    ("tamper", "changed"),
    ("extra", "not in the manifest"),
    ("path", "bad path"),
    ("python", "for Python"),
])
def test_refused_releases_leave_nothing(tmp_path: Path, case: str, match: str) -> None:
    kwargs: dict = {}
    if case == "tamper":
        kwargs["tamper"] = True
    elif case == "extra":
        kwargs["extra"] = {"basecamp/planted.py": "evil"}
    elif case == "path":
        kwargs["extra"] = {"../outside.py": "evil"}
    else:
        kwargs["python"] = "3.12"
    zip_path, sig, pub = make_release(tmp_path, **kwargs)
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ir.InstallError, match=match):
        ir.install(zip_path, sig, pub, root)
    assert not (root / "current.txt").exists()
    assert not (root / "versions").exists() or not any((root / "versions").iterdir())
    assert not (tmp_path / "outside.py").exists()


def test_leftover_version_folder_is_checked_again(tmp_path: Path) -> None:
    zip_path, sig, pub = make_release(tmp_path)
    root = tmp_path / "root"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "versions" / "0.1.0" / "junk.py").write_text("left from a stopped try")
    ir.install(zip_path, sig, pub, root)
    assert not (root / "versions" / "0.1.0" / "junk.py").exists()


def test_cli_check_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    zip_path, sig, pub = make_release(tmp_path)
    args = ["--zip", str(zip_path), "--sig", str(sig), "--key", str(pub)]
    assert ir.main(["--check", *args]) == 0
    assert "version=0.1.0" in capsys.readouterr().out
    assert not (tmp_path / "root").exists()
    pub.write_text("not a key\n")
    assert ir.main(["--check", *args]) == 1
    assert "error=" in capsys.readouterr().out


def test_helper_is_standard_library_only() -> None:
    # It runs on the bare embeddable Python before any dependency is installed.
    out = subprocess.run([sys.executable, "-I", "-c",
                          "import sys; sys.path.insert(0, sys.argv[1]); import install_release; "
                          "print(sorted(m for m in sys.modules if m.split('.')[0] in "
                          "('cryptography', 'basecamp', 'psutil', 'pydantic')))",
                          str(REPO / "scripts")], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# -- PowerShell dry runs ------------------------------------------------------------------

POWERSHELL = shutil.which("powershell.exe")
needs_ps = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell only")


def ps(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",  # type: ignore[list-item]
                           str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, timeout=120)


@needs_ps
def test_install_whatif_changes_nothing(tmp_path: Path) -> None:
    zip_path, _sig, pub = make_release(tmp_path)
    root, data = tmp_path / "Basecamp", tmp_path / "BasecampData"
    r = ps("install.ps1", "-Root", str(root), "-DataRoot", str(data), "-ReleaseZip", str(zip_path),
           "-PublicKey", str(pub), "-Port", "8158", "-WhatIf")
    assert r.returncode == 0, r.stdout + r.stderr
    for want in (f"What if: Create folder {root}", "embed-amd64.zip", "Write a starter",
                 "<LogonType>S4U</LogonType>", "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
                 "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
                 "<Delay>PT1M</Delay>", "<Interval>PT5M</Interval>", "<Priority>4</Priority>",
                 "<RunLevel>LeastPrivilege</RunLevel>", "tailscale serve --bg --https=443",
                 "Dry run: nothing was changed."):
        assert want in r.stdout, want
    assert "Performing the operation \"New Alias\"" not in r.stdout
    assert not root.exists() and not data.exists()


@needs_ps
def test_install_refuses_someone_elses_folder(tmp_path: Path) -> None:
    root = tmp_path / "OtherApp"
    root.mkdir()
    (root / "app.jar").write_text("x")
    r = ps("install.ps1", "-Root", str(root), "-DataRoot", str(tmp_path / "d"), "-SkipTask",
           "-PublicKey", str(tmp_path / "none.pub"), "-WhatIf")
    assert r.returncode != 0
    assert "isn't a Basecamp folder" in r.stdout + r.stderr


@needs_ps
def test_uninstall_whatif_changes_nothing(tmp_path: Path) -> None:
    root, data = tmp_path / "Basecamp", tmp_path / "BasecampData"
    (root / "versions" / "0.1.0").mkdir(parents=True)
    (root / "launcher.py").write_text("x")
    data.mkdir()
    (data / "settings.toml").write_text("")
    r = ps("uninstall.ps1", "-Root", str(root), "-DataRoot", str(data), "-TaskName",
           "Basecamp test task that does not exist", "-RemoveData", "-WhatIf")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"What if: Remove {root} and everything in it" in r.stdout
    assert f"What if: Remove {data} and everything in it" in r.stdout
    assert root.exists() and data.exists()


@needs_ps
@pytest.mark.parametrize("marker", [True, False])
def test_uninstall_again_after_hosts_kept_python(tmp_path: Path, marker: bool) -> None:
    # A first run with games running leaves only python\ (and the note); the second run,
    # after they've stopped, must finish the job, while a bare python\ stays refused.
    root, data = tmp_path / "Basecamp", tmp_path / "BasecampData"
    (root / "python").mkdir(parents=True)
    if marker:
        (root / "uninstall-pending.txt").write_text("x")
    r = ps("uninstall.ps1", "-Root", str(root), "-DataRoot", str(data), "-TaskName",
           "Basecamp test task that does not exist", "-WhatIf")
    if marker:
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"What if: Remove {root} and everything in it" in r.stdout
    else:
        assert r.returncode != 0
        assert "doesn't look like a Basecamp program folder" in r.stdout + r.stderr
    assert (root / "python").exists()


@needs_ps
@pytest.mark.parametrize("exe, port, match", [
    ("game", "27015", None),
    ("python", "27015", "shell or script runtime"),
    ("game", "445", "outside 1024-65535"),
    ("game", "2000-3000", "ranges aren't allowed"),
    ("game", ",".join(str(p) for p in range(27000, 27011)), "at most 10"),
    ("windows", "27015", "part of Windows"),
    ("relative", "27015", "full path"),
])
def test_firewall_rule_whatif_and_refusals(tmp_path: Path, exe: str, port: str,
                                           match: str | None) -> None:
    game = tmp_path / "GameServer.exe"
    game.write_text("not a program")
    (tmp_path / "python.exe").write_text("not a program")
    path = {"game": str(game), "python": str(tmp_path / "python.exe"),
            "windows": os.path.join(os.environ["SYSTEMROOT"], "System32", "notepad.exe"),
            "relative": "GameServer.exe"}[exe]
    r = ps("add-game-firewall-rule.ps1", "-Exe", path, "-Port", port, "-Protocol", "UDP",
           "-Root", str(tmp_path / "Basecamp"), "-WhatIf")
    if match is None:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "What if: Add firewall rule 'Basecamp - GameServer UDP 27015'" in r.stdout
        assert "from 100.64.0.0/10, group Basecamp, all profiles" in r.stdout
    else:
        assert r.returncode != 0
        assert match in r.stdout + r.stderr
