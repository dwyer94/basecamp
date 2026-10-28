// Admin page: dashboard, server page with live console, config files and backups, config
// editor, add/edit form, templates, settings (with Discord), audit.
// Hash routes: #  #server/<id>  #config/<id>/<n>  #new  #edit/<id>  #templates  #settings
//              #audit  (#discord is the settings page)
"use strict";

let me = {};
let servers = [];          // latest list from the status WebSocket
let page = null;           // the current view's {update(servers), close()}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

function errorText(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      const where = (d.loc || []).filter((x) => x !== "body").join(".");
      return where ? `${where}: ${d.msg}` : d.msg;
    }).join("\n");
  }
  return JSON.stringify(detail);
}

function notice(text, kind = "") {
  return el("div", { class: `notice ${kind}` }, text);
}

// The stop warning a server would send now (R-25): its lead time in minutes, or 0.
function warnMinutes(s) {
  const w = s.config.stop_warning;
  return s.state === "running" && w && w.minutes && w.minutes.length ? w.minutes[0] : 0;
}

async function act(server, action) {
  const lead = warnMinutes(server);
  const warned = lead ? ` Players get ${lead} min warning first.` : "";
  if (action === "start" && server.steam && server.steam.failed && !server.steam.job
    && !confirm(`The last ${server.steam.last.kind} of ${server.name} failed, so its files may be half updated. Start anyway?`)) return;
  if (action === "stop" && !server.restart_at && !confirm(`Stop ${server.name}?${warned}`)) return;
  if (action === "restart" && !confirm(`Restart ${server.name}?${warned}`)) return;
  if (action === "kill" && !confirm(`Kill ${server.name} without saving?${server.stop_warning ? " This also ends the countdown." : ""}`)) return;
  if (action === "stop-now") {
    const verb = { restart: "Restart", update: "Update" }[server.stop_warning && server.stop_warning.kind] || "Stop";
    if (!confirm(`${verb} ${server.name} now, without waiting for the rest of the warning?`)) return;
    try {
      await api("POST", `/api/servers/${server.id}/stop?now=true`);
      toast(`${verb} now: ${server.name}`);
    } catch (e) { alert(`${server.name}: ${e.message}`); }
    return;
  }
  try {
    const result = await api("POST", `/api/servers/${server.id}/${action}`);
    if (result && result.warnings && result.warnings.length) toast(result.warnings.join(" "));
    else toast(`${action[0].toUpperCase() + action.slice(1)}: ${server.name}`);
  } catch (e) {
    alert(`${server.name}: ${e.message}`);
  }
}

// -- SteamCMD (R-11, R-30..R-32) ------------------------------------------------------

const PHASES = { queued: "Queued", backup: "Backing up the save", warning: "Warning players",
  stopping: "Stopping", steamcmd: "SteamCMD running", starting: "Starting again" };

async function steamAction(s, action) {
  const st = s.steam;
  if (action === "update") {
    const running = ["starting", "running"].includes(s.state);
    const ask = running
      ? `Update ${s.name}? It backs up the save, stops the server, runs SteamCMD and starts it again.`
      : `Update ${s.name}? It backs up the save and runs SteamCMD.`;
    if (!confirm(ask)) return;
  } else if (action === "install") {
    if (!confirm(`Install ${s.name} into ${s.config.install_dir}? SteamCMD downloads it there.`)) return;
  } else if (action === "cancel") {
    const midway = st.job && st.job.phase === "steamcmd";
    if (!confirm(midway ? `Stop SteamCMD? ${s.name} may be left half updated and won't be started.`
      : `Cancel the ${st.job.kind} of ${s.name}?`)) return;
  }
  try {
    await api("POST", `/api/servers/${s.id}/${action === "cancel" ? "steam/cancel" : action}`);
    toast(action === "cancel" ? "Cancelling" : `${action === "install" ? "Install" : "Update"} queued: ${s.name}`);
  } catch (e) {
    alert(`${s.name}: ${e.message}`);
  }
}

function jobText(job) {
  if (job.position > 0) return `${job.kind === "install" ? "Install" : "Update"} queued (${job.position} ahead)`;
  let text = PHASES[job.phase] || job.phase;
  if (job.phase === "steamcmd" && job.progress !== null) text += ` · ${job.progress.toFixed(0)}%`;
  return job.cancelling ? `${text} · cancelling…` : text;
}

function steamButton(s) {
  const st = s.steam;
  if (!st) return null;
  if (st.job) {
    return el("button", { disabled: st.job.phase === "starting" || st.job.cancelling,
      onclick: () => steamAction(s, "cancel") }, "Cancel update");
  }
  if (!st.installed) {
    const idle = s.state === "stopped" || s.state === "crashed";
    return el("button", { disabled: !idle, onclick: () => steamAction(s, "install") }, "Install");
  }
  return el("button", { disabled: ["stopping", "updating"].includes(s.state) || Boolean(s.stop_warning),
    onclick: () => steamAction(s, "update") }, "Update");
}

function steamLine(s) {
  const st = s.steam;
  if (!st) return null;
  return el("div", {},
    st.job && st.job.phase !== "warning" ? el("div", { class: "notice" }, jobText(st.job),
      st.job.phase === "steamcmd" && st.job.progress !== null && st.job.position === 0
        ? el("progress", { max: "100", value: String(st.job.progress) }) : null) : null,
    !st.job && st.failed && st.last ? el("div", { class: "notice error" },
      `Last ${st.last.kind} ${st.last.result}: ${st.last.message} `,
      el("a", { href: `/api/servers/${s.id}/steam/runs/${st.last.run_id}/log`, target: "_blank" },
        "SteamCMD output")) : null);
}

// -- stop countdown (R-25) -----------------------------------------------------------

