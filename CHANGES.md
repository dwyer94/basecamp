# Changes

Release notes. `scripts\release.ps1 <version>` publishes the section for that version as the
GitHub release's notes, which the admin dashboard shows next to its Install button.

## 0.1.0

First release: the core supervisor with per-game host processes, the admin and friend
pages over Tailscale, templates, SteamCMD installs and one-click updates, save backups,
crash restart, Discord messages, the config editor, the settings page, and self-update from
GitHub Releases through the launcher.

Game templates: Palworld, Enshrouded and RuneScape: Dragonwilds are checked against running
servers (exes, ports, ready lines, config and save folders; graceful stops still unchecked).
V Rising and Windrose come from the guides only, with what's unchecked in their notes.
