// The owner's pages (batch 3): config editor, add/edit form, new game, templates, settings.
// Friends never reach these (route() sends them to the dashboard; the API answers 403).
"use strict";

function field(label, input, hint, attrs = {}) {
  const id = input.id || (input.id = `f-${Math.random().toString(36).slice(2, 9)}`);
  return el("div", { class: "field", ...attrs }, el("label", { for: id }, label), input,
    hint ? el("span", { class: "hint" }, hint) : null);
}

function jumpTo(id) {
  const target = document.getElementById(id);
  if (target) target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function formIndex(label, items) {
  return el("nav", { class: "formindex", "aria-label": label }, items.map(([id, text, cls]) =>
    el("a", { href: `#${id}`, class: cls || null, onclick: (e) => { e.preventDefault(); jumpTo(id); } }, text)));
}

// -- config editor (R-40..R-43) -----------------------------------------------------------

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

function configPage(root, id, n) {
  let file = null;          // the file as last loaded or saved
  let saved = "";           // its text, to tell unsaved edits
  let mode = "text";
  let parsed = null;
  let optionIndex = -1;
  const base = `/api/servers/${id}/configs/${n}`;
  const server = () => serverById(id);
  const dirty = () => file !== null && area.value !== saved;

  const title = el("h1", {}, "Config file");
  const chips = el("div", { class: "chips" });
  const crumbBox = el("div");
  const seg = el("span", { class: "seg", role: "group", "aria-label": "Edit as", hidden: true });
  const area = el("textarea", { class: "editor", rows: 18, wrap: "off", spellcheck: "false", autocapitalize: "off",
    autocomplete: "off", "aria-label": "File contents", id: "cfg-area" });
  const wrapBox = el("input", { type: "checkbox", id: "cfg-wrap" });
  const filter = el("input", { type: "text", placeholder: "Filter settings, e.g. rate", autocomplete: "off", spellcheck: "false", "aria-label": "Filter settings" });
  const fieldGrid = el("div", { class: "fgrid" });
  const fieldHint = el("p", { class: "hint" });
  const fieldsPanel = el("div", { class: "panel", hidden: true }, filter, fieldGrid, fieldHint);
  const textPanel = el("div", { class: "panel" }, area, el("label", { class: "check" }, wrapBox, "Wrap long lines"));
  const messages = el("div", { class: "stack", style: "gap:10px" });
  const barText = el("div", { class: "grow small" });
  const saveBtn = el("button", { class: "btn", type: "button", onclick: () => save(false) }, "Save");
  const saveRestartBtn = el("button", { class: "btn primary", type: "button", onclick: () => save(true) }, "Save and restart");
  const copiesList = el("div", { class: "list" });
  const copyView = el("div", { class: "stack", style: "gap:8px" });
  const pathHint = el("p", { class: "hint" });

  wrapBox.addEventListener("change", () => area.setAttribute("wrap", wrapBox.checked ? "soft" : "off"));
  area.addEventListener("input", () => renderHead());
  filter.addEventListener("input", () => renderFieldFilter());

  const setMode = (m) => {
    mode = m;
    for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.mode === m));
    if (m === "fields") buildFields();
    fieldsPanel.hidden = m !== "fields";
    textPanel.hidden = m !== "text";
  };
  seg.append(el("button", { type: "button", "data-mode": "fields", "aria-pressed": "false", onclick: () => setMode("fields") }, "Fields"),
    el("button", { type: "button", "data-mode": "text", "aria-pressed": "true", onclick: () => setMode("text") }, "Text"));

  const findOptions = () => {
    const all = area.value.split("\n");
    optionIndex = all.findIndex((l) => OPTION_LINE.test(l));
    parsed = optionIndex >= 0 ? parseOptionLine(all[optionIndex]) : null;
    return parsed;
  };

  const buildFields = () => {
    if (!findOptions()) { setMode("text"); toast("Couldn't read the OptionSettings line as fields", "bad"); return; }
    fieldGrid.replaceChildren(...parsed.pairs.map((p) => {
      const isQuoted = /^".*"$/.test(p.raw);
      const isBool = /^(True|False)$/.test(p.raw);
      const input = isBool
        ? el("select", {}, ["True", "False"].map((v) => el("option", { value: v, selected: p.raw === v }, v)))
        : el("input", { type: "text", value: isQuoted ? p.raw.slice(1, -1) : p.raw, autocomplete: "off", spellcheck: "false" });
      input.addEventListener("input", () => {
        let v = input.value;
        if (isQuoted) v = `"${v.replace(/"/g, "")}"`;  // the format has no escapes
        p.raw = v;
        const now = area.value.split("\n");
        now[optionIndex] = optionLine(parsed);
        area.value = now.join("\n");
        renderHead();
      });
      input.addEventListener("change", () => input.dispatchEvent(new Event("input")));
      return field(p.key, input, null, { "data-key": p.key.toLowerCase() });
    }));
    renderFieldFilter();
  };

  const renderFieldFilter = () => {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    for (const f of fieldGrid.children) { f.hidden = Boolean(q) && !f.dataset.key.includes(q); if (!f.hidden) shown++; }
    fieldHint.textContent = `Showing ${shown} of ${fieldGrid.children.length} settings. Text values keep their quotes when saved.`;
  };

  const renderHead = () => {
    const s = server();
    crumbBox.replaceChildren(crumb(["Servers", "#dashboard"], [s ? s.name : `Server ${id}`, `#server/${id}/files`], "Config files"));
    if (!file) return;
    title.textContent = file.name;
    const changed = dirty();
    chips.replaceChildren(el("span", { class: "chip" }, file.kind.toUpperCase()),
      file.exists ? el("span", { class: "chip" }, file.encoding) : el("span", { class: "chip c-warn" }, "Doesn't exist yet"),
      file.kind === "json" ? el("span", { class: "chip" }, "Checked before saving") : null,
      changed ? el("span", { class: "chip c-warn" }, "Unsaved changes") : null);
    const running = s && ["starting", "running"].includes(s.state);
    barText.replaceChildren(s ? el("b", {}, running ? `${s.name} is running.` : `${s.name} is ${s.state}.`) : "",
      " ", el("span", { class: "muted" }, running ? "It reads this file when it starts." : file.exists ? "Changes apply on its next start." : "Saving creates the file."));
    saveRestartBtn.hidden = !running;
    saveBtn.className = running ? "btn" : "btn primary";
    pathHint.replaceChildren(el("code", {}, file.path));
  };

  const load = async () => {
    try { file = await api("GET", base); } catch (e) { errorView(root, e.message, `#server/${id}/files`); return; }
    area.value = file.text;
    saved = area.value;
    seg.hidden = !findOptions();
    if (!seg.hidden && mode === "fields") buildFields();
    else if (seg.hidden) setMode("text");
    renderHead();
    renderCopies(file.copies);
  };

  const save = async (restart, force = false) => {
    const s = server();
    if (restart && !force && !(await confirmSheet({ title: `Save and restart ${s ? s.name : "it"}?`,
      body: leadMinutes(s) ? `Players get a ${leadMinutes(s)} minute warning first.` : "It stops and starts again.", ok: "Save and restart" }))) return;
    saveBtn.disabled = saveRestartBtn.disabled = true;
    try {
      const res = await api("PUT", base, { text: area.value, base_sha256: file.sha256, force });
      file.sha256 = res.sha256;
      file.exists = true;
      saved = area.value;
      renderHead();
      renderCopies(res.copies);
      if (restart && res.running) {
        try { await api("POST", `/api/servers/${id}/restart`); toast(`Saved, restarting ${s.name}`); }
        catch (e) { toast(`Saved, but the restart didn't start: ${e.message}`, "bad"); }
      } else {
        toast(res.running ? `Saved. Restart ${s ? s.name : "it"} to use it.` : res.copy ? "Saved. The old file is kept as a copy." : "Saved");
      }
      messages.replaceChildren();
    } catch (e) {
      if (/changed on disk/.test(e.message)) {
        const over = await confirmSheet({ title: "The file changed on disk", body: `${e.message}. Overwrite it with your version? Its current contents are kept as a copy.`, ok: "Overwrite", danger: true });
        saveBtn.disabled = saveRestartBtn.disabled = false;
        if (over) return save(restart, true);
        messages.replaceChildren(callout("warn", "Not saved. Reload to see what's on disk (your edits are lost), or save again to overwrite."));
      } else {
        messages.replaceChildren(callout("bad", e.message));
      }
    }
    saveBtn.disabled = saveRestartBtn.disabled = false;
  };

  const reload = async () => {
    if (dirty() && !(await confirmSheet({ title: "Throw away your edits?", body: "Reload reads the file from disk again.", ok: "Reload", danger: true }))) return;
    messages.replaceChildren();
    await load();
    toast("Reloaded from disk");
  };

  const viewCopy = async (c) => {
    try {
      const res = await api("GET", `${base}/copies/${encodeURIComponent(c.name)}`);
      const pre = el("textarea", { class: "editor", rows: 12, wrap: "off", readonly: true, "aria-label": "Copy contents" });
      pre.value = res.text;
      copyView.replaceChildren(el("div", { class: "row", style: "border:0;padding:0" },
        el("div", { class: "grow" }, el("b", {}, `Copy from ${whenShort(c.created)}`), el("div", { class: "hint" }, res.encoding)),
        el("button", { class: "btn sm", type: "button", onclick: () => copyView.replaceChildren() }, "Close")), pre);
      copyView.scrollIntoView({ block: "nearest" });
    } catch (e) { toast(e.message, "bad"); }
  };

  const restore = async (c) => {
    if (!(await confirmSheet({ title: "Put this copy back?", body: [`The copy from ${whenShort(c.created)} replaces the file. The current file is kept as a copy first.`,
      dirty() ? "Your unsaved edits are lost." : null], ok: "Restore", danger: dirty() }))) return;
    try {
      const res = await api("POST", `${base}/copies/${encodeURIComponent(c.name)}/restore`);
      copyView.replaceChildren();
      await load();
      toast(res.running ? "Restored. Restart the server to use it." : "Restored. The file it replaced is kept as a copy.");
    } catch (e) { toast(e.message, "bad"); }
  };

  const renderCopies = (copies) => {
    copiesList.replaceChildren(...(copies.length ? copies.map((c, i) => el("div", { class: "row" },
      el("div", { class: "grow" }, el("b", { title: fullDate(c.created) }, whenShort(c.created)), " ", i === 0 ? el("span", { class: "chip" }, "newest") : null,
        el("div", { class: "hint" }, `${c.reason === "restore" ? "Before a restore" : "Before a save"}, ${fmtSize(c.size_bytes)}`)),
      el("div", { class: "btnrow" }, el("button", { class: "btn sm", type: "button", onclick: () => viewCopy(c) }, "View"),
        el("button", { class: "btn sm", type: "button", onclick: () => restore(c) }, "Restore"))))
      : [el("p", { class: "hint" }, "None yet. Each save keeps the old file here.")]));
  };

  const onLeave = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", onLeave);

  root.replaceChildren(el("section", { class: "view mid", id: "v-config" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, crumbBox, title, chips), seg),
    el("div", { class: "twocol" },
      el("div", { class: "stack" }, fieldsPanel, textPanel, messages,
        el("div", { class: "stickybar" }, barText,
          el("button", { class: "btn", type: "button", onclick: reload }, "Reload"), saveBtn, saveRestartBtn)),
      el("aside", { class: "panel stick" },
        el("header", {}, el("h3", {}, "Earlier copies"), el("span", { class: "hint" }, "The newest 20 are kept")),
        copiesList, copyView, pathHint))));
  renderHead();
  load();
  return { update: renderHead, dirty, close: () => window.removeEventListener("beforeunload", onLeave) };
}