function fmtCountdown(stopAt) {
  const left = Math.max(0, Math.round(stopAt - Date.now() / 1000));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

// Every element with data-stop-at shows the time left; one timer for the whole page.
setInterval(() => {
  for (const node of document.querySelectorAll("[data-stop-at]")) {
    node.textContent = fmtCountdown(Number(node.dataset.stopAt));
  }
}, 1000);

function countdownLine(s) {
  const w = s.stop_warning;
  if (!w) return null;
  const what = { stop: "Stopping", restart: "Restarting", update: "Updating" }[w.kind] || "Stopping";
  return el("div", { class: "notice countdown" },
    `${what} in `, el("strong", { "data-stop-at": String(w.stop_at) }, fmtCountdown(w.stop_at)),
    " · players are being warned ",
    el("button", { class: "small", onclick: () => act(s, "stop-now") },
      w.kind === "stop" ? "Stop now" : `${what.replace(/ing$/, "")} now`));
}

function actionButtons(s) {
  const active = ["starting", "running", "stopping", "updating"].includes(s.state);
  const updating = s.state === "updating" || Boolean(s.steam && s.steam.job && s.steam.job.position === 0
    && s.steam.job.phase !== "queued");
  const counting = Boolean(s.stop_warning);
  return el("div", { class: "buttons" },
    el("button", { class: "primary", disabled: active, onclick: () => act(s, "start") }, "Start"),
    s.restart_at
      ? el("button", { onclick: () => act(s, "stop") }, "Cancel restart")
      : el("button", { disabled: !active || updating || counting || s.state === "stopping",
        onclick: () => act(s, "stop") }, "Stop"),
    el("button", { disabled: !active || updating || counting, onclick: () => act(s, "restart") }, "Restart"),
    steamButton(s));
}

function addressBlock(s) {
  if (!s.join_address) {
    return el("div", { class: "hint" }, me.join_host
      ? "No join port set." : el("span", {}, "Join address: set the join host on the ",
        el("a", { href: "#settings" }, "Settings"), " page."));
  }
  return el("div", { class: "address" },
    el("code", {}, s.join_address),
    el("button", { class: "small", onclick: () => copyText(s.join_address) }, "Copy"),
    el("button", { class: "small", onclick: () => copyText(s.discord_line, "Copied for Discord") },
      "Copy for Discord"));
}

// R-34: "update available" from the last build check; "unknown" when it failed.
function buildBadge(s) {
  const st = s.steam;
  if (!st || !st.build_id || !st.latest || st.job) return null;
  if (st.update_available) {
    return el("span", { class: "tag update", title: `Steam has build ${st.latest.build_id}; this is ${st.build_id}` },
      "update available");
  }
  if (!st.latest.build_id) {
    return el("span", { class: "tag muted", title: `Last check: ${st.latest.error || "no answer"}` }, "update: unknown");
  }
  return null;
}

function cardHead(s, nameNode) {
  return el("div", { class: "card-head" }, nameNode, el("span", { class: "tags" }, buildBadge(s), statePill(s.state)));
}

function metaLine(s) {
  return el("div", { class: "meta" },
    s.uptime_s !== null ? el("span", {}, fmtUptime(s.uptime_s)) : null,
    s.cpu_percent !== null ? el("span", {}, `CPU ${s.cpu_percent}%`) : null,
    s.rss_mb !== null ? el("span", {}, `RAM ${s.rss_mb} MB`) : null,
    s.state === "crashed" && s.last_exit_code !== null
      ? el("span", {}, `exit code ${s.last_exit_code}`) : null,
    s.adopted && !s.has_console && s.state === "running"
      ? el("span", {}, "no console (restart to get it back)") : null,
    s.restart_at ? el("span", { class: "unverified" },
      `restarting at ${new Date(s.restart_at * 1000).toLocaleTimeString()}`) : null,
    s.steam ? el("span", {}, s.steam.build_id ? `build ${s.steam.build_id}`
      : s.steam.installed ? "build unknown" : "not installed") : null);
}

// -- Basecamp's own updates (R-90..R-92) ---------------------------------------------

// After Install, Basecamp exits and the launcher starts the new version: wait for it.
async function waitForVersion(from, box) {
  box.replaceChildren(notice("Installing… Basecamp restarts; the page reloads when it's back."));
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
  box.replaceChildren(notice("Basecamp hasn't come back after 5 minutes. Check the launcher's log on the host PC.", "error"));
}

async function installUpdate(u, box) {
  const v = u.ready_version;
  const again = u.failed_versions.includes(v)
    ? `\n\n${v} already failed its health check once and rolled back.` : "";
  if (!confirm(`Install Basecamp ${v}? The page goes away for a minute; game servers keep running.${again}`)) return;
  try {
    await api("POST", "/api/update/install");
    waitForVersion(u.current, box);
  } catch (e) { alert(e.message); }
}

async function dismissUpdate(box) {
  try { await api("POST", "/api/update/dismiss"); updateBanner(box); } catch (e) { alert(e.message); }
}

function releaseNotes(latest) {
  if (!latest || !latest.notes) return null;
  return el("details", {}, el("summary", {}, "Release notes"),
    el("pre", { class: "notes" }, latest.notes));
}

async function updateBanner(box) {
  let u;
  try { u = await api("GET", "/api/update"); } catch (e) { return; }
  if (u.status === "installing") { waitForVersion(u.current, box); return; }
  const parts = [];
  if (u.failure) {
    parts.push(el("div", { class: "notice error" },
      `Basecamp ${u.failure.to} wasn't healthy (${u.failure.reason}), so it rolled back to ${u.failure.from}`
        + (u.failure.db_restored ? " and its database copy." : "."),
      " ", el("button", { class: "small", onclick: () => dismissUpdate(box) }, "Dismiss")));
  }
  if (u.install_error) {
    parts.push(el("div", { class: "notice error" }, u.install_error, " ",
      el("button", { class: "small", onclick: () => dismissUpdate(box) }, "Dismiss")));
  }
  const latest = u.latest;
  if (u.ready) {
    parts.push(el("div", { class: "notice ok" },
      el("strong", {}, `Basecamp ${u.ready_version} is ready to install`), ` (this PC runs ${u.current}). `,
      u.can_install
        ? el("button", { class: "small primary", onclick: () => installUpdate(u, box) }, "Install")
        : el("span", { class: "muted" }, "Basecamp isn't running under the launcher here, so it can't install."),
      releaseNotes(latest)));
  } else if (u.available && (u.status === "checking" || u.status === "downloading")) {
    parts.push(notice(`Downloading Basecamp ${latest.version}…`));
  } else if (u.available && u.status === "failed") {
    parts.push(el("div", { class: "notice error" }, `Basecamp ${latest.version} is out but can't be installed: ${u.error}`,
      releaseNotes(latest)));
  }
  box.replaceChildren(...parts);
}

// -- dashboard ----------------------------------------------------------------------

function dashboard(root) {
  const list = el("div");
  const banner = el("div");
  updateBanner(banner);
  root.replaceChildren(
    banner,
    el("div", { class: "buttons", style: "margin-bottom:12px" },
      el("a", { class: "button primary", href: "#new" }, "Add server"),
      el("a", { class: "button", href: "#templates" }, "Templates"),
      el("a", { class: "button", href: "#settings" }, "Settings"),
      el("a", { class: "button", href: "#audit" }, "Audit log")),
    list);
  const update = () => {
    list.replaceChildren();
    if (!servers.length) {
      list.append(el("p", { class: "empty" }, "No servers yet. Add one from a template."));
    }
    for (const s of servers) {
      list.append(el("div", { class: "card" },
        cardHead(s, el("a", { class: "name", href: `#server/${s.id}` }, s.name)),
        metaLine(s), countdownLine(s), steamLine(s), addressBlock(s), actionButtons(s)));
    }
  };
  update();
  return { update };
}

// -- server page ------------------------------------------------------------------------

function serverPage(root, id) {
  const head = el("div", { class: "card" });
  const out = el("div", { class: "console", "aria-live": "off" });
  const input = el("input", { placeholder: "Command (sent to the server's console)",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
  // stdin, or RCON / REST once the server has the port and password (D-97).
  const channel = el("select", { "aria-label": "Send by" });
  let channelsKey = "";
  const consoleState = el("div", { class: "hint" });
  const history = el("div");
  const backupBox = el("div");
  const configBox = el("div");
  const steamBox = el("div");
  let backupBusy = false;
  let lastSeq = 0;
  let follow = true;
  out.addEventListener("scroll", () => {
    follow = out.scrollTop + out.clientHeight >= out.scrollHeight - 20;
  });

  const addLine = (line) => {
    if (line.seq <= lastSeq) return;
    lastSeq = line.seq;
    const cls = { in: "in", sys: "sys", steam: "steam" }[line.source] || "";
    const prefix = line.source === "in" ? "> " : line.source === "sys" ? "# " : "";
    out.append(el("div", { class: cls }, prefix + line.text));
    while (out.childElementCount > 2000) out.firstElementChild.remove();
    if (follow) out.scrollTop = out.scrollHeight;
  };

  const sock = keepSocket(() => `/ws/servers/${id}/console?after=${lastSeq}`, {
    onMessage: (msg) => {
      if (msg.type === "lines") msg.lines.forEach(addLine);
      else if (msg.type === "line") addLine(msg);
      else if (msg.type === "error") toast(msg.message);
    },
    onState: (up) => { consoleState.textContent = up ? "" : "Console reconnecting…"; },
  });

  const send = () => {
    const text = input.value.trim();
    if (!text) return;
    sock.send({ type: "command", text, channel: channel.value });
    input.value = "";
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  channel.addEventListener("change", () => update());

  const loadHistory = async () => {
    const rows = await api("GET", `/api/audit?server_id=${id}&limit=20`).catch(() => []);
    history.replaceChildren(rows.length ? el("table", {},
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("td", {}, new Date(r.ts).toLocaleString()), el("td", {}, r.who),
        el("td", {}, r.action), el("td", {}, ["console", "backup", "config"].some((a) => r.action.startsWith(a)) ? r.detail : "")))))
      : el("p", { class: "muted" }, "Nothing yet."));
  };

  const backupNow = async () => {
    backupBusy = true; loadBackups();
    try {
      const res = await api("POST", `/api/servers/${id}/backups`);
      toast(`Backup made: ${res.backup.name}`);
    } catch (e) { alert(e.message); }
    backupBusy = false; loadBackups();
  };
  const restore = async (b) => {
    if (!confirm(`Restore ${b.name}? The current save is backed up first, then replaced.`)) return;
    backupBusy = true; loadBackups();
    try {
      const res = await api("POST", `/api/servers/${id}/backups/${encodeURIComponent(b.name)}/restore`);
      toast(res.safety_backup ? `Restored. The save before it: ${res.safety_backup}` : "Restored");
    } catch (e) { alert(e.message); }
    backupBusy = false; loadBackups();
  };
  const removeBackup = async (b) => {
    if (!confirm(`Delete backup ${b.name}?`)) return;
    try { await api("DELETE", `/api/servers/${id}/backups/${encodeURIComponent(b.name)}`); } catch (e) { alert(e.message); }
    loadBackups();
  };
  const loadBackups = async () => {
    let res;
    try { res = await api("GET", `/api/servers/${id}/backups`); } catch (e) {
      backupBox.replaceChildren(notice(e.message, "error")); return;
    }
    const s = servers.find((x) => x.id === id);
    const stopped = s && (s.state === "stopped" || s.state === "crashed");
    const schedule = res.every_h > 0
      ? `Every ${res.every_h} h while running, keeping the newest ${res.keep}.`
      : `No schedule (set one under Edit). Keeping the newest ${res.keep}.`;
    backupBox.replaceChildren(el("div", {},
      !res.save_dir ? notice("No save folder set. Set one under Edit to make backups.")
        : !res.save_dir_exists ? notice(`Save folder not found: ${res.save_dir}`) : null,
      el("p", { class: "muted" }, res.save_dir ? el("span", {}, "From ", el("code", {}, res.save_dir), ". ") : null,
        schedule, " Stored in ", el("code", {}, res.folder), "."),
      el("div", { class: "buttons" },
        el("button", { class: "primary", disabled: backupBusy || !res.save_dir_exists, onclick: backupNow },
          backupBusy ? "Working…" : "Back up now")),
      res.backups.length ? el("table", {}, el("tbody", {}, res.backups.map((b) => el("tr", {},
        el("td", {}, new Date(b.created * 1000).toLocaleString(), el("div", { class: "hint" }, b.reason)),
        el("td", {}, fmtSize(b.size_bytes), b.files !== null ? el("div", { class: "hint" }, `${b.files} files`) : null),
        el("td", {}, el("div", { class: "buttons", style: "margin:0" },
          el("button", { class: "small", disabled: backupBusy || !stopped || !res.save_dir,
            title: stopped ? "" : "Stop the server first", onclick: () => restore(b) }, "Restore"),
          el("a", { class: "button small", href: `/api/servers/${id}/backups/${encodeURIComponent(b.name)}/download`,
            download: b.name }, "Download"),
          el("button", { class: "small danger", disabled: backupBusy, onclick: () => removeBackup(b) }, "Delete")))))))
        : el("p", { class: "muted" }, "No backups yet."),
      res.backups.length && !stopped ? el("div", { class: "hint" }, "Stop the server to restore a backup.") : null));
    loadHistory();
  };

  const loadConfigs = async () => {
    let res;
    try { res = await api("GET", `/api/servers/${id}/configs`); } catch (e) {
      configBox.replaceChildren(notice(e.message, "error")); return;
    }
    configBox.replaceChildren(res.files.length ? el("table", {}, el("tbody", {}, res.files.map((f) => el("tr", {},
      el("td", {}, el("strong", {}, f.name), el("div", { class: "hint" }, f.path || f.entry)),
      el("td", {}, f.error ? el("span", { class: "unverified" }, f.error)
        : f.exists ? el("span", {}, fmtSize(f.size_bytes), el("div", { class: "hint" }, new Date(f.modified * 1000).toLocaleString()))
          : el("span", { class: "hint" }, "doesn't exist yet")),
      el("td", {}, f.error ? null : el("a", { class: "button small", href: `#config/${id}/${f.n}` }, "Open"))))))
      : el("p", { class: "muted" }, "None listed. Add paths under Edit → Files → Config files."));
  };

  root.replaceChildren(head,
    el("h2", {}, "Console"), out,
    el("div", { class: "command" }, channel, input, el("button", { class: "primary", onclick: send }, "Send")),
    consoleState,
    steamBox,
    el("h2", {}, "Config files"), configBox,
    el("h2", {}, "Backups"), backupBox,
    el("h2", {}, "History"), history);

  let lastState = null;
  loadConfigs();
  const update = () => {
    const s = servers.find((x) => x.id === id);
    if (!s) {
      head.replaceChildren(el("p", {}, "No such server. ", el("a", { href: "#" }, "Back")));
      return;
    }
    const c = s.config;
    // replaceChildren would print a null as "null"; countdownLine and steamLine can be null.
    head.replaceChildren(...[
      cardHead(s, el("span", { class: "name" }, s.name)),
      metaLine(s), countdownLine(s), steamLine(s), addressBlock(s), actionButtons(s),
      el("div", { class: "buttons" },
        el("a", { class: "button", href: `#edit/${id}` }, "Edit"),
        ["starting", "running", "stopping"].includes(s.state)
          ? el("button", { class: "danger", onclick: () => act(s, "kill") }, "Kill") : null),
      el("p", { class: "muted" }, c.exe_path, c.args ? ` ${c.args}` : "",
        s.pid ? ` · PID ${s.pid}` : "")].filter(Boolean));
    const remote = s.remote || { channels: ["stdin"], default: "stdin" };
    const key = remote.channels.join(",");
    if (key !== channelsKey) {
      channelsKey = key;
      channel.replaceChildren(...remote.channels.map((x) =>
        el("option", { value: x, selected: x === remote.default }, x === "stdin" ? "console" : x.toUpperCase())));
    }
    channel.hidden = remote.channels.length < 2;
    const up = s.state !== "stopped" && s.state !== "crashed";
    input.disabled = !(up && (s.has_console || channel.value !== "stdin"));
    steamBox.replaceChildren(s.steam ? steamSection(s) : el("div"));
    const stateKey = `${s.state} ${s.steam && s.steam.job ? s.steam.job.phase : ""}`;
    if (stateKey !== lastState) { lastState = stateKey; loadBackups(); }  // and the history
  };
  update();
  return { update, close: () => sock.close() };
}

async function checkBuild(s) {
  try {
    await api("POST", `/api/servers/${s.id}/steam/check`);
    toast(`Asking Steam about ${s.name}`);
  } catch (e) { alert(`${s.name}: ${e.message}`); }
}

function latestLine(s) {
  const st = s.steam;
  const l = st.latest;
  const when = l ? ` (checked ${new Date(l.checked_at * 1000).toLocaleString()})` : "";
  let text;
  if (st.checking) text = "Asking Steam for the latest build…";
  else if (!l) text = "Latest build on Steam: not checked yet.";
  else if (!l.build_id) text = `Latest build on Steam: unknown${when}.`;
  else if (st.update_available) text = `Steam has build ${l.build_id} on ${l.branch}${when}: an update is available.`;
  else if (st.update_available === false) text = `Up to date with Steam's ${l.branch} branch${when}.`;
  else text = `Latest build on Steam: ${l.build_id} on ${l.branch}${when}.`;
  return el("p", { class: "muted" }, el("span", { class: st.update_available ? "update-text" : "" }, text), " ",
    el("button", { class: "small", disabled: st.checking || !st.steamcmd_found, onclick: () => checkBuild(s) },
      "Check for update"));
}

function steamSection(s) {
  const st = s.steam;
  const c = s.config;
  const last = st.last;
  return el("div", {},
    el("h2", {}, "SteamCMD"),
    !st.steamcmd_found ? notice(el("span", {}, `SteamCMD not found at ${st.steamcmd}. Set its path on the `,
      el("a", { href: "#settings" }, "Settings"), " page."), "error") : null,
    el("p", { class: "muted" }, `App ${c.steam_app_id || "?"}`,
      c.steam_beta ? `, beta branch ${c.steam_beta}` : "", c.steam_validate ? ", validate on update" : "",
      ". Installed build: ", el("strong", {}, st.build_id || (st.installed ? "unknown" : "not installed")),
      ". Folder ", el("code", {}, c.install_dir || "(not set)"), "."),
    st.build_id ? latestLine(s) : null,
    last ? el("p", { class: "muted" },
      `Last ${last.kind}: ${last.result}`, last.ended_at ? `, ${new Date(last.ended_at).toLocaleString()}` : "",
      last.who ? ` by ${last.who}` : "", last.backup ? `. Backup ${last.backup}` : "", ". ",
      el("a", { href: `/api/servers/${s.id}/steam/runs/${last.run_id}/log`, target: "_blank" }, "Output")) : null,
    el("div", { class: "hint" }, "SteamCMD output shows in the console above while it runs."));
}

// -- add / edit form ----------------------------------------------------------------

const EVENTS = {
  up: "Server is up (with the join address)",
  stopped: "Stopped from Basecamp",
  crashed: "Crashed (and restarting)",
  gave_up: "Gave up restarting",
  backup_failed: "Scheduled backup failed",
  update: "Update started / finished / failed",
};

function fmtSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

const BLANK = {
  name: "", kind: "custom", template_id: "", install_dir: "", exe_path: "", args: "",
  working_dir: "", ports: [], join_port: null, start_on_boot: false, boot_order: 0,
  steam_app_id: null, steam_beta: "", steam_validate: false, config_files: [], save_dir: "",
  log_source: "stdout", log_file: "", rcon_host: "127.0.0.1", rcon_port: null, rest_port: null,
  real_exe_name: "", ready_pattern: "", ready_after_s: 15, min_free_ram_mb: 0,
  stop_recipe: [], stop_warning: null, notes: "", backup_every_h: 0, backup_keep: 10, notify_off: [],
};

function field(label, input, hint) {
  return el("div", {}, el("label", {}, label), input, hint ? el("div", { class: "hint" }, hint) : null);
}

function serverForm(root, id) {
  const editing = id !== null;
  const messages = el("div");
  const form = el("form", { novalidate: true });
  const tplSelect = el("select", {}, el("option", { value: "" }, "Detect from install folder"));
  const installInput = el("input", { placeholder: "C:\\GameServers\\Palworld" });
  let values = { ...BLANK };

  const fill = (data) => {
    values = { ...BLANK, ...(me.backup_defaults || {}), ...data };
    renderForm();
  };

  const inputs = {};
  const text = (key, attrs = {}) => {
    const i = el("input", { value: values[key] ?? "", ...attrs });
    inputs[key] = () => i.value;
    return i;
  };
  const num = (key, attrs = {}) => {
    const i = el("input", { type: "number", value: values[key] ?? "", ...attrs });
    inputs[key] = () => (i.value === "" ? null : Number(i.value));
    return i;
  };
  const check = (key, label) => {
    const i = el("input", { type: "checkbox", checked: !!values[key] });
    inputs[key] = () => i.checked;
    return el("label", { class: "check" }, i, label);
  };
  const select = (key, options) => {
    const s = el("select", {}, options.map((o) => el("option", { value: o, selected: values[key] === o }, o)));
    inputs[key] = () => s.value;
    return s;
  };
  const lines = (key) => {
    const t = el("textarea", {});
    t.value = (values[key] || []).join("\n");
    inputs[key] = () => t.value.split("\n").map((x) => x.trim()).filter(Boolean);
    return t;
  };

  const portRows = el("div");
  const addPortRow = (p = { port: "", protocol: "udp", label: "" }) => {
    const row = el("div", { class: "row port" },
      el("input", { type: "number", placeholder: "port", value: p.port, "data-k": "port" }),
      el("select", { "data-k": "protocol" },
        ["udp", "tcp"].map((x) => el("option", { value: x, selected: p.protocol === x }, x))),
      el("input", { placeholder: "label (game, query…)", value: p.label, "data-k": "label" }),
      el("button", { type: "button", class: "small", onclick: () => row.remove() }, "✕"));
    portRows.append(row);
  };
  const stepRows = el("div");
  const addStepRow = (s = { action: "ctrl_break", command: "", wait_s: 30 }) => {
    const row = el("div", { class: "row step" },
      el("select", { "data-k": "action" },
        ["stdin", "rcon", "rest", "ctrl_break", "ctrl_c", "wait_save"].map((x) => el("option", { value: x, selected: s.action === x }, x))),
      el("input", { placeholder: "command", value: s.command, "data-k": "command" }),
      el("input", { type: "number", placeholder: "wait s", value: s.wait_s, "data-k": "wait_s" }),
      el("button", { type: "button", class: "small", onclick: () => row.remove() }, "✕"));
    stepRows.append(row);
  };

  // R-25: the warning players get before a stop, restart or update from Basecamp.
  const warningFields = () => {
    const w = values.stop_warning || { action: "rcon", command: "", minutes: [] };
    const action = el("select", {}, ["stdin", "rcon", "rest"].map((x) => el("option", { value: x, selected: w.action === x }, x)));
    const command = el("input", { value: w.command, placeholder: "e.g. broadcast Restarting in {minutes} min",
      autocomplete: "off", spellcheck: "false" });
    const minutes = el("input", { value: (w.minutes || []).join(", "), placeholder: "e.g. 5, 1 (empty = off)",
      inputmode: "decimal", autocomplete: "off" });
    inputs.stop_warning = () => {
      const mins = minutes.value.split(/[,\s]+/).filter(Boolean).map(Number);
      if (!command.value.trim() && !mins.length) return null;
      return { action: action.value, command: command.value, minutes: mins };
    };
    return el("div", { style: "margin-top:12px" },
      el("label", {}, "Warn players first"),
      el("div", { class: "row warn" }, action, command),
      field("Warn at (minutes before)", minutes,
        "Sent before a stop, restart or update you start (never before a crash restart). The first number is the lead time, the others are reminders. {minutes} and {seconds} in the command are filled in. Kill and Stop now skip it."));
  };

  const collect = () => {
    const data = {};
    for (const [key, get] of Object.entries(inputs)) data[key] = get();
    data.template_id = values.template_id || "";
    data.ports = [...portRows.querySelectorAll(".port")].map((r) => ({
      port: Number($("[data-k=port]", r).value),
      protocol: $("[data-k=protocol]", r).value,
      label: $("[data-k=label]", r).value.trim(),
    })).filter((p) => p.port);
    data.stop_recipe = [...stepRows.querySelectorAll(".step")].map((r) => ({
      action: $("[data-k=action]", r).value,
      command: $("[data-k=command]", r).value,
      wait_s: Number($("[data-k=wait_s]", r).value || 0),
    }));
    return data;
  };

  const renderForm = () => {
    for (const key of Object.keys(inputs)) delete inputs[key];
    portRows.replaceChildren();
    (values.ports || []).forEach(addPortRow);
    stepRows.replaceChildren();
    (values.stop_recipe || []).forEach(addStepRow);
    form.replaceChildren(
      field("Name", text("name", { required: true })),
      field("Type", select("kind", ["steam", "custom"])),
      field("Install folder", text("install_dir")),
      field("Exe", text("exe_path", { required: true }), "Full path to the server's exe."),
      field("Launch arguments", text("args")),
      field("Working folder", text("working_dir"), "Empty = the exe's folder."),
      el("fieldset", {}, el("legend", {}, "Ports"), portRows,
        el("button", { type: "button", class: "small", onclick: () => addPortRow() }, "Add port"),
        field("Join port", num("join_port"), "The port friends type in (R-24).")),
      check("start_on_boot", "Start when Basecamp starts"),
      field("Boot order", num("boot_order")),
      el("fieldset", {}, el("legend", {}, "Steam"),
        field("App ID", num("steam_app_id")),
        field("Beta branch", text("steam_beta")),
        check("steam_validate", "Validate on update")),
      el("fieldset", {}, el("legend", {}, "Running"),
        field("Output", select("log_source", ["stdout", "file"]),
          "stdout = the console pipe; file = tail a log file the game writes."),
        field("Log file", text("log_file"), "Relative to the working folder."),
        field("Ready line", text("ready_pattern"), "Text in the output that means the server is up."),
        field("Ready after (s)", num("ready_after_s"), "Running after this long if no ready line shows."),
        field("Real exe name", text("real_exe_name"), "For games whose launcher starts another exe."),
        field("Warn below free RAM (MB)", num("min_free_ram_mb"))),
      el("fieldset", {}, el("legend", {}, "Stop recipe"), stepRows,
        el("button", { type: "button", class: "small", onclick: () => addStepRow() }, "Add step"),
        el("div", { class: "hint" },
          "Each step runs, then waits up to its seconds for the exit (wait_save: for the game to save to the save folder, then goes on). Kill always comes last. Empty = Ctrl+Break, 30 s, kill."),
        warningFields()),
      el("fieldset", {}, el("legend", {}, "Files"),
        field("Config files", lines("config_files"), "One path per line."),
        field("Save folder", text("save_dir")),
        field("RCON port", num("rcon_port")),
        field("REST port", num("rest_port"), "Palworld's REST API (RESTAPIPort, 8212). Its commands: announce <text>, save, shutdown <seconds> <text>, info, players, metrics."),
        passwordFields()),
      el("fieldset", {}, el("legend", {}, "Backups and Discord"),
        field("Back up every (hours)", num("backup_every_h", { min: 0, step: "any" }),
          "While the server runs. 0 = only by hand (and before updates)."),
        field("Backups to keep", num("backup_keep", { min: 1 })),
        el("label", {}, "Post to Discord when"),
        (() => {
          const boxes = Object.entries(EVENTS).map(([key, label]) => {
            const i = el("input", { type: "checkbox", checked: !(values.notify_off || []).includes(key), "data-ev": key });
            return el("label", { class: "check" }, i, label);
          });
          inputs.notify_off = () => boxes.map((b) => $("input", b)).filter((i) => !i.checked).map((i) => i.dataset.ev);
          return el("div", {}, boxes);
        })(),
        el("div", { class: "hint" }, "The webhook and the defaults for new servers are on the Settings page.")),
      field("Notes", (() => {
        const t = el("textarea", {}); t.value = values.notes || "";
        inputs.notes = () => t.value; return t;
      })()),
      el("div", { class: "buttons" },
        el("button", { class: "primary", type: "submit" }, editing ? "Save" : "Add server"),
        el("a", { class: "button", href: editing ? `#server/${id}` : "#" }, "Cancel"),
        editing ? el("button", { type: "button", class: "danger", onclick: remove }, "Delete") : null));
  };

  // RCON and REST passwords (D-97): kept in secrets.toml, saved apart from the form, never shown.
  const passwords = {};
  const passwordFields = () => {
    const set = values.password_set || {};
    const box = (ch, label) => {
      const i = el("input", { type: "password", autocomplete: "new-password",
        placeholder: set[ch] ? "set (type to change)" : "not set" });
      const clear = el("label", { class: "check" }, el("input", { type: "checkbox" }), "remove");
      passwords[ch] = () => ($("input", clear).checked ? "" : i.value || null);
      return field(label, el("div", {}, i, set[ch] ? clear : null));
    };
    return el("div", {}, box("rcon", "RCON password"), box("rest", "REST password"),
      el("div", { class: "hint" }, "The game's admin password for RCON or REST, as in its own settings file. Kept in the data folder's secrets.toml, never in the server's settings or the audit log."));
  };
  const savePasswords = async (serverId) => {
    const body = {};
    for (const [ch, get] of Object.entries(passwords)) {
      const v = get();
      if (v !== null) body[`${ch}_password`] = v;
    }
    if (Object.keys(body).length) await api("PUT", `/api/servers/${serverId}/remote`, body);
  };

  const remove = async () => {
    if (!confirm(`Delete ${values.name}? Game files are not touched.`)) return;
    try {
      await api("DELETE", `/api/servers/${id}`);
      location.hash = "#";
    } catch (e) { alert(e.message); }
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const saved = editing
        ? await api("PUT", `/api/servers/${id}`, collect())
        : await api("POST", "/api/servers", collect());
      await savePasswords(saved.id);
      location.hash = `#server/${saved.id}`;
    } catch (err) {
      messages.replaceChildren(notice(err.message, "error"));
      messages.scrollIntoView();
    }
  });

  const useTemplate = async () => {
    try {
      const res = await api("POST", "/api/drafts",
        { template_id: tplSelect.value, install_dir: installInput.value, name: values.name });
      fill(res.draft);
      messages.replaceChildren(...res.messages.map((m) =>
        notice(m, m.startsWith("Not yet checked") ? "" : "ok")));
    } catch (err) {
      messages.replaceChildren(notice(err.message, "error"));
    }
  };

  const top = editing ? el("div") : el("div", { class: "card" },
    el("h2", { style: "margin-top:0" }, "Start from a template"),
    field("Game", tplSelect),
    field("Install folder", installInput,
      "Leave the game on 'Detect' to read steamapps\\appmanifest_*.acf from an existing install. For a new install, pick the game and type a folder that doesn't exist yet (or is empty): save, then press Install on the server's page."),
    el("div", { class: "buttons" }, el("button", { class: "primary", onclick: useTemplate }, "Fill in")));

  root.replaceChildren(el("h2", {}, editing ? "Edit server" : "Add server"), top, messages, form);

  if (editing) {
    api("GET", `/api/servers/${id}`).then((s) => fill({ ...s.config, password_set: (s.remote || {}).password_set }))
      .catch((e) => root.replaceChildren(notice(e.message, "error")));
  } else {
    fill({});
    api("GET", "/api/templates").then((res) => {
      for (const t of res.templates) {
        tplSelect.append(el("option", { value: t.id },
          `${t.name}${t.source === "data folder" ? " (data folder)" : ""}`));
      }
    });
  }
  return {};
}

