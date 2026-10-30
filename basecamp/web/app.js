// The page for everyone (batch 3, D-135): the admin sees it all; friends see the same
// layout with the owner's parts hidden (body[data-role], .admin-only, .friend-only, and
// .ctl for the buttons friend controls allow). Friends never get the owner's data: their
// API answers come from allow-lists on the server.
// This file: shell, routing, server cards, dashboard, server page, Activity, first run.
// pages.js: config editor, add/edit form, new game, templates, settings.
// Hash routes: #dashboard  #server/<id>[/<tab>]  #activity  #welcome
//              #config/<id>/<n>  #new  #edit/<id>  #newgame  #setup/<id>  #templates  #prefs
"use strict";

let me = {};
let servers = [];          // latest list from the status WebSocket
let host = null;           // {ram_free_mb, ram_total_mb, boot_time (admin)}
let failures = 0;          // open hard failures (the red badge, admin only)
let page = null;           // the current view's {update(), close(), dirty()}
let firewallInfo = null;   // GET /api/firewall, cached by Basecamp for a minute
let firewallAt = 0;

const isAdmin = () => Boolean(me.is_admin);
const canControl = () => Boolean(me.can_control);
const serverById = (id) => servers.find((x) => x.id === id);

// -- state: one word, a colour, an icon and the fact that matters ------------------------

const PHASES = { queued: "Queued", backup: "Backing up the save", warning: "Warning players",
  stopping: "Stopping", steamcmd: "SteamCMD running", starting: "Starting again" };
const WARN_VERB = { stop: "Stopping", restart: "Restarting", update: "Updating" };
const NOW_VERB = { stop: "Stop", restart: "Restart", update: "Update" };
const nowVerb = (s) => NOW_VERB[s.stop_warning && s.stop_warning.kind] || "Stop";
const cancelLabel = (s) => `Cancel ${(s.stop_warning && s.stop_warning.kind) || "stop"}`;

function jobActive(s) {
  return Boolean(s.job && s.job.position === 0 && !["queued", "warning"].includes(s.job.phase));
}

function stateOf(s) {
  const w = s.stop_warning;
  if (w && ["running", "starting"].includes(s.state)) {
    return { st: "warn", key: "warn", label: `${WARN_VERB[w.kind] || "Stopping"} in`, stopAt: w.stop_at,
      mark: "dot", fact: "players warned" };
  }
  if (jobActive(s) || s.state === "updating") {
    const job = s.job || {};
    const fact = job.phase === "steamcmd" && job.progress !== null && job.progress !== undefined
      ? `SteamCMD ${Math.round(job.progress)}%` : PHASES[job.phase] || "";
    return { st: "upd", key: "upd", label: job.kind === "install" ? "Installing" : "Updating", mark: "refresh", fact };
  }
  if (s.state === "crashed") {
    return { st: "bad", key: "bad", label: isAdmin() ? "Crashed" : "Down", mark: "alert",
      fact: s.restart_at ? `restarting at ${hhmm(s.restart_at)}` : s.gave_up_after ? `gave up after ${s.gave_up_after} tries` : "not restarting" };
  }
  if (s.state === "starting") return { st: "upd", key: "starting", label: "Starting", mark: "refresh", fact: "" };
  if (s.state === "stopping") return { st: "upd", key: "stopping", label: "Stopping", mark: "refresh", fact: "" };
  if (s.state === "running") {
    return { st: "run", key: "run", label: "Running", mark: "live", fact: s.uptime_s !== null ? `up ${fmtUp(s.uptime_s)}` : "" };
  }
  const queued = s.job && !jobActive(s);
  return { st: "idle", key: "idle", label: "Stopped", mark: "hollow",
    fact: queued ? `${s.job.kind === "install" ? "install" : "update"} queued` : s.state_since ? `since ${sinceWhen(s.state_since)}` : "" };
}

function stateMark(st) {
  if (st.mark === "live") return el("span", { class: "dot live" });
  if (st.mark === "hollow") return el("span", { class: "dot hollow" });
  if (st.mark === "dot") return el("span", { class: "dot" });
  return icon(st.mark);
}

function stateLabel(st) {
  if (!st.stopAt) return el("span", {}, st.label);
  return el("span", {}, `${st.label} `, el("span", { class: "num", "data-stop-at": String(st.stopAt) }, fmtCountdown(st.stopAt)));
}

// Every element with data-stop-at shows the time left; one timer for the whole page.
setInterval(() => {
  for (const node of $$("[data-stop-at]")) node.textContent = fmtCountdown(Number(node.dataset.stopAt));
}, 1000);

function playersBlock(s) {
  const up = ["running", "starting"].includes(s.state) || Boolean(s.stop_warning);
  if (!up) return el("div", { class: "players" }, el("b", {}, "–"), el("span", {}, "offline"));
  const p = s.players;
  if (!p) return el("div", { class: "players" }, el("b", {}, "–"), el("span", {}, "not counted"));
  if (p.count === null || p.count === undefined) {
    const why = s.players_detail && s.players_detail.error;
    return el("div", { class: "players", title: why ? `Unknown: ${why}` : "Not asked yet" }, el("b", {}, "?"), el("span", {}, "unknown"));
  }
  return el("div", { class: "players" }, el("b", {}, String(p.count)), el("span", {}, p.max ? `of ${p.max} on` : "on"));
}

function updatedLine(s) {
  const u = s.last_updated;
  if (!u) return null;
  return el("span", { class: "updated", title: `Game last updated ${fullDate(u.at)}` }, icon("download"), `Updated ${relTime(u.at)}`);
}

function addrBox(address, label = "Copy join address") {
  return el("div", { class: "addr" }, el("code", {}, address),
    el("button", { class: "btn ghost sm", type: "button", "aria-label": label, onclick: () => copyText(address) },
      icon("copy"), "Copy"));
}

function progressBar(job) {
  if (!job || job.progress === null || job.progress === undefined || job.phase !== "steamcmd") return null;
  const v = Math.max(0, Math.min(100, job.progress));
  return el("div", { class: "progress", role: "progressbar", "aria-valuenow": String(Math.round(v)),
    "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": "Update progress" }, el("i", { style: `width:${v}%` }));
}

// -- firewall (D-128): read-only, a minute's cache on the server ---------------------------

const FW_CHIP = { open: ["c-ok", "Firewall open"], too_broad: ["c-warn", "Firewall too broad"],
  blocked: ["c-bad", "Firewall blocks it"], missing: ["c-warn", "Firewall rule missing"] };
const FW_TEXT = { open: "Open to Tailscale", too_broad: "Too broad", blocked: "Blocked", missing: "Missing", unknown: "Unknown" };
const FW_CLASS = { open: "c-ok", too_broad: "c-warn", blocked: "c-bad", missing: "c-warn", unknown: "" };

async function loadFirewall(force = false) {
  if (!force && firewallInfo && Date.now() - firewallAt < 55000) return firewallInfo;
  try { firewallInfo = await api(force ? "POST" : "GET", force ? "/api/firewall/check" : "/api/firewall"); }
  catch (e) { firewallInfo = { error: e.message, servers: {}, notes: [] }; }
  firewallAt = Date.now();
  return firewallInfo;
}

function firewallOf(id) { return firewallInfo && firewallInfo.servers ? firewallInfo.servers[String(id)] : null; }

function firewallChip(s) {
  const f = firewallOf(s.id);
  if (!f || !f.ports || !f.ports.length || !FW_CHIP[f.status]) return null;
  const [cls, text] = FW_CHIP[f.status];
  return el("span", { class: `chip ${cls}`, title: "Windows Firewall, for Tailscale traffic. Not a reachability test." }, icon("shield"), text);
}

// -- actions ---------------------------------------------------------------------------

function leadMinutes(s) {
  const w = s.config && s.config.stop_warning;
  return s.state === "running" && w && w.minutes && w.minutes.length ? w.minutes[0] : 0;
}

function sheetFor(s, action) {
  const n = s.name;
  const lead = leadMinutes(s);
  const warned = isAdmin() ? (lead ? `Players get a ${lead} minute warning first, then ` : "")
    : "Everyone on it gets the game's stop warning first, if it has one, then ";
  const running = ["running", "starting"].includes(s.state);
  const midway = s.job && s.job.phase === "steamcmd";
  const sheets = {
    stop: [`Stop ${n}?`, `${warned}${warned ? "it" : "It"} saves and stops.`, "Stop", false],
    restart: [`Restart ${n}?`, `${warned}${warned ? "it" : "It"} stops and starts again.`, "Restart", false],
    update: [`Update ${n}?`, running ? "Basecamp backs up the save, warns players, stops it, runs SteamCMD and starts it again."
      : "Basecamp backs up the save and runs SteamCMD.", "Update", false],
    install: [`Install ${n}?`, `SteamCMD downloads it into ${s.config ? s.config.install_dir : "its folder"}.`, "Install", false],
    now: [`${nowVerb(s)} ${n} now?`, "Skips the rest of the warning. Anyone still on gets dropped.", `${nowVerb(s)} now`, false],
    cancel: midway ? ["Stop SteamCMD?", `${n} may be left half updated and won't be started.`, "Stop SteamCMD", true]
      : [`Cancel the ${s.job ? s.job.kind : "update"} of ${n}?`, "Nothing has been changed yet.", "Cancel it", false],
    kill: [`Kill ${n}?`, `Ends the process at once without saving. Anything since the last save is lost.${s.stop_warning ? " This also ends the countdown." : ""} Use it only when Stop doesn't work.`, "Kill without saving", true],
  };
  const sheet = sheets[action];
  if (!sheet) return null;
  const [title, body, ok, danger] = sheet;
  const tail = isAdmin() ? "" : " The owner sees in Activity that you did it.";
  return { title, body: body + tail, ok, danger };
}

