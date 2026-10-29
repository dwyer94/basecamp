# Basecamp UI mock (batch 3, first piece)

A clickable static mock of the fresher UI, for the owner to review before the real overhaul ([docs/05-plan.md](../../docs/05-plan.md), batch 3). Nothing under `basecamp/` changed.

- Open `index.html` in a browser. Plain HTML/CSS/JS, no build step, placeholder data only.
- The pink bar at the top and the pink dashed boxes are mock-only. They switch between the admin and friend views, show the "connection lost" state, and explain what changed. Untick "Notes" to hide the boxes.
- The sun/moon button in the top bar is the real theme toggle: it follows the system, then light, then dark.
- "All views" lists every screen.
- Checked at 375, 1280 and 1920 px wide, light and dark.

Round 1 of the owner's feedback is applied (see "Decisions" below). Round 2, on how much of the admin UI friends see, is still to come; the friend view keeps its current structure until then.

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
| Activity | `#activity` | Audit log: a raw table of the last 200 rows | **New.** A feed grouped by day, with filters (a side column on a desktop), a red "Failure" tag on hard failures and the failure count in the menu |
| First run | `#welcome` | "No servers yet. Add one from a template." | **New.** Three steps: SteamCMD found, join address, first game |
| Friend page | `#friend` | Plain cards: name, state, uptime, address, small buttons | **Redesigned for phones.** Big join address with Copy (full width on a phone), big Start for a stopped server, "Down" instead of "Crashed", the game's last update. Cards sit side by side on a desktop. Tick the box to see it with friend controls off. |
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
- **Friend page is phone-first.** One column on a phone, a large join address on a dashed "ticket" with a full-width Copy button, and big Start/Stop/Restart buttons when friend controls are on. Every server keeps its address, including stopped and down ones.
- **Schedules get their own tab** on the server page, with the next run spelled out ("Next: tomorrow 05:00").
- **Plainer words.** "Activity" instead of "Audit log", "Add existing server" instead of "Add server", Discord copy about messages rather than webhooks where possible.
- **Light and dark** from one set of CSS colour tokens, following the system, with a toggle that's remembered in the browser.

## Notes for the build

- **Fonts are bundled.** The real app loads nothing from the internet, so ship the three fonts with Basecamp: Bricolage Grotesque (headings), Instrument Sans (text) and JetBrains Mono (console, addresses). Put the `.woff2` files (Latin subset, the weights the mock uses: 500/650/750, 400/500/600, 400/500, about 150 KB together) under `basecamp/web/fonts/` with their SIL Open Font License text (`OFL.txt` for each family), and declare them with `@font-face` in `style.css`. No Google Fonts link in the real app; the mock keeps loading them from Google only because it is a single file.
- **Last updated** comes from the Steam app manifest (`steamapps\appmanifest_<appid>.acf`, its `LastUpdated` field, a Unix time). That also covers updates done outside Basecamp, such as SteamCMD run by hand. When there's no manifest (a custom server, or a folder outside a Steam library), fall back to Basecamp's own record of its last successful update for that server. Show it relative on cards ("Updated 2 days ago", full date in the `title`) and as a full date on the server page.
- **Quick commands** need a new optional template field, for example:

  ```json
  "quick_commands": [
    {"label": "Save", "command": "save"},
    {"label": "Players", "command": "showplayers"},
    {"label": "Broadcast…", "command": "broadcast {message}"}
  ]
  ```

  Each is sent over the server's usual console channel (RCON, REST or stdin). A command with `{message}` isn't sent straight away: it goes into the command box for the owner to finish. The row shows only while the server is running and the template lists commands. Older templates without the field keep working (add-only, like the migrations).
- **Activity's red count** is hard failures only: crashes, giving up restarting, failed updates and installs, failed scheduled runs (restart, update, backup), failed Discord messages and failed backups. It is not "new since you looked". Discord delivery failures need to be written to the audit log, which they aren't today.

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

## Open questions

1. **When does a failure stop counting** towards Activity's red count: after 24 hours, once that server runs normally again, or when you dismiss it?
2. **Quick commands for friends.** If round 2 opens the console to friends, should they get the quick commands too, or only some of them (players yes, broadcast no)?
