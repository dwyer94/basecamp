// Admin page: dashboard, server page with live console and backups, add/edit form,
// templates, Discord, audit.
// Hash routes: #  #server/<id>  #new  #edit/<id>  #templates  #discord  #audit
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

async function act(server, action) {
  if (action === "start" && server.steam && server.steam.failed && !server.steam.job
    && !confirm(`The last ${server.steam.last.kind} of ${server.name} failed, so its files may be half updated. Start anyway?`)) return;
  if (action === "stop" && !server.restart_at && !confirm(`Stop ${server.name}?`)) return;
  if (action === "restart" && !confirm(`Restart ${server.name}?`)) return;
  if (action === "kill" && !confirm(`Kill ${server.name} without saving?`)) return;
  try {
    const result = await api("POST", `/api/servers/${server.id}/${action}`);
    if (result && result.warnings && result.warnings.length) toast(result.warnings.join(" "));
    else toast(`${action[0].toUpperCase() + action.slice(1)}: ${server.name}`);
  } catch (e) {
    alert(`${server.name}: ${e.message}`);
  }
}

// -- SteamCMD (R-11, R-30..R-32) ------------------------------------------------------

const PHASES = { queued: "Queued", backup: "Backing up the save", stopping: "Stopping",
  steamcmd: "SteamCMD running", starting: "Starting again" };

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
  return el("button", { disabled: ["stopping", "updating"].includes(s.state),
    onclick: () => steamAction(s, "update") }, "Update");
}

function steamLine(s) {
  const st = s.steam;
  if (!st) return null;
  return el("div", {},
    st.job ? el("div", { class: "notice" }, jobText(st.job),
      st.job.phase === "steamcmd" && st.job.progress !== null && st.job.position === 0
        ? el("progress", { max: "100", value: String(st.job.progress) }) : null) : null,
    !st.job && st.failed && st.last ? el("div", { class: "notice error" },
      `Last ${st.last.kind} ${st.last.result}: ${st.last.message} `,
      el("a", { href: `/api/servers/${s.id}/steam/runs/${st.last.run_id}/log`, target: "_blank" },
        "SteamCMD output")) : null);
}

function actionButtons(s) {
  const active = ["starting", "running", "stopping", "updating"].includes(s.state);
  const updating = s.state === "updating" || Boolean(s.steam && s.steam.job && s.steam.job.position === 0
    && s.steam.job.phase !== "queued");
  return el("div", { class: "buttons" },
    el("button", { class: "primary", disabled: active, onclick: () => act(s, "start") }, "Start"),
    s.restart_at
      ? el("button", { onclick: () => act(s, "stop") }, "Cancel restart")
      : el("button", { disabled: !active || updating || s.state === "stopping",
        onclick: () => act(s, "stop") }, "Stop"),
    el("button", { disabled: !active || updating, onclick: () => act(s, "restart") }, "Restart"),
    steamButton(s));
}

