# 09 · Debug views

Read-only JSON for looking at a problem on the host PC from the dev PC, without remoting in (batch 4, item 11, D-141). Code: `basecamp/debug.py`, routes in `basecamp/api/app.py`.

## Who can read them

- The owner: a login in `admin_logins`, or a request from the host PC itself.
- Friends get 403, and so does the owner while previewing the friend view (D-143).
- Every call is a `debug` row in the audit log (`/api/audit`, and the owner's Activity under "Config and console"). Friends' Activity never shows them.

From the dev PC, `tailscale serve` adds the dev PC's Tailscale login to the request, so `curl` there is the owner as long as that login is in `admin_logins`. GETs need no `Origin` header.

```bash
curl -s https://<host-pc>.<tailnet>.ts.net/api/debug/health
curl -s "https://<host-pc>.<tailnet>.ts.net/api/debug/logs/basecamp?lines=300"
curl -s "https://<host-pc>.<tailnet>.ts.net/api/debug/logs/launcher?since=2026-10-29T19:00"
curl -s "https://<host-pc>.<tailnet>.ts.net/api/debug/servers/1/console?lines=100"
curl -s https://<host-pc>.<tailnet>.ts.net/api/debug/settings
curl -s https://<host-pc>.<tailnet>.ts.net/api/debug/errors
```

On the host PC itself, the same paths on `http://127.0.0.1:8150`.

## Endpoints

| Path | What |
|---|---|
| `/api/debug` | This list |
| `/api/debug/health` | Version, Python, PID, uptime; the launcher (under it or not, its version and PID, the root folder); the self-update state; SteamCMD found and its queue; the schedule loop and `schedule.json`; the player count poller (running, last round, counts); the backup scheduler; the live status message; Discord sent, failed and waiting; the settings file's last error; the open failure count; RAM; each server's state, PID, uptime, countdown and pending crash restart |
| `/api/debug/logs/basecamp` | `<data>\logs\basecamp.log` and its rotated copies |
| `/api/debug/logs/launcher` | `<root>\logs\launcher.log` and its copies (404 without the launcher) |
| `/api/debug/logs/basecamp-output` | `<root>\logs\basecamp-output.log`, Basecamp's stdout and stderr under the launcher |
| `/api/debug/servers/{id}/console` | That server's `console.log` and copies: the game's output, Basecamp's notes, SteamCMD's output |
| `/api/debug/settings` | The settings in effect, from an allow-list (`debug.SETTINGS`); which secrets are set (main webhook, per-server webhooks, the servers with RCON or REST passwords), never their values; the settings file's state |
| `/api/debug/errors` | WARNING and ERROR lines from `basecamp.log`, the hard failures from Activity (including Discord messages that didn't send), the Discord counters and the live status message's last error |

Log answers are `{"files", "lines", "truncated"}`.

- `lines`: how many of the newest lines (default 200, at most 2000; `errors` defaults to 100).
- `since`: only lines from then on, as an ISO date and time in the host PC's local time (`2026-10-29T19:00`) or epoch seconds. A line without a time of its own (a traceback) goes with the line before it. With both, the newest `lines` lines since then.
- At most the last 4 MB of each file is read.

## What never comes out

- **No secrets.** Every line passes a redactor that replaces with `<hidden>`: each value in `secrets.toml` (webhook URLs, RCON and REST passwords), any Discord webhook URL, and the value after a key with "password", "token" or "secret" in its name (`ServerPassword=x`, `-AdminPassword x`). It can blank a harmless word after "password"; that's on purpose. The tests plant fake secrets and check none comes back.
- **No paths from the request.** Logs are picked by name from a fixed list and server consoles by id; nothing in a request is used as a path. Only files in Basecamp's data folder and the launcher's root are read.
- **Nothing changes.** Every endpoint is a GET and only reads.
