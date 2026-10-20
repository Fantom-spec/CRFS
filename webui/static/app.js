"use strict";
/* CRFS Monitor - every number on screen comes from /api/*; nothing here is sample data. */
const $ = (s, r = document) => r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.setAttribute("style", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
const fmtB = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KiB" : (n / 1048576).toFixed(1) + " MiB";
const fmtT = s => new Date(s * 1000).toLocaleTimeString([], { hour12: false }) + "." + String(Math.floor((s % 1) * 1000)).padStart(3, "0");
const fmtNs = ns => ns ? new Date(Number(ns) / 1e6).toLocaleString() : "-";
const pill = (txt, cls) => h("span", { class: "pill " + (cls || "mute") }, txt);
const REGION_COLOR = { superblock: "#8b5cf6", journal: "#d97706", bitmap: "#2563eb", "inode table": "#0d9488", data: "#475569" };

let TOKEN = "", OV = null, SCREEN = "overview", TIMER = null, IMAGES = [];

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-CRFS-Token": TOKEN }, body: JSON.stringify(body) };
  let r;
  try { r = await fetch(path, opt); } catch (e) { throw new Error("cannot reach the CRFS server: " + e.message); }
  const j = await r.json().catch(() => ({ ok: false, error: { type: "BadResponse", message: "non-JSON response" } }));
  if (!r.ok || j.ok === false) { const er = j.error || {}; const err = new Error(er.message || r.statusText); err.etype = er.type; throw err; }
  return j;
}
function toast(msg, detail, bad) {
  const t = h("div", { class: "toast" + (bad ? " err" : ""), role: bad ? "alert" : "status" }, h("div", {}, msg), detail ? h("div", { class: "m" }, detail) : null);
  $("#toasts").append(t); setTimeout(() => t.remove(), bad ? 9000 : 4000);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg(r)); return r; }
  catch (e) { toast(e.message, e.etype, true); return null; }
}
function modal(title, body, buttons) {
  return new Promise(res => {
    const close = v => { bg.remove(); res(v); };
    const bg = h("div", { class: "modal-bg", onclick: e => { if (e.target === bg) close(null); }, onkeydown: e => { if (e.key === "Escape") close(null); } },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title }, h("h3", {}, title), body,
        h("div", { class: "row end", style: "margin-top:14px" }, buttons(close))));
    $("#modal-root").append(bg);
    const f = bg.querySelector("input,textarea,button.primary,button"); if (f) f.focus();
  });
}
function confirmTyped(title, text, phrase) {
  const inp = h("input", { type: "text", "aria-label": "type to confirm", style: "width:100%", autocomplete: "off" });
  return modal(title, h("div", {}, h("p", {}, text), h("p", {}, "Type ", h("code", {}, phrase), " to confirm:"), inp), close => {
    const ok = h("button", { class: "btn danger", disabled: true, onclick: () => close(phrase) }, "Confirm");
    inp.addEventListener("input", () => { ok.disabled = inp.value !== phrase; });
    inp.addEventListener("keydown", e => { if (e.key === "Enter" && inp.value === phrase) close(phrase); });
    return [h("button", { class: "btn", onclick: () => close(null) }, "Cancel"), ok];
  });
}
function promptModal(title, fields, okLabel) {
  const els = fields.map(f => f.type === "textarea" ? h("textarea", { "aria-label": f.label }, f.value || "")
    : f.type === "select" ? h("select", { "aria-label": f.label }, f.options.map(o => h("option", { value: o }, o)))
      : h("input", { type: f.type || "text", value: f.value ?? "", "aria-label": f.label, style: "width:100%" }));
  const body = h("div", {}, fields.map((f, i) => h("div", { style: "margin-bottom:10px" }, h("div", { class: "lbl" }, f.label), els[i], f.hint ? h("div", { class: "note" }, f.hint) : null)));
  return modal(title, body, close => [h("button", { class: "btn", onclick: () => close(null) }, "Cancel"),
  h("button", { class: "btn primary", onclick: () => close(els.map(e => e.value)) }, okLabel || "OK")]);
}