const DONE = { start: "Starting", stop: "Stopping", restart: "Restarting", update: "Update queued",
  install: "Install queued", now: "Going ahead now", cancel: "Cancelling", kill: "Killed", cancelrestart: "Restart cancelled",
  cancelwarn: "Cancelled; it keeps running" };

async function act(s, action) {
  if (action === "start" && isAdmin() && s.steam && s.steam.failed && !s.job) {
    const go = await confirmSheet({ title: `Start ${s.name} anyway?`,
      body: `The last ${s.steam.last.kind} failed, so its files may be half updated.`, ok: "Start anyway" });
    if (!go) return;
  } else if (!["start", "cancelrestart", "cancelwarn"].includes(action)) {
    const sheet = sheetFor(s, action);
    if (sheet && !(await confirmSheet(sheet))) return;
  }
  try {
    let result;
    if (!isAdmin()) {
      result = await api("POST", `/api/friend/servers/${s.id}/${action === "cancelwarn" ? "countdown/cancel" : action}`);
    } else {
      const path = { cancel: "steam/cancel", now: "stop?now=true", cancelrestart: "stop", cancelwarn: "countdown/cancel" }[action] || action;
      result = await api("POST", `/api/servers/${s.id}/${path}`);
    }
    if (result && result.warnings && result.warnings.length) toast(result.warnings.join(" "));
    else if (result && result.warning_s) toast(`${DONE[action]} in ${Math.round(result.warning_s / 60)} min; players are being warned`);
    else toast(`${DONE[action] || action}: ${s.name}`);
  } catch (e) {
    toast(`${s.name}: ${e.message}`, "bad");
  }
}

function actBtn(s, action, label, { primary = false, ctl = true, cls = "", iconName = null } = {}) {
  return el("button", { type: "button", class: `btn${primary ? " primary" : ""}${ctl ? " ctl" : ""} ${cls}`.trim(),
    onclick: () => act(s, action) }, iconName ? icon(iconName) : null, label);
}

function canUpdate(s) {
  if (isAdmin()) return Boolean(s.steam && s.steam.installed);
  return s.update_available !== null || Boolean(s.last_updated);  // a Steam server, as far as a friend can tell
}

// The buttons that fit the state; a friend's go through .ctl so "friend controls off" hides them.
function cardActions(s) {
  const st = stateOf(s);
  const open = (label = "Open") => el("a", { class: "btn", href: `#server/${s.id}` }, label);
  const out = [];
  if (st.key === "warn") {
    out.push(actBtn(s, "now", `${nowVerb(s)} now`, { primary: true, ctl: false, cls: "admin-only" }));
    out.push(actBtn(s, "cancelwarn", cancelLabel(s)));
    out.push(el("a", { class: "btn friend-only", href: `#server/${s.id}` }, "Open"));
  } else if (st.key === "upd") {
    if (s.job) out.push(actBtn(s, "cancel", `Cancel ${s.job.kind === "install" ? "install" : "update"}`, { ctl: false, cls: "admin-only" }));
    out.push(el("a", { class: `btn${s.job ? " friend-only" : ""}`, href: `#server/${s.id}` }, "Open"));
  } else if (st.key === "bad") {
    out.push(actBtn(s, "start", "Start", { primary: true }));
    if (s.restart_at) out.push(actBtn(s, "cancelrestart", "Cancel restart", { ctl: false, cls: "admin-only" }));
    out.push(open("Open console"));
  } else if (st.key === "run") {
    out.push(actBtn(s, "restart", "Restart"), actBtn(s, "stop", "Stop"));
    if (canUpdate(s) && !s.job) out.push(actBtn(s, "update", "Update"));
  } else if (st.key === "idle") {
    if (isAdmin() && s.steam && !s.steam.installed && !s.job) {
      out.push(actBtn(s, "install", "Install", { primary: true, ctl: false }));
    } else {
      out.push(actBtn(s, "start", "Start", { primary: true }));
      if (canUpdate(s) && !s.job) out.push(actBtn(s, "update", "Update"));
    }
    if (s.job) out.push(actBtn(s, "cancel", `Cancel ${s.job.kind}`, { ctl: false, cls: "admin-only" }));
  } else {
    out.push(open());
  }
  return out;
}

// -- server card ---------------------------------------------------------------------

function cardHint(s, st) {
  if (st.key === "run" || st.key === "warn") {
    const parts = [s.cpu_percent !== null && s.cpu_percent !== undefined ? `CPU ${s.cpu_percent}%` : "",
      s.rss_mb ? `RAM ${fmtMB(s.rss_mb)}` : ""].filter(Boolean);
    return parts.join(", ");
  }
  if (st.key === "bad") {
    if (isAdmin() && s.last_exit_code !== null && s.last_exit_code !== undefined) return `Exit code ${s.last_exit_code}`;
    return "It stopped unexpectedly.";
  }
  if (st.key === "upd" && s.job) return s.job.kind === "install" ? "Installing with SteamCMD." : "Save backed up first. Starts again when done.";
  if (st.key === "idle" && isAdmin() && s.steam) {
    if (!s.steam.installed) return "Not installed yet";
    if (s.steam.build_id) return `Build ${s.steam.build_id}${s.steam.update_available === false ? " up to date" : ""}`;
  }
  return "";
}

function cardChips(s) {
  const chips = [];
  if (s.update_available && !s.job) chips.push(el("span", { class: "chip c-upd" }, icon("download"), "Update available"));
  if (isAdmin() && s.steam && s.steam.failed && !s.job) chips.push(el("span", { class: "chip c-bad" }, icon("alert"), `Last ${s.steam.last.kind} failed`));
  if (s.job && !jobActive(s)) chips.push(el("span", { class: "chip c-upd" }, icon("clock"), `${s.job.kind === "install" ? "Install" : "Update"} queued`));
  if (isAdmin() && s.schedule && s.schedule.restart_at) chips.push(el("span", { class: "chip" }, icon("clock"), `Restarts ${s.schedule.restart_at}`));
  if (isAdmin() && s.schedule && Object.keys(s.schedule.waiting || {}).length) {
    chips.push(el("span", { class: "chip c-warn" }, icon("users"), "Waiting for players to leave"));
  }
  if (isAdmin() && s.adopted && !s.has_console && s.state === "running") chips.push(el("span", { class: "chip", title: "Restart it to get the console back" }, "No console"));
  chips.push(firewallChip(s));
  return chips.filter(Boolean);
}

function serverCard(s) {
  const st = stateOf(s);
  const href = `#server/${s.id}`;
  const hint = cardHint(s, st);
  const showLastLine = isAdmin() && st.key === "bad" && s.last_line;
  const bar = st.key === "upd" ? progressBar(s.job) : null;
  const chips = cardChips(s);
  return el("article", { class: "srv", "data-st": st.st },
    el("a", { class: "band", href }, stateMark(st), stateLabel(st), el("span", { class: "fact" }, st.fact)),
    el("div", { class: "srv-body" },
      el("div", { class: "srv-title" },
        el("span", { class: "mono", "aria-hidden": "true" }, monogram(s.name)),
        el("div", { class: "grow" },
          el("a", { class: "srv-name", href }, s.name),
          hint ? el("div", { class: "hint" }, hint) : null,
          updatedLine(s)),
        playersBlock(s)),
      bar,
      showLastLine ? el("div", { class: "lastline", title: s.last_line }, s.last_line) : null,
      !bar && !showLastLine && s.join_address ? addrBox(s.join_address) : null,
      chips.length ? el("div", { class: "chips" }, chips) : null),
    el("div", { class: "srv-actions" }, cardActions(s)));
}

// -- shell -----------------------------------------------------------------------------

function setFailures(n) {
  failures = n;
  for (const c of $$("[data-failcount]")) { c.textContent = String(n); c.hidden = !n; }
}

function renderWho() {
  const who = $("#who");
  const name = me.name || (me.login === "localhost" ? "This PC" : me.login) || "Friend";
  const initials = monogram(name.replace(/@.*/, "")).toUpperCase();
  who.replaceChildren(...[
    isAdmin() ? null : el("span", { class: "rolebadge" }, "Friend"),
    el("span", { class: "avatar", "aria-hidden": "true" }, initials),
    el("span", { class: "name", title: me.login || "" }, name)].filter(Boolean));
}

function renderFoot() {
  const foot = $("#side-foot");
  if (!isAdmin()) {
    foot.replaceChildren(me.login ? `Signed in as ${me.login}, friend` : "Signed in as a friend");
    return;
  }
  const up = host && host.boot_time ? Date.now() / 1000 - host.boot_time : null;
  const days = up !== null ? Math.floor(up / 86400) : null;
  const upText = up === null ? "" : days >= 1 ? `${days} day${days === 1 ? "" : "s"}` : fmtUp(up);
  foot.replaceChildren(...[`Basecamp ${me.version}`, up !== null ? el("br") : null, up !== null ? `Up ${upText} on the host PC` : null].filter(Boolean));
}