// -- add / edit form ----------------------------------------------------------------

const EVENTS = {
  up: "It's up, with the join address",
  crashed: "It crashes",
  gave_up: "Basecamp gives up restarting it",
  stopped: "It's stopped from Basecamp",
  backup_failed: "A scheduled backup fails",
  update: "An update starts, finishes or fails",
};
const STEP_ACTIONS = { stdin: "Type in its console", rcon: "Send by RCON", rest: "Send by REST", ctrl_break: "Ctrl+Break",
  ctrl_c: "Ctrl+C", wait_save: "Wait for a save" };

const BLANK = {
  name: "", kind: "custom", template_id: "", install_dir: "", exe_path: "", args: "",
  working_dir: "", ports: [], join_port: null, start_on_boot: false, boot_order: 0,
  steam_app_id: null, steam_beta: "", steam_validate: false, config_files: [], save_dir: "",
  log_source: "stdout", log_file: "", rcon_host: "127.0.0.1", rcon_port: null, rest_port: null,
  real_exe_name: "", ready_pattern: "", ready_after_s: 15, min_free_ram_mb: 0,
  stop_recipe: [], stop_warning: null, notes: "", backup_every_h: 0, backup_keep: 10, notify_off: [],
  players: null, restart_at: "", update_at: "", schedule_players: "ignore",
};