/* ---------------- shell ---------------- */
const SCREENS = [
  ["overview", "System overview", "M3 12h4l3-8 4 16 3-8h4"],
  ["disk", "Disk explorer", "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"],
  ["files", "File browser", "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
  ["journal", "Journal & transactions", "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM9 9h6M9 13h6"],
  ["lab", "Crash lab", "M13 2 4 14h7l-1 8 9-12h-7z"],
  ["check", "Consistency checker", "M9 12l2 2 4-4M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"],
  ["events", "Event log", "M4 6h16M4 12h16M4 18h10"],
];
function buildNav() {
  const nav = $("#nav"); nav.replaceChildren(h("div", { class: "grp" }, "Monitor"));
  for (const [id, label, d] of SCREENS) {
    const svg = document.createElementNS(SVGNS, "svg"); svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.8"); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
    const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", d); svg.append(p);
    nav.append(h("button", { "aria-current": id === SCREEN ? "page" : false, onclick: () => go(id) }, svg, label));
  }
}
function go(id) { SCREEN = id; location.hash = id; buildNav(); render(); }
async function refreshOverview() {
  try { OV = await api("/api/state"); } catch (e) { OV = null; }
  const cb = $("#crashbar"); cb.replaceChildren();
  if (OV && OV.crashed) {
    const c = OV.crashed;
    cb.append(h("div", { class: "crashbar", role: "alert" },
      h("div", {}, h("b", {}, "CRASHED · recovery has NOT run. "), `The process was killed (returncode ${c.returncode}). Image is mounted read-only exactly as the crash left it. Committed but unapplied tx: ${c.pending_tx.join(", ") || "none"}. `,
        c.mode === "overwrite" ? `Open ${c.path}: the disk still shows the old text.` : `Open ${c.path}: the file exists but is empty.`),
      h("button", { class: "btn primary", onclick: () => act(async () => { const r = await api("/api/recover", {}); await refreshOverview(); render(); return r; }, r => `Recovery replayed ${r.transactions_replayed} transaction(s)`) }, "Run recovery")));
  }
  const mp = $("#mountpill");
  mp.replaceChildren(OV && OV.mounted ? (OV.crashed ? pill("CRASHED · read-only · " + OV.image.split("/").pop(), "bad") : pill("mounted · " + OV.image.split("/").pop(), "ok")) : pill("no image mounted", "warn"));
  return OV;
}
async function refreshImages() {
  const r = await api("/api/images"); IMAGES = r.images;
  const sel = $("#imgsel"); sel.replaceChildren(...(IMAGES.length ? IMAGES : [{ name: "" }]).map(i => h("option", { value: i.name }, i.name ? `${i.name}  (${fmtB(i.size)})` : "— no images yet —")));
  if (r.current) sel.value = r.current;
}
function render() {
  clearInterval(TIMER);
  const m = $("#main"); m.replaceChildren();
  const fn = { overview: vOverview, disk: vDisk, files: vFiles, journal: vJournal, lab: vLab, check: vCheck, events: vEvents }[SCREEN];
  const needs = !["lab", "overview", "events"].includes(SCREEN);
  const run = async () => { await refreshOverview(); if (needs && !(OV && OV.mounted)) m.replaceChildren(noImage()); else await fn(m); };
  run().catch(e => m.replaceChildren(h("div", { class: "banner bad", role: "alert" }, e.message)));
}
const noImage = () => h("div", { class: "empty" }, h("p", {}, "No disk image is mounted."), h("p", { class: "note" }, "Create or open an image from the toolbar above."));
function head(t, s, ...right) { return h("div", { class: "row", style: "justify-content:space-between;align-items:flex-start;margin-bottom:6px" }, h("div", {}, h("h1", {}, t), h("p", { class: "sub" }, s)), h("div", { class: "row" }, right)); }
const kv = rows => h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
function stack(parts) { return h("div", { class: "bar" }, parts.map(([w, c, t]) => h("i", { style: `width:${w}%;background:${c}`, title: t }))); }
const pct = (a, b) => b ? (100 * a / b) : 0;

/* ---------------- 1. overview ---------------- */
async function vOverview(m) {
  if (!OV || !OV.mounted) {
    m.replaceChildren(head("System overview", "Live values read from the mounted image."), noImage()); return;
  }
  const o = OV, j = o.journal;
  const chk = o.last_check;
  m.replaceChildren(
    head("System overview", "Every value below is read from the mounted image or the engine's own counters. Nothing is sampled or simulated.",
      h("button", { class: "btn", onclick: () => act(async () => { await api("/api/checkpoint", {}); await refreshOverview(); render(); }, () => "Checkpoint complete") }, "Force checkpoint")),
    o.read_only ? h("div", { class: "banner warn" }, "Image is mounted read-only: ", "recovery could not complete safely; writes are refused.") : null,
    o.recovery ? h("div", { class: "banner " + (o.recovery.success ? "ok" : "bad") }, `Recovery on last mount: ${o.recovery.replayed} transaction(s) replayed, ${o.recovery.discarded} discarded${o.recovery.warnings.length ? " — " + o.recovery.warnings.join("; ") : ""}.`) : null,
    h("div", { class: "grid g4" },
      h("div", { class: "card" }, h("h2", {}, "Blocks (data region)"), h("div", { class: "big" }, o.blocks.data_used, h("small", {}, ` / ${o.blocks.data_total} used`)),
        stack([[pct(o.blocks.data_used, o.blocks.data_total), "var(--accent-d)", "used"]]), h("div", { class: "lbl", style: "margin-top:6px" }, `${o.blocks.data_free} free · ${o.blocks.total} blocks total × ${o.block_size} B`)),
      h("div", { class: "card" }, h("h2", {}, "Inodes"), h("div", { class: "big" }, o.inodes.used, h("small", {}, ` / ${o.inodes.total} used`)),
        stack([[pct(o.inodes.used, o.inodes.total), "var(--blue)", "used"]]), h("div", { class: "lbl", style: "margin-top:6px" }, `${o.inodes.free} free`)),
      h("div", { class: "card" }, h("h2", {}, "Journal ring"), h("div", { class: "big" }, j.used_blocks, h("small", {}, ` / ${j.capacity_blocks} live`)),
        stack([[pct(j.used_blocks, j.capacity_blocks), "var(--amber)", "live"]]), h("div", { class: "lbl", style: "margin-top:6px" }, `tail ${j.tail} · head ${j.head} · checkpoint tx ${j.checkpoint_txid}`)),
      h("div", { class: "card" }, h("h2", {}, "Consistency"), chk ? h("div", {}, pill(chk.passed ? "last fsck passed" : "last fsck FAILED", chk.passed ? "ok" : "bad"), h("div", { class: "lbl", style: "margin-top:8px" }, `${chk.errors} errors, ${chk.warnings} warnings`)) : h("div", {}, pill("not checked this session", "mute"), h("div", { class: "lbl", style: "margin-top:8px" }, "Run it on the Consistency checker screen.")))),
    h("div", { class: "grid g2", style: "margin-top:14px" },
      h("div", { class: "card" }, h("h2", {}, "Image"), kv([["file", o.image], ["volume label", o.volume_label || "(none)"], ["size", fmtB(o.disk_size)], ["block size", o.block_size + " B"], ["state", pill(o.read_only ? "read-only" : "read-write", o.read_only ? "warn" : "ok")]])),
      h("div", { class: "card" }, h("h2", {}, "Engine mode ", h("span", { class: "tag" }, "configuration")), kv([["disk cache model", o.cache_mode], ["checkpoint policy", o.checkpoint_policy], ["latest transaction", o.latest_tx ? `tx ${o.latest_tx.txid} · ${o.latest_tx.state}` : "none"], ["pending tx (committed, unchecked)", j.pending_tx.length ? j.pending_tx.join(", ") : "none"]]),
        h("p", { class: "note", style: "margin:10px 0 0" }, "volatile = writes sit in a cache until flush(), so only flushed data survives a crash. This is what makes the ordering tests meaningful.")),
      h("div", { class: "card" }, h("h2", {}, "I/O counters ", h("span", { class: "tag" }, "this mount")), kv([["block reads", o.io.reads], ["block writes", o.io.writes], ["flush calls", o.io.flushes], ["blocks made durable", o.io.blocks_flushed]]))));
  TIMER = setInterval(() => { if (SCREEN === "overview") refreshOverview().then(() => vOverview(m)); }, 4000);
}

/* ---------------- 2. disk explorer ---------------- */
let DSEL = { block: null };
function hexdump(hex, limit = 512) {
  const lines = [];
  for (let i = 0; i < Math.min(hex.length / 2, limit); i += 16) {
    let a = "", t = "";
    for (let k = 0; k < 16; k++) {
      const b = parseInt(hex.substr((i + k) * 2, 2), 16);
      a += b.toString(16).padStart(2, "0") + (k === 7 ? "  " : " ");
      t += b >= 32 && b < 127 ? String.fromCharCode(b) : ".";
    }
    lines.push(i.toString(16).padStart(4, "0") + "  " + a + " |" + t + "|");
  }
  return lines.join("\n");
}
async function vDisk(m) {
  const [lay, bm] = await Promise.all([api("/api/layout"), api("/api/bitmap")]);
  const total = lay.regions.reduce((s, r) => s + r.block_count, 0);
  const alloc = new Set(bm.allocated);
  const insp = h("div", { class: "card insp" }, h("div", { class: "empty" }, "Select a block or region."));
  const cells = h("div", { class: "cells" });
  async function pick(n) {
    DSEL.block = n; cells.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.n === n)));
    const b = await act(() => api("/api/block?n=" + n)); if (!b) return;
    const d = b.decoded;
    let dec = null;
    if (b.decoded_kind === "superblock") dec = kv(Object.entries(d).map(([k, v]) => [k, k === "checksum_ok" ? pill(v ? "valid" : "BAD", v ? "ok" : "bad") : String(v)]));
    else if (d) dec = h("pre", { class: "hex", style: "max-height:220px" }, JSON.stringify(d, null, 1));
    insp.replaceChildren(h("h2", {}, `Block ${n} `, pill(b.region, "info")),
      kv([["byte offset", "0x" + b.offset_bytes.toString(16) + " (" + b.offset_bytes + ")"], ["decoded as", b.decoded_kind || "raw"], ["all zero", b.all_zero ? "yes" : "no"]]),
      h("div", { class: "sep" }), dec || null, dec ? h("div", { class: "sep" }) : null,
      h("h2", {}, "Raw bytes (first 512 of 4096)"), h("pre", { class: "hex" }, hexdump(b.hex)));
  }
  lay.regions.forEach(r => { });
  const rb = lay.regions.map(r => h("button", { style: `flex:${Math.max(r.block_count, total * 0.07)};background:${REGION_COLOR[r.name] || "#444"}`, "aria-pressed": "false", title: `${r.name}: blocks ${r.start_block}-${r.end_block}`, onclick: () => pick(r.start_block) }, h("b", {}, r.name), h("span", {}, `${r.start_block}–${r.end_block}`)));
  // sample cells: every block is one cell, region-coloured; data region shows allocated/free
  lay.regions.forEach(r => {
    for (let b = r.start_block; b <= r.end_block; b++) {
      let c = REGION_COLOR[r.name];
      const isData = r.name === "data";
      const used = isData ? alloc.has(b - bm.data_start) : true;
      const cell = h("button", { "data-n": b, "aria-label": `block ${b}`, title: `block ${b} · ${r.name}${isData ? (used ? " · allocated" : " · free") : ""}`, style: `background:${used || !isData ? c : "#1c232c"};opacity:${isData && !used ? 1 : 0.9}`, onclick: () => pick(b) });
      cells.append(cell);
    }
  });
  const ino = await api("/api/inodes");
  m.replaceChildren(head("Virtual disk explorer", "The image as the engine sees it. Region boundaries come from the on-disk superblock; block and inode panes decode the real bytes."),
    h("div", { class: "diskmap", role: "group", "aria-label": "Disk regions" }, rb),
    h("div", { class: "legend" }, Object.entries(REGION_COLOR).map(([k, c]) => h("span", {}, h("i", { style: "background:" + c }), k)), h("span", {}, h("i", { style: "background:#1c232c" }), "free data block")),
    h("div", { class: "grid", style: "grid-template-columns:minmax(0,1fr) 360px;margin-top:14px;align-items:start" },
      h("div", { class: "grid" },
        h("div", { class: "card" }, h("h2", {}, `All ${total} blocks (click one)`), cells),
        h("div", { class: "card flat" }, h("table", {}, h("thead", {}, h("tr", {}, ["region", "blocks", "byte offset", "size", "in use", "meaning"].map(x => h("th", {}, x)))),
          h("tbody", {}, lay.regions.map(r => h("tr", { class: "click", onclick: () => pick(r.start_block) }, h("td", {}, h("span", { class: "mono" }, r.name)), h("td", { class: "mono" }, `${r.start_block}–${r.end_block}`), h("td", { class: "mono" }, "0x" + r.offset_bytes.toString(16)), h("td", {}, fmtB(r.size_bytes)), h("td", { class: "mono" }, `${r.used_blocks}/${r.block_count}`), h("td", { class: "lbl" }, r.note)))))),
        h("div", { class: "card flat" }, h("div", { style: "padding:12px 16px 0" }, h("h2", {}, "Allocated inodes")),
          h("table", {}, h("thead", {}, h("tr", {}, ["inode", "type", "size", "links", "block pointers", "inode-table location"].map(x => h("th", {}, x)))),
            h("tbody", {}, ino.inodes.map(i => h("tr", { class: "click", onclick: () => { pick(i.location.block); } }, h("td", { class: "mono" }, i.inode), h("td", {}, i.type), h("td", { class: "mono" }, i.size), h("td", { class: "mono" }, i.links), h("td", { class: "mono" }, i.block_pointers.join(", ") || "-"), h("td", { class: "mono" }, `blk ${i.location.block} +${i.location.offset}`))))))),
      insp));
  if (DSEL.block != null) pick(DSEL.block);
}