function setNav(name) {
  for (const a of $$("[data-nav]")) {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

function crumb(...parts) {
  const out = [];
  parts.forEach((p, i) => {
    if (i) out.push(icon("chevron"));
    out.push(Array.isArray(p) ? el("a", { href: p[1] }, p[0]) : el("span", {}, p));
  });
  return el("div", { class: "crumb" }, out);
}

function errorView(root, message, back = "#dashboard") {
  root.replaceChildren(el("section", { class: "view narrow" },
    el("div", { class: "callout", "data-st": "bad" }, icon("alert"), el("div", { class: "grow" }, el("div", {}, message))),
    el("div", { class: "btnrow" }, el("a", { class: "btn", href: back }, "Back"))));
}

// -- dashboard -------------------------------------------------------------------------

async function needsYouExtras() {
  const out = { update: null, setups: [] };
  const [u, setups] = await Promise.all([api("GET", "/api/update").catch(() => null), api("GET", "/api/setups").catch(() => [])]);
  out.update = u;
  out.setups = setups || [];
  return out;
}

const STEP_TEXT = { install: "installing", first_start: "first start", values: "waiting for the values it needs",
  firewall: "checking the firewall", ready: "ready to start" };

function attRow(st, iconName, title, hint, buttons = [], extra = null) {
  return el("div", { class: "att", "data-st": st },
    el("span", { class: "ico" }, icon(iconName)),
    el("div", { class: "what" }, el("b", {}, title), hint ? el("div", { class: "hint" }, hint) : null),
    buttons.length ? el("div", { class: "btnrow" }, buttons) : null,
    extra);
}

function needsYouRows(extras) {
  const rows = [];
  for (const s of servers) {
    if (s.state === "crashed" && !s.restart_at) {
      rows.push(attRow("bad", "alert", `${s.name} crashed and Basecamp stopped retrying${s.gave_up_after ? ` after ${s.gave_up_after} tries` : ""}.`,
        `${s.last_exit_code !== null && s.last_exit_code !== undefined ? `Exit code ${s.last_exit_code}. ` : ""}Check the console, then start it again.`,
        [el("a", { class: "btn sm", href: `#server/${s.id}` }, "Open console"),
          el("button", { class: "btn sm primary", type: "button", onclick: () => act(s, "start") }, "Start")]));
    } else if (s.steam && s.steam.failed && !s.job && s.steam.last) {
      rows.push(attRow("bad", "alert", `The last ${s.steam.last.kind} of ${s.name} ${s.steam.last.result}.`, s.steam.last.message,
        [el("a", { class: "btn sm", href: `/api/servers/${s.id}/steam/runs/${s.steam.last.run_id}/log`, target: "_blank", rel: "noopener" }, "SteamCMD output"),
          el("button", { class: "btn sm primary", type: "button", onclick: () => act(s, s.steam.last.kind === "install" ? "install" : "update") }, "Try again")]));
    }
  }
  if (servers.some((s) => s.steam) && me.steamcmd_found === false) {
    rows.push(attRow("warn", "alert", "SteamCMD isn't where Basecamp looks for it.", el("span", {}, "Not found: ", el("code", {}, me.steamcmd || "")),
      [el("a", { class: "btn sm primary", href: "#prefs/s-steam" }, "Set its path")]));
  }
  const u = extras.update;
  if (u) {
    if (u.failure) {
      rows.push(attRow("bad", "alert", `Basecamp ${u.failure.to} wasn't healthy, so it rolled back to ${u.failure.from}.`,
        `${u.failure.reason}${u.failure.db_restored ? " Its database copy was put back too." : ""}`,
        [el("button", { class: "btn sm", type: "button", onclick: () => dismissUpdate() }, "Dismiss")]));
    }
    if (u.install_error) {
      rows.push(attRow("bad", "alert", "Basecamp's update didn't install.", u.install_error,
        [el("button", { class: "btn sm", type: "button", onclick: () => dismissUpdate() }, "Dismiss")]));
    }
    if (u.status === "installing") {
      rows.push(attRow("upd", "download", "Installing Basecamp's update…", "The page reloads when it's back."));
      waitForVersion(u.current);
    } else if (u.ready) {
      const again = u.failed_versions.includes(u.ready_version) ? ` ${u.ready_version} already failed its health check once and rolled back.` : "";
      rows.push(attRow("upd", "download", `Basecamp ${u.ready_version} is ready to install.`,
        u.can_install ? `This PC runs ${u.current}. The page goes away for about a minute; game servers keep running.${again}`
          : "Basecamp isn't running under its launcher here, so it can't install.",
        [el("button", { class: "btn sm", type: "button", onclick: () => dismissUpdate() }, "Not now"),
          u.can_install ? el("button", { class: "btn sm primary", type: "button", onclick: () => installUpdate(u) }, "Install") : null].filter(Boolean),
        u.latest && u.latest.notes ? el("details", {}, el("summary", {}, "Release notes"), el("pre", {}, u.latest.notes)) : null));
    } else if (u.available && u.status === "failed") {
      rows.push(attRow("warn", "alert", `Basecamp ${u.latest.version} is out but can't be installed.`, u.error));
    }
  }
  for (const v of extras.setups) {
    const failed = v.status === "failed";
    rows.push(attRow(failed ? "bad" : "warn", "plus", `Setting up ${v.name}: ${failed ? "stopped at " : ""}${STEP_TEXT[v.step] || v.step}.`,
      v.message || null, [el("a", { class: "btn sm primary", href: `#setup/${v.server_id}` }, "Continue")]));
  }
  return rows;
}

async function dismissUpdate() {
  try { await api("POST", "/api/update/dismiss"); if (page && page.refresh) page.refresh(); }
  catch (e) { toast(e.message, "bad"); }
}

async function installUpdate(u) {
  const again = u.failed_versions.includes(u.ready_version) ? ` ${u.ready_version} already failed its health check once and rolled back.` : "";
  const go = await confirmSheet({ title: `Install Basecamp ${u.ready_version}?`,
    body: `The page goes away for about a minute. Game servers keep running. If ${u.ready_version} isn't healthy, Basecamp rolls back to ${u.current}.${again}`, ok: "Install" });
  if (!go) return;
  try { await api("POST", "/api/update/install"); toast("Installing; the page reloads when Basecamp is back"); waitForVersion(u.current); }
  catch (e) { toast(e.message, "bad"); }
}

let waiting = false;
// After Install, Basecamp exits and the launcher starts the new version: wait for it.
async function waitForVersion(from) {
  if (waiting) return;
  waiting = true;
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const now = await api("GET", "/api/me");
      if (now.version !== from) { location.reload(); return; }
      const u = await api("GET", "/api/update");
      if (u.status !== "installing") { location.reload(); return; }
    } catch (e) { /* down while it restarts */ }
  }
  waiting = false;
  toast("Basecamp hasn't come back after 5 minutes. Check the launcher's log on the host PC.", "bad");
}

// After Restart Basecamp (D-126): wait for a Basecamp that started after this one.
async function waitForRestart(startedAt) {
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const now = await api("GET", "/api/me");
      if (now.started_at !== startedAt) { location.reload(); return; }
    } catch (e) { /* down while it restarts */ }
  }
  toast("Basecamp hasn't come back after 5 minutes. Check the launcher's log on the host PC.", "bad");
}

function dashboard(root) {
  if (isAdmin() && !servers.length) return welcomePage(root);
  let extras = { update: null, setups: [] };
  const top = el("div", { class: "dash-top" });
  const grid = el("div", { class: "grid" });
  root.replaceChildren(el("section", { class: "view" }, top,
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, el("h2", {}, "Servers")),
      el("div", { class: "btnrow admin-only" }, el("a", { class: "btn sm", href: "#new" }, icon("plus"), "Add existing server"))),
    grid));
  const update = () => {
    if (isAdmin() && !servers.length) { route(); return; }
    const up = servers.filter((s) => s.state === "running").length;
    const players = servers.reduce((n, s) => n + (s.players && s.players.count ? s.players.count : 0), 0);
    const rows = isAdmin() ? needsYouRows(extras) : [];
    top.classList.toggle("has-att", rows.length > 0);
    top.replaceChildren(
      el("div", { class: "summary" },
        el("h1", {}, servers.length ? `${up} of ${servers.length} server${servers.length === 1 ? "" : "s"} up` : "No servers yet"),
        el("div", { class: "stats" },
          el("div", { class: "stat" }, el("b", {}, String(players)), el("span", {}, players === 1 ? "player online" : "players online")),
          isAdmin() ? el("div", { class: "stat" }, el("b", {}, String(rows.length)), el("span", {}, rows.length === 1 ? "needs you" : "need you")) : null,
          host ? el("div", { class: "stat" }, el("b", {}, fmtMB(host.ram_free_mb)), el("span", {}, `RAM free of ${Math.round(host.ram_total_mb / 1024)} GB`)) : null)),
      rows.length ? el("section", { class: "attention", "aria-label": "Needs you" }, el("header", {}, el("h2", {}, "Needs you")), rows) : null);
    grid.replaceChildren(...servers.map(serverCard));
    if (!servers.length) grid.replaceChildren(el("p", { class: "empty" }, "No servers yet."));
  };
  const refresh = async () => { extras = isAdmin() ? await needsYouExtras() : extras; update(); };
  const fw = () => loadFirewall().then(update);
  update();
  refresh();
  fw();
  const timer = setInterval(() => { refresh(); fw(); }, 60 * 1000);
  return { update, refresh, close: () => clearInterval(timer) };
}

// -- first run -----------------------------------------------------------------------