function serverForm(root, id) {
  const editing = id !== null;
  let values = { ...BLANK };
  let library = null;
  let tplById = {};
  let changed = false;
  const inputs = {};
  const passwords = {};
  const formBox = el("div", { class: "formsections" });
  const startMsgs = el("div", { class: "stack", style: "gap:8px" });
  const messages = el("div", { class: "stack", style: "gap:8px" });
  const barText = el("div", { class: "grow small muted" }, editing ? "No changes yet" : "Not saved yet");
  const markChanged = () => { changed = true; barText.textContent = editing ? "Unsaved changes" : "Not saved yet"; };

  const text = (key, attrs = {}) => {
    const i = el("input", { type: "text", value: values[key] ?? "", autocomplete: "off", spellcheck: "false", ...attrs });
    inputs[key] = () => i.value;
    return i;
  };
  const num = (key, attrs = {}) => {
    const i = el("input", { type: "number", value: values[key] ?? "", ...attrs });
    inputs[key] = () => (i.value === "" ? null : Number(i.value));
    return i;
  };
  const check = (key, label, hint) => {
    const i = el("input", { type: "checkbox", checked: Boolean(values[key]) });
    inputs[key] = () => i.checked;
    return el("label", { class: "check" }, i, el("span", {}, label, hint ? el("span", { class: "hint", style: "display:block" }, hint) : null));
  };
  const select = (key, options) => {
    const s = el("select", {}, Object.entries(options).map(([v, label]) => el("option", { value: v, selected: String(values[key]) === v }, label)));
    inputs[key] = () => s.value;
    return s;
  };

  // folders in SteamCMD's steamapps\common and the templates (D-125), to start from
  const folderSelect = el("select", { id: "e-import" });
  const tplSelect = el("select", { id: "e-tpl" }, el("option", { value: "" }, "Detect from the folder"));
  const installInput = el("input", { type: "text", id: "e-dir", autocomplete: "off", spellcheck: "false", placeholder: "C:\\GameServers\\MyGame" });
  const libraryHint = el("span", { class: "hint" });
  const defaultInstall = (tpl, name) => {
    if (!library || !library.steamcmd_found || (tpl && tpl.kind !== "steam")) return "";
    const folder = tpl ? (tpl.steam_installdir || tpl.id) : (name || "").replace(/[\\/:*?"<>|\x00-\x1f]+/g, " ").trim();
    return folder ? `${library.common}\\${folder}` : "";
  };
  const showLibrary = () => {
    const folders = library.folders || [];
    folderSelect.replaceChildren(el("option", { value: "" }, folders.length ? `Pick one of ${folders.length} folders in steamapps\\common…` : "No folders there yet"),
      ...folders.map((f) => el("option", { value: f.path },
        f.name + (f.app_id ? `, app ${f.app_id}` : ", no appmanifest") + (f.used_by ? `, already "${f.used_by}"` : ""))));
    folderSelect.disabled = !folders.length;
    libraryHint.replaceChildren(library.error ? library.error
      : el("span", {}, "In ", el("code", {}, library.common), library.exists ? ". Picking a folder also picks its game." : " (SteamCMD makes it on the first install)."));
  };
  const fillFromDraft = async () => {
    try {
      const res = await api("POST", "/api/drafts", { template_id: tplSelect.value, install_dir: installInput.value, name: values.name });
      fill(res.draft);
      startMsgs.replaceChildren(...res.messages.map((m) => callout(m.startsWith("Not yet checked") || m.includes("doesn't exist") ? "warn" : "run", m)));
      markChanged();
    } catch (e) { startMsgs.replaceChildren(callout("bad", e.message)); }
  };
  folderSelect.addEventListener("change", () => {
    if (!folderSelect.value) return;
    installInput.value = folderSelect.value;
    const f = library.folders.find((x) => x.path === folderSelect.value);
    const match = f && f.app_id && Object.values(tplById).find((t) => t.steam_app_id === f.app_id);
    tplSelect.value = match ? match.id : "";
    fillFromDraft();
  });
  tplSelect.addEventListener("change", () => {
    const tpl = tplById[tplSelect.value];
    if (!installInput.value || installInput.dataset.auto === "1") {
      installInput.value = defaultInstall(tpl) || "";
      installInput.dataset.auto = "1";
    }
    if (tplSelect.value || installInput.value) fillFromDraft();
  });
  installInput.addEventListener("input", () => { installInput.dataset.auto = ""; });

  const portRows = el("tbody");
  const addPortRow = (p = { port: "", protocol: "udp", label: "" }, join = false) => {
    const row = el("tr", { class: "port" },
      el("td", {}, el("input", { type: "number", value: p.port, "aria-label": "Port", "data-k": "port", min: "1", max: "65535" })),
      el("td", {}, el("select", { "aria-label": "Protocol", "data-k": "protocol" }, ["udp", "tcp"].map((x) => el("option", { value: x, selected: p.protocol === x }, x.toUpperCase())))),
      el("td", {}, el("input", { type: "text", value: p.label || "", "aria-label": "Label", "data-k": "label", placeholder: "game, query…" })),
      el("td", {}, el("input", { type: "radio", name: "joinport", "aria-label": "Friends join here", "data-k": "join", checked: join })),
      el("td", {}, el("button", { type: "button", class: "btn ghost sm", "aria-label": "Remove port", onclick: () => { row.remove(); markChanged(); } }, "✕")));
    portRows.append(row);
  };
  const stepRows = el("div", { class: "list" });
  const addStepRow = (s = { action: "ctrl_break", command: "", wait_s: 30 }) => {
    const action = el("select", { "aria-label": "Step", "data-k": "action" }, Object.entries(STEP_ACTIONS).map(([v, label]) => el("option", { value: v, selected: s.action === v }, label)));
    const command = el("input", { type: "text", value: s.command || "", placeholder: "command, e.g. save", "aria-label": "Command", "data-k": "command", class: "cmd", autocomplete: "off", spellcheck: "false" });
    const wait = el("input", { type: "number", value: s.wait_s ?? 30, min: "0", "aria-label": "Wait up to (s)", title: "Wait up to this many seconds", "data-k": "wait_s" });
    const show = () => { command.hidden = !["stdin", "rcon", "rest"].includes(action.value); };
    action.addEventListener("change", show);
    show();
    const row = el("div", { class: "steprow step" }, el("span", { class: "chip" }, "Step"), action, command, wait,
      el("button", { type: "button", class: "btn ghost sm", "aria-label": "Remove step", onclick: () => { row.remove(); markChanged(); } }, "✕"));
    stepRows.append(row);
  };

  const warningFields = () => {
    const w = values.stop_warning || { action: "rcon", command: "", minutes: [] };
    const action = el("select", { "aria-label": "Warn by" }, ["stdin", "rcon", "rest"].map((x) => el("option", { value: x, selected: w.action === x }, STEP_ACTIONS[x])));
    const command = el("input", { type: "text", value: w.command, placeholder: "broadcast Server restarts in {minutes} min", autocomplete: "off", spellcheck: "false" });
    const minutes = el("input", { type: "text", value: (w.minutes || []).join(", "), placeholder: "5, 1 (empty = off)", inputmode: "decimal", autocomplete: "off" });
    inputs.stop_warning = () => {
      const mins = minutes.value.split(/[,\s]+/).filter(Boolean).map(Number);
      if (!command.value.trim() && !mins.length) return null;
      return { action: action.value, command: command.value, minutes: mins };
    };
    return el("div", { class: "stack", style: "gap:14px" },
      el("div", { class: "fgrid" }, field("Warn players by", action), field("With", command, "{minutes} and {seconds} are filled in."), field("Warn at (minutes before)", minutes)),
      el("p", { class: "hint" }, "Sent before a stop, restart or update you start (never before a crash restart). The first number is the lead time, the others are reminders. Kill and \"now\" skip it."));
  };

  const playersFields = () => {
    const p = values.players || { source: "", command: "", pattern: "", port: null };
    const source = el("select", {}, [["", "Don't ask"], ["rest", "REST API (Palworld)"], ["rcon", "RCON command"], ["a2s", "Steam server query (A2S)"]]
      .map(([v, label]) => el("option", { value: v, selected: (p.source || "") === v }, label)));
    const command = el("input", { type: "text", value: p.command || "", placeholder: "listplayers", autocomplete: "off", spellcheck: "false" });
    const pattern = el("input", { type: "text", value: p.pattern || "", placeholder: "Players: (\\d+)", autocomplete: "off", spellcheck: "false" });
    const port = el("input", { type: "number", value: p.port ?? "", placeholder: "query port" });
    const cmdField = field("RCON command", command);
    const patField = field("Pattern", pattern, "A group reads the number; without one, each matching line is a player.");
    const portField = field("Query port", port);
    const show = () => { cmdField.hidden = patField.hidden = source.value !== "rcon"; portField.hidden = source.value !== "a2s"; };
    source.addEventListener("change", show);
    show();
    inputs.players = () => (source.value ? { source: source.value, command: command.value, pattern: pattern.value,
      port: port.value === "" ? null : Number(port.value) } : null);
    return [field("Player count from", source, "Asked every minute while it runs; friends see the count only."), portField, cmdField, patField];
  };

  const passwordField = (ch, label) => {
    const set = (values.password_set || {})[ch];
    const i = el("input", { type: "password", autocomplete: "new-password", placeholder: set ? "Set (type to change)" : "Not set" });
    const clear = el("input", { type: "checkbox" });
    passwords[ch] = () => (clear.checked ? "" : i.value || null);
    const f = field(label, i);
    if (set) f.append(el("label", { class: "check small" }, clear, "Remove it"));
    return f;
  };

  const notifyBoxes = () => {
    const boxes = Object.entries(EVENTS).map(([key, label]) => {
      const i = el("input", { type: "checkbox", checked: !(values.notify_off || []).includes(key), "data-ev": key });
      return el("label", { class: "check" }, i, label);
    });
    inputs.notify_off = () => boxes.map((b) => $("input", b)).filter((i) => !i.checked).map((i) => i.dataset.ev);
    return el("div", { class: "fgrid" }, boxes);
  };

  const filesArea = () => {
    const t = el("textarea", { rows: 3, spellcheck: "false", autocapitalize: "off" });
    t.value = (values.config_files || []).join("\n");
    inputs.config_files = () => t.value.split("\n").map((x) => x.trim()).filter(Boolean);
    return t;
  };
  const notesArea = () => {
    const t = el("textarea", { rows: 3 });
    t.value = values.notes || "";
    inputs.notes = () => t.value;
    return t;
  };

  const collect = () => {
    const data = { ...values };
    for (const key of ["password_set"]) delete data[key];
    for (const [key, get] of Object.entries(inputs)) data[key] = get();
    data.ports = [...portRows.querySelectorAll(".port")].map((r) => ({
      port: Number($("[data-k=port]", r).value), protocol: $("[data-k=protocol]", r).value, label: $("[data-k=label]", r).value.trim(),
      join: $("[data-k=join]", r).checked })).filter((p) => p.port);
    const join = data.ports.find((p) => p.join);
    data.join_port = join ? join.port : null;
    data.ports = data.ports.map(({ join: _, ...p }) => p);
    data.stop_recipe = [...stepRows.querySelectorAll(".step")].map((r) => ({
      action: $("[data-k=action]", r).value, command: $("[data-k=command]", r).value, wait_s: Number($("[data-k=wait_s]", r).value || 0) }));
    return data;
  };

  const savePasswords = async (serverId) => {
    const body = {};
    for (const [ch, get] of Object.entries(passwords)) { const v = get(); if (v !== null) body[`${ch}_password`] = v; }
    if (Object.keys(body).length) await api("PUT", `/api/servers/${serverId}/remote`, body);
  };

  const submit = async () => {
    try {
      const saved = editing ? await api("PUT", `/api/servers/${id}`, collect()) : await api("POST", "/api/servers", collect());
      await savePasswords(saved.id);
      changed = false;
      toast(editing ? `Saved ${saved.name}` : `Added ${saved.name}`);
      location.hash = `#server/${saved.id}`;
    } catch (e) {
      messages.replaceChildren(callout("bad", el("pre", {}, e.message)));
      messages.scrollIntoView({ block: "center" });
    }
  };

  const remove = async () => {
    const go = await confirmSheet({ title: `Delete ${values.name}?`, body: "It's removed from Basecamp. Game files, saves and backups stay on disk.", ok: "Delete server", danger: true });
    if (!go) return;
    try { await api("DELETE", `/api/servers/${id}`); changed = false; toast(`Deleted ${values.name}`); location.hash = "#dashboard"; }
    catch (e) { toast(e.message, "bad"); }
  };

  const fill = (data) => {
    values = { ...BLANK, ...(editing ? {} : me.backup_defaults || {}), ...data };
    render();
  };

  const render = () => {
    for (const key of Object.keys(inputs)) delete inputs[key];
    portRows.replaceChildren();
    (values.ports || []).forEach((p) => addPortRow(p, values.join_port === p.port));
    stepRows.replaceChildren();
    (values.stop_recipe || []).forEach(addStepRow);
    const sections = [
      editing ? null : el("section", { class: "panel", id: "f-start" },
        el("header", {}, el("h2", {}, "Start from")),
        el("div", { class: "field" }, el("label", { for: "e-import" }, "Installed game folders"), folderSelect, libraryHint),
        el("div", { class: "fgrid" },
          field("Game template", tplSelect),
          el("div", { class: "field" }, el("label", { for: "e-dir" }, "Install folder"),
            el("div", { class: "inline" }, installInput, el("button", { class: "btn sm", type: "button", onclick: () => {
              const path = defaultInstall(tplById[tplSelect.value], values.name);
              if (path) { installInput.value = path; installInput.dataset.auto = "1"; } else toast(library && library.error ? library.error : "No default: type a folder", "bad");
            } }, "Default")))),
        el("div", { class: "btnrow" }, el("button", { class: "btn", type: "button", onclick: fillFromDraft }, "Fill in from these")),
        startMsgs),
      el("section", { class: "panel", id: "f-basics" },
        el("header", {}, el("h2", {}, "Basics")),
        el("div", { class: "fgrid" }, field("Name", text("name", { required: true })), field("Type", select("kind", { steam: "Steam", custom: "Custom" }))),
        field("Install folder", text("install_dir")),
        field("Exe", text("exe_path", { required: true }), "Full path to the server's exe."),
        el("div", { class: "fgrid" }, field("Launch arguments", text("args")), field("Working folder", text("working_dir", { placeholder: "The exe's folder" }))),
        el("div", { class: "fgrid" }, check("start_on_boot", "Start when Basecamp starts"), field("Boot order", num("boot_order"))),
        field("Notes", notesArea())),
      el("section", { class: "panel", id: "f-ports" },
        el("header", {}, el("h2", {}, "Ports and joining")),
        el("div", { class: "tablewrap" }, el("table", { class: "porttable" },
          el("thead", {}, el("tr", {}, el("th", {}, "Port"), el("th", {}, "Protocol"), el("th", {}, "Label"), el("th", {}, "Friends join here"), el("th", {}, el("span", { hidden: true }, "Remove")))),
          portRows)),
        el("div", { class: "btnrow" }, el("button", { class: "btn sm", type: "button", onclick: () => { addPortRow(); markChanged(); } }, icon("plus"), "Add port")),
        el("p", { class: "hint" }, "The join port is what the Copy buttons give friends, with the join host from Settings.")),
      el("section", { class: "panel", id: "f-running" },
        el("header", {}, el("h2", {}, "Running")),
        el("div", { class: "fgrid" },
          field("Output", select("log_source", { stdout: "Console pipe (stdout)", file: "A log file the game writes" })),
          field("Log file", text("log_file"), "Relative to the working folder."),
          field("Ready line", text("ready_pattern"), "Text in the output that means it's up."),
          field("Ready after (s), if no ready line", num("ready_after_s", { min: "0", step: "any" }))),
        field("Config files", filesArea(), "One path per line. They show under Config files on its page."),
        el("details", {}, el("summary", { class: "flabel" }, "More: real exe name, Steam app and branch, free RAM warning"),
          el("div", { class: "fgrid", style: "margin-top:12px" },
            field("Real exe name", text("real_exe_name"), "For games whose launcher starts another exe."),
            field("Steam app ID", num("steam_app_id")),
            field("Beta branch", text("steam_beta")),
            field("Warn below free RAM (MB)", num("min_free_ram_mb", { min: "0" })),
            check("steam_validate", "Validate files on update")))),
      el("section", { class: "panel", id: "f-stop" },
        el("header", {}, el("h2", {}, "Stopping")),
        el("div", { class: "flabel" }, "Steps, in order"),
        stepRows,
        el("div", { class: "btnrow" }, el("button", { class: "btn sm", type: "button", onclick: () => { addStepRow(); markChanged(); } }, icon("plus"), "Add step")),
        el("p", { class: "hint" }, "Each step runs, then waits up to its seconds for the exit (\"Wait for a save\" waits for a file in the save folder to change, then goes on). Kill always comes last. No steps = Ctrl+Break, 30 s, kill."),
        warningFields()),
      el("section", { class: "panel", id: "f-remote" },
        el("header", {}, el("h2", {}, "Remote access and player count")),
        el("div", { class: "fgrid" },
          field("RCON port", num("rcon_port", { min: "1", max: "65535" })), passwordField("rcon", "RCON password"),
          field("REST port", num("rest_port", { min: "1", max: "65535" }), "Palworld's REST API (8212)."), passwordField("rest", "REST password"),
          ...playersFields()),
        el("p", { class: "hint" }, "Passwords are kept in the data folder's secrets file, never shown again and never in Activity.")),
      el("section", { class: "panel", id: "f-backups" },
        el("header", {}, el("h2", {}, "Backups and Discord")),
        el("div", { class: "fgrid" },
          field("Save folder", text("save_dir")),
          field("Back up every (hours)", num("backup_every_h", { min: "0", step: "any" }), "While it runs. 0 = only by hand and before updates."),
          field("Backups to keep", num("backup_keep", { min: "1" }))),
        el("div", { class: "flabel" }, "Post to Discord when"),
        notifyBoxes(),
        el("p", { class: "hint" }, "The Discord channel and the defaults for new servers are in ", el("a", { href: "#prefs/s-discord" }, "Settings"), ".")),
      editing ? el("section", { class: "panel dangerzone", id: "f-danger" },
        el("header", {}, el("h2", {}, "Delete this server")),
        el("p", { class: "hint" }, "Removes it from Basecamp. Game files, saves and backups stay on disk."),
        el("div", { class: "btnrow" }, el("button", { class: "btn danger", type: "button", onclick: remove }, icon("trash"), "Delete server"))) : null,
      messages,
      el("div", { class: "stickybar" }, barText,
        el("a", { class: "btn", href: editing ? `#server/${id}` : "#dashboard" }, "Cancel"),
        el("button", { class: "btn primary", type: "button", onclick: submit }, editing ? "Save" : "Add server")),
    ];
    formBox.replaceChildren(...sections.filter(Boolean));
  };
  formBox.addEventListener("input", markChanged);
  formBox.addEventListener("change", markChanged);

  const index = formIndex("Form sections", [
    ...(editing ? [] : [["f-start", "Start from"]]), ["f-basics", "Basics"], ["f-ports", "Ports and joining"], ["f-running", "Running"],
    ["f-stop", "Stopping"], ["f-remote", "Remote access"], ["f-backups", "Backups and Discord"],
    ...(editing ? [["f-danger", "Delete", "danger-link"]] : [])]);
  const headTitle = el("h1", {}, editing ? "Edit server" : "Add a server that's already installed");
  root.replaceChildren(el("section", { class: "view mid", id: "v-edit" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" },
      editing ? crumb(["Servers", "#dashboard"], ["…", `#server/${id}`], "Edit settings") : crumb(["Servers", "#dashboard"], "Add existing server"),
      headTitle,
      editing ? null : el("p", { class: "muted" }, "For a fresh install, use ", el("a", { href: "#newgame" }, "New game"), " instead. Editing a server uses this same form."))),
    el("div", { class: "formlayout" }, index, formBox)));

  if (editing) {
    api("GET", `/api/servers/${id}`).then((s) => {
      fill({ ...s.config, password_set: (s.remote || {}).password_set });
      headTitle.textContent = `Edit ${s.name}`;
      $(".crumb a:nth-of-type(2)", root).textContent = s.name;
    }).catch((e) => errorView(root, e.message));
  } else {
    fill({});
    api("GET", "/api/templates").then((res) => {
      for (const t of res.templates) {
        tplById[t.id] = t;
        tplSelect.append(el("option", { value: t.id }, `${t.name}${t.source === "data folder" ? " (yours)" : ""}`));
      }
    }).catch(() => {});
    api("GET", "/api/steam/library").then((res) => { library = res; showLibrary(); })
      .catch((e) => libraryHint.replaceChildren(e.message));
  }
  return { dirty: () => changed, update: () => {} };
}

// -- new game from a template, in one flow (D-130) -------------------------------------

const SETUP_STEPS = [["pick", "Pick a game"], ["install", "Install"], ["first_start", "First start"],
  ["values", "Values"], ["firewall", "Firewall"], ["ready", "Ready"]];

function stepper(current, states = {}) {
  return el("nav", { class: "stepper", "aria-label": "Setup steps" }, SETUP_STEPS.map(([key, label]) => {
    const state = states[key] || (key === current ? "current" : "todo");
    return el("span", { class: state === "done" || state === "skipped" ? "done" : state === "failed" ? "failed" : "",
      "aria-current": state === "current" || state === "failed" ? "step" : null }, label);
  }));
}

async function newGamePage(root) {
  const res = await api("GET", "/api/templates");
  let chosen = null;
  const about = el("div", { class: "stack", style: "gap:10px" });
  const nameInput = el("input", { type: "text", id: "ng-name", autocomplete: "off", placeholder: "The game's name" });
  const dirInput = el("input", { type: "text", id: "ng-dir", autocomplete: "off", spellcheck: "false", placeholder: "SteamCMD's usual folder" });
  const out = el("div");
  const go = el("button", { class: "btn primary lg", type: "button", disabled: true, onclick: () => begin() }, "Pick a game");
  const tiles = res.templates.map((t) => el("button", { type: "button", class: "game", "aria-pressed": "false", onclick: (e) => pick(t, e.currentTarget) },
    el("span", { class: "mono", "aria-hidden": "true" }, monogram(t.name)),
    el("span", {}, el("b", {}, t.name), el("span", { class: "hint" }, t.source === "data folder" ? "your template" : t.steam_app_id ? `app ${t.steam_app_id}` : "custom"))));
  const pick = (t, tile) => {
    chosen = t;
    tiles.forEach((x) => x.setAttribute("aria-pressed", String(x === tile)));
    nameInput.placeholder = t.name;
    dirInput.placeholder = t.kind === "steam" ? `steamapps\\common\\${t.steam_installdir || t.id}` : "The folder it's in";
    const asks = t.required_values.map((v) => v.label.toLowerCase());
    about.replaceChildren(...[
      t.unverified.length || asks.length ? el("div", { class: "callout", "data-st": "warn" }, icon("alert"), el("div", { class: "grow" }, el("div", {},
        t.unverified.length ? [el("strong", {}, "Not yet checked in this template: "), `${t.unverified.join(", ")}. `] : null,
        asks.length ? `It will ask for ${asks.join(" and ")}.` : null))) : null,
      t.notes ? el("details", {}, el("summary", { class: "flabel" }, "Setup notes"), el("p", { class: "hint", style: "margin-top:8px" }, t.notes)) : null].filter(Boolean));
    go.disabled = false;
    go.textContent = t.kind === "steam" ? `Install ${t.name}` : `Set up ${t.name}`;
  };
  const begin = async () => {
    if (!chosen) return;
    go.disabled = true;
    try {
      const v = await api("POST", "/api/setups", { template_id: chosen.id, name: nameInput.value.trim(), install_dir: dirInput.value.trim() });
      location.hash = `#setup/${v.server_id}`;
    } catch (e) { out.replaceChildren(callout("bad", e.message)); go.disabled = false; }
  };
  root.replaceChildren(el("section", { class: "view narrow", id: "v-newgame" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, el("h1", {}, "New game"),
      el("p", { class: "muted" }, "Basecamp installs it, starts it once to write its config, asks for what it needs, checks the firewall, then it's ready. You can leave and come back; the dashboard keeps your place."))),
    stepper("pick"),
    el("div", { class: "panel" },
      res.templates.length ? el("div", { class: "games", role: "group", "aria-label": "Game" }, tiles) : el("p", { class: "hint" }, "No templates. ", el("a", { href: "#templates" }, "Add one"), "."),
      about,
      el("div", { class: "fgrid" }, field("Server name", nameInput),
        field("Install folder", dirInput, "Leave empty for SteamCMD's usual folder. It must be new or empty. For a game that's already installed, use Add existing server.")),
      out,
      el("div", { class: "btnrow" }, go, el("a", { class: "btn ghost", href: "#dashboard" }, "Cancel")))));
  return null;
}

