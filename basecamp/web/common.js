// Shared by the admin page and the friend view. Plain JS, no build step, no CDN.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

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

function toast(text) {
  const t = el("div", { class: "toast", role: "status" }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 2200);
}

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
  if (ok) toast(`${what}: ${text}`);
  else window.prompt("Copy this:", text);
}

function fmtUptime(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `up ${d}d ${h}h`;
  if (h) return `up ${h}h ${m}m`;
  return `up ${m}m`;
}

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
      onState && onState(false);
      if (ev.code === 4401) { location.reload(); return; }  // admin or not changed (D-127)
      if (closed || ev.code === 4403 || ev.code === 4404) return;
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

// The player count (D-132): {count, max}; a null count is "unknown".
function playersText(p) {
  if (p.count === null || p.count === undefined) return "players: unknown";
  return `${p.count}${p.max ? `/${p.max}` : ""} player${p.count === 1 && !p.max ? "" : "s"} on`;
}

function statePill(state) {
  return el("span", { class: `pill ${state}` }, state);
}