// -- templates -----------------------------------------------------------------------

async function templatesPage(root) {
  root.replaceChildren(el("h2", {}, "Game templates"), el("p", { class: "empty" }, "Loading…"));
  const res = await api("GET", "/api/templates");
  root.replaceChildren(el("h2", {}, "Game templates"),
    el("p", { class: "muted" }, "Built-in templates ship with Basecamp. Add or override one by putting a JSON file in ",
      el("code", {}, res.data_folder), " (see docs/08-templates.md). Changes show up here at once."),
    ...res.errors.map((e) => notice(`${e.file}: ${e.error}`, "error")),
    ...res.templates.map((t) => el("div", { class: "card" },
      el("div", { class: "card-head" }, el("span", { class: "name" }, t.name),
        el("span", { class: "tag" }, t.overrides ? "data folder (overrides built-in)" : t.source)),
      el("div", { class: "meta" },
        t.steam_app_id ? el("span", {}, `app ${t.steam_app_id}`) : null,
        el("span", {}, t.exe),
        el("span", {}, t.ports.map((p) => `${p.protocol}/${p.port}`).join(", "))),
      t.unverified.length
        ? el("div", { class: "unverified" }, "Not yet checked: ", t.unverified.join(", ")) : null,
      t.notes ? el("p", { class: "muted" }, t.notes) : null,
      el("div", { class: "muted" }, t.docs.map((d) => el("div", {}, el("a", { href: d, target: "_blank", rel: "noreferrer" }, d)))))));
}

