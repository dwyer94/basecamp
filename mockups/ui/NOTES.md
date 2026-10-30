# Basecamp UI mock (batch 3, first piece)

A clickable static mock of the fresher UI, for the owner to review before the real overhaul ([docs/05-plan.md](../../docs/05-plan.md), batch 3). Approved; the real page is built from it (D-134 to D-139 in [docs/06-decisions.md](../../docs/06-decisions.md)). Where the build differs from the mock, "Built" at the end says so. The mock itself is kept as it was reviewed.

- Open `index.html` in a browser. Plain HTML/CSS/JS, no build step, placeholder data only.
- The pink bar at the top and the pink dashed boxes are mock-only. They switch between the admin and friend views, show the "connection lost" state, and explain what changed. Untick "Notes" to hide the boxes.
- The sun/moon button in the top bar is the real theme toggle: it follows the system, then light, then dark.
- "Admin / Friend" in the pink bar switches who is looking, on the page you're on, so the same server page can be compared in both modes. In friend mode "Friend controls on" turns friend controls off (read-only).
- "All views" lists every screen.
- Checked at 375, 1280 and 1920 px wide, light and dark.

Rounds 1 and 2 of the owner's feedback are applied (see "Decisions" below).

## Views

| View | Hash | Today | In the mock |
|---|---|---|---|
| Servers (dashboard) | `#dashboard` | Row of buttons, then one card per server with tags, address and greyed-out buttons | Summary (servers up, players online, RAM free) beside the "Needs you" list on a wide screen; server cards with a state band and the game's last update, 3 to 5 across on a desktop |
| Needs you | on the dashboard | Crash state on its card, the update banner at the top, unfinished setups as notices | **New.** One list of everything that waits on the owner, with the action next to each. It holds the self-update banner. |
| Server page | `#server` | One long page: head card, console, SteamCMD, firewall, config files, backups, history | Header with state and actions; tabs for Console, Config files, Backups, Schedule, Firewall and History, with a details column beside them on a desktop (join address, uptime, build, last updated, next restart, last backup, firewall). Open each server from the dashboard to see it crashed, updating, counting down or stopped. |
| Quick commands | in the console | None | **New.** One tap per command from the game's template, above the command box |
| Config editor | `#config` | Buttons at the top, textarea, "Edit as fields" | Fields or Text switch, filter, Save bar that stays at the bottom, "Save and restart" for a running server; earlier copies beside it on a desktop |
| Add / edit server | `#edit` | One long form, template box at the top | Named sections with a jump list (chips on a phone), installed-folder pick-list and import first, rare fields folded away, join port picked in the port table, Delete in its own red section |
| New game | `#newgame` | Drop-down, name, folder, then a list of steps | Game tiles, a six-step bar (click the steps in the mock), the start button names the game |
| Templates | `#templates` | Add box, then one card per template | Compact list with source tags (Built-in, Yours, Yours replaces built-in), with "add by file or paste" beside it on a desktop |
| Settings | `#prefs` | One long page of cards | Same cards with a jump list; Friends moved near the top; two columns of cards on a wide screen |
| Activity | `#activity` | Audit log: a raw table of the last 200 rows | **New.** A feed grouped by day, with filters (a side column on a desktop), a red "Failure" tag on hard failures, the failure count in the menu, and Dismiss / Dismiss all |
| First run | `#welcome` | "No servers yet. Add one from a template." | **New.** Three steps: SteamCMD found, join address, first game |
| Friend view | Admin/Friend switch | A separate page of plain cards: name, state, uptime, address, small buttons | **The admin layout with the owner's parts hidden** (details below): Servers and Activity in the menu, the same cards and server page, "Down" for a crashed server. `#friend` opens it on the dashboard. |
| Confirm sheet | any risky button | Browser `confirm()` boxes | In-page sheet that says what will happen; destructive ones (Kill, Delete, Stop SteamCMD) get a red button |
| Connection lost | mock bar | "reconnecting…" text in the header | Amber "Reconnecting…" badge in the top bar |

## What changed and why