function addressBlock(s) {
  if (!s.join_address) {
    return el("div", { class: "hint" }, me.join_host
      ? "No join port set." : "Join address: set join_host in the data folder's settings.toml.");
  }
  return el("div", { class: "address" },
    el("code", {}, s.join_address),
    el("button", { class: "small", onclick: () => copyText(s.join_address) }, "Copy"),
    el("button", { class: "small", onclick: () => copyText(s.discord_line, "Copied for Discord") },
      "Copy for Discord"));
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

// -- dashboard ----------------------------------------------------------------------

function dashboard(root) {
  const list = el("div");
  root.replaceChildren(
    el("div", { class: "buttons", style: "margin-bottom:12px" },
      el("a", { class: "button primary", href: "#new" }, "Add server"),
      el("a", { class: "button", href: "#templates" }, "Templates"),
      el("a", { class: "button", href: "#discord" }, "Discord"),
      el("a", { class: "button", href: "#audit" }, "Audit log")),
    list);
  const update = () => {
    list.replaceChildren();
    if (!servers.length) {
      list.append(el("p", { class: "empty" }, "No servers yet. Add one from a template."));
    }
    for (const s of servers) {
      list.append(el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("a", { class: "name", href: `#server/${s.id}` }, s.name), statePill(s.state)),
        metaLine(s), steamLine(s), addressBlock(s), actionButtons(s)));
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
  const consoleState = el("div", { class: "hint" });
  const history = el("div");
  const backupBox = el("div");
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
    sock.send({ type: "command", text });
    input.value = "";
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

  const loadHistory = async () => {
    const rows = await api("GET", `/api/audit?server_id=${id}&limit=20`).catch(() => []);
    history.replaceChildren(rows.length ? el("table", {},
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("td", {}, new Date(r.ts).toLocaleString()), el("td", {}, r.who),
        el("td", {}, r.action), el("td", {}, r.action === "console" || r.action.startsWith("backup") ? r.detail : "")))))
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
          el("button", { class: "small danger", disabled: backupBusy, onclick: () => removeBackup(b) }, "Delete")))))))
        : el("p", { class: "muted" }, "No backups yet."),
      res.backups.length && !stopped ? el("div", { class: "hint" }, "Stop the server to restore a backup.") : null));
    loadHistory();
  };

  root.replaceChildren(head,
    el("h2", {}, "Console"), out,
    el("div", { class: "command" }, input, el("button", { class: "primary", onclick: send }, "Send")),
    consoleState,
    steamBox,
    el("h2", {}, "Backups"), backupBox,
    el("h2", {}, "History"), history);

  let lastState = null;
  const update = () => {
    const s = servers.find((x) => x.id === id);
    if (!s) {
      head.replaceChildren(el("p", {}, "No such server. ", el("a", { href: "#" }, "Back")));
      return;
    }
    const c = s.config;
    head.replaceChildren(
      el("div", { class: "card-head" }, el("span", { class: "name" }, s.name), statePill(s.state)),
      metaLine(s), steamLine(s), addressBlock(s), actionButtons(s),
      el("div", { class: "buttons" },
        el("a", { class: "button", href: `#edit/${id}` }, "Edit"),
        ["starting", "running", "stopping"].includes(s.state)
          ? el("button", { class: "danger", onclick: () => act(s, "kill") }, "Kill") : null),
      el("p", { class: "muted" }, c.exe_path, c.args ? ` ${c.args}` : "",
        s.pid ? ` · PID ${s.pid}` : ""));
    input.disabled = !(s.has_console && s.state !== "stopped" && s.state !== "crashed");
    steamBox.replaceChildren(s.steam ? steamSection(s) : el("div"));
    const key = `${s.state} ${s.steam && s.steam.job ? s.steam.job.phase : ""}`;
    if (key !== lastState) { lastState = key; loadBackups(); }  // and the history
  };
  update();
  return { update, close: () => sock.close() };
}