/* ---------------- 3. file browser ---------------- */
let CWD = "/", SELF = null;
const join = (a, b) => (a === "/" ? "" : a) + "/" + b;
async function vFiles(m) {
  let ls;
  try { ls = await api("/api/ls?path=" + encodeURIComponent(CWD)); } catch (e) { CWD = "/"; ls = await api("/api/ls?path=/"); }
  const tree = await api("/api/tree?depth=6");
  const out = h("div", { class: "card insp" }, h("div", { class: "empty" }, "Select a file to view it."));
  const reload = async () => { await refreshOverview(); await vFiles(m); };
  const doOp = (fn, msg) => act(async () => { const r = await fn(); await reload(); return r; }, r => msg + (r.txid ? ` (tx ${r.txid})` : ""));
  async function show(path) {
    SELF = path;
    const [st, rd] = await Promise.all([api("/api/stat?path=" + encodeURIComponent(path)), api("/api/read?path=" + encodeURIComponent(path)).catch(() => null)]);
    out.replaceChildren(h("h2", {}, path),
      kv([["inode", st.inode], ["type", st.type], ["size", st.size + " B"], ["blocks", st.blocks], ["block pointers", (st.block_pointers || []).join(", ") || "-"], ["links", st.links], ["modified", fmtNs(st.mtime)]]),
      h("div", { class: "sep" }),
      rd ? h("div", {}, h("h2", {}, "Content (read back from disk)"), rd.binary ? h("div", { class: "note" }, "binary data, first 512 bytes shown latin-1") : null, h("pre", { class: "hex", style: "white-space:pre-wrap" }, rd.text || "(empty)")) : null,
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn sm", onclick: async () => { const r = await promptModal("Overwrite " + path, [{ label: "New content", type: "textarea", value: rd ? rd.text : "" }], "Write"); if (r) { await doOp(() => api("/api/fs/write", { path, text: r[0] }), "Wrote " + path); show(path); } } }, "Edit"),
        h("button", { class: "btn sm", onclick: async () => { const r = await promptModal("Append to " + path, [{ label: "Text to append", type: "textarea" }], "Append"); if (r) { await doOp(() => api("/api/fs/append", { path, text: r[0] }), "Appended"); show(path); } } }, "Append"),
        h("button", { class: "btn sm", onclick: async () => { const r = await promptModal("Rename", [{ label: "New full path", value: path }], "Rename"); if (r) await doOp(() => api("/api/fs/rename", { old: path, new: r[0] }), "Renamed"); } }, "Rename"),
        h("button", { class: "btn sm danger", onclick: async () => { if (await confirmTyped("Delete " + path, "This runs a real journaled transaction.", "DELETE")) await doOp(() => api("/api/fs/delete", { path }), "Deleted " + path); } }, "Delete")));
  }
  const nodes = (n, d = 0) => [h("button", { "aria-current": n.path === CWD ? "true" : false, style: `padding-left:${8 + d * 14}px`, onclick: () => { if (n.type === "DIRECTORY") { CWD = n.path; vFiles(m); } else show(n.path); } }, n.type === "DIRECTORY" ? "▸ " : "· ", n.name === "/" ? "/" : n.name), ...(n.children || []).flatMap(c => nodes(c, d + 1))];
  const parts = CWD.split("/").filter(Boolean);
  const crumbs = h("div", { class: "crumbs" }, h("button", { onclick: () => { CWD = "/"; vFiles(m); } }, "/"), ...parts.flatMap((p, i) => [h("span", {}, "›"), h("button", { onclick: () => { CWD = "/" + parts.slice(0, i + 1).join("/"); vFiles(m); } }, p)]));
  m.replaceChildren(head("File browser", "Every action here is a call to the real engine and runs as one journaled transaction. The transaction id is shown after each operation.",
    h("button", { class: "btn", onclick: async () => { const r = await promptModal("New file", [{ label: "Name", hint: "created in " + CWD }, { label: "Initial content", type: "textarea" }], "Create"); if (r && r[0]) await doOp(async () => { const p = join(CWD, r[0]); const a = await api("/api/fs/create", { path: p }); if (r[1]) await api("/api/fs/write", { path: p, text: r[1] }); return a; }, "Created " + r[0]); } }, "New file"),
    h("button", { class: "btn danger", title: "Really kills a process in the middle of an operation on THIS image", onclick: async () => {
      const r = await promptModal("Crash demo on this image", [
        { label: "What to do", type: "select", options: ["overwrite an existing file", "create a new file with content"], hint: "overwrite: the crashed disk shows the OLD text. create: the file exists but is EMPTY until recovery." },
        { label: "Full path", value: SELF || join(CWD, "demo.txt") }, { label: "New text", type: "textarea", value: "TEXT WRITTEN JUST BEFORE THE CRASH" }], "Continue");
      if (!r) return;
      if (!await confirmTyped("Crash this image?", "A separate process performs the operation and is SIGKILLed right after the commit becomes durable. The image is then opened WITHOUT recovery so you can see the crashed state.", "CRASH THIS IMAGE")) return;
      await act(async () => { const x = await api("/api/demo/crash", { mode: r[0].startsWith("overwrite") ? "overwrite" : "create", path: r[1], text: r[2], confirm: "CRASH THIS IMAGE" }); SELF = x.path; await refreshOverview(); await vFiles(m); return x; }, x => x.killed ? "Process was killed (SIGKILL). Open the file to see the crashed disk." : "Operation ended without a crash");
    } }, "Crash demo…"),
    h("button", { class: "btn", onclick: async () => { const r = await promptModal("New directory", [{ label: "Name", hint: "created in " + CWD }], "Create"); if (r && r[0]) await doOp(() => api("/api/fs/mkdir", { path: join(CWD, r[0]) }), "Created directory " + r[0]); } }, "New folder")),
    h("div", { class: "split" },
      h("div", { class: "card flat" }, h("div", { class: "tree", role: "tree" }, nodes(tree))),
      h("div", { class: "card flat" }, h("div", { style: "padding:12px 14px;border-bottom:1px solid var(--line)" }, crumbs),
        ls.entries.length ? h("table", {}, h("thead", {}, h("tr", {}, ["name", "type", "size", "inode", "blocks", "modified", ""].map(x => h("th", {}, x)))),
          h("tbody", {}, ls.entries.map(e => h("tr", { class: "click" + (SELF === join(CWD, e.name) ? " sel" : ""), onclick: () => { if (e.type === "dir") { CWD = join(CWD, e.name); vFiles(m); } else show(join(CWD, e.name)); } },
            h("td", {}, (e.type === "dir" ? "▸ " : "") + e.name), h("td", {}, pill(e.type, e.type === "dir" ? "info" : "mute")), h("td", { class: "mono" }, e.size), h("td", { class: "mono" }, e.inode), h("td", { class: "mono" }, e.blocks), h("td", { class: "lbl" }, fmtNs(e.mtime)),
            h("td", {}, e.type === "dir" ? h("button", { class: "btn sm danger", onclick: async ev => { ev.stopPropagation(); if (await confirmTyped("Remove directory " + e.name, "The directory must be empty.", "RMDIR")) await doOp(() => api("/api/fs/rmdir", { path: join(CWD, e.name) }), "Removed " + e.name); } }, "rmdir") : null)))))
          : h("div", { class: "empty" }, "This directory is empty.")),
      out));
  if (SELF) show(SELF).catch(() => { });
}