// -- config editor (R-40..R-43) -------------------------------------------------------------

// Palworld keeps its whole config on one OptionSettings=(Key=Value,...) line. Split it into
// fields (top-level commas only; quoted strings and (a,b) lists stay whole) and back.
const OPTION_LINE = /^(\s*OptionSettings\s*=\s*\()(.*)(\)\s*)$/;

function parseOptionLine(line) {
  const m = line.match(OPTION_LINE);
  if (!m) return null;
  const items = [];
  let depth = 0, quoted = false, cur = "";
  for (const ch of m[2]) {
    if (quoted) { cur += ch; if (ch === '"') quoted = false; continue; }
    if (ch === '"') quoted = true;
    else if (ch === "(") depth++;
    else if (ch === ")") depth--;
    else if (ch === "," && depth === 0) { items.push(cur); cur = ""; continue; }
    cur += ch;
  }
  if (cur) items.push(cur);
  const pairs = items.map((it) => {
    const i = it.indexOf("=");
    return i > 0 ? { key: it.slice(0, i), raw: it.slice(i + 1) } : null;
  });
  if (quoted || depth !== 0 || !pairs.length || pairs.some((p) => !p)) return null;
  return { head: m[1], tail: m[3], pairs };
}

function optionLine(parsed) {
  return parsed.head + parsed.pairs.map((p) => `${p.key}=${p.raw}`).join(",") + parsed.tail;
}

