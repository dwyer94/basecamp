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
                                                         "1.3.0 available" banner + Discord
                                                         you tap Install (or auto, if on)
                                                         download → check signature + hash
                                                         unpack to versions\1.3.0\
                                                         back up basecamp.db
                                                         Basecamp exits ──► launcher starts 1.3.0
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
  pending.txt          only during an update
```

The scheduled task runs `C:\Basecamp\python\pythonw.exe C:\Basecamp\launcher.py`. The **launcher** stays running and starts Basecamp as a child process from the folder named in `current.txt`. It's the piece that does the swap, because Windows won't let a running program overwrite its own files.

Launcher rules:

1. Start Basecamp from `versions\<current>\`.
2. If Basecamp exits with **code 3** ("update ready") and `pending.txt` names a version: move current → previous, pending → current, start the new one.
3. **Health check:** the new version must answer `http://127.0.0.1:8150/healthz` within 60 s. If not, kill it, restore `basecamp.db` from the pre-update copy, switch back to the previous version, start it, and write `update-failed.txt` (Basecamp shows it in the UI and posts to Discord).
4. Any other exit: restart after 5 s (crash loop protection: back off up to 5 min). The 5-minute watchdog task only restarts the launcher itself.
5. Keep the last 3 version folders; delete older ones.

The launcher is deliberately tiny (~150 lines, standard library only) so it almost never needs changing. If a release needs a newer launcher, its manifest says `min_launcher: 2`; Basecamp then says "this update needs a manual reinstall" instead of installing.

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
- New version found: banner on the admin dashboard with the release notes, and a Discord message. **Install** button.
- **Auto-install** setting (off by default): install automatically at a set hour (e.g. 4 am) when a new release is found.

## Is it genuine? (signing)

Basecamp runs whatever it downloads, so it checks two things before unpacking:

1. **Signature.** `release.ps1` signs the zip with an **Ed25519 private key that lives only on the dev PC** (`%USERPROFILE%\.basecamp-signing\release.key`, not in the repo, not in GitHub). Basecamp ships with the matching **public key** and refuses any zip whose `.sig` doesn't match. So even someone who got into your GitHub account couldn't push code onto the prod PC.
2. **Manifest.** Inside the zip, `manifest.json` has the version (must match the tag and be newer than current), `min_launcher`, `python` (e.g. `3.13`), and SHA-256 of every file. Checked after unpacking.

Back up the signing key (password manager or a USB stick). If it's lost, make a new key pair and reinstall once by hand to put the new public key on the prod PC.

## Database changes

- Each version carries numbered migrations (`migrations\0001_init.sql`, ...). On start, Basecamp applies any new ones.
- Before exiting for an update, Basecamp copies `basecamp.db` to `C:\BasecampData\db-backups\before-<version>.db`. Rollback restores it. Keep the last 5.
- Rule: migrations only add (new tables, new columns with defaults), so an older version can usually still read a newer DB. Anything destructive waits a release.

## Making a release (dev PC)

```powershell
scripts\release.ps1 1.3.0
```

1. Refuses if the git tree is dirty, not on `main`, or tests fail.
2. Sets `__version__`, commits, tags `v1.3.0`.
3. Builds the zip: `basecamp\` package, `web\`, `templates\`, `migrations\`, and dependencies installed with `pip install --target lib --only-binary=:all: --platform win_amd64 --python-version 3.13` (so the prod PC never needs pip or PyPI).
4. Writes `manifest.json`, signs the zip.
5. Pushes and runs `gh release create v1.3.0 <zip> <sig> --notes-file CHANGES.md` (notes from a `CHANGES.md` section).

Releases are built on the dev PC rather than GitHub Actions so the signing key never leaves it. Publishing a release isn't a deploy by itself: it only goes onto the prod PC when you tap Install (or auto-install is on).

## Updating the Python runtime or launcher

Rare. Run `scripts\install.ps1` again in an admin PowerShell on the prod PC. It replaces `python\` and `launcher.py`, keeps `versions\`, data and settings. Running hosts use `python\pythonw.exe`, so the files are in use while any server runs: `install.ps1` must either ask for the servers to be stopped first, or install the new runtime beside the old one (e.g. `python-3.13.x\`) and leave the old one until no host uses it.

## First install

`install.ps1` downloads the latest release (same signature check) instead of copying from the repo, so the first install and later updates use the same package.