/* ---------------- 4. journal ---------------- */
const STAGES = [["PREPARE", "metadata staged"], ["JOURNAL WRITE", "records in log"], ["FLUSH", "log durable"], ["COMMIT", "commit record durable"], ["APPLY", "home blocks written"], ["CHECKPOINT", "tail advanced"]];
/* the journal only holds *committed or incomplete* transactions, so we derive reached stages from what the backend reports for that tx */
function stagesFor(t) {
  if (t.state === "checkpointed") return { n: 6, label: "all stages complete" };
  if (t.state === "committed-pending-replay") return { n: 4, label: "committed; APPLY/CHECKPOINT not recorded - recovery will replay" };
  return { n: 3, label: "no durable commit record: will be discarded" };
}
async function vJournal(m) {
  const jv = await api("/api/journal");
  const ev = (await api("/api/events?limit=3000")).events;
  const s = jv.stats; const ring = new Array(s.journal_blocks).fill("free");
  jv.transactions.forEach(t => { for (let i = 0; i < t.blocks; i++) ring[(t.start_block + i) % s.journal_blocks] = t.state === "checkpointed" ? "checkpointed" : t.state === "uncommitted" ? "uncommitted" : "pending"; });
  const byTx = {}; ev.forEach(e => { if (e.txid != null) (byTx[e.txid] = byTx[e.txid] || []).push(e); });
  const sel = h("div", { class: "card" }, h("div", { class: "empty" }, "Select a transaction to see its lifecycle and records."));
  const txs = jv.transactions.slice().reverse();
  function showTx(t) {
    const events = (byTx[t.txid] || []).filter(e => ["tx_begin", "tx_prepare", "journal_write", "journal_flush", "commit_write", "commit_flush", "home_apply", "home_flush", "checkpoint", "tx_abort", "recovery_replay", "recovery_discard"].includes(e.kind));
    const seen = new Set(events.map(e => e.kind));
    const reached = [seen.has("tx_prepare"), seen.has("journal_write"), seen.has("journal_flush") || seen.has("commit_write"), seen.has("commit_flush"), seen.has("home_apply") || seen.has("home_flush"), seen.has("checkpoint")];
    const st = stagesFor(t);
    sel.replaceChildren(h("h2", {}, `Transaction ${t.txid} `, pill(t.state, t.state === "checkpointed" ? "ok" : t.state.startsWith("committed") ? "warn" : "bad")),
      h("div", { class: "lbl" }, "Lifecycle stages reported by the backend for this tx:"),
      events.length ? h("div", { class: "pipeline" }, STAGES.map(([a, b], i) => h("div", { class: "st" + (reached[i] ? " done" : "") }, h("b", {}, a), b)))
        : h("div", { class: "note", style: "margin:6px 0" }, "No live events for this tx in this session (it was written by an earlier mount or process). Disk-derived state: " + st.label + "."),
      kv([["journal blocks", `${t.start_block}…${t.start_block + t.blocks - 1}`], ["state on disk", t.state], ["timestamp", fmtNs(t.timestamp)]]),
      h("div", { class: "sep" }), h("h2", {}, "Records on disk"),
      h("table", {}, h("thead", {}, h("tr", {}, ["type", "target block", "offset", "length", "crc"].map(x => h("th", {}, x)))),
        h("tbody", {}, t.records.map(r => h("tr", {}, h("td", { class: "mono" }, r.type), h("td", { class: "mono" }, r.target_block ?? (r.block != null ? "journal " + r.block : "-")), h("td", { class: "mono" }, r.offset ?? "-"), h("td", { class: "mono" }, r.length ?? "-"), h("td", {}, pill(r.crc_ok ? "ok" : "BAD", r.crc_ok ? "ok" : "bad")))))),
      events.length ? h("div", {}, h("div", { class: "sep" }), h("h2", {}, "Events for this transaction"), h("ul", { class: "rec-list" }, events.map(e => h("li", {}, `${fmtT(e.ts)}  ${e.kind}  ${e.message}`)))) : null);
  }
  m.replaceChildren(head("Journal & transactions", "Circular redo log. Records are decoded from the journal region on disk; lifecycle stages come from the engine's event stream, so only stages that actually happened are lit."),
    h("div", { class: "grid g4" },
      h("div", { class: "card" }, h("h2", {}, "Head / tail"), kv([["tail (oldest live)", s.tail], ["head (next write)", s.head], ["live blocks", `${s.used_blocks}/${s.journal_blocks}`]])),
      h("div", { class: "card" }, h("h2", {}, "Txids"), kv([["checkpointed up to", s.checkpoint_txid], ["next txid", s.next_txid], ["committed, not checkpointed", s.uncheckpointed.length ? s.uncheckpointed.join(", ") : "none"]]))),
    ring.length ? h("div", { class: "card", style: "margin-top:14px" }, h("h2", {}, "Ring occupancy"), h("div", { class: "ring", role: "img", "aria-label": "journal ring" }, ring.map((st, i) => h("i", { "data-s": st, class: (i === s.head ? "h " : "") + (i === s.tail ? "t" : ""), title: `journal block ${i}: ${st}` }))), h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:var(--amber)" }), "committed, awaiting checkpoint"), h("span", {}, h("i", { style: "background:#2f6f64" }), "checkpointed (reclaimable)"), h("span", {}, h("i", { style: "background:var(--red)" }), "incomplete"), h("span", {}, "white bar = head, blue bar = tail"))) : null,
    h("div", { class: "grid", style: "grid-template-columns:minmax(260px,380px) minmax(0,1fr);margin-top:14px;align-items:start" },
      h("div", { class: "card flat" }, txs.length ? h("table", {}, h("thead", {}, h("tr", {}, ["tx", "state", "recs", "blk"].map(x => h("th", {}, x)))),
        h("tbody", {}, txs.map(t => h("tr", { class: "click", onclick: ev => { m.querySelectorAll("tr.sel").forEach(r => r.classList.remove("sel")); ev.currentTarget.classList.add("sel"); showTx(t); } }, h("td", { class: "mono" }, t.txid), h("td", {}, pill(t.state, t.state === "checkpointed" ? "ok" : t.state.startsWith("committed") ? "warn" : "bad")), h("td", { class: "mono" }, t.records.length), h("td", { class: "mono" }, t.start_block))))) : h("div", { class: "empty" }, "The journal holds no transactions yet.")),
      sel));
  if (txs.length) { m.querySelector("tbody tr").click(); }
  TIMER = setInterval(() => { if (SCREEN === "journal") refreshOverview().then(() => vJournal(m)); }, 5000);
}

/* ---------------- 5. crash lab ---------------- */
let LAB = { scenario: null, operation: "create_file", job: null, jobData: null };
async function vLab(m) {
  const meta = await api("/api/lab/scenarios");
  if (!LAB.scenario) LAB.scenario = meta.scenarios[0].name;
  const sc = () => meta.scenarios.find(s => s.name === LAB.scenario);
  const out = h("div", {});
  const scList = h("div", { class: "card flat" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "crash point", "expected"].map(x => h("th", {}, x)))),
    h("tbody", {}, meta.scenarios.map((s, i) => h("tr", { class: "click" + (s.name === LAB.scenario ? " sel" : ""), onclick: () => { LAB.scenario = s.name; LAB.jobData = null; vLab(m); } },
      h("td", { class: "mono" }, i + 1), h("td", {}, h("div", { class: "mono" }, s.crash_point), h("div", { class: "lbl" }, s.description)), h("td", {}, pill(s.expected_state === "committed" ? "survives" : "rolled back", s.expected_state === "committed" ? "ok" : "warn")))))));
  const opSel = h("select", { "aria-label": "operation", onchange: e => { LAB.operation = e.target.value; } }, meta.operations.map(o => h("option", { value: o.name, selected: o.name === LAB.operation }, o.label)));
  const cacheSel = h("select", { "aria-label": "cache model" }, h("option", { value: "volatile" }, "volatile cache (strict)"), h("option", { value: "writethrough" }, "write-through"));
  const runBtn = h("button", { class: "btn primary", onclick: async () => {
    const s = sc(); const ok = await modal("Run crash lab", h("div", {}, h("p", {}, `A throw-away image is created in a temp directory. A child process performs "${meta.operations.find(o => o.name === LAB.operation).label}" and is SIGKILLed at ${s.crash_point}. A fresh process then recovers and verifies it. Your mounted image is not touched.`)), close => [h("button", { class: "btn", onclick: () => close(null) }, "Cancel"), h("button", { class: "btn primary", onclick: () => close(true) }, "Run")]);
    if (!ok) return;
    const r = await act(() => api("/api/lab/run", { scenario: s.name, operation: LAB.operation, cache_mode: cacheSel.value, confirm: "RUN CRASH LAB" }));
    if (!r) return; LAB.job = r.job; LAB.jobData = { status: "running", steps: [] }; poll();
  } }, "Run scenario");
  async function poll() {
    if (!LAB.job) return;
    const j = await api("/api/lab/job?id=" + LAB.job); LAB.jobData = j; paint();
    if (j.status === "running") setTimeout(() => { if (SCREEN === "lab") poll(); }, 350); else refreshOverview();
  }
  function paint() { out.replaceChildren(...labResult(LAB.jobData, sc())); }
  m.replaceChildren(head("Crash simulation lab", "Each run formats a temp image, starts a worker process that is really SIGKILLed at the chosen point, then verifies the image in a new process. The expected column is written before the run; the actual column is read from the disk afterwards."),
    h("div", { class: "row", style: "margin-bottom:14px" }, h("span", { class: "lbl" }, "operation"), opSel, h("span", { class: "lbl" }, "disk model"), cacheSel, runBtn),
    h("div", { class: "grid", style: "grid-template-columns:minmax(300px,420px) minmax(0,1fr);align-items:start" }, scList, out));
  paint();
}
function diff(expected, actual) {
  const keys = [...new Set([...Object.keys(expected.listing || {}), ...Object.keys(actual.listing || {})])].sort();
  return keys.map(k => { const e = k in (expected.listing || {}), a = k in (actual.listing || {}); const ce = (expected.files || {})[k], ca = (actual.files || {})[k]; const bad = e !== a || ce !== ca; return { k, e, a, ce, ca, bad }; });
}
function labResult(j, s) {
  if (!j) return [h("div", { class: "card" }, h("h2", {}, "Selected scenario"), kv([["crash point", s.crash_point], ["expectation", s.expected_state === "committed" ? "operation survives" : "operation rolled back"], ["torn bytes", s.torn_bytes || "none"], ["trigger count", s.count]]), h("p", { class: "sub", style: "margin:10px 0 0" }, s.outcome), h("p", { class: "note" }, "Press Run scenario to execute it for real."))];
  const steps = h("div", { class: "card" }, h("h2", {}, "Execution steps ", h("span", { class: "tag" }, "from worker processes")),
    h("ol", { class: "steps" }, (j.steps || []).map((st, i) => h("li", { class: st.killed ? "kill" : "done" }, h("span", { class: "n" }, i + 1), h("div", {}, h("div", {}, st.step), h("div", { class: "lbl mono" }, st.killed ? `process killed (returncode ${st.returncode} = SIGKILL)` : `exit ${st.returncode} in ${st.seconds}s`)))),
      j.status === "running" ? h("li", {}, h("span", { class: "n" }, "…"), h("div", { class: "lbl" }, "running")) : null));
  if (j.status === "running") return [steps];
  if (j.status === "error") return [steps, h("div", { class: "banner bad", role: "alert" }, j.error)];
  const r = j.result, rows = diff(r.expected_probe, r.actual_probe);
  const pre = r.pre_recovery ? r.pre_recovery.journal.transactions.slice(-1)[0] : null;
  return [steps,
    h("div", { class: "card", style: "margin-top:14px" }, h("div", { class: "row", style: "justify-content:space-between" }, h("h2", { style: "margin:0" }, "Verdict"), h("span", { class: "verdict " + r.verdict }, r.verdict)),
      h("p", { class: "lbl" }, `${r.scenario} · ${r.crash_point} · ${r.operation} · ${r.cache_mode}`)),
    timeline3(r),
    h("div", { class: "cmp", style: "margin-top:14px" },
      h("div", { class: "card" }, h("h2", {}, "Expected ", h("span", { class: "tag" }, "model, set before run")), h("div", { class: "state" }, rows.map(x => h("div", { class: x.e ? "same" : "del" }, (x.e ? "  " : "- ") + x.k + (x.e ? ` = ${JSON.stringify(x.ce)}` : " (absent)")))), h("p", { class: "note" }, r.expected_state === "committed" ? "Operation must be present after recovery." : "Operation must be absent after recovery.")),
      h("div", { class: "card" }, h("h2", {}, "Actual ", h("span", { class: "tag" }, "read from the recovered image")), h("div", { class: "state" }, rows.map(x => h("div", { class: x.bad ? "del" : "add" }, (x.bad ? "! " : "✓ ") + x.k + (x.a ? ` = ${JSON.stringify(x.ca)}` : " (absent)")))))),
    h("div", { class: "grid g2", style: "margin-top:14px" },
      h("div", { class: "card" }, h("h2", {}, "Checks"), h("ul", { class: "chk" }, Object.entries(r.checks).map(([k, v]) => h("li", {}, h("span", { class: v ? "ok" : "no" }, v ? "✓" : "✗"), k.replaceAll("_", " "))))),
      h("div", { class: "card" }, h("h2", {}, "Recovery report"), r.recovery ? kv([["replayed", r.recovery.transactions_replayed + (r.recovery.replayed.length ? " (tx " + r.recovery.replayed.map(x => x.txid).join(", ") + ")" : "")], ["discarded", r.recovery.transactions_discarded + (r.recovery.discarded.length ? " — " + r.recovery.discarded.map(x => x.reason).join("; ") : "")], ["checkpoint tx", r.recovery.checkpoint_txid], ["duration", r.recovery.duration_ms + " ms"], ["fsck after", r.check.passed ? "passed" : "FAILED (" + r.check.errors.length + ")"]]) : null,
        pre ? h("p", { class: "note" }, `Journal before recovery: last tx ${pre.txid} was ${pre.state}.`) : null)),
    r.events && r.events.length ? h("details", { class: "card", style: "margin-top:14px" }, h("summary", {}, `Event trace from the crashed process (${r.events.length})`), h("ul", { class: "rec-list" }, r.events.map(e => h("li", { class: "sev-" + e.severity }, `${e.kind}  ${e.message}`)))) : null];
}

