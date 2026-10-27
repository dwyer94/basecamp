# 07 · Updating Basecamp itself (GitHub Releases)

Basecamp checks the repo's GitHub Releases, downloads a new version, checks it's genuine, and swaps itself over in place. If the new version doesn't come up healthy, it rolls back on its own.

This is about Basecamp's own code. Game updates are SteamCMD ([02-architecture.md](02-architecture.md)).

## The flow

```
 dev PC                                GitHub                        prod PC
 ──────                                ──────                        ───────
 tests pass
 scripts\release.ps1 1.3.0
   build zip (code + deps)
   sign zip (Ed25519, key on dev PC)
   gh release create v1.3.0 ─────────► Release v1.3.0
                                         basecamp-1.3.0.zip
                                         basecamp-1.3.0.zip.sig
                                                    ◄─── every 6 h: GET /releases/latest
                                                         download → check signature, unpack,
                                                         check hashes, try migrations on a copy
                                                         "1.3.0 ready" banner + Discord
                                                         you tap Install (or auto, if on)
                                                         Basecamp exits, backing up basecamp.db
                                                         launcher starts 1.3.0 (exit code 3)
                                                         1.3.0 healthy in 60 s? keep : roll back
```

## On the prod PC: launcher + version folders

```
C:\Basecamp\
  python\              Python runtime (changes only with a manual reinstall)
  launcher.py          small, stable; what the scheduled task runs
  versions\
    1.2.0\             previous version (kept for rollback)
    1.3.0\             current version: basecamp\ package + lib\ (vendored dependencies)
  current.txt          "1.3.0"
  previous.txt         "1.2.0"
  pending.txt          only during an update (written by Basecamp as it exits)
  trial.txt            only while a new version waits for its health check
  update-failed.json   the last rollback: from, to, why (Basecamp shows it until dismissed)
  launcher.lock        held while the launcher runs, so only one runs
  logs\               launcher.log, basecamp-output.log (Basecamp's stdout/stderr)
```

The scheduled task runs `C:\Basecamp\python\pythonw.exe C:\Basecamp\launcher.py --data C:\BasecampData`. The **launcher** stays running and starts Basecamp as a child process from the folder named in `current.txt`. It's the piece that does the swap, because Windows won't let a running program overwrite its own files.

Launcher rules:

1. Start Basecamp from `versions\<current>\`: `python -s -m basecamp` with `PYTHONPATH=<version>;<version>\lib` and `BASECAMP_ROOT`, `BASECAMP_DATA_DIR`, `BASECAMP_LAUNCHER` (the launcher's version) set (D-56).
2. If Basecamp exits with **code 3** ("update ready") and `pending.txt` names an unpacked version: move current → previous, pending → current, mark it on trial (`trial.txt`), start it. Without a usable `pending.txt`, just start the same version again.
3. **Health check:** a version on trial must answer `http://127.0.0.1:<port>/healthz` with its own version within 60 s. If it exits or doesn't, kill Basecamp's process (only that one, never the tree, so game hosts live), put `db-backups\before-<v>.db` back as `basecamp.db` (removing `-wal`/`-shm`), switch back to the previous version, start it, and write `update-failed.json` (Basecamp shows it on the dashboard and posts to Discord). `trial.txt` survives a launcher restart, so a reboot in the middle still checks.
4. Any other exit: restart after 5 s, doubling up to 5 min while it keeps failing (reset after 5 min up). The 5-minute watchdog task only restarts the launcher itself.
5. Keep the newest 3 version folders, plus current, previous and pending; delete older ones.

The launcher is deliberately small (~300 lines, standard library only) so it almost never needs changing. If a release needs a newer launcher, its manifest says `min_launcher: 2`; Basecamp then says "this update needs a manual reinstall" instead of installing.

## Game servers during an update

Basecamp's exit doesn't stop the games (R-04a): each runs under its own host process, detached from Basecamp (D-25). The new version reads `state.json` and reattaches to every host on start, so friends aren't kicked and the live console, its history and stdin commands carry straight on.

## Host processes and updates

A host lives as long as its game, so after a self-update the new Basecamp talks to hosts started by an older one, and after a rollback an older Basecamp may meet a newer host. Rules (D-26):