function welcomePage(root) {
  setNav("dashboard");
  const hostInput = el("input", { type: "text", id: "w-host", value: me.join_host || "", placeholder: "e.g. 100.64.0.10",
    autocomplete: "off", spellcheck: "false", style: "flex:1 1 160px", "aria-label": "Join host" });
  const saveHost = async () => {
    try {
      await api("PUT", "/api/settings", { join_host: hostInput.value.trim() });
      me = await api("GET", "/api/me");
      toast("Saved");
      welcomePage(root);
    } catch (e) { toast(e.message, "bad"); }
  };
  root.replaceChildren(el("section", { class: "view", id: "v-welcome" },
    el("div", { class: "welcome" },
      el("div", { class: "stack" },
        el("h1", {}, "Set up your first game server"),
        el("p", { class: "muted", style: "max-width:52ch" }, "Basecamp runs your game servers on this PC, restarts them when they crash, backs up saves and lets friends on your tailnet see what's up. Three things and you're done.")),
      el("ol", { class: "steps" },
        el("li", { class: me.steamcmd_found ? "ok" : "" },
          el("b", {}, me.steamcmd_found ? "SteamCMD found" : "Find SteamCMD"),
          el("span", { class: "hint" }, me.steamcmd_found ? el("code", {}, me.steamcmd)
            : el("span", {}, "Not at ", el("code", {}, me.steamcmd || "?"), ". Steam games need it.")),
          me.steamcmd_found ? null : el("div", { class: "btnrow" }, el("a", { class: "btn", href: "#prefs/s-steam" }, "Set its path"))),
        el("li", { class: me.join_host ? "ok" : "" },
          el("b", {}, "Set the join address"),
          el("span", { class: "hint" }, "Your host PC's Tailscale address, so friends get a full address to copy."),
          el("div", { class: "btnrow" }, hostInput, el("button", { class: "btn", type: "button", onclick: saveHost }, "Save"))),
        el("li", {},
          el("b", {}, "Add your first game"),
          el("span", { class: "hint" }, "Pick from the templates. Basecamp installs it and walks you through the rest."),
          el("div", { class: "btnrow" }, el("a", { class: "btn primary", href: "#newgame" }, "New game"),
            el("a", { class: "btn ghost", href: "#new" }, "I already have one installed")))))));
  return { update: () => { if (servers.length && location.hash !== "#welcome") route(); } };
}

// -- server page -------------------------------------------------------------------------

const TAB_DEFS = [["console", "Console", "terminal", false], ["files", "Config files", "file", true],
  ["backups", "Backups", "box", true], ["schedule", "Schedule", "cal", false],
  ["firewall", "Firewall", "shield", false], ["history", "History", "activity", false]];

function serverPage(root, id, firstTab) {
  let detail = null;          // GET /api/servers/<id>: quick commands, last backup, (friend) build and schedule
  let tab = TAB_DEFS.some(([t, , , admin]) => t === firstTab && (!admin || isAdmin())) ? firstTab : "console";
  let lastKey = "";
  const head = el("div", { class: "srvhead" });
  const side = el("aside", { class: "srvside", "aria-label": "Details" });
  const panels = {};
  const tabButtons = TAB_DEFS.map(([t, label, ic, admin]) => el("button", {
    type: "button", role: "tab", class: admin ? "admin-only" : null, "aria-selected": String(t === tab),
    onclick: () => showTab(t) }, icon(ic), label));
  for (const [t] of TAB_DEFS) panels[t] = el("div", { class: "tabpanel", role: "tabpanel", hidden: t !== tab });

  // console
  const lines = el("div", { class: "lines", "aria-live": "off" });
  const quickbar = el("div", { class: `quickbar${isAdmin() ? "" : " ctl"}`, hidden: true });
  const channel = el("select", { "aria-label": "Send by" });
  const input = el("input", { placeholder: "Command, e.g. broadcast Hello", autocomplete: "off", autocapitalize: "off",
    spellcheck: "false", "aria-label": "Console command" });
  const consoleState = el("p", { class: "hint", hidden: true });
  let channelsKey = "";
  let lastSeq = 0;
  let follow = true;
  lines.addEventListener("scroll", () => { follow = lines.scrollTop + lines.clientHeight >= lines.scrollHeight - 20; });
  const addLine = (line) => {
    if (line.seq <= lastSeq) return;
    lastSeq = line.seq;
    const cls = { in: "l-in", sys: "l-sys", steam: "l-steam" }[line.source] || "";
    const prefix = line.source === "in" ? "> " : line.source === "sys" ? "# " : "";
    lines.append(el("div", { class: cls }, prefix + line.text));
    while (lines.childElementCount > 2000) lines.firstElementChild.remove();
    if (follow) lines.scrollTop = lines.scrollHeight;
  };
  const sock = keepSocket(() => `/ws/servers/${id}/console?after=${lastSeq}`, {
    onMessage: (msg) => {
      if (msg.type === "lines") msg.lines.forEach(addLine);
      else if (msg.type === "line") addLine(msg);
      else if (msg.type === "error") toast(msg.message, "bad");
    },
    onState: (up) => { consoleState.hidden = up; consoleState.textContent = "Console reconnecting…"; },
  });
  const send = (text) => {
    if (!text) return;
    sock.send({ type: "command", text, channel: channel.value });
  };
  const form = el("form", { class: "admin-only", onsubmit: (e) => { e.preventDefault(); send(input.value.trim()); input.value = ""; } },
    channel, input, el("button", { class: "btn primary", type: "submit" }, "Send"));
  quickbar.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-n]");
    if (!b) return;
    const q = (detail && detail.quick_commands || []).find((x) => String(x.n) === b.dataset.n);
    if (!q) return;
    if (!isAdmin()) {
      try { await api("POST", `/api/friend/servers/${id}/quick/${q.n}`); toast(`Sent: ${q.label}`); }
      catch (err) { toast(err.message, "bad"); }
      return;
    }
    if (q.command.includes("{message}")) {  // ask for the rest: put it in the box, ready to finish
      input.value = q.command.replace("{message}", "").trimEnd() + " ";
      input.focus();
      return;
    }
    if (q.channel && [...channel.options].some((o) => o.value === q.channel)) sock.send({ type: "command", text: q.command, channel: q.channel });
    else send(q.command);
  });
  panels.console.append(el("div", { class: "console" }, lines, quickbar,
    el("div", { class: "readonly friend-only" }, icon("eye"), "Read-only console. Only the owner can type commands."), form), consoleState);

  const showTab = (t) => {
    tab = t;
    tabButtons.forEach((b, i) => b.setAttribute("aria-selected", String(TAB_DEFS[i][0] === t)));
    for (const [k, p] of Object.entries(panels)) p.hidden = k !== t;
    history.replaceState(null, "", `#server/${id}${t === "console" ? "" : `/${t}`}`);
    if (t === "console") lines.scrollTop = lines.scrollHeight;
    loadTab(t);
  };

  // tabs other than the console load when shown, and again when the state changes
  const loadTab = (t) => {
    const s = serverById(id);
    if (!s) return;
    if (t === "files") loadConfigs(panels.files, id);
    else if (t === "backups") loadBackups(panels.backups, s, () => loadDetail());
    else if (t === "schedule") scheduleTab(panels.schedule, s, detail);
    else if (t === "firewall") firewallTab(panels.firewall, s);
    else if (t === "history") historyTab(panels.history, s);
  };

  const loadDetail = async () => {
    try { detail = await api("GET", `/api/servers/${id}`); } catch (e) { detail = null; }
    update();
    if (tab === "schedule") loadTab("schedule");
  };

  root.replaceChildren(el("section", { class: "view", id: "v-server" }, head,
    el("div", { class: "srvgrid" },
      el("div", {}, el("div", { class: "tabs", role: "tablist" }, tabButtons), Object.values(panels)),
      side)));

  const update = () => {
    const s = serverById(id);
    if (!s) { errorView(root, "No such server.", "#dashboard"); return; }
    const st = stateOf(s);
    renderHead(head, s, st);
    renderSide(side, s, st, detail);
    // console input, channels, quick commands
    const remote = s.remote || { channels: ["stdin"], default: "stdin" };
    const key = remote.channels.join(",");
    if (key !== channelsKey) {
      channelsKey = key;
      channel.replaceChildren(...remote.channels.map((x) =>
        el("option", { value: x, selected: x === remote.default }, x === "stdin" ? "console" : x.toUpperCase())));
    }
    channel.hidden = remote.channels.length < 2;
    const up = !["stopped", "crashed"].includes(s.state);
    input.disabled = !(up && (s.has_console || channel.value !== "stdin"));
    input.placeholder = up ? "Command, e.g. broadcast Hello" : "Start the server to send commands";
    const quick = (detail && detail.quick_commands) || [];
    const quickKey = JSON.stringify(quick);
    if (quickKey !== quickbar.dataset.key) {  // not on every tick: a click mid-render would be lost
      quickbar.dataset.key = quickKey;
      quickbar.replaceChildren(el("span", { class: "lbl" }, "Quick"),
        ...quick.map((q) => el("button", { type: "button", "data-n": String(q.n), title: q.command || q.label }, q.label)));
    }
    quickbar.hidden = !quick.length || s.state !== "running";
    const stateKey = `${s.state} ${s.job ? s.job.phase : ""} ${Boolean(s.stop_warning)}`;
    if (stateKey !== lastKey) {
      const first = !lastKey;
      lastKey = stateKey;
      if (!first) loadDetail();
      if (tab !== "console" && tab !== "schedule") loadTab(tab);
    }
  };
  channel.addEventListener("change", update);
  update();
  loadDetail();
  loadTab(tab);
  loadFirewall().then(update);
  const timer = setInterval(() => { loadFirewall().then(update); }, 60 * 1000);
  return { update, close: () => { sock.close(); clearInterval(timer); } };
}