/* before crash / crashed disk (no recovery yet) / after recovery - the visible story of a run */
function timeline3(r) {
  const B = r.before || { listing: {}, files: {} }, C = (r.pre_recovery && r.pre_recovery.probe) || null, A = r.actual_probe;
  if (!C) return null;
  const names = [...new Set([...Object.keys(B.listing), ...Object.keys(C.listing), ...Object.keys(A.listing)])].sort();
  const show = (p, n) => { if (!(n in p.listing)) return null; const v = (p.files || {})[n]; if (v == null) return "(directory)"; const t = String(v); return t.length > 36 ? JSON.stringify(t.slice(0, 12)) + `… (${t.length} bytes)` : JSON.stringify(t); };
  const cell = (v, changed) => h("td", { class: "mono" }, v == null ? h("span", { class: "sev-error" }, "absent") : h("span", { class: changed ? "sev-warning" : "" }, v));
  const rows = names.map(n => { const b = show(B, n), c = show(C, n), a = show(A, n); return h("tr", {}, h("td", { class: "mono" }, n), cell(b, false), cell(c, c !== b), cell(a, a !== c)); });
  const crashedDiffers = names.some(n => show(C, n) !== show(A, n));
  return h("div", { class: "card", style: "margin-top:14px" }, h("h2", {}, "What the disk looked like at each moment ", h("span", { class: "tag" }, "read from the image")),
    h("div", { class: "card flat", style: "border:0" }, h("table", {}, h("thead", {}, h("tr", {}, ["file", "1 · before", "2 · crashed (no recovery)", "3 · after recovery"].map(x => h("th", {}, x)))), h("tbody", {}, rows))),
    h("p", { class: "note", style: "margin:8px 0 0" }, crashedDiffers
      ? "Column 2 is the crashed image opened read-only without replaying the journal. The change was committed in the journal but not yet applied, so the disk still shows the old state. Recovery replays the journal and column 3 shows the finished operation."
      : "Column 2 equals column 3: the operation had not committed, so nothing was half-applied and recovery had nothing to replay. The old data was never damaged."));
}