function steamSection(s) {
  const st = s.steam;
  const c = s.config;
  const last = st.last;
  return el("div", {},
    el("h2", {}, "SteamCMD"),
    !st.steamcmd_found ? notice(`SteamCMD not found at ${st.steamcmd}. Set steamcmd_path in the data folder's settings.toml.`, "error") : null,
    el("p", { class: "muted" }, `App ${c.steam_app_id || "?"}`,
      c.steam_beta ? `, beta branch ${c.steam_beta}` : "", c.steam_validate ? ", validate on update" : "",
      ". Installed build: ", el("strong", {}, st.build_id || (st.installed ? "unknown" : "not installed")),
      ". Folder ", el("code", {}, c.install_dir || "(not set)"), "."),
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
  log_source: "stdout", log_file: "", rcon_host: "127.0.0.1", rcon_port: null,
  real_exe_name: "", ready_pattern: "", ready_after_s: 15, min_free_ram_mb: 0,
  stop_recipe: [], notes: "", backup_every_h: 0, backup_keep: 10, notify_off: [],
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
    values = { ...BLANK, ...data };
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
        ["stdin", "rcon", "ctrl_break"].map((x) => el("option", { value: x, selected: s.action === x }, x))),
      el("input", { placeholder: "command", value: s.command, "data-k": "command" }),
      el("input", { type: "number", placeholder: "wait s", value: s.wait_s, "data-k": "wait_s" }),
      el("button", { type: "button", class: "small", onclick: () => row.remove() }, "✕"));
    stepRows.append(row);
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
          "Each step runs, then waits up to its seconds for the exit. Kill always comes last. Empty = Ctrl+Break, 30 s, kill.")),
      el("fieldset", {}, el("legend", {}, "Files"),
        field("Config files", lines("config_files"), "One path per line."),
        field("Save folder", text("save_dir")),
        field("RCON port", num("rcon_port"))),
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
        el("div", { class: "hint" }, "The webhook is set on the Discord page.")),
      field("Notes", (() => {
        const t = el("textarea", {}); t.value = values.notes || "";
        inputs.notes = () => t.value; return t;
      })()),
      el("div", { class: "buttons" },
        el("button", { class: "primary", type: "submit" }, editing ? "Save" : "Add server"),
        el("a", { class: "button", href: editing ? `#server/${id}` : "#" }, "Cancel"),
        editing ? el("button", { type: "button", class: "danger", onclick: remove }, "Delete") : null));
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
    api("GET", `/api/servers/${id}`).then((s) => fill(s.config))
      .catch((e) => root.replaceChildren(notice(e.message, "error")));
  } else {
    renderForm();
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

// -- Discord ---------------------------------------------------------------------------

async function discordPage(root) {
  const res = await api("GET", "/api/notify");
  const urlInput = el("input", { placeholder: "https://discord.com/api/webhooks/…", autocomplete: "off" });
  const save = async (serverId, input) => {
    const path = serverId ? `/api/servers/${serverId}/webhook` : "/api/notify";
    try {
      await api("PUT", path, { webhook_url: input.value.trim() });
      toast(input.value.trim() ? "Webhook saved" : "Webhook removed");
      discordPage(root);
    } catch (e) { alert(e.message); }
  };
  const test = async (serverId) => {
    try {
      await api("POST", `/api/notify/test${serverId ? `?server_id=${serverId}` : ""}`);
      toast("Test message sent");
    } catch (e) { alert(e.message); }
  };
  const serverRows = servers.map((s) => {
    const own = res.servers[String(s.id)];
    const input = el("input", { placeholder: own ? "Leave empty and save to remove" : "Optional: its own webhook", autocomplete: "off" });
    return el("div", { class: "card" },
      el("div", { class: "card-head" }, el("span", { class: "name" }, s.name)),
      el("div", { class: "hint" }, own ? el("span", {}, "Own webhook: ", el("code", {}, own)) : "Uses the main webhook."),
      input,
      el("div", { class: "buttons" },
        el("button", { class: "small", onclick: () => save(s.id, input) }, "Save"),
        own ? el("button", { class: "small", onclick: () => test(s.id) }, "Test") : null));
  });
  root.replaceChildren(el("h2", {}, "Discord"),
    el("p", { class: "muted" }, "Messages go to a channel webhook (Channel settings → Integrations → Webhooks). The URL is kept in the data folder's secrets.toml, never shown in full here."),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("span", { class: "name" }, "Main webhook"),
        el("span", { class: "tag" }, res.configured ? "set" : "not set")),
      res.configured ? el("div", { class: "hint" }, el("code", {}, res.webhook)) : null,
      field(res.configured ? "Replace with" : "Webhook URL", urlInput, res.configured ? "Leave empty and save to remove it." : null),
      el("div", { class: "buttons" },
        el("button", { class: "primary", onclick: () => save(null, urlInput) }, "Save"),
        res.configured ? el("button", { onclick: () => test(null) }, "Send a test") : null),
      el("div", { class: "hint" }, `Sent ${res.sent}, failed ${res.failed}, waiting ${res.pending} since Basecamp started.`)),
    el("h2", {}, "Per server"),
    el("p", { class: "muted" }, "A server's own webhook replaces the main one. Which events post is set in each server's Edit form."),
    ...serverRows);
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

function route() {
  if (page && page.close) page.close();
  page = null;
  const root = $("#view");
  const [name, arg] = location.hash.replace(/^#\/?/, "").split("/");
  const id = arg ? Number(arg) : null;
  window.scrollTo(0, 0);
  if (name === "server" && id) page = serverPage(root, id);
  else if (name === "new") page = serverForm(root, null);
  else if (name === "edit" && id) page = serverForm(root, id);
  else if (name === "templates") templatesPage(root).catch((e) => root.replaceChildren(notice(e.message, "error")));
  else if (name === "discord") discordPage(root).catch((e) => root.replaceChildren(notice(e.message, "error")));
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