function setupPage(root, id) {
  let view = null;
  let shownKey = "";
  const stepBox = el("div");
  const head = el("div", { class: "stack", style: "gap:10px" });
  const body = el("div", { class: "panel" });
  root.replaceChildren(el("section", { class: "view narrow", id: "v-setup" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, el("h1", {}, "New game"),
      el("p", { class: "muted" }, "It moves on by itself. You can leave and come back; the dashboard keeps your place."))),
    stepBox, head, body));

  const call = async (path, payload, btn) => {
    if (btn) btn.disabled = true;
    try { view = await api("POST", `/api/setups/${id}/${path}`, payload); shownKey = ""; render(); }
    catch (e) { toast(e.message, "bad"); }
    if (btn) btn.disabled = false;
  };
  const finish = async (start, btn) => {
    if (!start && btn.dataset.leave && !(await confirmSheet({ title: "Leave the setup?", body: "The server stays as it is; carry on from the dashboard or its page.", ok: "Leave setup" }))) return;
    btn.disabled = true;
    try { await api("POST", `/api/setups/${id}/finish${start ? "?start=true" : ""}`); location.hash = `#server/${id}`; }
    catch (e) { toast(e.message, "bad"); btn.disabled = false; }
  };
  const leaveBtn = () => el("button", { class: "btn ghost", type: "button", "data-leave": "1", onclick: (e) => finish(false, e.currentTarget) }, "Leave setup");

  const valuesForm = () => {
    const inputs = view.values.map((v) => el("input", { type: "text", value: v.current ?? "", autocomplete: "off", spellcheck: "false" }));
    return el("div", { class: "stack", style: "gap:14px" },
      el("p", { class: "hint" }, "They go into its config file the same way the config editor saves it, keeping a copy of the old file."),
      ...view.values.map((v, i) => field(v.label, inputs[i], el("span", {}, v.hint ? `${v.hint} ` : "",
        el("code", {}, `${v.file.split(/[\\/]/).pop()}${v.section ? ` [${v.section}]` : ""} ${v.key}`), v.error ? el("span", { style: "color:var(--warn)" }, ` ${v.error}`) : null))),
      el("div", { class: "btnrow" }, el("button", { class: "btn primary", type: "button", onclick: (e) =>
        call("values", { values: Object.fromEntries(inputs.map((x, i) => [String(i), x.value])) }, e.currentTarget) },
      view.step === "values" ? "Save and go on" : "Save again"), view.step === "values" ? leaveBtn() : null));
  };

  const render = () => {
    if (!view) return;
    const s = serverById(id);
    const failed = view.status === "failed";
    const states = { pick: "done" };
    for (const st of view.steps) states[st.id] = failed && st.state === "current" ? "failed" : st.state;
    stepBox.replaceChildren(stepper(view.step, states));
    head.replaceChildren(...[
      el("div", { class: "row", style: "border:0;padding:0" }, el("span", { class: "mono", "aria-hidden": "true" }, monogram(view.name)),
        el("div", { class: "grow" }, el("a", { class: "srv-name", href: `#server/${id}` }, view.name)),
        s ? el("span", { class: "pill", "data-st": stateOf(s).st }, stateMark(stateOf(s)), stateLabel(stateOf(s))) : null),
      view.message ? callout(failed ? "bad" : "upd", view.message) : null].filter(Boolean));
    const job = s && s.job;
    if (view.status === "running" || view.running) {
      shownKey = "";
      if (view.step === "install") {
        body.replaceChildren(el("h2", {}, "Installing with SteamCMD"), progressBar(job) || el("div", { class: "progress" }, el("i", { style: "width:0" })),
          el("p", { class: "hint num" }, job ? (PHASES[job.phase] || job.phase) + (job.progress !== null && job.progress !== undefined ? `, ${Math.round(job.progress)}%` : "") : "Starting SteamCMD…",
            ". Output is in the server's ", el("a", { href: `#server/${id}` }, "console"), "."),
          job ? el("div", { class: "btnrow" }, el("button", { class: "btn", type: "button", onclick: async () => {
            if (!(await confirmSheet({ title: "Stop SteamCMD?", body: `${view.name} is left half installed. Try again from here later.`, ok: "Stop SteamCMD", danger: true }))) return;
            try { await api("POST", `/api/servers/${id}/steam/cancel`); toast("Cancelling"); } catch (e) { toast(e.message, "bad"); }
          } }, "Cancel install")) : null);
      } else {
        body.replaceChildren(el("h2", {}, "Starting it once so it writes its config files"),
          el("p", { class: "hint" }, "Waiting for the ready line, then stopping it. Usually under a minute. Output is in the server's ", el("a", { href: `#server/${id}` }, "console"), "."));
      }
      return;
    }
    const key = `${view.step} ${view.status} ${view.running}`;
    if (key === shownKey) return;  // don't wipe what the owner is typing
    shownKey = key;
    if (failed) {
      const last = s && s.steam && s.steam.last;
      body.replaceChildren(el("h2", {}, "Stopped"),
        el("p", { class: "hint" }, "Fix what it says above, then try again."),
        view.step === "install" && last ? el("p", {}, el("a", { href: `/api/servers/${id}/steam/runs/${last.run_id}/log`, target: "_blank", rel: "noopener" }, "SteamCMD output")) : null,
        el("div", { class: "btnrow" }, el("button", { class: "btn primary", type: "button", onclick: (e) => call("retry", undefined, e.currentTarget) }, "Try again"),
          el("a", { class: "btn", href: `#server/${id}` }, "Server page"), leaveBtn()));
      return;
    }
    if (view.step === "values") {
      body.replaceChildren(el("h2", {}, "Values it needs"), valuesForm());
    } else if (view.step === "firewall") {
      const box = el("div");
      const draw = () => {
        const f = firewallOf(id);
        box.replaceChildren(...(f ? [
          f.ports.length ? el("div", { class: "tablewrap" }, el("table", {}, el("tbody", {}, f.ports.map((p) => el("tr", {},
            el("td", { class: "num" }, `${p.protocol.toUpperCase()} ${p.port}`, p.label ? el("div", { class: "hint" }, p.label) : null),
            el("td", {}, el("span", { class: `chip ${FW_CLASS[p.status] || ""}` }, FW_TEXT[p.status] || p.status)),
            el("td", { class: "hint" }, p.detail)))))) : el("p", { class: "hint" }, "No ports listed."),
          ...(f.commands || []).map((c) => el("div", { class: "field" }, el("span", { class: "flabel" }, "Run this in an admin PowerShell in Basecamp's program folder on the host PC"),
            el("div", { class: "addr" }, el("code", {}, c), el("button", { class: "btn ghost sm", type: "button", onclick: () => copyText(c, "Copied the command") }, icon("copy"), "Copy"))))]
          : [el("p", { class: "hint" }, firewallInfo && firewallInfo.error ? firewallInfo.error : "Reading the firewall rules…")]));
      };
      draw();
      loadFirewall(true).then(draw);
      body.replaceChildren(el("h2", {}, "Firewall"),
        el("p", { class: "hint" }, "Friends can only join if Windows Firewall lets the game's ports in from Tailscale."),
        box,
        el("div", { class: "btnrow" },
          el("button", { class: "btn", type: "button", onclick: async () => { await loadFirewall(true); draw(); } }, icon("refresh"), "Check again"),
          el("button", { class: "btn primary", type: "button", onclick: (e) => call("next", undefined, e.currentTarget) }, "Next"), leaveBtn()),
        view.values.length ? el("details", {}, el("summary", { class: "flabel" }, "Change the values"), valuesForm()) : null);
    } else if (view.step === "ready") {
      const addr = s && s.join_address;
      body.replaceChildren(el("h2", {}, `${view.name} is ready to start`),
        addr ? addrBox(addr) : el("p", { class: "hint" }, "No join address yet: set the join host in ", el("a", { href: "#prefs/s-join" }, "Settings"), "."),
        el("div", { class: "btnrow" },
          el("button", { class: "btn primary lg", type: "button", onclick: (e) => finish(true, e.currentTarget) }, icon("play"), `Start ${view.name}`),
          el("button", { class: "btn", type: "button", onclick: (e) => finish(false, e.currentTarget) }, "Done, don't start")));
    } else {
      body.replaceChildren(el("p", { class: "hint" }, "Working…"), leaveBtn());
    }
  };

  const load = async () => {
    try { view = await api("GET", `/api/setups/${id}`); render(); }
    catch (e) { clearInterval(timer); errorView(root, e.message, `#server/${id}`); }
  };
  const timer = setInterval(load, 1500);
  load();
  return { update: render, close: () => clearInterval(timer) };
}