function renderHead(head, s, st) {
  const bar = st.key === "upd" ? progressBar(s.job) : null;
  const callouts = [];
  if (st.key === "warn") {
    const verb = WARN_VERB[s.stop_warning.kind] || "Stopping";
    callouts.push(el("div", { class: "callout", "data-st": "warn" }, icon("clock"),
      el("div", { class: "grow" }, el("div", {}, el("strong", {}, `${verb} in `, el("span", { class: "num", "data-stop-at": String(s.stop_warning.stop_at) }, fmtCountdown(s.stop_warning.stop_at))),
        ". Players are being warned.")),
      actBtn(s, "now", `${nowVerb(s)} now`, { ctl: false, cls: "sm admin-only" }),
      actBtn(s, "cancelwarn", cancelLabel(s), { cls: "sm" })));
  }
  if (st.key === "bad") {
    callouts.push(el("div", { class: "callout", "data-st": "bad" }, icon("alert"),
      el("div", { class: "grow" }, el("div", {}, s.restart_at
        ? [el("strong", {}, "Crashed"), `, so Basecamp starts it again at ${hhmm(s.restart_at)}.`]
        : [el("strong", {}, isAdmin() ? "Crashed" : "Down"), isAdmin()
          ? `. Basecamp ${s.gave_up_after ? `gave up after ${s.gave_up_after} tries and ` : ""}isn't restarting it.${s.last_exit_code !== null && s.last_exit_code !== undefined ? ` Last exit code ${s.last_exit_code}.` : ""} The console below has its last output.`
          : ". The owner has been told; you can start it again."]))));
  }
  if (st.key === "upd" && s.job) {
    callouts.push(el("div", { class: "callout", "data-st": "upd" }, icon("refresh"),
      el("div", { class: "grow" }, el("div", {}, el("strong", {}, `${st.label}${st.fact ? `: ${st.fact}` : ""}`),
        s.job.kind === "update" ? ". The save is backed up first. It starts again when the update is done, if it was running." : "."), bar)));
  }
  if (isAdmin() && s.steam && s.steam.failed && !s.job && s.steam.last) {
    callouts.push(el("div", { class: "callout", "data-st": "bad" }, icon("alert"),
      el("div", { class: "grow" }, el("div", {}, el("strong", {}, `Last ${s.steam.last.kind} ${s.steam.last.result}`), `: ${s.steam.last.message} `,
        el("a", { href: `/api/servers/${s.id}/steam/runs/${s.steam.last.run_id}/log`, target: "_blank", rel: "noopener" }, "SteamCMD output")))));
  }
  if (isAdmin() && s.steam && !s.steam.steamcmd_found) {
    callouts.push(el("div", { class: "callout", "data-st": "warn" }, icon("alert"),
      el("div", { class: "grow" }, el("div", {}, "SteamCMD isn't at ", el("code", {}, s.steam.steamcmd), ". ", el("a", { href: "#prefs/s-steam" }, "Set its path"), "."))));
  }
  if (isAdmin() && s.adopted && !s.has_console && s.state === "running") {
    callouts.push(el("div", { class: "callout", "data-st": "idle" }, icon("terminal"),
      el("div", { class: "grow" }, el("div", {}, "Picked up after Basecamp restarted, without its console. Restart it to get the console back."))));
  }
  const active = ["starting", "running", "stopping", "updating"].includes(s.state);
  const actions = [];
  if (["idle", "bad"].includes(st.key) && !(isAdmin() && s.steam && !s.steam.installed)) {
    actions.push(actBtn(s, "start", "Start", { primary: true, iconName: "play" }));
  }
  if (isAdmin() && st.key === "idle" && s.steam && !s.steam.installed && !s.job) actions.push(actBtn(s, "install", "Install", { primary: true, ctl: false, iconName: "download" }));
  if (st.key === "bad" && s.restart_at) actions.push(actBtn(s, "cancelrestart", "Cancel restart", { ctl: false, cls: "admin-only" }));
  if (st.key === "run") actions.push(actBtn(s, "restart", "Restart", { iconName: "refresh" }), actBtn(s, "stop", "Stop", { iconName: "stop" }));
  if (["run", "idle"].includes(st.key) && canUpdate(s) && !s.job) actions.push(actBtn(s, "update", "Update", { iconName: "download" }));
  // During an update's countdown the callout's Cancel update does it (and says who).
  if (s.job && st.key !== "warn") actions.push(actBtn(s, "cancel", `Cancel ${s.job.kind === "install" ? "install" : "update"}`, { ctl: false, cls: "admin-only" }));
  if (isAdmin() && s.steam && s.steam.installed && !s.job) {
    actions.push(el("button", { class: "btn ghost", type: "button", disabled: s.steam.checking || !s.steam.steamcmd_found,
      onclick: async () => { try { await api("POST", `/api/servers/${s.id}/steam/check`); toast(`Asking Steam about ${s.name}`); } catch (e) { toast(e.message, "bad"); } } },
    icon("refresh"), s.steam.checking ? "Asking Steam…" : "Check for update"));
  }
  actions.push(el("a", { class: "btn ghost admin-only", href: `#edit/${s.id}` }, icon("sliders"), "Edit settings"));
  actions.push(el("span", { class: "sep" }));
  if (isAdmin() && active) {
    actions.push(el("div", { class: "force" }, el("span", { class: "hint" }, "Not responding?"),
      el("button", { class: "btn danger sm", type: "button", onclick: () => act(s, "kill") }, icon("bolt"), "Kill")));
  }
  head.replaceChildren(
    crumb(["Servers", "#dashboard"], s.name),
    el("div", { class: "top" },
      el("span", { class: "mono", "aria-hidden": "true" }, monogram(s.name)),
      el("div", { class: "grow" }, el("h1", {}, s.name),
        el("div", {}, el("span", { class: "pill", "data-st": st.st }, stateMark(st), stateLabel(st)),
          el("span", { class: "rolebadge friend-only", style: "margin-left:8px" }, "Friend view"))),
      ["run", "warn"].includes(st.key) ? playersBlock(s) : null),
    ...callouts,
    el("div", { class: "actionbar" }, actions));
}

function renderSide(side, s, st, detail) {
  const kv = [];
  const row = (k, v, cls = null) => { if (v === null || v === undefined || v === "") return; kv.push(el("dt", { class: cls }, k), el("dd", { class: cls }, v)); };
  if (["run", "warn"].includes(st.key)) {
    row("Up", s.uptime_s !== null ? fmtUp(s.uptime_s) : "");
    row("CPU, RAM", [s.cpu_percent !== null && s.cpu_percent !== undefined ? `${s.cpu_percent}%` : "", s.rss_mb ? fmtMB(s.rss_mb) : ""].filter(Boolean).join(", "));
  }
  if (["idle", "bad"].includes(st.key) && s.state_since) row(st.key === "bad" ? `${st.label} since` : "Stopped since", whenShort(s.state_since));
  const build = isAdmin() ? s.steam && s.steam.build_id : detail && detail.build_id;
  const latest = isAdmin() ? s.steam && s.steam.latest && s.steam.latest.build_id : detail && detail.latest_build;
  row("Build", build);
  if (s.last_updated) {
    row("Updated", el("span", {}, fullDate(s.last_updated.at),
      el("div", { class: "hint" }, s.last_updated.source === "steam" ? "From Steam's app manifest" : "Basecamp's last update")));
  }
  if (latest) row("Steam has", s.update_available ? `${latest}, newer` : "Up to date");
  const schedule = isAdmin() ? s.schedule : detail && detail.schedule;
  row("Next restart", schedule && schedule.restart_at ? cap(nextAt(schedule.restart_at)) : "None set");
  if (schedule && schedule.update_at) row("Next update", cap(nextAt(schedule.update_at)));
  if (detail) row("Last backup", detail.last_backup ? whenShort(detail.last_backup) : "None yet");
  const f = firewallOf(s.id);
  if (f && f.ports && f.ports.length) row("Firewall", el("span", { class: `chip ${FW_CLASS[f.status] || ""}` }, FW_TEXT[f.status] || f.status));
  if (isAdmin()) {
    row("PID", s.pid ? String(s.pid) : "none");
    if (s.config && s.config.install_dir) row("Install folder", el("code", { style: "font-weight:500" }, s.config.install_dir));
  }
  const discord = s.discord_line || (s.join_address ? `${s.name}: ${s.join_address}` : "");
  side.replaceChildren(el("div", { class: "panel" },
    el("div", { class: "field" },
      el("span", { class: "flabel" }, "Join address"),
      s.join_address ? addrBox(s.join_address) : el("p", { class: "hint" }, isAdmin()
        ? el("span", {}, me.join_host ? "No join port set. " : "No join host set. ", el("a", { href: me.join_host ? `#edit/${s.id}` : "#prefs/s-join" }, "Set it"), ".")
        : "Not set by the owner yet."),
      discord && s.join_address ? el("div", { class: "addr-actions" },
        el("button", { class: "btn sm", type: "button", onclick: () => copyText(discord, "Copied for Discord") }, icon("chat"), "Copy for Discord")) : null),
    el("dl", { class: "kv" }, kv)));
}

function cap(text) { return text ? text[0].toUpperCase() + text.slice(1) : text; }

// -- server page tabs ------------------------------------------------------------------

async function loadConfigs(box, id) {
  let res;
  try { res = await api("GET", `/api/servers/${id}/configs`); } catch (e) { box.replaceChildren(callout("bad", e.message)); return; }
  box.replaceChildren(el("div", { class: "panel" }, res.files.length ? el("div", { class: "list" }, res.files.map((f) =>
    el("div", { class: "row" }, icon("file"),
      el("div", { class: "grow" }, el("b", {}, f.name), el("div", { class: "hint" }, el("code", {}, f.path || f.entry))),
      el("span", { class: "hint" }, f.error ? f.error : f.exists ? `${fmtSize(f.size_bytes)}, changed ${relTime(f.modified)}` : "Doesn't exist yet"),
      f.error ? null : el("a", { class: "btn sm", href: `#config/${id}/${f.n}` }, f.exists ? "Open" : "Create"))))
    : el("p", { class: "hint" }, "None listed. ", el("a", { href: `#edit/${id}` }, "Add paths under Edit settings"), "."),
  el("p", { class: "hint" }, `Each save keeps the old file; the newest ${res.keep} copies are kept.`)));
}

const BACKUP_WHY = { scheduled: "Schedule", manual: "By hand", "pre-update": "Before update", "pre-restore": "Before restore" };