- **State at a glance.** Every server card starts with a coloured band holding the state word and the one fact that matters for that state: uptime, the countdown, crash count, update progress, or how long it's been stopped. Colour is never the only signal; the word and an icon are always there. The states use their own colours (green, amber, red, blue, grey) and the accent is indigo, so a button is never mistaken for a state.
- **Only the actions that make sense.** Cards show the buttons that fit the current state instead of a full row of greyed-out ones. A running card keeps Restart, Stop and Update. The primary action is filled: Start for a stopped or crashed server, Restart now during a countdown.
- **Dangerous actions set apart.** Kill is only on the server page, at the end of the action row, behind "Not responding?" and a red outline. Delete server is its own red section at the end of the Edit form, away from Save. Deleting a backup or template uses a red outlined button. All of them go through the confirm sheet.
- **Player count up front.** It's the biggest number on each card, for both the owner and friends.
- **Last updated.** Admin cards and friend cards show "Updated 2 days ago" (the full date on hover); the server page shows the full date and where it came from.
- **Long pages split up.** The server page becomes tabs; Edit and Settings get a jump list. On a phone the jump list becomes a row of chips.
- **A real desktop layout.** From 900 px up the pages use the width instead of stretching a phone column:
  - the dashboard puts the summary beside "Needs you" and the cards in a grid (3 across at 1280, 5 at 1920);
  - the server page puts the console beside a details column, and the console grows with the window;
  - the config editor, Templates and Activity get a side column (earlier copies, the add form, the filters);
  - forms stop at about 880 px wide; Settings goes to two columns of cards on a wide screen;
  - tables and lists are denser, rows, cards and tiles have hover states, and buttons size to their label instead of filling the width.
- **Phone layout for the admin too.** Under 900 px the sidebar becomes a bottom tab bar (Servers, Activity, Templates, Settings) and New game moves to the top bar. Cards stack in one column, action buttons stretch to full width, and the server page shows the details panel above the tabs.
- **Friends see the admin UI, minus the owner's parts.** One level for every friend, no roles. It's the same pages with things hidden, so it stays phone-friendly the same way the admin view is:
  - **Can do** (when friend controls are on): Start, Stop, Restart and Update, each with the same warning (and, for Update, the save backup) as the owner; the quick commands marked for friends (Palworld: Save and Players, not Broadcast).
  - **Can see:** dashboard cards (state, players, address, uptime, CPU/RAM, last updated, update available), the server page with a read-only console (earlier lines included), the details panel without the install folder (with "last backup"), Schedule (read-only), Firewall status (read-only, no Check again), History without config saves, and Activity without the owner's events (settings changes, config saves, templates, Basecamp updates, logins, Discord). Events a friend did show their Tailscale login. No backup list (decided after round 2, below).
  - **Never see:** Needs you, the Basecamp self-update, the Config files tab, install and exe paths, Templates, Add/Edit, New game, Settings, Kill, Restart now, Cancel restart, Cancel update, typing console commands, Back up now, Restore, Download and Delete.
  - **Friend controls off:** the same pages with no action buttons and no quick commands.
- **Schedules get their own tab** on the server page, with the next run spelled out ("Next: tomorrow 05:00").
- **Plainer words.** "Activity" instead of "Audit log", "Add existing server" instead of "Add server", Discord copy about messages rather than webhooks where possible.
- **Light and dark** from one set of CSS colour tokens, following the system, with a toggle that's remembered in the browser.

## Notes for the build

- **Fonts are bundled.** The real app loads nothing from the internet, so ship the three fonts with Basecamp: Bricolage Grotesque (headings), Instrument Sans (text) and JetBrains Mono (console, addresses). Put the `.woff2` files (Latin subset, the weights the mock uses: 500/650/750, 400/500/600, 400/500, about 150 KB together) under `basecamp/web/fonts/` with their SIL Open Font License text (`OFL.txt` for each family), and declare them with `@font-face` in `style.css`. No Google Fonts link in the real app; the mock keeps loading them from Google only because it is a single file.
- **Last updated** comes from the Steam app manifest (`steamapps\appmanifest_<appid>.acf`, its `LastUpdated` field, a Unix time). That also covers updates done outside Basecamp, such as SteamCMD run by hand. When there's no manifest (a custom server, or a folder outside a Steam library), fall back to Basecamp's own record of its last successful update for that server. Show it relative on cards ("Updated 2 days ago", full date in the `title`) and as a full date on the server page.
- **Quick commands** need a new optional template field, for example:

  ```json
  "quick_commands": [
    {"label": "Save", "command": "save", "friend": true},
    {"label": "Players", "command": "showplayers", "friend": true},
    {"label": "Broadcast…", "command": "broadcast {message}"}
  ]
  ```

  Add `"friend": true` to a command friends may tap too (Palworld marks Save and Players). Each is sent over the server's usual console channel (RCON, REST or stdin). A command with `{message}` isn't sent straight away: it goes into the command box for the owner to finish. The row shows only while the server is running and the template lists commands. Older templates without the field keep working (add-only, like the migrations).