// -- templates (D-129) -----------------------------------------------------------------

const TEMPLATE_DOCS = "https://github.com/dwyer94/basecamp/blob/main/docs/08-templates.md";

async function templatesPage(root) {
  const res = await api("GET", "/api/templates");
  const again = () => templatesPage(root).then((p) => { page = p; }).catch((e) => errorView(root, e.message));
  const remove = async (tid, overrides) => {
    const go = await confirmSheet({ title: `Delete the template ${tid}?`,
      body: `Servers made from it keep their settings.${overrides ? " The built-in template of the same name comes back." : ""}`, ok: "Delete template", danger: true });
    if (!go) return;
    try { await api("DELETE", `/api/templates/${encodeURIComponent(tid)}`); toast(`Deleted ${tid}`); again(); }
    catch (e) { toast(e.message, "bad"); }
  };
  const brokenId = (file) => { const m = file.match(/^data folder: ([a-z0-9][a-z0-9-]{0,62})\.json$/); return m ? m[1] : null; };
  const portsText = (ports) => {
    const by = {};
    for (const p of ports) (by[p.protocol.toUpperCase()] = by[p.protocol.toUpperCase()] || []).push(p.port);
    return Object.entries(by).map(([proto, list]) => `${proto} ${list.join(" and ")}`).join(", ");
  };
  const rows = res.templates.map((t) => el("div", { class: "row" },
    el("span", { class: "mono", "aria-hidden": "true" }, monogram(t.name)),
    el("div", { class: "grow" }, el("b", {}, t.name),
      el("div", { class: "hint" }, [t.exe, portsText(t.ports), t.steam_app_id ? `app ${t.steam_app_id}` : "",
        t.quick_commands && t.quick_commands.length ? `${t.quick_commands.length} quick command${t.quick_commands.length === 1 ? "" : "s"}` : ""].filter(Boolean).join(", ")),
      t.unverified.length ? el("div", { class: "hint", style: "color:var(--warn)" }, `Not yet checked: ${t.unverified.join(", ")}`) : null),
    el("span", { class: `chip ${t.overrides ? "c-upd" : t.source === "data folder" ? "c-ok" : ""}` },
      t.overrides ? "Yours, replaces built-in" : t.source === "data folder" ? "Yours" : "Built-in"),
    t.source === "data folder" ? el("button", { class: "btn sm danger", type: "button", onclick: () => remove(t.id, t.overrides) }, "Delete") : null));
  const errors = res.errors.map((e) => el("div", { class: "row" }, el("span", { class: "mono", style: "color:var(--bad)" }, "!"),
    el("div", { class: "grow" }, el("b", {}, e.file), el("div", { class: "hint", style: "color:var(--bad)" }, e.error)),
    brokenId(e.file) ? el("button", { class: "btn sm danger", type: "button", onclick: () => remove(brokenId(e.file), false) }, "Delete this file") : null));
  root.replaceChildren(el("section", { class: "view mid", id: "v-templates" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, el("h1", {}, "Game templates"),
      el("p", { class: "muted" }, "What Basecamp knows about each game: its exe, ports, stop steps and the values it asks for. Built-in ones ship with Basecamp; yours live in ", el("code", {}, res.data_folder), ".")),
    el("button", { class: "btn primary", type: "button", onclick: () => jumpTo("tpl-add") }, icon("plus"), "Add template")),
    el("div", { class: "twocol" },
      el("div", { class: "panel" }, el("div", { class: "list" }, rows, errors)),
      addTemplatePanel(again))));
  return null;
}