async function loadBackups(box, s, changed) {
  const id = s.id;
  let res;
  try { res = await api("GET", `/api/servers/${id}/backups`); } catch (e) { box.replaceChildren(callout("bad", e.message)); return; }
  const stopped = ["stopped", "crashed"].includes(s.state);
  let busy = false;
  const again = () => { const now = serverById(id); if (now) loadBackups(box, now, changed); changed(); };
  const backupNow = async (btn) => {
    busy = true; btn.disabled = true; btn.textContent = "Working…";
    try { const out = await api("POST", `/api/servers/${id}/backups`); toast(`Backup made: ${out.backup.name}`); }
    catch (e) { toast(e.message, "bad"); }
    busy = false; again();
  };
  const restore = async (b) => {
    if (!(await confirmSheet({ title: "Restore this backup?", body: `${whenShort(b.created)}. The current save is backed up first, then replaced.`, ok: "Restore" }))) return;
    try { const out = await api("POST", `/api/servers/${id}/backups/${encodeURIComponent(b.name)}/restore`); toast(out.safety_backup ? `Restored. The save before it: ${out.safety_backup}` : "Restored"); }
    catch (e) { toast(e.message, "bad"); }
    again();
  };
  const remove = async (b) => {
    if (!(await confirmSheet({ title: "Delete this backup?", body: "The zip is removed from the backups folder. This can't be undone.", ok: "Delete backup", danger: true }))) return;
    try { await api("DELETE", `/api/servers/${id}/backups/${encodeURIComponent(b.name)}`); toast("Backup deleted"); }
    catch (e) { toast(e.message, "bad"); }
    again();
  };
  const schedule = res.every_h > 0 ? `Every ${res.every_h} h while running, keeping the newest ${res.keep}` : `Only by hand and before updates, keeping the newest ${res.keep}`;
  box.replaceChildren(el("div", { class: "panel" },
    el("header", {}, el("h3", {}, schedule),
      el("button", { class: "btn primary sm", type: "button", disabled: !res.save_dir_exists, onclick: (e) => backupNow(e.currentTarget) }, "Back up now")),
    !res.save_dir ? callout("warn", el("span", {}, "No save folder set. ", el("a", { href: `#edit/${id}` }, "Set one"), " to make backups."))
      : !res.save_dir_exists ? callout("warn", el("span", {}, "Save folder not found: ", el("code", {}, res.save_dir))) : null,
    el("p", { class: "hint" }, res.save_dir ? el("span", {}, "From ", el("code", {}, res.save_dir), ". ") : null,
      stopped ? "Restore puts one back; the current save is backed up first." : "Stopping the server unlocks Restore.", " Stored in ", el("code", {}, res.folder), "."),
    res.backups.length ? el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Made"), el("th", {}, "Size"), el("th", {}, "Why"), el("th", {}, el("span", { hidden: true }, "Actions")))),
      el("tbody", {}, res.backups.map((b) => el("tr", {},
        el("td", { class: "num", title: fullDate(b.created) }, whenShort(b.created)),
        el("td", { class: "num" }, fmtSize(b.size_bytes)),
        el("td", {}, BACKUP_WHY[b.reason] || b.reason),
        el("td", {}, el("div", { class: "btnrow", style: "justify-content:flex-end" },
          el("button", { class: "btn sm", type: "button", disabled: busy || !stopped || !res.save_dir, title: stopped ? "" : "Stop the server first", onclick: () => restore(b) }, "Restore"),
          el("a", { class: "btn sm ghost", href: `/api/servers/${id}/backups/${encodeURIComponent(b.name)}/download`, download: b.name }, icon("download"), "Download"),
          el("button", { class: "btn sm ghost danger", type: "button", "aria-label": "Delete backup", title: "Delete backup", onclick: () => remove(b) }, icon("trash")))))))))
      : el("p", { class: "hint" }, "No backups yet.")));
}

function scheduleTab(box, s, detail) {
  const schedule = isAdmin() ? s.schedule : detail && detail.schedule;
  const cfg = s.config || {};
  const restartAt = isAdmin() ? cfg.restart_at || "" : schedule ? schedule.restart_at : "";
  const updateAt = isAdmin() ? cfg.update_at || "" : schedule ? schedule.update_at : "";
  const players = isAdmin() ? cfg.schedule_players || "ignore" : schedule ? schedule.players : "ignore";
  const ro = !isAdmin();
  const restartIn = el("input", { type: "time", id: "sch-restart", value: restartAt, disabled: ro });
  const updateIn = el("input", { type: "time", id: "sch-update", value: updateAt, disabled: ro });
  const choice = (value, title, hint) => el("label", {}, el("input", { type: "radio", name: "sch-players", value, checked: players === value, disabled: ro }),
    el("span", {}, el("b", {}, title), hint ? el("span", { class: "hint" }, hint) : null));
  const waiting = schedule && Object.keys(schedule.waiting || {});
  const save = async (btn) => {
    btn.disabled = true;
    const body = { ...cfg, restart_at: restartIn.value, update_at: updateIn.value,
      schedule_players: $("input[name=sch-players]:checked", box).value };
    try { await api("PUT", `/api/servers/${s.id}`, body); toast("Schedule saved"); }
    catch (e) { toast(e.message, "bad"); }
    btn.disabled = false;
  };
  const steam = isAdmin() ? Boolean(s.steam) : Boolean(detail && (detail.build_id || s.last_updated));
  box.replaceChildren(
    waiting && waiting.length ? callout("warn", `The scheduled ${waiting.join(" and ")} is waiting for players to leave.`) : null,
    el("div", { class: "split" },
      el("div", { class: "panel" }, el("h3", {}, "Daily restart"),
        el("div", { class: "field" }, el("label", { for: "sch-restart" }, "Time on the host PC"), restartIn),
        el("p", { class: "hint" }, restartAt ? `Next: ${nextAt(restartAt)}, with the stop warning. Only while it's running.` : "Off. Only while it's running, with the stop warning.")),
      el("div", { class: "panel" }, el("h3", {}, "Daily update"),
        el("div", { class: "field" }, el("label", { for: "sch-update" }, "Time on the host PC"), updateIn),
        el("p", { class: "hint" }, !steam ? "Steam servers only." : updateAt
          ? `Only when Steam has a newer build${s.update_available ? `. Steam has one now, so it runs ${nextAt(updateAt)}` : ` (checked every 6 h). Next check-in ${nextAt(updateAt)}`}.`
          : "Off. Only when Steam has a newer build (checked every 6 h)."))),
    el("div", { class: "panel" }, el("h3", {}, "While players are on"),
      el("div", { class: "choice" },
        choice("ignore", "Go ahead", "They get the stop warning first."),
        choice("skip", "Skip that day", null),
        choice("delay", "Wait until nobody's on", "Checks every 5 min. After 2 h it goes ahead anyway.")),
      el("p", { class: "hint" }, "Needs a player count: an unknown count counts as nobody on."),
      el("div", { class: "btnrow admin-only" }, el("button", { class: "btn primary", type: "button", onclick: (e) => save(e.currentTarget) }, "Save schedule")),
      el("p", { class: "hint friend-only" }, "Only the owner can change the schedule.")));
}

const FW_FRIEND = { open: "Friends can get in through it.", too_broad: "Allows any address. The owner has been told.",
  blocked: "A rule blocks it. The owner has been told.", missing: "No rule lets it in yet. The owner has been told.", unknown: "" };

async function firewallTab(box, s) {
  const render = () => {
    const f = firewallOf(s.id);
    if (!firewallInfo) { box.replaceChildren(el("p", { class: "hint" }, "Reading the firewall rules…")); return; }
    const check = el("button", { class: "btn sm admin-only", type: "button", onclick: async (e) => {
      e.currentTarget.disabled = true; e.currentTarget.textContent = "Checking…";
      await loadFirewall(true); render(); if (page && page.update) page.update();
    } }, icon("refresh"), "Check again");
    if (!f) { box.replaceChildren(el("div", { class: "panel" }, callout("warn", firewallInfo.error || "Not checked yet."), el("div", { class: "btnrow" }, check))); return; }
    box.replaceChildren(el("div", { class: "panel" },
      el("header", {}, el("h3", {}, "Windows Firewall, for Tailscale traffic"), check),
      f.ports.length ? el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Port"), el("th", {}, "Status"), el("th", {}, "Rule"))),
        el("tbody", {}, f.ports.map((p) => el("tr", {},
          el("td", { class: "num" }, `${p.protocol.toUpperCase()} ${p.port}`, p.label ? el("div", { class: "hint" }, p.label) : null),
          el("td", {}, el("span", { class: `chip ${FW_CLASS[p.status] || ""}` }, FW_TEXT[p.status] || p.status)),
          el("td", { class: "hint" }, isAdmin() ? p.detail : FW_FRIEND[p.status] || ""))))))
        : el("p", { class: "hint" }, "No ports listed."),
      isAdmin() && f.note ? callout("warn", f.note) : null,
      ...(isAdmin() ? f.commands || [] : []).map((c) => el("div", { class: "field" },
        el("span", { class: "flabel" }, "To add the missing rule, run this in an admin PowerShell in Basecamp's program folder on the host PC"),
        el("div", { class: "addr" }, el("code", {}, c), el("button", { class: "btn ghost sm", type: "button", onclick: () => copyText(c, "Copied the command") }, icon("copy"), "Copy")))),
      ...(isAdmin() ? firewallInfo.notes || [] : []).map((n) => el("p", { class: "hint" }, n)),
      el("p", { class: "hint" }, "This only reads the firewall's rules. Tailscale's access rules or the game's listen address can still keep friends out, so it isn't a reachability test.",
        firewallInfo.checked_at ? ` Checked ${hhmm(firewallInfo.checked_at)}.` : "")));
  };
  render();
  await loadFirewall();
  render();
}