- `host.py` is **standard library only**, and Basecamp runs it from a copy in `C:\BasecampData\hosts\bin\host-<hash>.py`, not from `versions\<v>\`, so the launcher can prune old version folders while their hosts are still running.
- Every control connection starts with a hello; the protocol used is `min(Basecamp's, host's)`. `PROTOCOL` in `host.py` and `MIN_HOST_PROTOCOL` / `MAX_HOST_PROTOCOL` in `hostclient.py` hold the numbers.
- Protocol changes are **additive only**: a new op or a new field means a new protocol number; nothing is removed or changes meaning. A host keeps answering every older version, and Basecamp only uses ops that exist in the negotiated one.
- `MIN_HOST_PROTOCOL` is raised only when no host of the older version can still be running (every server restarted since). If it happens anyway, Basecamp adopts that game bare (no console) and says "restart to get the console back".
- The format of `state.json`, `spec.json`, `host.json` and `exit.json` follows the same rule: add keys, never rename or remove them.
- A new host version takes effect for a server at its next start; Basecamp updates never restart games to swap hosts.

## Checking for updates

- `GET https://api.github.com/repos/<owner>/<repo>/releases/latest` every 6 hours, plus a "Check now" button. Uses the ETag so repeat checks are cheap; unauthenticated limit is 60/hour, plenty.
- Draft releases and pre-releases are ignored unless the **beta channel** setting is on.
- The repo is **public** (D-15), so no token is needed. (If it ever goes private: a fine-grained read-only "Contents" token in `secrets.toml`.)
- New version found: it's downloaded and checked straight away (below), then the admin dashboard shows a banner with the release notes and an **Install** button, and Discord gets one message per version. The Settings page has **Check now**, the beta channel and the auto-install hour.
- **Auto-install** setting (off by default): install automatically at a set hour (e.g. 4 am) when a new release is ready. A version that rolled back once is never auto-installed (D-57).
- Settings in `settings.toml`: `update_repo`, `update_api` (only changed to point the dev PC at `tools/fake_releases.py`), `update_check_h` (0 = Check now only), `update_beta`, `update_auto_hour` (D-58).
- Check, Install and Dismiss are admin-only and in the audit log.
- Without the launcher (`python -m basecamp` on the dev PC) updates are checked and staged in `<data>\updates\`, but Install is refused (D-60).

## Is it genuine? (signing)

Basecamp runs whatever it downloads, so it checks two things before unpacking:

1. **Signature.** `release.ps1` signs the zip with an **Ed25519 private key that lives only on the dev PC** (`%USERPROFILE%\.basecamp-signing\release.key`, not in the repo, not in GitHub). Basecamp ships with the matching **public key** (`basecamp/release_key.pub`) and refuses any zip whose `.sig` (the raw 64-byte signature of the whole zip) doesn't match. So even someone who got into your GitHub account couldn't push code onto the prod PC.
2. **Manifest.** Inside the zip, `manifest.json` has the version (must match the tag and be newer than current), `min_launcher`, `python` (e.g. `3.13`), and SHA-256 of every file. Checked after unpacking to `versions\<v>.tmp\`: nothing missing, extra or changed.
3. **Migrations.** The release's migrations are run on a throwaway copy of the DB. Only if all three pass is the folder renamed to `versions\<v>\` and offered (D-54).

Make the key once with `scripts\new-signing-key.ps1`: it writes the private key, writes `release_key.pub`, and prints only the public key. Back the key folder up (password manager or a USB stick). If it's lost, make a new key pair and reinstall once by hand to put the new public key on the prod PC.

## Database changes

- Each version carries numbered migrations (`migrations\0001_init.sql`, ...). On start, Basecamp applies any new ones.
- Before a release is offered, its migrations are tried on a copy of the DB (D-54).
- As it exits for an update, after closing the DB, Basecamp copies `basecamp.db` to `C:\BasecampData\db-backups\before-<version>.db` with SQLite's backup API. Rollback restores it. Keep the last 5. If the copy fails, the update is called off (D-55).
- Rule: migrations only add (new tables, new columns with defaults), so an older version can usually still read a newer DB. Anything destructive waits a release.

## Making a release (dev PC)

```powershell
scripts\release.ps1 1.3.0
```

1. Refuses if the git tree is dirty, not on `main` or not equal to `origin/main`, the tag exists, `CHANGES.md` has no `## 1.3.0` section, or the tests or ruff fail.
2. Sets `__version__` and runs `scripts\build_release.py`. The zip holds the `basecamp\` package (with `web\` and `release_key.pub`), `templates\`, `migrations\`, `launcher.py`, `CHANGES.md`, and dependencies installed with `pip install --target lib --only-binary=:all: --platform win_amd64 --python-version 3.13` (so the prod PC never needs pip or PyPI). It writes `manifest.json` and signs the zip; the key must match `release_key.pub`.
3. Commits and tags `v1.3.0` locally, then asks you to type `yes`.
4. Pushes and runs `gh release create v1.3.0 <zip> <sig> --notes-file <that CHANGES.md section>` (`--prerelease` for a version with a `-`).

`scripts\release.ps1 1.3.0 -NoPublish` runs the checks and the signed build only.

Releases are built on the dev PC rather than GitHub Actions so the signing key never leaves it. Publishing a release isn't a deploy by itself: it only goes onto the prod PC when you tap Install (or auto-install is on).

## Updating the Python runtime or launcher

Rare. Run `scripts\install.ps1` again in an admin PowerShell on the prod PC. It replaces `python\` and `launcher.py`, keeps `versions\`, data and settings. Running hosts use `python\pythonw.exe`, so the files are in use while any server runs: `install.ps1` must either ask for the servers to be stopped first, or install the new runtime beside the old one (e.g. `python-3.13.x\`) and leave the old one until no host uses it.

## Testing on the dev PC

`tools\e2e_selfupdate.py --root C:\BasecampTest --work <scratch folder>` runs the whole flow without GitHub or the real key: a throwaway key, a local fake release server (`tools\fake_releases.py`), real vendored dependencies, the launcher on the base Python, and a fake game that must keep running through an update (v0.1.0 → v0.2.0 with a new migration), a broken v0.3.0 that must roll back by itself, and a v0.4.0 with a bad signature that must be refused.

## First install

`install.ps1` downloads the latest release (same signature check) instead of copying from the repo, so the first install and later updates use the same package.