// Add a template (D-129): paste JSON or pick a .json file; checked like a file (D-31).
function addTemplatePanel(done) {
  const MAX = 256 * 1024;
  const area = el("textarea", { id: "tpl-paste", rows: 9, spellcheck: "false", autocapitalize: "off",
    placeholder: ['{', '  "id": "mygame",', '  "name": "My Game",', '  "steam_app_id": 123456,', '  "exe": "MyGameServer.exe",',
      '  "ports": [{"port": 7777, "protocol": "udp", "label": "game"}]', '}'].join("\n") });
  const file = el("input", { type: "file", id: "tpl-file", accept: ".json,application/json", hidden: true });
  const out = el("div");
  const readFile = async (f) => {
    if (!f) return;
    if (f.size > MAX) { out.replaceChildren(callout("bad", `${f.name} is ${Math.round(f.size / 1024)} KB; a template is at most 256 KB.`)); return; }
    area.value = await f.text();
    out.replaceChildren(callout("run", `Loaded ${f.name}. Check or save it.`));
  };
  file.addEventListener("change", () => readFile(file.files[0]));
  const drop = el("label", { class: "drop", for: "tpl-file" }, icon("upload"), el("b", { style: "color:var(--ink)" }, "Choose a .json file"),
    el("span", { class: "small" }, "or drop it here. On a phone, pick it from Files or Downloads."), file);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); readFile(e.dataTransfer.files[0]); });
  const send = async (save) => {
    const text = area.value;
    if (!text.trim()) { out.replaceChildren(callout("bad", "Paste a template or pick a file first.")); return; }
    try {
      const check = await api("POST", "/api/templates", { text, dry_run: true });
      const unv = check.unverified.length ? ` Not yet checked: ${check.unverified.join(", ")}.` : "";
      const conflict = check.conflict === "override" ? " Saving it replaces the built-in template while it's there."
        : check.conflict === "replace" ? " Saving it replaces your data-folder template with that id." : "";
      if (!save) { out.replaceChildren(callout("run", el("span", {}, el("strong", {}, `${check.name} (${check.id}) is valid.`), unv + conflict))); return; }
      if (check.conflict && !(await confirmSheet({ title: check.conflict === "override" ? `Replace the built-in ${check.id}?` : `Replace your ${check.id}?`,
        body: check.conflict === "override" ? "Delete yours later to get the built-in one back." : "The old file is overwritten.", ok: "Save template" }))) return;
      const saved = await api("POST", "/api/templates", { text, replace: Boolean(check.conflict) });
      toast(`Saved ${saved.id}`);
      done();
    } catch (e) {
      out.replaceChildren(el("div", { class: "callout", "data-st": "bad" }, icon("alert"), el("div", { class: "grow" }, el("strong", {}, "Not valid"), el("pre", {}, e.message))));
    }
  };
  return el("div", { class: "panel stick", id: "tpl-add" },
    el("header", {}, el("h2", {}, "Add a template")),
    drop,
    field("Or paste it", area),
    out,
    el("div", { class: "btnrow" },
      el("button", { class: "btn", type: "button", onclick: () => send(false) }, "Check"),
      el("button", { class: "btn primary", type: "button", onclick: () => send(true) }, "Save template"),
      el("a", { class: "btn ghost", href: TEMPLATE_DOCS, target: "_blank", rel: "noreferrer" }, "Template format")));
}

