# Basecamp UI mock (batch 3, first piece)

A clickable static mock of the fresher UI, for the owner to review before the real overhaul ([docs/05-plan.md](../../docs/05-plan.md), batch 3). Nothing under `basecamp/` changed.

- Open `index.html` in a browser. Plain HTML/CSS/JS, no build step, placeholder data only.
- The pink bar at the top and the pink dashed boxes are mock-only. They switch between the admin and friend views, show the "connection lost" state, and explain what changed. Untick "Notes" to hide the boxes.
- The sun/moon button in the top bar is the real theme toggle: it follows the system, then light, then dark.
- "All views" lists every screen.

## Views

| View | Hash | Today | In the mock |
|---|---|---|---|
| Servers (dashboard) | `#dashboard` | Row of buttons, then one card per server with tags, address and greyed-out buttons | Summary line (servers up, players online), "Needs you" list, server cards with a state band |
| Needs you | on the dashboard | Crash state on its card, the update banner at the top, unfinished setups as notices | **New.** One list of everything that waits on the owner, with the action next to each. It holds the self-update banner. |
| Server page | `#server` | One long page: head card, console, SteamCMD, firewall, config files, backups, history | Header with state, facts, address and actions; tabs for Console, Config files, Backups, Schedule, Firewall and History. Open each server from the dashboard to see it crashed, updating, counting down or stopped. |
| Config editor | `#config` | Buttons at the top, textarea, "Edit as fields" | Fields or Text switch, filter, Save bar that stays at the bottom, "Save and restart" for a running server, earlier copies |
| Add / edit server | `#edit` | One long form, template box at the top | Named sections with a jump list (chips on a phone), installed-folder pick-list and import first, rare fields folded away, join port picked in the port table, Delete in its own red section |
| New game | `#newgame` | Drop-down, name, folder, then a list of steps | Game tiles, a six-step bar (click the steps in the mock), the start button names the game |
| Templates | `#templates` | Add box, then one card per template | Compact list with source tags (Built-in, Yours, Yours replaces built-in), add by file or paste, check before save |
| Settings | `#prefs` | One long page of cards | Same cards with a jump list; Friends moved near the top |
| Activity | `#activity` | Audit log: a raw table of the last 200 rows | **New.** A feed grouped by day with filters (crashes, friends, updates, backups, config and console), and a count in the menu |
| First run | `#welcome` | "No servers yet. Add one from a template." | **New.** Three steps: SteamCMD found, join address, first game |
| Friend page | `#friend` | Plain cards: name, state, uptime, address, small buttons | **Redesigned for phones.** Big join address with a full-width Copy, big Start for a stopped server, "Down" instead of "Crashed", "How do I join?" help (**new**). Tick the box to see it with friend controls off. |
| Confirm sheet | any risky button | Browser `confirm()` boxes | In-page sheet that says what will happen; destructive ones (Kill, Delete, Stop SteamCMD) get a red button |
| Connection lost | mock bar | "reconnecting…" text in the header | Amber "Reconnecting…" badge in the top bar |

## What changed and why

- **State at a glance.** Every server card starts with a coloured band holding the state word and the one fact that matters for that state: uptime, the countdown, crash count, update progress, or how long it's been stopped. Colour is never the only signal; the word and an icon are always there. The states use their own colours (green, amber, red, blue, grey) and the accent is indigo, so a button is never mistaken for a state.
- **Only the actions that make sense.** Cards show the buttons that fit the current state instead of a full row of greyed-out ones. The primary action is filled: Start for a stopped or crashed server, Restart now during a countdown.
- **Dangerous actions set apart.** Kill is only on the server page, at the end of the action row, behind "Not responding?" and a red outline. Delete server is its own red section at the end of the Edit form, away from Save. Deleting a backup or template uses a red outlined button. All of them go through the confirm sheet.
- **Player count up front.** It's the biggest number on each card, for both the owner and friends.
- **Long pages split up.** The server page becomes tabs; Edit and Settings get a jump list. On a phone the jump list becomes a row of chips.
- **Phone layout for the admin too.** Under 900 px the sidebar becomes a bottom tab bar (Servers, Activity, Templates, Settings) and New game moves to the top bar. Cards stack in one column, and action buttons stretch to full width.
- **Friend page is phone-first.** One column, a large join address on a dashed "ticket" with a full-width Copy button, and big Start/Stop/Restart buttons when friend controls are on.
- **Schedules get their own tab** on the server page, with the next run spelled out ("Next: tomorrow 05:00").
- **Plainer words.** "Activity" instead of "Audit log", "Add existing server" instead of "Add server", Discord copy about messages rather than webhooks where possible.
- **Light and dark** from one set of CSS colour tokens, following the system, with a toggle that's remembered in the browser.
- **Fonts.** Bricolage Grotesque for headings, Instrument Sans for text, JetBrains Mono for the console and addresses. The mock loads them from Google Fonts; see the questions below.

## Open questions for the owner

1. **Fonts.** The real app loads nothing from the internet. Bundle the three fonts with Basecamp (about 150 KB), or use the system fonts (Segoe UI and Cascadia Mono on Windows, San Francisco on iPhone) and lose some character?
2. **Card actions.** Is Restart, Stop and Update on every running card right, or should cards show only one main action and keep the rest on the server page?
3. **Kill.** Is "Not responding? Kill" at the end of the action row the right place, or should it hide behind a "More" menu?
4. **Activity feed.** Is a feed better than the audit table for you? Should it include Discord delivery failures and friend page visits? Should the red count be "new since you last looked" or "crashes and failures only"?
5. **Quick console commands.** Worth adding one-tap commands from the template (save, showplayers, broadcast…)?
6. **Friend wording.** Should friends see "Down" for a crashed server, or the plain "Crashed"? Should a stopped server hide its address?
7. **"How do I join?"** Keep it on the friend page, and should its steps be per game?
8. **Join port.** Pick it with a radio button in the port table (mock) or keep the separate field?
9. **Dashboard summary.** Is "RAM free" useful at the top, or would you rather see something else (CPU, disk free for backups)?
10. **Colour.** Happy with indigo as the accent and the lichen-grey background, or a different direction?
