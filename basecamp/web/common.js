// Shared helpers for the page. Plain JS, no build step, nothing from the internet.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// el("div", {class: "x", onclick: fn}, child, "text", [more]) builds a node. A null, false
// or undefined child or attribute is left out.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// replaceChildren prints a null child as "null"; the pages build children with
// `cond ? node : null` everywhere, so leave those out on every call.
const nativeReplaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...nodes) {
  return nativeReplaceChildren.apply(this, nodes.filter((n) => n !== null && n !== undefined && n !== false));
};

// An icon from the sprite in index.html.
function icon(name, extra = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `i ${extra}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// localStorage can throw (private windows, blocked storage): it only remembers niceties.
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* not remembered */ } },
};

// -- theme: follows the system, then light, then dark -----------------------------------

const THEMES = ["system", "light", "dark"];
const THEME_ICON = { system: "auto", light: "sun", dark: "moon" };
let theme = THEMES.includes(store.get("bc-theme")) ? store.get("bc-theme") : "system";

function applyTheme() {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const btn = $("#theme");
  if (!btn) return;
  btn.replaceChildren(icon(THEME_ICON[theme]));
  const label = theme === "system" ? "Theme: follows the system" : `Theme: ${theme}`;
  btn.setAttribute("aria-label", label);
  btn.title = label;
}

function nextTheme() {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  store.set("bc-theme", theme);
  applyTheme();
  toast(theme === "system" ? "Theme follows the system" : `Theme: ${theme}`);
}

// -- toast and confirm sheet ---------------------------------------------------------

function toast(text, kind = "") {
  $$(".toast").forEach((t) => t.remove());
  const t = el("div", { class: `toast ${kind}`, role: kind === "bad" ? "alert" : "status" }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 6000 : 2400);
}

// The in-page sheet that replaces the browser's confirm box: says what will happen. A
// destructive action gets a red button. Resolves true for OK.
function confirmSheet({ title, body = "", ok = "OK", danger = false, cancel = "Cancel" }) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  const bodyNode = $("#confirm-body");
  bodyNode.replaceChildren(...(Array.isArray(body) ? body : [body]).filter(Boolean).map((b) =>
    b instanceof Node ? b : el("p", {}, b)));
  bodyNode.hidden = !bodyNode.childElementCount;
  const okBtn = $("#confirm-ok");
  okBtn.textContent = ok;
  okBtn.className = danger ? "btn danger solid" : "btn primary";
  $("#confirm-cancel").textContent = cancel;
  return new Promise((resolve) => {
    const done = () => { dlg.removeEventListener("close", done); resolve(dlg.returnValue === "ok"); };
    dlg.returnValue = "";
    dlg.addEventListener("close", done);
    if (typeof dlg.showModal === "function") dlg.showModal();
    else { dlg.setAttribute("open", ""); }
    okBtn.focus();
  });
}

// -- copying ---------------------------------------------------------------------------

// navigator.clipboard needs a secure page (HTTPS via tailscale serve, or localhost).
// Anything else falls back to the old select-and-copy trick.
function oldCopy(text) {
  const area = el("textarea", { readonly: true, style: "position:fixed;opacity:0" });
  area.value = text;
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  area.remove();
  return ok;
}

async function copyText(text, what = "Copied") {
  let ok = false;
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { ok = false; }
  }
  if (!ok) ok = oldCopy(text);
  toast(ok ? `${what}: ${text}` : `Couldn't copy. Select it: ${text}`);
}

// -- time and size ---------------------------------------------------------------------

function fmtUp(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min`;
}

function fmtCountdown(stopAt) {
  const left = Math.max(0, Math.round(stopAt - Date.now() / 1000));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function daysAgo(ts) { return Math.round((startOfDay(new Date()) - startOfDay(new Date(ts * 1000))) / 86400000); }
function hhmm(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

// "27 Oct 2026, 05:12"
function fullDate(ts) {
  return new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit" });
}

// "Today 12:00", "Yesterday 04:31", "12 Oct 18:03"
function whenShort(ts) {
  const n = daysAgo(ts);
  if (n === 0) return `Today ${hhmm(ts)}`;
  if (n === 1) return `Yesterday ${hhmm(ts)}`;
  return new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// "just now", "5 min ago", "3 h ago", "yesterday", "2 days ago"
function relTime(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  const n = daysAgo(ts);
  if (n === 0) return `${Math.floor(s / 3600)} h ago`;
  if (n === 1) return "yesterday";
  return `${n} days ago`;
}

function dayLabel(ts) {
  const n = daysAgo(ts);
  if (n === 0) return "Today";
  if (n === 1) return "Yesterday";
  return new Date(ts * 1000).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
}

function fmtMB(mb) {
  if (mb === null || mb === undefined) return "";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

function fmtSize(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${Math.round(bytes / 1024 / 1024)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

// The next HH:MM on this device's clock: "today 05:00" or "tomorrow 05:00".
function nextAt(at) {
  if (!at) return "";
  const [h, m] = at.split(":").map(Number);
  const now = new Date();
  const next = new Date(now);
  next.setHours(h, m, 0, 0);
  return `${next > now ? "today" : "tomorrow"} ${at}`;
}

// -- WebSockets ----------------------------------------------------------------------

function wsUrl(path) {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
}

// Keeps a WebSocket open, reconnecting with back-off. onOpen gets the socket each time.
function keepSocket(path, { onMessage, onOpen, onState }) {
  let delay = 1000;
  let socket = null;
  let closed = false;
  const connect = () => {
    socket = new WebSocket(typeof path === "function" ? wsUrl(path()) : wsUrl(path));
    socket.onopen = () => { delay = 1000; onState && onState(true); onOpen && onOpen(socket); };
    socket.onmessage = (ev) => onMessage(JSON.parse(ev.data));
    socket.onclose = (ev) => {
      if (closed) return;
      onState && onState(false);
      if (ev.code === 4401) { location.reload(); return; }  // admin or not changed (D-127)
      if (ev.code === 4403 || ev.code === 4404) return;
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 15000);
    };
  };
  connect();
  return {
    send: (data) => socket && socket.readyState === 1 && socket.send(JSON.stringify(data)),
    close: () => { closed = true; socket && socket.close(); },
  };
}

// -- API -------------------------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(errorText(data.detail) || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
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

// Two letters for a server's tile: "V Rising" -> "VR", "Enshrouded" -> "En".
function monogram(name) {
  const words = (name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  const w = words[0] || "?";
  return w[0].toUpperCase() + (w[1] || "").toLowerCase();
}