function optionFields(parsed, onChange) {
  const filter = el("input", { placeholder: "Filter settings", autocomplete: "off", spellcheck: "false" });
  const rows = parsed.pairs.map((p) => {
    const isQuoted = /^".*"$/.test(p.raw);
    const isBool = /^(True|False)$/.test(p.raw);
    let input;
    if (isBool) {
      input = el("select", {}, ["True", "False"].map((v) => el("option", { value: v, selected: p.raw === v }, v)));
    } else {
      input = el("input", { value: isQuoted ? p.raw.slice(1, -1) : p.raw, autocomplete: "off", spellcheck: "false" });
    }
    input.addEventListener("input", () => {
      let v = input.value;
      if (isQuoted) v = `"${v.replace(/"/g, "")}"`;  // the format has no escapes
      p.raw = v;
      onChange();
    });
    return el("div", { class: "opt", "data-key": p.key.toLowerCase() },
      el("label", {}, p.key, isQuoted ? el("span", { class: "hint" }, " (text)") : null), input);
  });
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    for (const r of rows) r.hidden = q && !r.dataset.key.includes(q);
  });
  return el("div", {}, filter, el("div", { class: "opts" }, rows));
}

function configPage(root, id, n) {
  let file = null;          // the file as last loaded or saved
  let saved = "";           // its text, to tell unsaved edits
  const head = el("div", { class: "card" });
  const messages = el("div");
  const area = el("textarea", { class: "editor", wrap: "off", spellcheck: "false",
    autocapitalize: "off", autocomplete: "off", "aria-label": "File contents" });
  const fieldsBox = el("div", { hidden: true });
  const copiesBox = el("div");
  const copyView = el("div");
  const wrapBox = el("input", { type: "checkbox" });
  const fieldsBtn = el("button", { type: "button", hidden: true }, "Edit as fields");
  const saveBtn = el("button", { class: "primary", type: "button" }, "Save");
  const server = () => servers.find((x) => x.id === id);
  const dirty = () => file !== null && area.value !== saved;
  const base = `/api/servers/${id}/configs/${n}`;

  wrapBox.addEventListener("change", () => area.setAttribute("wrap", wrapBox.checked ? "soft" : "off"));

  const optionIndex = () => area.value.split("\n").findIndex((l) => OPTION_LINE.test(l));
  let fieldsOn = false;
  const showFields = (on) => {
    fieldsOn = on;
    if (on) {
      const lines = area.value.split("\n");
      const i = optionIndex();
      const parsed = i >= 0 ? parseOptionLine(lines[i]) : null;
      if (!parsed) { toast("Couldn't read the OptionSettings line as fields"); fieldsOn = false; }
      else {
        fieldsBox.replaceChildren(optionFields(parsed, () => {
          const now = area.value.split("\n");
          now[i] = optionLine(parsed);
          area.value = now.join("\n");
          renderHead();
        }));
      }
    }
    fieldsBox.hidden = !fieldsOn;
    area.hidden = fieldsOn;
    fieldsBtn.textContent = fieldsOn ? "Edit as text" : "Edit as fields";
  };
  fieldsBtn.addEventListener("click", () => showFields(!fieldsOn));
  area.addEventListener("input", () => renderHead());

  const renderHead = () => {
    const s = server();
    head.replaceChildren(
      el("div", { class: "card-head" },
        el("a", { class: "name", href: `#server/${id}` }, s ? s.name : `Server ${id}`),
        s ? statePill(s.state) : null),
      file ? el("div", {},
        el("div", { style: "margin-top:6px" }, el("strong", {}, file.name), " ",
          el("span", { class: "tag" }, file.kind), " ", dirty() ? el("span", { class: "tag unverified" }, "unsaved") : null),
        el("div", { class: "hint" }, el("code", {}, file.path)),
        el("div", { class: "hint" }, file.exists ? file.encoding : "Doesn't exist yet: saving creates it.",
          file.kind === "json" ? " · JSON is checked before saving." : "")) : "");  // not null: replaceChildren prints it
  };

  const restartOffer = (what = "Saved") => el("div", { class: "notice ok" },
    `${what}. The server is running; it reads the file when it starts. `,
    el("div", { class: "buttons" },
      el("button", { class: "primary", onclick: async (e) => {
        const s = server();
        if (!s || !confirm(`Restart ${s.name} now?`)) return;
        e.target.disabled = true;
        try { await api("POST", `/api/servers/${id}/restart`); toast(`Restart: ${s.name}`); messages.replaceChildren(); }
        catch (err) { alert(err.message); e.target.disabled = false; }
      } }, "Restart now"),
      el("button", { onclick: () => messages.replaceChildren() }, "Later")));

  const load = async () => {
    try { file = await api("GET", base); } catch (e) {
      root.replaceChildren(head, notice(e.message, "error"), el("a", { href: `#server/${id}` }, "Back"));
      renderHead();
      return;
    }
    area.value = file.text;
    saved = area.value;
    const hasLine = optionIndex() >= 0 && parseOptionLine(area.value.split("\n")[optionIndex()]);
    fieldsBtn.hidden = !hasLine;
    if (fieldsOn) showFields(Boolean(hasLine));
    renderHead();
    renderCopies(file.copies);
  };

  const save = async (force = false) => {
    saveBtn.disabled = true;
    try {
      const res = await api("PUT", base, { text: area.value, base_sha256: file.sha256, force });
      file.sha256 = res.sha256;
      file.exists = true;
      saved = area.value;
      renderHead();
      renderCopies(res.copies);
      messages.replaceChildren(res.running ? restartOffer()
        : notice(res.copy ? "Saved. The old file was kept as a copy below." : "Saved.", "ok"));
    } catch (e) {
      if (/changed on disk/.test(e.message)) {
        if (confirm(`${e.message}. Overwrite it with your version? (Its current contents are kept as a copy.)`)) {
          saveBtn.disabled = false; return save(true);
        }
        messages.replaceChildren(notice(`${e.message}. Not saved. Reload to see it (your edits are lost), or save again to overwrite.`, "error"));
      } else {
        messages.replaceChildren(notice(e.message, "error"));
      }
    }
    saveBtn.disabled = false;
  };
  saveBtn.addEventListener("click", () => save());

  const reload = () => {
    if (dirty() && !confirm("Throw away your unsaved edits?")) return;
    messages.replaceChildren();
    load();
  };

  const viewCopy = async (c) => {
    try {
      const res = await api("GET", `${base}/copies/${encodeURIComponent(c.name)}`);
      const pre = el("textarea", { class: "editor", wrap: "off", readonly: true, "aria-label": "Copy contents" });
      pre.value = res.text;
      copyView.replaceChildren(el("div", { class: "card" },
        el("div", { class: "card-head" }, el("span", { class: "name" }, `Copy from ${new Date(c.created * 1000).toLocaleString()}`),
          el("button", { class: "small", onclick: () => copyView.replaceChildren() }, "Close")),
        el("div", { class: "hint" }, res.encoding), pre));
      copyView.scrollIntoView();
    } catch (e) { alert(e.message); }
  };

  const restore = async (c) => {
    if (dirty() && !confirm("You have unsaved edits; restoring throws them away. Go on?")) return;
    if (!confirm(`Put back the copy from ${new Date(c.created * 1000).toLocaleString()}? The current file is kept as a copy first.`)) return;
    try {
      const res = await api("POST", `${base}/copies/${encodeURIComponent(c.name)}/restore`);
      copyView.replaceChildren();
      await load();
      messages.replaceChildren(res.running ? restartOffer("Restored")
        : notice("Restored. The file it replaced was kept as a copy.", "ok"));
    } catch (e) { alert(e.message); }
  };

  const renderCopies = (copies) => {
    copiesBox.replaceChildren(copies.length ? el("table", {}, el("tbody", {}, copies.map((c, i) => el("tr", {},
      el("td", {}, new Date(c.created * 1000).toLocaleString(), " ", i === 0 ? el("span", { class: "tag" }, "newest") : null,
        el("div", { class: "hint" }, c.reason === "restore" ? "before a restore" : "before a save")),
      el("td", {}, fmtSize(c.size_bytes)),
      el("td", {}, el("div", { class: "buttons", style: "margin:0" },
        el("button", { class: "small", onclick: () => viewCopy(c) }, "View"),
        el("button", { class: "small", onclick: () => restore(c) }, "Restore")))))))
      : el("p", { class: "muted" }, "None yet. Each save keeps the old file here."));
  };

  const onLeave = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", onLeave);

  root.replaceChildren(head, messages,
    el("div", { class: "buttons", style: "margin:0 0 8px" }, saveBtn,
      el("button", { type: "button", onclick: reload }, "Reload"), fieldsBtn,
      el("label", { class: "check", style: "margin:0" }, wrapBox, "Wrap long lines")),
    area, fieldsBox,
    el("h2", {}, "Earlier copies"),
    el("p", { class: "muted" }, "The newest 20 are kept. Restore puts one back exactly as it was."),
    copiesBox, copyView);
  renderHead();
  load();
  return { update: renderHead, dirty, close: () => window.removeEventListener("beforeunload", onLeave) };
}

