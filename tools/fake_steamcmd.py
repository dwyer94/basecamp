"""Fake steamcmd.exe for tests and dev. Standard library only.

Takes SteamCMD's arguments and prints output shaped like the real thing:

    python tools/fake_steamcmd.py +force_install_dir C:\\x +login anonymous
        +app_update 2394010 [-beta <branch>] [validate] +quit

On success it writes ``<install>\\steamapps\\appmanifest_<appid>.acf`` with a build ID one
higher than before (1000 for a first install), like a real update.

What it does is picked by ``FAKE_STEAMCMD_MODE``, else by the beta branch when that is a mode
name (so the browser can try each one by setting the server's beta branch), else ``ok``:

    ok          progress lines, then "Success! App '<id>' fully installed", exit 0
    uptodate    "Success! App '<id>' already up to date", exit 0, build ID unchanged
    fail        progress, then "ERROR! Failed to install app ...", exit 8
    selfupdate  first run: SteamCMD updating itself, exit 7 (marker file written);
                the next run clears the marker and does "ok"
    hang        some progress, then stops responding (for the timeout and cancel)

``FAKE_STEAMCMD_DELAY`` is the pause between progress lines (default 0.3 s).
``FAKE_STEAMCMD_STATE`` is the self-update marker file (default in the temp folder).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

MODES = ("ok", "uptodate", "fail", "selfupdate", "hang")


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def parse(argv: list[str]) -> dict:
    out: dict = {"install": None, "app": None, "beta": "", "validate": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "+force_install_dir" and i + 1 < len(argv):
            out["install"] = argv[i + 1]
            i += 1
        elif arg == "+app_update" and i + 1 < len(argv):
            out["app"] = argv[i + 1]
            i += 1
        elif arg == "-beta" and i + 1 < len(argv):
            out["beta"] = argv[i + 1]
            i += 1
        elif arg == "validate":
            out["validate"] = True
        i += 1
    return out


def read_build(manifest: Path) -> int | None:
    try:
        match = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8"))
    except OSError:
        return None
    return int(match[1]) if match else None


def write_manifest(manifest: Path, app: str, build: int, beta: str) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{app}"\n'
        f'\t"name"\t\t"Fake Dedicated Server {app}"\n'
        f'\t"buildid"\t\t"{build}"\n'
        f'\t"installdir"\t\t"{manifest.parent.parent.name}"\n'
        '\t"UserConfig"\n\t{\n'
        f'\t\t"BetaKey"\t\t"{beta}"\n'
        '\t}\n}\n', encoding="utf-8")


def banner() -> None:
    say("Redirecting stderr to 'logs\\stderr.txt'")
    say("[  0%] Checking for available updates...")
    say("[----] Verifying installation...")
    say("Steam Console Client (c) Valve Corporation - version 1700000000")
    say("-- type 'quit' to exit --")
    say("Loading Steam API...OK")


def progress(delay: float, stop_at: float = 100.0) -> None:
    say(" Update state (0x3) reconfiguring, progress: 0.00 (0 / 0)")
    total = 52_428_800
    for pct in (5.0, 21.5, 43.25, 67.0, 88.5, 100.0):
        if pct > stop_at:
            return
        time.sleep(delay)
        done = int(total * pct / 100)
        say(f" Update state (0x61) downloading, progress: {pct:.2f} ({done} / {total})")
    time.sleep(delay)
    say(f" Update state (0x81) verifying update, progress: 100.00 ({total} / {total})")


def main(argv: list[str]) -> int:
    args = parse(argv)
    delay = float(os.environ.get("FAKE_STEAMCMD_DELAY", "0.3"))
    mode = os.environ.get("FAKE_STEAMCMD_MODE", "")
    if not mode:
        mode = args["beta"] if args["beta"] in MODES else "ok"
    marker = Path(os.environ.get("FAKE_STEAMCMD_STATE")
                  or Path(tempfile.gettempdir()) / "fake_steamcmd_selfupdated")

    if mode == "selfupdate":
        if not marker.exists():
            say("[  0%] Checking for available updates...")
            for kb in (0, 20_661, 41_322):
                time.sleep(delay)
                pct = "----" if kb == 0 else f"{kb * 100 // 41_322:3d}%"
                say(f"[{pct}] Downloading update ({kb:,} of 41,322 KB)...")
            say("[----] Extracting package...")
            say("[----] Installing update...")
            say("[----] Cleaning up...")
            say("[----] Update complete, launching Steamcmd...")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("updated", encoding="utf-8")
            return 7
        marker.unlink(missing_ok=True)
        mode = "ok"

    banner()
    say("Connecting anonymously to Steam Public...OK")
    say("Waiting for client config...OK")
    say("Waiting for user info...OK")
    app, install = args["app"], args["install"]
    if not app or not install:
        say("ERROR! Missing +force_install_dir or +app_update")
        return 2
    manifest = Path(install) / "steamapps" / f"appmanifest_{app}.acf"
    before = read_build(manifest)

    if mode == "hang":
        progress(delay, stop_at=43.25)
        while True:
            time.sleep(60)
    if mode == "uptodate" and before is not None:
        time.sleep(delay)
        say(f"Success! App '{app}' already up to date.")
        return 0
    if mode == "fail":
        progress(delay, stop_at=21.5)
        time.sleep(delay)
        say(f"Error! App '{app}' state is 0x202 after update job.")
        say(f"ERROR! Failed to install app '{app}' (Disk write failure)")
        return 8
    if args["validate"]:
        say(" Update state (0x5) verifying install, progress: 50.00 (1 / 2)")
    progress(delay)
    Path(install).mkdir(parents=True, exist_ok=True)
    write_manifest(manifest, app, (before or 999) + 1, args["beta"])
    say(f"Success! App '{app}' fully installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