/* ---------------- 6. checker ---------------- */
async function vCheck(m) {
  const show = r => {
    const f = r.findings || [];
    m.replaceChildren(head("Consistency checker", "A read-only fsck: superblock, journal, bitmap vs inodes, directory tree, link counts. Findings include the reason the check exists.",
      h("button", { class: "btn primary", onclick: () => vCheck(m) }, "Run again")),
      h("div", { class: "banner " + (r.passed ? "ok" : "bad") }, h("div", {}, h("b", {}, r.passed ? "PASSED" : "FAILED"), ` — ${r.errors.length} error(s), ${r.warnings.length} warning(s)`)),
      h("div", { class: "grid g2" },
        h("div", { class: "card flat" }, f.length ? h("table", {}, h("thead", {}, h("tr", {}, ["severity", "code", "finding"].map(x => h("th", {}, x)))), h("tbody", {}, f.map(x => h("tr", {}, h("td", {}, pill(x.severity, x.severity === "error" ? "bad" : "warn")), h("td", { class: "mono" }, x.code), h("td", {}, h("div", {}, x.message), x.why ? h("div", { class: "note" }, x.why) : null))))) : h("div", { class: "empty" }, "No findings.")),
        h("div", { class: "card" }, h("h2", {}, "What was examined"), kv([["blocks (total / free)", `${r.stats.superblock.total_blocks} / ${r.stats.superblock.free_blocks}`], ["inodes allocated", r.stats.inodes_allocated], ["inodes reachable from /", r.stats.reachable_inodes], ["blocks allocated", r.stats.blocks_allocated], ["files / directories", `${r.stats.inodes.files} / ${r.stats.inodes.directories}`], ["pending journal tx", r.stats.journal_pending_tx]]))));
  };
  const r = await act(() => api("/api/check", {})); if (r) show(r);
}