// -- settings (D-52) and Discord (R-60..R-62) ------------------------------------------------

async function settingsPage(root) {
  const res = await api("GET", "/api/settings");
  const n = res.notify;
  const joinInput = el("input", { value: res.join_host, placeholder: "e.g. 100.x.y.z", autocomplete: "off", spellcheck: "false" });
  const everyInput = el("input", { type: "number", min: "0", step: "any", value: res.default_backup_every_h });
  const keepInput = el("input", { type: "number", min: "1", value: res.default_backup_keep });
  const allBox = el("input", { type: "checkbox" });
  const betaBox = el("input", { type: "checkbox", checked: res.update_beta });
  const hourSelect = el("select", {},
    el("option", { value: "-1", selected: res.update_auto_hour === -1 }, "Off (install by hand)"),
    [...Array(24).keys()].map((h) => el("option", { value: String(h), selected: res.update_auto_hour === h },
      `${String(h).padStart(2, "0")}:00`)));
  const steamInput = el("input", { value: res.steamcmd_path, placeholder: "%USERPROFILE%\\Desktop\\SteamCmd\\steamcmd.exe", autocomplete: "off", spellcheck: "false" });
  const saveSettings = async () => {
    if (allBox.checked && !confirm(`Give all ${servers.length} servers this backup schedule and count?`)) return;
    try {
      const out = await api("PUT", "/api/settings", {
        join_host: joinInput.value.trim(), steamcmd_path: steamInput.value.trim(),
        default_backup_every_h: Number(everyInput.value || 0), default_backup_keep: Number(keepInput.value || 1),
        update_beta: betaBox.checked, update_auto_hour: Number(hourSelect.value),
        apply_backup_defaults: allBox.checked });
      me = await api("GET", "/api/me");
      toast(out.changed.length || out.servers_updated.length
        ? `Saved${out.servers_updated.length ? `; ${out.servers_updated.length} servers updated` : ""}` : "Nothing changed");
      settingsPage(root);
    } catch (e) { alert(e.message); }
  };

  const live = res.live_status;
  const liveBox = el("input", { type: "checkbox", checked: live.on });
  const saveLive = async () => {
    try {
      await api("PUT", "/api/settings", { discord_live_status: liveBox.checked });
      toast(liveBox.checked ? "Live status on" : "Live status off");
      settingsPage(root);
    } catch (e) { alert(e.message); }
  };
  const urlInput = el("input", { placeholder: "https://discord.com/api/webhooks/…", autocomplete: "off" });
  const save = async (serverId, input) => {
    const path = serverId ? `/api/servers/${serverId}/webhook` : "/api/notify";
    try {
      await api("PUT", path, { webhook_url: input.value.trim() });
      toast(input.value.trim() ? "Webhook saved" : "Webhook removed");
      settingsPage(root);
    } catch (e) { alert(e.message); }
  };
  const test = async (serverId) => {
    try {
      await api("POST", `/api/notify/test${serverId ? `?server_id=${serverId}` : ""}`);
      toast("Test message sent");
    } catch (e) { alert(e.message); }
  };
  const serverRows = servers.map((s) => {
    const own = n.servers[String(s.id)];
    const input = el("input", { placeholder: own ? "Leave empty and save to remove" : "Optional: its own webhook", autocomplete: "off" });
    return el("div", { class: "card" },
      el("div", { class: "card-head" }, el("span", { class: "name" }, s.name)),
      el("div", { class: "hint" }, own ? el("span", {}, "Own webhook: ", el("code", {}, own)) : "Uses the main webhook."),
      input,
      el("div", { class: "buttons" },
        el("button", { class: "small", onclick: () => save(s.id, input) }, "Save"),
        own ? el("button", { class: "small", onclick: () => test(s.id) }, "Test") : null));
  });
  const saveMain = () => {
    if (!urlInput.value.trim() && !(n.configured && confirm("Remove the main webhook? Nothing will post to Discord."))) return;
    save(null, urlInput);
  };
  const ownCount = Object.keys(n.servers).length;
  const updateBox = el("div");
  let checkNow = null;
  const showUpdate = (u) => {
    const status = { idle: "up to date", checking: "checking…", downloading: "downloading…",
      ready: `${u.ready_version} is ready to install (see the dashboard)`, failed: u.error, installing: "installing…" }[u.status];
    updateBox.replaceChildren(
      el("div", { class: "hint" }, `This is Basecamp ${u.current}. `,
        u.last_check ? `Last checked ${new Date(u.last_check).toLocaleString()}: ` : "Not checked yet. ",
        el("span", { class: u.status === "failed" ? "unverified" : "" }, status || u.status)),
      ...(u.has_key ? [] : [el("div", { class: "hint unverified" }, "This build has no release key, so it can't check updates.")]),
      el("div", { class: "hint" }, u.check_h > 0 ? `Checks every ${u.check_h} h.` : "Automatic checks are off (update_check_h = 0)."),
      el("div", { class: "buttons" }, el("button", { class: "small", onclick: checkNow }, "Check now")));
  };
  checkNow = async () => {
    try {
      let u = await api("POST", "/api/update/check");
      showUpdate(u);
      for (let i = 0; i < 150 && ["checking", "downloading"].includes(u.status); i++) {
        await new Promise((r) => setTimeout(r, 2000));
        u = await api("GET", "/api/update");
        showUpdate(u);
      }
    } catch (e) { alert(e.message); }
  };
  showUpdate(res.update);
  root.replaceChildren(el("h2", {}, "Settings"),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("span", { class: "name" }, "Join address")),
      field("Join host", joinInput, "The host PC's Tailscale IP (or name) friends type in. The Copy buttons give <join host>:<the server's join port>."),
      el("div", { class: "card-head", style: "margin-top:18px" }, el("span", { class: "name" }, "Backups")),
      el("div", { class: "hint" }, "What a new server starts with; each server can change its own under Edit."),
      field("Back up every (hours)", everyInput, "While the server runs. 0 = only by hand and before updates."),
      field("Backups to keep", keepInput),
      el("label", { class: "check" }, allBox, `Also give all ${servers.length} existing servers this schedule and count`),
      el("div", { class: "hint" }, "Stored in ", el("code", {}, res.backups_dir), "."),
      el("div", { class: "card-head", style: "margin-top:18px" }, el("span", { class: "name" }, "SteamCMD")),
      field("steamcmd.exe", steamInput, "Empty = the default (the greyed-out text)."),
      el("div", { class: res.steamcmd_found ? "hint" : "hint unverified" },
        res.steamcmd_found ? "Found: " : "Not found: ", el("code", {}, res.steamcmd)),
      el("div", { class: "card-head", style: "margin-top:18px" }, el("span", { class: "name" }, "Basecamp updates")),
      updateBox,
      el("label", { class: "check" }, betaBox, "Beta channel: offer pre-releases too"),
      field("Auto-install", hourSelect, "When a new release is out, install it at this hour (this PC's time). A version that rolled back once is never auto-installed."),
      el("div", { class: "buttons" }, el("button", { class: "primary", onclick: saveSettings }, "Save settings"))),

    el("h2", {}, "Discord"),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("span", { class: "name" }, "Webhook"),
        el("span", { class: "tag" }, n.configured ? "set" : "not set")),
      el("div", { class: "hint" }, "One channel gets every message. Make a webhook in Discord (Channel settings → Integrations → Webhooks). It's kept in the data folder's secrets.toml and never shown in full."),
      n.configured ? el("div", { class: "hint" }, el("code", {}, n.webhook)) : null,
      field(n.configured ? "Replace with" : "Webhook URL", urlInput, n.configured ? "Leave empty and save to remove it." : null),
      el("div", { class: "buttons" },
        el("button", { class: "primary", onclick: saveMain }, "Save webhook"),
        n.configured ? el("button", { onclick: () => test(null) }, "Send a test") : null),
      el("div", { class: "hint" }, `Sent ${n.sent}, failed ${n.failed}, waiting ${n.pending} since Basecamp started.`),
      el("label", { class: "check", style: "margin-top:12px" }, liveBox, "Live status message: keep one message in the channel showing every server's state and join address, edited in place"),
      el("div", { class: "hint" }, live.on
        ? (live.error ? el("span", { class: "unverified" }, `Not updated: ${live.error}. Basecamp keeps trying.`)
          : `${live.has_message ? "Editing its message" : "Posts its message when a server changes"}${live.edits ? ` (${live.edits} edits since Basecamp started)` : ""}. Delete it in Discord and Basecamp posts a new one.`)
        : "Off. Event messages post either way."),
      el("div", { class: "buttons" }, el("button", { class: "small", onclick: saveLive }, "Save"))),
    el("details", { class: "card", open: ownCount > 0 },
      el("summary", {}, `Advanced: a webhook per server${ownCount ? ` (${ownCount} set)` : ""}`),
      el("p", { class: "muted" }, "A server's own webhook replaces the main one for that server. Which events post is set in each server's Edit form."),
      ...serverRows),

    el("h2", {}, "Set in settings.toml"),
    el("div", { class: "card" },
      el("p", { class: "muted", style: "margin-top:0" }, "Change these in ", el("code", {}, res.settings_file), " and restart Basecamp."),
      el("table", {}, el("tbody", {},
        el("tr", {}, el("td", {}, "Admin logins"), el("td", {}, res.admin_logins.length ? res.admin_logins.join(", ") : "none (only this PC)")),
        el("tr", {}, el("td", {}, "Config copies kept"), el("td", {}, String(res.config_copies_keep)))))));
}