// -- settings (D-52) and Discord (R-60..R-62) -------------------------------------------

async function settingsPage(root, jump) {
  const res = await api("GET", "/api/settings");
  const n = res.notify;
  const reload = () => settingsPage(root).then((p) => { page = p; });
  const putSettings = async (body, btn, msg = "Saved") => {
    if (btn) btn.disabled = true;
    try {
      const out = await api("PUT", "/api/settings", body);
      me = await api("GET", "/api/me");
      toast(out.changed.length || out.servers_updated.length ? `${msg}${out.servers_updated.length ? `; ${out.servers_updated.length} servers updated` : ""}` : "Nothing changed");
      reload();
    } catch (e) { toast(e.message, "bad"); if (btn) btn.disabled = false; }
  };
  const saveBtn = (fn, label = "Save") => el("button", { class: "btn primary", type: "button", onclick: (e) => fn(e.currentTarget) }, label);

  // join address
  const joinInput = el("input", { type: "text", id: "s-host", value: res.join_host, placeholder: "e.g. 100.64.0.10", autocomplete: "off", spellcheck: "false" });
  const joinCard = el("section", { class: "panel", id: "s-join" }, el("header", {}, el("h2", {}, "Join address")),
    field("Join host", joinInput, "The host PC's Tailscale address (or name) friends type in. Copy buttons give this plus each server's join port."),
    el("div", { class: "btnrow" }, saveBtn((b) => putSettings({ join_host: joinInput.value.trim() }, b))));

  // friends (D-131)
  const modes = [["off", "Only me", "Friends can look, not touch."],
    ["logins", "These Tailscale logins", "One per line, as in the Tailscale admin list."],
    ["anyone", "Anyone who can open the page", "Everyone the host PC is shared with on Tailscale."]];
  const logins = el("textarea", { rows: 3, spellcheck: "false", autocapitalize: "off", placeholder: "pal@example.com", "aria-label": "Tailscale logins" });
  logins.value = res.friend_control_logins.join("\n");
  const loginsBox = el("div", { class: "field" }, logins, el("span", { class: "hint", style: "color:var(--warn)" },
    "Not yet checked (Q-02): whether a friend's request through tailscale serve carries their login. If it doesn't, this list lets nobody in; \"Anyone\" works either way."));
  const radios = modes.map(([value, title, hint]) => el("label", {}, el("input", { type: "radio", name: "fc", value, checked: res.friend_control === value,
    onchange: () => { loginsBox.hidden = value !== "logins"; } }), el("span", {}, el("b", {}, title), el("span", { class: "hint" }, hint))));
  loginsBox.hidden = res.friend_control !== "logins";
  const friendsCard = el("section", { class: "panel", id: "s-friends" }, el("header", {}, el("h2", {}, "Friends")),
    el("p", { class: "hint" }, "Friends always see each server's state, player count, join address, console (read-only) and Activity. Here you choose who may also start, stop, restart and update, and tap the quick commands marked for friends. Never config, delete, typing commands, backups or passwords."),
    el("div", { class: "choice" }, radios), loginsBox,
    el("p", { class: "hint" }, `A stop, restart or update still warns players first. Each server takes one friend click per ${res.friend_control_gap_s} s, and every click goes in Activity with their login.`),
    el("div", { class: "btnrow" }, saveBtn((b) => putSettings({ friend_control: $("input[name=fc]:checked", friendsCard).value,
      friend_control_logins: logins.value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean) }, b))));

  // Discord
  const live = res.live_status;
  const urlInput = el("input", { type: "url", id: "s-hook", placeholder: "https://discord.com/api/webhooks/…", autocomplete: "off" });
  const liveBox = el("input", { type: "checkbox", checked: live.on });
  const test = async (serverId) => {
    try { await api("POST", `/api/notify/test${serverId ? `?server_id=${serverId}` : ""}`); toast("Test message sent"); }
    catch (e) { toast(e.message, "bad"); }
  };
  const saveHook = async (serverId, input, btn) => {
    const url = input.value.trim();
    if (!url && !serverId && n.configured && !(await confirmSheet({ title: "Remove the Discord channel?", body: "Nothing will post to Discord.", ok: "Remove", danger: true }))) return;
    btn.disabled = true;
    try { await api("PUT", serverId ? `/api/servers/${serverId}/webhook` : "/api/notify", { webhook_url: url }); toast(url ? "Saved" : "Removed"); reload(); }
    catch (e) { toast(e.message, "bad"); btn.disabled = false; }
  };
  const ownCount = Object.keys(n.servers).length;
  const perServer = servers.map((s) => {
    const own = n.servers[String(s.id)];
    const input = el("input", { type: "url", placeholder: own ? "Leave empty and save to remove" : "Optional: its own webhook", autocomplete: "off", "aria-label": `${s.name} webhook` });
    return el("div", { class: "row" }, el("div", { class: "grow" }, el("b", {}, s.name),
      el("div", { class: "hint" }, own ? el("span", {}, "Own channel: ", el("code", {}, own)) : "Uses the main one."), input),
    el("div", { class: "btnrow" }, el("button", { class: "btn sm", type: "button", onclick: (e) => saveHook(s.id, input, e.currentTarget) }, "Save"),
      own ? el("button", { class: "btn sm", type: "button", onclick: () => test(s.id) }, "Test") : null));
  });
  const liveText = live.on ? (live.error ? `Not updated: ${live.error}. Basecamp keeps trying.`
    : `${live.has_message ? "One message in the channel, edited as servers change" : "Posts its message when a server changes"}.${live.edits ? ` ${live.edits} edits since Basecamp started.` : ""}`)
    : "One message in the channel, edited as servers change. Event messages post either way.";
  const discordCard = el("section", { class: "panel", id: "s-discord" },
    el("header", {}, el("h2", {}, "Discord"), el("span", { class: `chip ${n.configured ? "c-ok" : ""}` }, n.configured ? "Channel set" : "Not set")),
    n.configured ? el("div", { class: "addr" }, el("code", {}, n.webhook), el("button", { class: "btn ghost sm", type: "button", onclick: () => test(null) }, "Send a test")) : null,
    el("p", { class: "hint" }, "Basecamp posts to one channel through a webhook (Channel settings, Integrations, Webhooks in Discord). It's kept in the data folder's secrets file and never shown in full."),
    el("div", { class: "field" }, el("label", { for: "s-hook" }, n.configured ? "Replace with" : "Webhook address"), urlInput,
      n.configured ? el("span", { class: "hint" }, "Leave empty and save to remove it.") : null),
    el("div", { class: "btnrow" }, el("button", { class: "btn primary", type: "button", onclick: (e) => saveHook(null, urlInput, e.currentTarget) }, "Save")),
    el("label", { class: "check" }, liveBox, el("span", {}, "Live status message", el("span", { class: "hint", style: "display:block" }, liveText))),
    el("div", { class: "btnrow" }, el("button", { class: "btn sm", type: "button", onclick: (e) => putSettings({ discord_live_status: liveBox.checked }, e.currentTarget) }, "Save live status")),
    el("details", { open: ownCount > 0 }, el("summary", { class: "flabel" }, `A different channel per server (${ownCount ? `${ownCount} set` : "none set"})`),
      el("p", { class: "hint", style: "margin:8px 0" }, "Which events post is set in each server's settings."), el("div", { class: "list" }, perServer)),
    el("p", { class: "hint num" }, `Sent ${n.sent}, failed ${n.failed}, waiting ${n.pending} since Basecamp started.`,
      n.failed ? el("span", {}, " Failures are in ", el("a", { href: "#activity" }, "Activity"), ".") : null));

  // new server defaults
  const everyInput = el("input", { type: "number", id: "s-every", min: "0", step: "any", value: res.default_backup_every_h });
  const keepInput = el("input", { type: "number", id: "s-keep", min: "1", value: res.default_backup_keep });
  const allBox = el("input", { type: "checkbox" });
  const backupCard = el("section", { class: "panel", id: "s-backup" }, el("header", {}, el("h2", {}, "New server defaults")),
    el("div", { class: "fgrid" }, field("Back up every (hours)", everyInput, "While it runs. 0 = only by hand and before updates."), field("Backups to keep", keepInput)),
    el("label", { class: "check" }, allBox, el("span", {}, `Also give all ${servers.length} existing servers this schedule`)),
    el("p", { class: "hint" }, "Stored in ", el("code", {}, res.backups_dir), "."),
    el("div", { class: "btnrow" }, saveBtn(async (b) => {
      if (allBox.checked && !(await confirmSheet({ title: `Change all ${servers.length} servers?`, body: "Each gets this backup schedule and count.", ok: "Change them" }))) return;
      putSettings({ default_backup_every_h: Number(everyInput.value || 0), default_backup_keep: Number(keepInput.value || 1), apply_backup_defaults: allBox.checked }, b);
    })));

  // SteamCMD
  const steamInput = el("input", { type: "text", id: "s-scmd", value: res.steamcmd_path, placeholder: res.steamcmd, autocomplete: "off", spellcheck: "false" });
  const steamCard = el("section", { class: "panel", id: "s-steam" },
    el("header", {}, el("h2", {}, "SteamCMD"), el("span", { class: `chip ${res.steamcmd_found ? "c-ok" : "c-warn"}` }, res.steamcmd_found ? "Found" : "Not found")),
    field("steamcmd.exe", steamInput, el("span", {}, "Empty = the default. Now: ", el("code", {}, res.steamcmd))),
    el("div", { class: "btnrow" }, saveBtn((b) => putSettings({ steamcmd_path: steamInput.value.trim() }, b))));

  // Basecamp updates
  const u = res.update;
  const hourSelect = el("select", { id: "s-auto" }, el("option", { value: "-1", selected: res.update_auto_hour === -1 }, "Off, I'll install by hand"),
    [...Array(24).keys()].map((h) => el("option", { value: String(h), selected: res.update_auto_hour === h }, `${String(h).padStart(2, "0")}:00`)));
  const betaBox = el("input", { type: "checkbox", checked: res.update_beta });
  const statusChip = { idle: ["c-ok", "Up to date"], checking: ["", "Checking…"], downloading: ["c-upd", "Downloading…"],
    ready: ["c-upd", `${u.ready_version} ready`], failed: ["c-warn", "Check failed"], installing: ["c-upd", "Installing…"] }[u.status] || ["", u.status];
  const checkNow = async (btn) => {
    btn.disabled = true;
    try {
      let v = await api("POST", "/api/update/check");
      for (let i = 0; i < 150 && ["checking", "downloading"].includes(v.status); i++) {
        await new Promise((r) => setTimeout(r, 2000));
        v = await api("GET", "/api/update");
      }
      toast(v.ready ? `${v.ready_version} is ready to install (see the dashboard)` : v.status === "failed" ? `Check failed: ${v.error}` : "Up to date");
      reload();
    } catch (e) { toast(e.message, "bad"); btn.disabled = false; }
  };
  const updatesCard = el("section", { class: "panel", id: "s-updates" },
    el("header", {}, el("h2", {}, "Basecamp updates"), el("span", { class: `chip ${statusChip[0]}` }, statusChip[1])),
    el("p", { class: "hint" }, `This is ${u.current}. `, u.last_check ? `Last checked ${new Date(u.last_check).toLocaleString()}` : "Not checked yet",
      u.check_h > 0 ? `, every ${u.check_h} h.` : ". Automatic checks are off (update_check_h = 0).",
      u.status === "failed" && u.error ? el("span", { style: "color:var(--warn)" }, ` ${u.error}`) : null),
    u.has_key ? null : el("p", { class: "hint", style: "color:var(--warn)" }, "This build has no release key, so it can't check for updates."),
    el("div", { class: "fgrid" }, field("Install automatically at", hourSelect, "This PC's time. A version that rolled back once is never auto-installed.")),
    el("label", { class: "check" }, betaBox, el("span", {}, "Beta channel: offer pre-releases too")),
    el("div", { class: "btnrow" }, el("button", { class: "btn", type: "button", onclick: (e) => checkNow(e.currentTarget) }, icon("refresh"), "Check now"),
      saveBtn((b) => putSettings({ update_auto_hour: Number(hourSelect.value), update_beta: betaBox.checked }, b))));

  // settings.toml (D-127)
  const f = res.file;
  const reloadFile = async (btn) => {
    btn.disabled = true;
    try {
      const out = await api("POST", "/api/settings/reload");
      const r = out.reloaded;
      me = await api("GET", "/api/me");
      toast(r.error ? "Not applied: the file has a problem" : r.applied.length || r.restart.length
        ? [r.applied.length ? `Applied ${r.applied.join(", ")}` : "", r.restart.length ? `after a restart: ${r.restart.join(", ")}` : ""].filter(Boolean).join("; ")
        : "No changes");
      if (!me.is_admin) { location.reload(); return; }
      reload();
    } catch (e) { toast(e.message, "bad"); btn.disabled = false; }
  };
  const fileCard = el("section", { class: "panel", id: "s-file" }, el("header", {}, el("h2", {}, "Settings file")),
    el("p", { class: "hint" }, "Admin logins and a few other settings live in ", el("code", {}, res.settings_file), `. Basecamp picks up a saved change within ${f.every_s} s. Only ${f.restart_keys.join(", ")} need a restart.`),
    f.error ? callout("bad", `The file wasn't applied, so the settings are as before: ${f.error}`) : null,
    f.restart_pending.length ? callout("warn", el("span", {}, el("strong", {}, "Used after a restart: "), f.restart_pending.map((p) => `${p.key} ${p.now} to ${p.after}`).join(", "), ".")) : null,
    el("div", { class: "tablewrap" }, el("table", {}, el("tbody", {},
      el("tr", {}, el("td", {}, "Admin logins"), el("td", {}, res.admin_logins.length ? res.admin_logins.join(", ") : "None (only this PC)")),
      el("tr", {}, el("td", {}, "Config copies kept"), el("td", { class: "num" }, String(res.config_copies_keep))),
      el("tr", {}, el("td", {}, "Last read"), el("td", { class: "num" }, whenShort(f.loaded_at)))))),
    el("div", { class: "btnrow" }, el("button", { class: "btn", type: "button", onclick: (e) => reloadFile(e.currentTarget) }, icon("refresh"), "Reload settings")));

  // Restart Basecamp (D-126)
  const restart = async (btn) => {
    if (servers.some((s) => s.job)) { toast("An install or update is running or queued. Wait for it or cancel it first.", "bad"); return; }
    if (!(await confirmSheet({ title: "Restart Basecamp?", body: "The page goes away for a few seconds. Game servers keep running.", ok: "Restart Basecamp", danger: true }))) return;
    try {
      const out = await api("POST", "/api/restart");
      btn.disabled = true;
      toast("Restarting; the page reloads when Basecamp is back");
      waitForRestart(out.started_at);
    } catch (e) { toast(e.message, "bad"); }
  };
  if (u.restarting) waitForRestart(me.started_at);
  const restartCard = el("section", { class: "panel", id: "s-restart" }, el("header", {}, el("h2", {}, "Restart Basecamp")),
    el("p", { class: "hint" }, u.can_install ? "Basecamp exits and its launcher starts it again within seconds. Game servers keep running. An install or update has to finish first."
      : "Basecamp isn't running under its launcher here (python -m basecamp), so nothing would start it again. Restart it by hand."),
    el("div", { class: "btnrow" }, el("button", { class: "btn danger", type: "button", disabled: !u.can_install || u.restarting, onclick: (e) => restart(e.currentTarget) }, icon("refresh"), "Restart Basecamp")));

  root.replaceChildren(el("section", { class: "view mid", id: "v-prefs" },
    el("div", { class: "pagehead" }, el("div", { class: "grow" }, el("h1", {}, "Settings"))),
    el("div", { class: "formlayout" },
      formIndex("Settings sections", [["s-join", "Join address"], ["s-friends", "Friends"], ["s-discord", "Discord"], ["s-backup", "New server defaults"],
        ["s-steam", "SteamCMD"], ["s-updates", "Basecamp updates"], ["s-file", "Settings file"], ["s-restart", "Restart Basecamp"]]),
      el("div", { class: "formsections" }, joinCard, friendsCard, discordCard, backupCard, steamCard, updatesCard, fileCard, restartCard))));
  if (jump) requestAnimationFrame(() => jumpTo(jump));
  return null;
}