- **Activity's red count** is hard failures only: crashes, giving up restarting, failed updates and installs, failed scheduled runs (restart, update, backup), failed Discord messages and failed backups. It is not "new since you looked". A failure stops counting when the owner dismisses it (Dismiss on each item, or Dismiss all), or when that server runs normally again: a good start clears a crash or gave-up, a good update clears a failed update. Failures with no server, such as a Discord delivery, clear only on dismiss. Discord delivery failures need to be written to the audit log, which they aren't today. The badge and Dismiss are the owner's; friends see the "Failure" tag on server events only.
- **Friend routes** (for the build piece):
  - The friend JSON is no longer a tiny allow-list, but each view's friend JSON must still be built from its own allow-list, never by trimming the admin JSON (D-30). That keeps out config contents, install and exe paths, and secrets (RCON passwords, webhook URLs, the admin logins).
  - The friend routes need tests that check the keys exactly, one per view (dashboard, server page details, backups list, schedule, firewall status, history, Activity).
  - The console stream to friends is read-only: no command route for friends except the template's `friend: true` quick commands, checked against the template on the server, not trusted from the page.
  - Update joins the friend routes, with the same per-server rate limit as Start, Stop and Restart (one friend click per 60 s), and it logs the friend's login.
  - Activity for friends filters out the owner's event kinds on the server, not in the page.

## Decisions from round 1

1. Fonts: bundle them (see "Notes for the build").
2. Card buttons: keep Restart, Stop and Update on every running card.
3. Kill: stays at the end of the server page's action row.
4. Activity: keep the feed, include Discord delivery failures; the red count is hard failures only.
5. Quick commands: yes, from the template.
6. Friends see "Down" for a crashed server; a stopped server keeps its address.
7. "How do I join?" is removed.
8. Join port: radio button in the port table.
9. Dashboard summary keeps "RAM free".
10. Colours stay as they are.

## Decisions from round 2

1. Friends get one level, the same for all; the friend view is the admin layout with parts hidden (see "What changed").
2. Friends may Update, with the same warning and save backup.
3. Friend quick commands: Players and Save, flagged `friend: true` in the template.
4. Red badge: clears on dismiss, or when that server runs normally again; failures with no server clear only on dismiss.

## Decisions after round 2

1. **Backups for friends:** friends don't see the backup list. They see only "last backup" in the details panel. The Backups tab is the owner's.
2. **Console history for friends:** friends see the console's earlier lines, not only new ones. (The build also leaves SteamCMD's output out and blanks the server's folders and launch arguments, which can hold a password; D-135.)

## Built (batch 3, piece 2)

The real page follows the mock. Where it differs, and why:

- **Monograms** are the first letters of two words, or the first two letters of one ("Pa" for Palworld, not "Pw"): the mock's were hand-picked.
- **Dates and times** use the browser's own format (for example "Oct 27, 2026, 07:03 PM" on an English-US browser).
- **Stopped cards** say "since 23:40" as in the mock (batch 4, D-142), in the browser's time format ("since 08:01 PM" on an English-US browser). After the PC restarted while a server ran, when it stopped isn't known, so the fact is empty.
- **Crashed cards** say "gave up after 3 tries" as in the mock (batch 4, D-142), or "restarting at 14:05" while a restart is pending.
- **A countdown card** has **Cancel restart** (Cancel stop, Cancel update) as in the mock (batch 4, D-140), and friends with friend controls on get it too: the owner made friends one trusted level after the mock showed it as the owner's only. The server page has it in the countdown callout, next to Restart now.
- **Updating cards** don't show "Backup 13:58" or "Build A to B" chips; the band shows SteamCMD's progress and the hint says the save is backed up first.
- **The friend firewall tab** shows each port's status with a plain sentence ("The owner has been told") instead of the rule name: friends get the status only.
- **PID** in the details panel is the owner's only (the mock showed it to friends); exit codes too.
- **Stopping steps** in the Edit form are editable rows (action, command, wait) rather than a read-only list with Edit buttons; **Notes** and **Config files** are in the form too (Basics and Running), since the mock left them out.
- **Settings → Friends** has **See what friends see** as in the mock (batch 4, D-143). Rather than the mock's role switch hiding parts, Basecamp serves the owner's browser the friend JSON until **Back to owner view** in the banner at the top, which also says which friend controls are in effect.
- **"Updated outside Basecamp"** Activity rows aren't written: noticing that needs Basecamp to watch the manifests over time. The card and server page do show the manifest's date, which includes such updates.
- **Fonts** are the upstream variable files, unmodified, about 480 KB rather than ~150 KB of Latin subsets (D-138).
- The mock-only parts (pink bar, notes, role switch, views index) aren't in the page.