// -- audit log -------------------------------------------------------------------------

async function auditPage(root) {
  const rows = await api("GET", "/api/audit?limit=200");
  const names = Object.fromEntries(servers.map((s) => [s.id, s.name]));
  root.replaceChildren(el("h2", {}, "Audit log"),
    rows.length ? el("table", {},
      el("thead", {}, el("tr", {}, ["When", "Who", "What", "Server", "Detail"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("td", {}, new Date(r.ts).toLocaleString()), el("td", {}, r.who), el("td", {}, r.action),
        el("td", {}, r.server_id === null ? "" : names[r.server_id] || `#${r.server_id}`),
        el("td", {}, r.detail)))))
      : el("p", { class: "empty" }, "Nothing yet."));
}

// -- routing ------------------------------------------------------------------------

let lastHash = location.hash;

function route() {
  if (page && page.dirty && page.dirty() && !confirm("Leave without saving your edits?")) {
    history.replaceState(null, "", lastHash || "#");
    return;
  }
  lastHash = location.hash;
  if (page && page.close) page.close();
  page = null;
  const root = $("#view");
  const [name, arg, arg2] = location.hash.replace(/^#\/?/, "").split("/");
  const id = arg ? Number(arg) : null;
  window.scrollTo(0, 0);
  if (name === "server" && id) page = serverPage(root, id);
  else if (name === "config" && id && /^\d+$/.test(arg2 || "")) page = configPage(root, id, Number(arg2));
  else if (name === "new") page = serverForm(root, null);
  else if (name === "edit" && id) page = serverForm(root, id);
  else if (name === "templates") templatesPage(root).catch((e) => root.replaceChildren(notice(e.message, "error")));
  else if (name === "settings" || name === "discord") settingsPage(root).catch((e) => root.replaceChildren(notice(e.message, "error")));
  else if (name === "audit") auditPage(root).catch((e) => root.replaceChildren(notice(e.message, "error")));
  else page = dashboard(root);
}

async function boot() {
  me = await api("GET", "/api/me");
  $("#who").textContent = me.name || me.login;
  window.addEventListener("hashchange", route);
  let first = true;
  keepSocket("/ws/status", {
    onMessage: (msg) => {
      if (msg.type !== "servers") return;
      servers = msg.servers;
      if (first) { first = false; route(); } else if (page && page.update) page.update();
    },
    onState: (up) => { $("#offline").hidden = up; },
  });
}

boot().catch((e) => $("#view").replaceChildren(notice(e.message, "error")));
