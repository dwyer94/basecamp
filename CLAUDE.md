# Basecamp: notes for Claude

Game-server manager for the prod PC. Read [README.md](README.md), then the doc for the area you're touching in `docs/`.

- Build and test on the dev PC. Dev server on `127.0.0.1:8150`, data in `.\data\`. Test against `tools/fake_server.py`, not real games.
- **Never deploy to the prod PC, change its firewall, tasks, users or Tailscale settings without the owner saying go for that step.** Before any of it, read [docs/04-deployment.md](docs/04-deployment.md) and the machine-specific notes in `CLAUDE.local.md` (not in the repo).
- **Never publish a GitHub release (`scripts/release.ps1`, `gh release create`) without the owner saying go.** A release is what the prod PC installs. Never read, copy or commit the signing key (`%USERPROFILE%\.basecamp-signing\`).
- Migrations are add-only (new tables, new columns with defaults), so rollback to the previous version keeps working.
- Other apps share the host PC. Never reuse their runtimes, folders or ports; the specifics are in `CLAUDE.local.md`.
- **The repo is public.** Secrets (webhook URL, RCON passwords, password hash) live in the data folder, never in the repo; no real webhook URLs, passwords or tokens in tests, fixtures or examples either. Don't put PC names, IP addresses, the tailnet's `*.ts.net` name or other apps on the host PC in tracked files; use placeholders (`<host-pc>`, `<tailscale-ip>`, `<tailnet>`) and keep the real values in `CLAUDE.local.md` / `private/` (gitignored).
- Record design decisions in [docs/06-decisions.md](docs/06-decisions.md); requirement IDs (`R-xx`) are in [docs/01-requirements.md](docs/01-requirements.md).