async function historyTab(box, s) {
  let res;
  try { res = await api("GET", `/api/activity?server_id=${s.id}&limit=30`); } catch (e) { box.replaceChildren(callout("bad", e.message)); return; }
  box.replaceChildren(el("div", { class: "panel" }, res.items.length ? el("div", { class: "list" }, res.items.map((item) => {
    const ev = describe(item);
    return el("div", { class: "row" }, el("div", { class: "grow" }, el("b", {}, ev.short), ` by ${whoText(item)}`,
      item.failure ? el("span", { class: "chip c-bad", style: "margin-left:6px" }, "Failure") : null,
      ev.detail ? el("div", { class: "hint" }, ev.detail) : null),
    el("span", { class: "hint num", title: fullDate(item.ts) }, whenShort(item.ts)));
  })) : el("p", { class: "hint" }, "Nothing yet."),
  el("div", { class: "btnrow" }, el("a", { class: "btn sm ghost", href: "#activity" }, "All activity"))));
}

function callout(st, text) {
  return el("div", { class: "callout", "data-st": st }, icon(st === "run" ? "check" : "alert"), el("div", { class: "grow" }, el("div", {}, text)));
}

// -- Activity (D-134) -------------------------------------------------------------------

// What a row says: [icon, state colour, sentence after the server name, short label].
const ACTIONS = {
  start: ["play", "run", "started", "Started"], "friend.start": ["users", "run", "started", "Started"],
  stop: ["stop", "idle", "stopped", "Stopped"], "friend.stop": ["users", "idle", "stopped", "Stopped"],
  "stop.now": ["stop", "idle", "stopped without waiting for the warning", "Stopped now"],
  restart: ["refresh", "idle", "restarted", "Restarted"], "friend.restart": ["users", "idle", "restarted", "Restarted"],
  kill: ["bolt", "bad", "killed", "Killed"],
  "countdown.cancel": ["stop", "run", "countdown cancelled; it kept running", "Countdown cancelled"],
  "friend.cancel": ["users", "run", "countdown cancelled; it kept running", "Countdown cancelled"],
  update:["download", "upd", "update started", "Update started"], "friend.update": ["users", "upd", "update started", "Update started"],
  install: ["download", "upd", "install started", "Install started"],
  "steam.update.ok": ["download", "run", "updated", "Updated"], "steam.install.ok": ["download", "run", "installed", "Installed"],
  "steam.update.failed": ["download", "bad", "update failed", "Update failed"], "steam.install.failed": ["download", "bad", "install failed", "Install failed"],
  "steam.update.cancelled": ["download", "idle", "update cancelled", "Update cancelled"], "steam.install.cancelled": ["download", "idle", "install cancelled", "Install cancelled"],
  "steam.cancel": ["download", "idle", "SteamCMD run cancelled", "Cancelled SteamCMD"], "steam.check": ["refresh", "idle", "asked Steam for the latest build", "Build check"],
  crash: ["alert", "bad", "crashed", "Crashed"], "crash.gave_up": ["alert", "bad", "crashed too often; Basecamp stopped retrying", "Gave up restarting"],
  "start.failed": ["alert", "bad", "couldn't be started", "Start failed"],
  backup: ["box", "run", "backup made", "Backup made"], "backup.scheduled": ["box", "run", "backup made", "Backup made"],
  "backup.failed": ["box", "bad", "backup failed", "Backup failed"], "backup.restore": ["box", "warn", "backup restored", "Backup restored"],
  "backup.delete": ["trash", "idle", "backup deleted", "Backup deleted"], "backup.download": ["download", "idle", "backup downloaded", "Backup downloaded"],
  console: ["terminal", "idle", "console:", "Console"], "friend.command": ["terminal", "idle", "quick command:", "Quick command"],
  "schedule.restart": ["clock", "warn", "restart started by the schedule", "Scheduled restart"],
  "schedule.update": ["clock", "upd", "update queued by the schedule", "Scheduled update"],
  "schedule.restart.skipped": ["clock", "idle", "scheduled restart skipped: players on", "Restart skipped"],
  "schedule.update.skipped": ["clock", "idle", "scheduled update skipped: players on", "Update skipped"],
  "schedule.restart.failed": ["clock", "bad", "scheduled restart failed", "Scheduled restart failed"],
  "schedule.update.failed": ["clock", "bad", "scheduled update failed", "Scheduled update failed"],
  "notify.failed": ["chat", "bad", "", "Discord message failed"], "notify.test": ["chat", "run", "", "Discord test"],
  "notify.webhook": ["chat", "idle", "", "Discord webhook"],
  "config.save": ["file", "idle", "config saved:", "Config saved"], "config.restore": ["file", "idle", "config copy restored:", "Config restored"],
  "config.read": ["file", "idle", "config opened:", "Config opened"], "config.refused": ["file", "warn", "config path refused:", "Config refused"],
  "config.invalid": ["file", "warn", "config not saved:", "Config not saved"],
  "server.add": ["plus", "idle", "added", "Added"], "server.update": ["sliders", "idle", "settings saved", "Settings saved"],
  "server.delete": ["trash", "idle", "deleted from Basecamp", "Deleted"],
  settings: ["sliders", "idle", "", "Settings saved"], "settings.reload": ["sliders", "idle", "", "Settings file read"],
  "settings.backups_to_all": ["sliders", "idle", "", "Backup defaults to all"],
  "template.add": ["template", "idle", "", "Template added"], "template.replace": ["template", "idle", "", "Template replaced"],
  "template.override": ["template", "idle", "", "Template added"], "template.delete": ["template", "idle", "", "Template deleted"],
  "template.refused": ["template", "warn", "", "Template refused"],
  "update.check": ["download", "idle", "", "Update check"], "update.install": ["download", "upd", "", "Basecamp update"],
  "update.refused": ["download", "warn", "", "Basecamp update refused"], "update.dismiss": ["download", "idle", "", "Update dismissed"],
  "basecamp.restart": ["refresh", "idle", "", "Basecamp restarted"], "basecamp.restart.refused": ["refresh", "warn", "", "Restart refused"],
  "setup.start": ["plus", "idle", "new game setup started", "Setup started"], "setup.finish": ["plus", "idle", "new game setup closed", "Setup closed"],
  "setup.retry": ["plus", "idle", "setup step tried again", "Setup retried"], "setup.values": ["plus", "idle", "setup values written", "Setup values"],
  "setup.next": ["plus", "idle", "setup moved on", "Setup next"],
  debug: ["terminal", "idle", "debug view read:", "Debug read"],
  "view.friend": ["eye", "idle", "", "Friend view preview"], "view.owner": ["eye", "idle", "", "Friend view preview ended"],
  "rcon.password": ["shield", "idle", "RCON password changed", "RCON password"], "rest.password": ["shield", "idle", "REST password changed", "REST password"],
};

// Basecamp-wide rows (no server): their own sentence.
const GLOBAL = {
  "notify.failed": (d) => ["Discord", ` message didn't send: ${d}`],
  "notify.test": (d) => ["Discord", d === "sent" ? " sent the test message." : ` test message failed: ${d}`],
  "notify.webhook": (d) => ["Discord", ` webhook ${d}.`],
  settings: (d) => ["Settings", ` saved: ${d}.`],
  "settings.reload": (d) => ["Settings file", ` read: ${d}.`],
  "settings.backups_to_all": (d) => ["Backup defaults", ` given to all servers: ${d}.`],
  "template.add": (d) => ["Template", ` ${d} added.`], "template.replace": (d) => ["Template", ` ${d} replaced.`],
  "template.override": (d) => ["Template", ` ${d} added, replacing the built-in one.`], "template.delete": (d) => ["Template", ` ${d} deleted.`],
  "template.refused": (d) => ["Template", ` refused: ${d}`],
  "update.check": () => ["Basecamp", " checked for updates."], "update.install": (d) => ["Basecamp", ` update: ${d}.`],
  "update.refused": (d) => ["Basecamp", ` update refused: ${d}`], "update.dismiss": () => ["Basecamp", " update banner dismissed."],
  debug: (d) => ["Debug view", ` read: ${d}.`],
  "view.friend": () => ["Friend view", " previewed by the owner."], "view.owner": () => ["Friend view", " preview ended."],
  "basecamp.restart": () => ["Basecamp", " restarted."], "basecamp.restart.refused": (d) => ["Basecamp", ` restart refused: ${d}`],
};

function describe(item) {
  const a = ACTIONS[item.action] || ["activity", "idle", item.action, item.action];
  const [ic, st, verb, short] = a;
  const failure = item.failure;
  if (item.server_id === null && GLOBAL[item.action]) {
    const [subject, rest] = GLOBAL[item.action](item.detail || "");
    return { icon: ic, st: failure ? "bad" : st, subject, text: rest, short, detail: item.detail };
  }
  const subject = item.server || (item.server_id !== null ? `Server ${item.server_id}` : "Basecamp");
  const showDetail = ["console", "friend.command", "config.save", "config.restore", "config.read", "config.refused", "config.invalid"].includes(item.action);
  const text = verb.endsWith(":") ? ` ${verb} ` : ` ${verb}.`;
  return { icon: ic, st: failure ? "bad" : st, subject, text, code: showDetail ? item.detail : "", short,
    detail: !showDetail && item.detail && item.detail !== item.server ? item.detail : showDetail ? item.detail : "" };
}

function whoText(item) {
  const w = item.who;
  if (item.action.startsWith("friend.")) return w && w !== "unknown" ? `${w}, friend` : "a friend";
  if (w === "basecamp") return "Basecamp";
  if (w === "schedule") return "Schedule";
  if (w === "auto-install") return "Basecamp, automatic install";
  if (w === "settings.toml") return "the settings file";
  if (w === "owner") return "Owner";
  if (w === "localhost") return "Owner (this PC)";
  return w || "someone";
}