/* ---------------- 7. events ---------------- */
let EF = { kind: "", severity: "", tx: "" };
async function vEvents(m) {
  const q = new URLSearchParams({ limit: "1500" }); if (EF.kind) q.set("kind", EF.kind); if (EF.severity) q.set("severity", EF.severity);
  const all = (await api("/api/events?limit=1500")).events;
  const kinds = [...new Set(all.map(e => e.kind))].sort();
  let ev = all.filter(e => (!EF.kind || e.kind === EF.kind) && (!EF.severity || e.severity === EF.severity) && (!EF.tx || String(e.txid) === EF.tx));
  const sevCls = s => s === "error" ? "bad" : s === "warning" ? "warn" : "info";
  m.replaceChildren(head("Event log", "Real events emitted by the engine while you use it (kept in server memory and mirrored to a JSONL file). Includes lab runs.",
    h("button", { class: "btn", onclick: async () => { await act(() => api("/api/events/clear", {})); vEvents(m); } }, "Clear log")),
    h("div", { class: "row", style: "margin-bottom:12px" },
      h("select", { "aria-label": "kind", onchange: e => { EF.kind = e.target.value; vEvents(m); } }, h("option", { value: "" }, "all types"), kinds.map(k => h("option", { value: k, selected: k === EF.kind }, k))),
      h("div", { class: "chips" }, ["", "info", "warning", "error"].map(s => h("button", { class: "chip", "aria-pressed": String(EF.severity === s), onclick: () => { EF.severity = s; vEvents(m); } }, s || "any severity"))),
      h("input", { type: "number", placeholder: "txid", value: EF.tx, "aria-label": "filter by txid", style: "width:90px", onchange: e => { EF.tx = e.target.value; vEvents(m); } }),
      h("span", { class: "lbl" }, `${ev.length} of ${all.length} events`)),
    h("div", { class: "card flat" }, ev.length ? h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "time", "severity", "type", "tx", "message"].map(x => h("th", {}, x)))),
      h("tbody", {}, ev.slice(-500).reverse().map(e => h("tr", {}, h("td", { class: "mono" }, e.seq), h("td", { class: "mono" }, fmtT(e.ts)), h("td", {}, pill(e.severity, sevCls(e.severity))), h("td", { class: "mono" }, e.kind), h("td", { class: "mono" }, e.txid ?? ""), h("td", {}, e.message)))))) : h("div", { class: "empty" }, "No events match.")));
  TIMER = setInterval(() => { if (SCREEN === "events" && !document.activeElement.matches("input,select")) vEvents(m); }, 3000);
}

/* ---------------- toolbar ---------------- */
$("#btn-open").onclick = () => { const n = $("#imgsel").value; if (!n) return toast("No image selected", "Create one first.", true); act(async () => { await api("/api/images/open", { name: n }); await refreshImages(); await refreshOverview(); render(); }, () => "Opened " + n); };
$("#btn-new").onclick = async () => {
  const r = await promptModal("New disk image", [{ label: "File name", value: "demo.img", hint: "created inside the server's image directory" }, { label: "Size (MiB, 1–256)", type: "number", value: 8 }, { label: "Cache model", type: "select", options: ["volatile", "writethrough"] }], "Create & format");
  if (!r) return; CWD = "/"; SELF = null;
  await act(async () => { await api("/api/images/create", { name: r[0], size_mb: +r[1], cache_mode: r[2] }); await refreshImages(); await refreshOverview(); render(); }, () => "Created " + r[0]);
};
(async function init() {
  try { TOKEN = (await api("/api/session")).token; await refreshImages(); } catch (e) { $("#main").append(h("div", { class: "banner bad" }, e.message)); }
  const hs = location.hash.slice(1); if (SCREENS.some(s => s[0] === hs)) SCREEN = hs;
  buildNav(); render();
})();