const FILTERS = [["all", "Everything"], ["fail", "Failures"], ["crash", "Crashes"], ["friend", "Friends"],
  ["update", "Updates"], ["backup", "Backups"], ["discord", "Discord"], ["config", "Config and console"]];

function activityPage(root) {
  let filter = store.get("bc-activity-filter") || "all";
  if (!FILTERS.some(([f]) => f === filter) || (!isAdmin() && filter === "discord")) filter = "all";
  let items = [];
  let more = false;
  const feed = el("div", { class: "feed" });
  const failBar = el("div");
  const filterBox = el("div", { class: "filters stick", role: "group", "aria-label": "Show" });
  const moreBtn = el("button", { class: "btn sm", type: "button", hidden: true, onclick: () => load(true) }, "Show older");
  const empty = el("p", { class: "hint", hidden: true }, "Nothing of that kind yet.");
  root.replaceChildren(el("section", { class: "view mid", id: "v-activity" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, el("h1", {}, "Activity"),
      el("p", { class: "muted" }, isAdmin() ? "What happened, who did it and when." : "What happened to the servers, who did it and when."))),
    el("div", { class: "twocol left" }, filterBox,
      el("div", { class: "stack", style: "gap:12px" }, failBar, feed, empty, el("div", { class: "btnrow" }, moreBtn)))));

  const matches = (item) => filter === "all" || (filter === "fail" ? item.failure : item.kind === filter
    || (filter === "config" && item.kind === "config"));

  const renderFilters = () => {
    filterBox.replaceChildren(...FILTERS.filter(([f]) => isAdmin() || f !== "discord").map(([f, label]) =>
      el("button", { type: "button", "aria-pressed": String(f === filter), onclick: () => { filter = f; store.set("bc-activity-filter", f); render(); } },
        el("span", {}, f === "config" && !isAdmin() ? "Console" : label),
        f === "fail" && isAdmin() && failures ? el("span", { class: "count" }, String(failures)) : null)));
  };

  const dismiss = async (item) => {
    try { const out = await api("POST", `/api/activity/${item.id}/dismiss`); setFailures(out.open_failures); item.open = false; render(); toast("Dismissed"); }
    catch (e) { toast(e.message, "bad"); }
  };
  const dismissAll = async () => {
    try { const out = await api("POST", "/api/activity/dismiss"); setFailures(out.open_failures); items.forEach((i) => { i.open = false; }); render(); toast("All failures dismissed"); }
    catch (e) { toast(e.message, "bad"); }
  };

  const render = () => {
    renderFilters();
    failBar.replaceChildren(isAdmin() && failures ? el("div", { class: "callout", "data-st": "bad" }, icon("alert"),
      el("div", { class: "grow" }, el("div", {}, el("strong", {}, `${failures} failure${failures === 1 ? "" : "s"}`), " still count towards the red badge.")),
      el("button", { class: "btn sm", type: "button", onclick: dismissAll }, "Dismiss all")) : "");
    const shown = items.filter(matches);
    const out = [];
    let day = "";
    for (const item of shown) {
      const label = dayLabel(item.ts);
      if (label !== day) { day = label; out.push(el("div", { class: "day" }, label)); }
      const ev = describe(item);
      const countNote = isAdmin() && item.open ? item.server_id !== null
        ? `. Counts until ${item.server || "it"} runs normally again, or you dismiss it.` : ". Counts until you dismiss it." : "";
      out.push(el("div", { class: "ev", "data-st": ev.st },
        el("span", { class: "ico" }, icon(ev.icon)),
        el("div", { class: "body" }, el("b", {}, ev.subject), ev.text, ev.code ? el("code", {}, ev.code) : null,
          item.failure && (!isAdmin() || item.open) ? el("span", { class: "fail" }, "Failure") : null,
          !ev.code && ev.detail && item.server_id !== null ? el("div", { class: "hint" }, ev.detail) : null,
          el("div", { class: "who muted" }, whoText(item), countNote),
          isAdmin() && item.open ? el("button", { class: "btn sm ghost dismiss", type: "button", onclick: () => dismiss(item) }, "Dismiss") : null),
        el("time", { datetime: new Date(item.ts * 1000).toISOString(), title: fullDate(item.ts) }, hhmm(item.ts))));
    }
    feed.replaceChildren(...out);
    empty.hidden = shown.length > 0;
    moreBtn.hidden = !more;
  };

  const PAGE = 200;
  const load = async (older = false) => {
    try {
      const before = older && items.length ? `&before=${items[items.length - 1].id}` : "";
      const res = await api("GET", `/api/activity?limit=${PAGE}${before}`);
      items = older ? items.concat(res.items) : res.items;
      more = res.items.length === PAGE;
      if (res.open_failures !== undefined) setFailures(res.open_failures);
      render();
    } catch (e) { feed.replaceChildren(callout("bad", e.message)); }
  };
  render();
  load();
  let shownFailures = failures;
  const timer = setInterval(() => { if (!document.hidden) load(); }, 30 * 1000);
  return { update: () => { if (failures !== shownFailures) { shownFailures = failures; load(); } }, close: () => clearInterval(timer) };
}

// -- routing ---------------------------------------------------------------------------

const ADMIN_ROUTES = ["config", "new", "edit", "newgame", "setup", "templates", "prefs", "settings", "discord", "audit", "welcome"];
let lastHash = location.hash;
let routing = false;

async function route() {
  if (routing) return;
  if (page && page.dirty && page.dirty()) {
    routing = true;
    const leave = await confirmSheet({ title: "Leave without saving?", body: "Your edits on this page are lost.", ok: "Leave", danger: true, cancel: "Stay" });
    routing = false;
    if (!leave) { history.replaceState(null, "", lastHash || "#dashboard"); return; }
  }
  lastHash = location.hash;
  if (page && page.close) page.close();
  page = null;
  const root = $("#main");
  let [name, arg, arg2] = location.hash.replace(/^#\/?/, "").split("/");
  name = name || "dashboard";
  if (!isAdmin() && ADMIN_ROUTES.includes(name)) name = "dashboard";
  const id = arg && /^\d+$/.test(arg) ? Number(arg) : null;
  window.scrollTo(0, 0);
  const nav = { activity: "activity", templates: "templates", prefs: "prefs", settings: "prefs", discord: "prefs",
    newgame: "newgame", setup: "newgame" }[name] || "dashboard";
  setNav(nav);
  const fail = (e) => errorView(root, e.message);
  try {
    if (name === "server" && id) page = serverPage(root, id, arg2);
    else if (name === "activity" || name === "audit") page = activityPage(root);
    else if (name === "welcome") page = welcomePage(root);
    else if (name === "config" && id && /^\d+$/.test(arg2 || "")) page = configPage(root, id, Number(arg2));
    else if (name === "new") page = serverForm(root, null);
    else if (name === "edit" && id) page = serverForm(root, id);
    else if (name === "setup" && id) page = setupPage(root, id);
    else if (name === "newgame" || name === "setup") page = await newGamePage(root);
    else if (name === "templates") page = await templatesPage(root);
    else if (name === "prefs" || name === "settings" || name === "discord") page = await settingsPage(root, arg);
    else page = dashboard(root);
  } catch (e) { fail(e); }
}

async function loadMe() {
  try {
    const now = await api("GET", "/api/me");
    const changed = now.can_control !== me.can_control;
    me = now;
    document.body.classList.toggle("friend-locked", !isAdmin() && !canControl());
    renderPreview();
    if (changed && page && page.update) page.update();
  } catch (e) { /* offline; the socket shows it */ }
}

// -- see what friends see (D-143) -----------------------------------------------------------

function friendControlText(p) {
  if (p.friend_control === "anyone") return "Friend controls are on for anyone who reaches the page.";
  if (p.friend_control === "logins") {
    return p.listed ? `Friend controls are on for ${p.listed} listed login${p.listed === 1 ? "" : "s"}; you're shown as one of them.`
      : "Friend controls are on for listed logins, but none are listed, so friends can only look.";
  }
  return "Friend controls are off, so friends can only look.";
}

async function setView(as) {
  try {
    await api("POST", "/api/view", { as });
    if (as === "friend") location.hash = "#dashboard";
    location.reload();
  } catch (e) { toast(e.message, "bad"); }
}

function renderPreview() {
  const bar = $("#previewbar");
  bar.hidden = !me.preview;
  if (!me.preview) return;
  bar.replaceChildren(icon("eye"),
    el("div", { class: "grow" }, el("strong", {}, "You're seeing what friends see. "), friendControlText(me.preview)),
    el("button", { class: "btn sm", type: "button", onclick: () => setView("owner") }, "Back to owner view"));
}

async function boot() {
  applyTheme();
  $("#theme").addEventListener("click", nextTheme);
  me = await api("GET", "/api/me");
  document.body.dataset.role = isAdmin() ? "admin" : "friend";
  document.body.classList.toggle("friend-locked", !isAdmin() && !canControl());
  if (isAdmin()) setFailures(me.failures || 0);
  renderWho();
  renderFoot();
  renderPreview();
  window.addEventListener("hashchange", route);
  let first = true;
  keepSocket("/ws/status", {
    onMessage: (msg) => {
      if (msg.type !== "servers") return;
      servers = msg.servers;
      host = msg.host || host;
      if (msg.failures !== undefined && msg.failures !== failures) setFailures(msg.failures);
      if (first) { first = false; renderFoot(); route(); } else if (page && page.update) page.update();
    },
    onState: (up) => { $("#offline").hidden = up; },
  });
  if (!isAdmin()) setInterval(loadMe, 60 * 1000);
  setInterval(renderFoot, 60 * 60 * 1000);
}

boot().catch((e) => errorView($("#main"), e.message));
