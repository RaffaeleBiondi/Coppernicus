(() => {
"use strict";
const M = JSON.parse(document.getElementById("meta").textContent);
const $ = (id) => document.getElementById(id);
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : String(k));
  return e;
}
const ok = (x) => typeof x === "number" && isFinite(x);
function sig(x, n = 4) { if (!ok(x)) return "–"; if (x === 0) return "0";
  const a = Math.abs(x); return (a >= 1e-3 && a < 1e6) ? String(Number(x.toPrecision(n))) : x.toExponential(n - 1); }
function fix(x, d) { return ok(x) ? x.toFixed(d) : "–"; }
function xy(x, y) { return `(${fix(x, 2)}, ${fix(y, 2)}) mm`; }

// ---------------------------------------------------------------- state
const S = { rail: 0, layer: 0, field: "drop", view: null, mark: null, drag: null,
  show: { sources: true, loads: true, hotspots: true } };
const FIELDS = [["drop", "Drop"], ["voltage", "Voltage"], ["current", "Current density"]];
const KINDS = { rise: "ground rise", return: "ground return" };   // other kinds read as they are
const data = new Map(), images = new Map(), luts = new Map();
const canvas = $("map"), ctx = canvas.getContext("2d");
const R = () => M.rails[S.rail], L = () => R().layers[S.layer];

// ---------------------------------------------------------------- decoding
async function inflate(id) {
  const bin = atob(document.getElementById(id).textContent);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
function undelta(raw, nx, ny) {   // high bytes, then low bytes, of row-wise differences
  const n = nx * ny, out = new Uint16Array(n);
  for (let r = 0; r < ny; r++) {
    let acc = 0; const o = r * nx;
    for (let i = 0; i < nx; i++) { acc = (acc + ((raw[o + i] << 8) | raw[n + o + i])) & 0xffff; out[o + i] = acc; }
  }
  return out;
}
function layerData(ri, li) {
  const key = ri + ":" + li;
  if (!data.has(key)) {
    const lay = M.rails[ri].layers[li], g = lay.grid;
    data.set(key, Promise.all([inflate(lay.v), inflate(lay.j)]).then(([v, j]) =>
      ({ v: undelta(v, g.nx, g.ny), j: undelta(j, g.nx, g.ny) })));
    if (data.size > 4) data.delete(data.keys().next().value);
  }
  return data.get(key);
}
function rgba(rgb) { return ((255 << 24) | ((rgb & 255) << 16) | (((rgb >> 8) & 255) << 8) | ((rgb >> 16) & 255)) >>> 0; }
function lutFor(ri, li, field) {
  const key = field === "current" ? `${ri}:${li}:c` : `${ri}:${field}`;
  if (luts.has(key)) return luts.get(key);
  const rail = M.rails[ri], lut = new Uint32Array(65536);
  const cm = (field === "current" ? M.cmaps.magma : M.cmaps.viridis).map(rgba);
  const pick = (t) => cm[Math.max(0, Math.min(255, Math.round(t * 255)))];
  if (field === "current") {
    const lay = rail.layers[li], step = lay.j_hi_a_mm2 / (M.codes.j - 1), top = lay.j_scale_a_mm2;
    for (let c = 1; c < 65536; c++) lut[c] = pick(Math.min(((c - 1) * step) / top, 1));
  } else {
    const span = rail.v_hi_v - rail.v_lo_v, step = span / (M.codes.v - 1);
    lut[1] = rgba(0xb4b4b4);
    for (let c = 2; c < 65536; c++) {
      const v = rail.v_lo_v + (c - 2) * step;
      lut[c] = pick(field === "drop" ? (rail.drop_max_mv > 0 ? ((rail.v_ref_v - v) * 1e3) / rail.drop_max_mv : 0)
                                     : (span > 0 ? (v - rail.v_lo_v) / span : 0));
    }
  }
  luts.set(key, lut);
  return lut;
}
async function layerImage(ri, li, field) {
  const key = `${ri}:${li}:${field}`;
  if (images.has(key)) return images.get(key);
  const d = await layerData(ri, li), g = M.rails[ri].layers[li].grid;
  const codes = field === "current" ? d.j : d.v, lut = lutFor(ri, li, field);
  const cv = document.createElement("canvas"); cv.width = g.nx; cv.height = g.ny;
  const c2 = cv.getContext("2d"), img = c2.createImageData(g.nx, g.ny), px = new Uint32Array(img.data.buffer);
  for (let i = 0; i < codes.length; i++) px[i] = lut[codes[i]];
  c2.putImageData(img, 0, 0);
  images.set(key, cv);
  if (images.size > 4) images.delete(images.keys().next().value);
  return cv;
}

// ---------------------------------------------------------------- view
function bounds() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const ring of M.outline) for (const [x, y] of ring) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  if (!isFinite(x0)) for (const lay of R().layers) { const g = lay.grid, c = R().cell_mm;
    x0 = Math.min(x0, g.x0); y0 = Math.min(y0, g.y0); x1 = Math.max(x1, g.x0 + g.nx * c); y1 = Math.max(y1, g.y0 + g.ny * c); }
  return [x0, y0, x1, y1];
}
function size() {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(r.width * dpr)); canvas.height = Math.max(1, Math.round(r.height * dpr));
  return [r.width, r.height, dpr];
}
function fit() {
  const [w, h] = size(), [x0, y0, x1, y1] = bounds();
  const s = 0.94 * Math.min(w / Math.max(x1 - x0, 1e-6), h / Math.max(y1 - y0, 1e-6));
  S.view = { s, tx: (w - (x1 - x0) * s) / 2 - x0 * s, ty: (h - (y1 - y0) * s) / 2 - y0 * s };
  draw();
}
function zoomTo(x, y, mm) {
  const [w, h] = size(), s = Math.min(w, h) / mm;
  S.view = { s, tx: w / 2 - x * s, ty: h / 2 - y * s }; draw();
}
let drawSeq = 0;
async function draw() {
  const seq = ++drawSeq, [w, h, dpr] = size(), v = S.view;
  if (!v) return;
  const busy = $("busy"), key = `${S.rail}:${S.layer}:${S.field}`;
  if (!images.has(key)) busy.hidden = false;
  let img = null;
  try { img = await layerImage(S.rail, S.layer, S.field); }
  catch (e) { busy.textContent = "Could not decode this layer: " + e; busy.hidden = false; return; }
  if (seq !== drawSeq) return;
  busy.hidden = true;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
  const g = L().grid, c = R().cell_mm;
  ctx.setTransform(dpr * v.s, 0, 0, dpr * v.s, dpr * v.tx, dpr * v.ty);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(img, g.x0, g.y0, g.nx * c, g.ny * c);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const X = (x) => x * v.s + v.tx, Y = (y) => y * v.s + v.ty;
  ctx.lineWidth = 1.2; ctx.strokeStyle = getComputedStyle(document.body).color; ctx.globalAlpha = 0.55;
  for (const ring of M.outline) { ctx.beginPath(); ring.forEach(([x, y], i) => i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))); ctx.closePath(); ctx.stroke(); }
  ctx.globalAlpha = 1; ctx.font = "600 12px system-ui, sans-serif"; ctx.lineJoin = "round";
  const label = (text, x, y, color) => {          // a light halo keeps labels legible on any colour
    ctx.lineWidth = 3.5; ctx.strokeStyle = "rgba(255,255,255,0.9)"; ctx.strokeText(text, X(x) + 7, Y(y) - 6);
    ctx.fillStyle = color; ctx.fillText(text, X(x) + 7, Y(y) - 6); ctx.lineWidth = 1.2; };
  if (S.show.sources) for (const t of R().sources) { ctx.fillStyle = "#1a9850"; ctx.strokeStyle = "#fff";
    for (const [x, y] of t.pads) { ctx.beginPath(); ctx.moveTo(X(x), Y(y) - 7); ctx.lineTo(X(x) + 6, Y(y) + 5); ctx.lineTo(X(x) - 6, Y(y) + 5); ctx.closePath(); ctx.fill(); ctx.stroke(); }
    if (t.pads.length) label(t.name, t.pads[0][0], t.pads[0][1], "#1a9850"); }
  if (S.show.loads) for (const t of R().loads) { ctx.fillStyle = "#d73027"; ctx.strokeStyle = "#fff";
    for (const [x, y] of t.pads) { ctx.beginPath(); ctx.arc(X(x), Y(y), 5, 0, 2 * Math.PI); ctx.fill(); ctx.stroke(); }
    if (t.pads.length) label(t.name, t.pads[0][0], t.pads[0][1], "#d73027"); }
  if (S.show.hotspots) for (const p of R().problems) if (p.kind === "hotspot" && p.layer === L().name) {
    ctx.strokeStyle = p.severity === "fail" ? "#ff1744" : "#ff9800"; ctx.lineWidth = 2.5;
    const x = X(p.x_mm), y = Y(p.y_mm);
    ctx.beginPath(); ctx.moveTo(x - 6, y - 6); ctx.lineTo(x + 6, y + 6); ctx.moveTo(x + 6, y - 6); ctx.lineTo(x - 6, y + 6); ctx.stroke(); }
  if (S.mark) { ctx.strokeStyle = "#2f6fb2"; ctx.lineWidth = 2.5; ctx.beginPath();
    ctx.arc(X(S.mark[0]), Y(S.mark[1]), 14, 0, 2 * Math.PI); ctx.stroke(); }
}

// ---------------------------------------------------------------- readout
async function readout(ev) {
  if (!S.view) return;
  const r = canvas.getBoundingClientRect(), x = (ev.clientX - r.left - S.view.tx) / S.view.s,
        y = (ev.clientY - r.top - S.view.ty) / S.view.s;
  const rail = R(), lay = L(), g = lay.grid, c = rail.cell_mm;
  const i = Math.floor((x - g.x0) / c), j = Math.floor((y - g.y0) / c);
  const out = $("readout");
  const where = `${xy(x, y)} on ${lay.name}`;
  if (i < 0 || j < 0 || i >= g.nx || j >= g.ny) { out.textContent = `${where}: no copper of ${rail.label} here.`; return; }
  const d = await layerData(S.rail, S.layer), k = j * g.nx + i, cv = d.v[k], cj = d.j[k];
  if (cv === 0) { out.textContent = `${where}: no copper of ${rail.label} here.`; return; }
  const vstep = (rail.v_hi_v - rail.v_lo_v) / (M.codes.v - 1), jstep = lay.j_hi_a_mm2 / (M.codes.j - 1);
  const J = (cj - 1) * jstep;
  let text;
  if (cv === 1) text = `${where}: copper with no path to a source (no voltage); |J| = 0.`;
  else { const V = rail.v_lo_v + (cv - 2) * vstep, drop = (rail.v_ref_v - V) * 1e3;
    text = `${where}: V = ${sig(V, 7)} V · drop = ${sig(drop, 4)} mV (${rail.v_ref_v ? sig(100 * drop / 1e3 / Math.abs(rail.v_ref_v), 3) : "n/a"} %) · |J| = ${sig(J, 4)} A/mm²`; }
  out.textContent = "";
  out.append(text, el("br"), el("span", { class: "muted small" },
    `Values at the centre of the ${sig(c, 3)} mm cell under the pointer, (${fix(g.x0 + (i + 0.5) * c, 3)}, ${fix(g.y0 + (j + 0.5) * c, 3)}) mm, ` +
    `taken from the full-resolution solution; stored in steps of ${sig(vstep * 1e6, 2)} µV and ${sig(jstep, 2)} A/mm².`));
}

// ---------------------------------------------------------------- colour bar and notes
function bar() {
  const rail = R(), lay = L(), cb = $("bar"), c2 = cb.getContext("2d");
  const cm = S.field === "current" ? M.cmaps.magma : M.cmaps.viridis;
  for (let i = 0; i < 256; i++) { const rgb = cm[255 - i]; c2.fillStyle = `rgb(${rgb >> 16},${(rgb >> 8) & 255},${rgb & 255})`; c2.fillRect(0, i, 18, 1); }
  let top, bottom, unit, note;
  if (S.field === "drop") { top = sig(rail.drop_max_mv, 4); bottom = "0"; unit = `mV below ${sig(rail.v_ref_v, 4)} V`;
    note = "Drop below the source voltage. Same scale on every layer of the rail."; }
  else if (S.field === "voltage") { top = sig(rail.v_hi_v, 7); bottom = sig(rail.v_lo_v, 7); unit = "V";
    note = "Same scale on every layer of the rail."; }
  else { top = "≥ " + sig(lay.j_scale_a_mm2, 4); bottom = "0"; unit = "A/mm²";
    note = "Scale clipped at this layer's 99.5th percentile of |J|. " +
      `The point maximum on this layer, ${sig(lay.j_point_max_a_mm2, 4)} A/mm², sits at a sharp corner or a hole edge and is mesh-dependent: use the hotspot means.`; }
  $("bar-top").textContent = top; $("bar-bottom").textContent = bottom; $("bar-unit").textContent = unit;
  $("scalenote").textContent = note + " Grey: copper with no path to a source.";
}

// ---------------------------------------------------------------- panels
function card(k, v, d) { return el("div", { class: "card" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), d ? el("div", { class: "d" }, d) : null); }
function badge(verdict) {
  if (verdict === "fail") return el("span", { class: "badge fail" }, "FAIL");
  if (verdict === "pass") return el("span", { class: "badge pass" }, "PASS");
  return el("span", { class: "badge nj" }, "not judged");
}
function table(id, head, rows, onclick) {
  const t = $(id); t.textContent = "";
  t.append(el("thead", {}, el("tr", {}, ...head.map(([h, cls]) => el("th", cls ? { class: cls } : {}, h)))));
  const body = el("tbody");
  rows.forEach((cells, n) => { const tr = el("tr", {}, ...cells.map(([v, cls]) => el("td", cls ? { class: cls } : {}, v)));
    if (onclick) tr.addEventListener("click", () => { for (const o of body.children) o.classList.remove("sel"); tr.classList.add("sel"); onclick(n); });
    body.append(tr); });
  t.append(body);
}
function verdictText(rail) {   // the limits in words; only limits the user set are named
  const lim = rail.limits, parts = [];
  if (lim.max_drop_pct != null) parts.push(`load drop ≤ ${sig(lim.max_drop_pct, 3)} % of the source voltage`);
  if (lim.max_current_density_a_mm2 != null) parts.push(`current-crowding mean |J| ≤ ${sig(lim.max_current_density_a_mm2, 4)} A/mm²`);
  if (lim.max_via_current_a != null) parts.push(`current per via ≤ ${sig(lim.max_via_current_a, 4)} A`);
  if (lim.max_ground_rise_mv != null) parts.push(`ground rise ≤ ${sig(lim.max_ground_rise_mv, 4)} mV above the source`);
  if (rail.verdict === "fail") return `Fails against the limits you set: ${parts.join("; ")}. ${rail.failed} problem${rail.failed === 1 ? "" : "s"} over a limit.`;
  if (rail.verdict === "pass") return `Within the limits you set: ${parts.join("; ")}. Limits you did not set are not checked.`;
  const nj = rail.not_judged || [];
  if (nj.length) return `Not judged against the limits you set (${parts.join("; ")}): ${nj.map((u) => `${u.item} (${u.limit}): ${u.reason}`).join("; ")}.`;
  return "Not judged: no limits were set, so the problems below are ranked, not judged.";
}
function panels() {
  const rail = R(), w = rail.worst;
  const v = $("verdict"); v.textContent = ""; v.className = rail.verdict;
  v.append(el("strong", {}, rail.label), " ", badge(rail.verdict), " ", el("span", {}, verdictText(rail)));
  if (rail.nets.length > 1) v.append(el("div", { class: "muted small" }, "Solved together through net ties: " + rail.nets.join(", ")));
  const cards = $("worst"); cards.textContent = "";
  if (w.load) cards.append(card("Worst load", `${w.load.name}: ${sig(w.load.drop_mv, 4)} mV`, `${sig(w.load.drop_pct, 3)} % below ${sig(rail.v_ref_v, 4)} V`));
  cards.append(card("Largest drop on the copper", `${sig(w.max_drop_mv, 4)} mV`, `${sig(w.max_drop_pct, 3)} %`));
  cards.append(card("Copper loss", `${sig(w.power_loss_w * 1e3, 4)} mW`, w.busiest_layer ? `most on ${w.busiest_layer}` : null));
  if (w.hotspot) cards.append(card("Strongest current crowding", `${sig(w.hotspot.mean_j_a_mm2, 4)} A/mm² mean`,
    `${w.hotspot.layer} at ${xy(w.hotspot.x_mm, w.hotspot.y_mm)}, over ${sig(w.hotspot.area_mm2, 3)} mm²; peak ${sig(w.hotspot.peak_j_a_mm2, 4)} A/mm² is mesh-dependent at sharp corners`));
  if (w.via) cards.append(card("Most loaded via", `${sig(w.via.current_a, 4)} A`, `at ${xy(w.via.x_mm, w.via.y_mm)}, drill ${sig(w.via.drill_mm, 3)} mm`));
  table("problems", [["", ""], ["Kind", ""], ["Problem", ""], ["Layer", ""], ["Where", ""]],
    rail.problems.map((p) => [[p.severity === "fail" ? el("span", { class: "badge fail" }, "FAIL") : "", ""], [KINDS[p.kind] || p.kind, ""],
      [el("span", {}, p.title, el("br"), el("span", { class: "muted small" }, p.detail)), ""], [p.layer || "–", ""], [xy(p.x_mm, p.y_mm), ""]]),
    (n) => { const p = rail.problems[n];
      if (p.layer) { const li = rail.layers.findIndex((l) => l.name === p.layer); if (li >= 0) setLayer(li, false); }
      S.mark = [p.x_mm, p.y_mm]; zoomTo(p.x_mm, p.y_mm, 8); $("map").scrollIntoView({ block: "nearest", behavior: "smooth" }); });
  const lim = rail.limits.max_drop_pct;
  table("loads", [["Load", ""], ["Ref", ""], ["Pins", "num"], ["Current (A)", "num"], ["V min (V)", "num"], ["Drop (mV)", "num"], ["Drop (%)", "num"], [lim ? `vs ${sig(lim, 3)} % limit` : "Limit", ""]],
    rail.loads.map((t) => [[t.estimated ? el("span", { title: "estimated at nominal voltages, without copper losses" }, t.name, " ", el("span", { class: "muted small" }, "estimated draw")) : t.name, ""], [t.ref, ""], [t.pins, "num"], [sig(t.current_a, 4), "num"], [sig(t.v_min_v, 7), "num"], [sig(t.drop_mv, 4), "num"],
      [sig(t.drop_pct, 3), "num"], [t.exceeds === null ? (lim ? "not judged" : "no limit set") : (t.exceeds ? el("span", { class: "badge fail" }, "FAIL") : "within"), ""]]));
  table("sources", [["Source", ""], ["Ref", ""], ["Pins", "num"], ["Voltage (V)", "num"], ["Current (A)", "num"]],
    rail.sources.map((t) => [[t.name, ""], [t.ref, ""], [t.pins, "num"], [sig(t.v_v, 6), "num"], [sig(t.current_a, 4), "num"]]));
  const losses = $("losses"); losses.textContent = "";
  const entries = Object.entries(rail.loss_breakdown_w).sort((a, b) => b[1] - a[1]), total = entries.reduce((s, e) => s + e[1], 0) || 1;
  for (const [k, val] of entries) losses.append(el("div", { class: "lossrow" }, el("span", {}, k),
    el("div", { class: "lossbar", style: `width:${Math.max(1, 100 * val / total)}%` }), el("span", { class: "num" }, `${sig(val * 1e3, 3)} mW`)));
  const set = $("settings"); set.textContent = "";
  for (const [k, val] of Object.entries(M.settings)) set.append(el("li", {}, `${k}: ${val === null ? "engine default" : val}`));
  set.append(el("li", {}, `map cells: ${sig(rail.cell_mm, 3)} mm`));
  if (rail.stats.triangles) set.append(el("li", {}, `mesh: ${rail.stats.triangles.toLocaleString("en-US")} triangles`));
  const fill = (id, items, empty) => { const ul = $(id); ul.textContent = ""; for (const s of items.length ? items : [empty]) ul.append(el("li", {}, s)); };
  fill("assumptions", [...rail.assumptions, ...rail.notes], "None.");
  fill("warnings", rail.warnings, "None.");
}
function buttons() {
  const lays = $("layers"); lays.textContent = "";
  R().layers.forEach((lay, i) => lays.append(el("button", { type: "button", "aria-pressed": String(i === S.layer),
    title: `${sig(lay.power_w * 1e3, 3)} mW lost on this layer`, onclick: () => setLayer(i, true) }, lay.name)));
  const fs = $("fields"); fs.textContent = "";
  for (const [f, name] of FIELDS) fs.append(el("button", { type: "button", "aria-pressed": String(f === S.field),
    onclick: () => { S.field = f; buttons(); bar(); draw(); } }, name));
}
function setLayer(i, redraw) { S.layer = i; buttons(); bar(); if (redraw) draw(); }
function setRail(ri) {
  S.rail = ri; S.mark = null;
  const busiest = R().layers.findIndex((l) => l.name === R().busiest);
  S.layer = Math.max(0, busiest);
  for (const [n, b] of [...$("rails").children].entries()) b.setAttribute("aria-selected", String(n === ri));
  buttons(); bar(); panels(); fit();
}

// ---------------------------------------------------------------- start
$("title").textContent = M.title;
$("subtitle").textContent = [M.project && `Project ${M.project}`, M.board && `board ${M.board}`,
  `${M.rails.length} rail${M.rails.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
$("footer").textContent = `Generated by Coppernicus ${M.version} on ${M.generated}. Board files were only read. ` +
  "This page works offline: everything it shows is inside this file. Coordinates are mm in KiCad's frame (y down).";
M.rails.forEach((rail, ri) => $("rails").append(el("button", { type: "button", role: "tab", "aria-selected": "false",
  onclick: () => setRail(ri) }, rail.label, badge(rail.verdict))));
for (const k of ["sources", "loads", "hotspots"]) $("t-" + k).addEventListener("change", (e) => { S.show[k] = e.target.checked; draw(); });
$("fit").addEventListener("click", () => { S.mark = null; fit(); });
canvas.addEventListener("wheel", (e) => { e.preventDefault(); const r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  const f = Math.exp(-e.deltaY * 0.0015), v = S.view; v.tx = mx - (mx - v.tx) * f; v.ty = my - (my - v.ty) * f; v.s *= f; draw(); }, { passive: false });
canvas.addEventListener("pointerdown", (e) => { S.drag = [e.clientX, e.clientY, S.view.tx, S.view.ty]; canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener("pointermove", (e) => { if (S.drag) { S.view.tx = S.drag[2] + e.clientX - S.drag[0]; S.view.ty = S.drag[3] + e.clientY - S.drag[1]; draw(); } else readout(e); });
canvas.addEventListener("pointerup", () => { S.drag = null; });
window.addEventListener("resize", () => draw());
if (typeof DecompressionStream === "undefined") { $("busy").textContent = "This browser cannot decompress the maps (no DecompressionStream); use a current Chrome, Edge, Firefox or Safari."; $("busy").hidden = false; }
if (M.rails.length) setRail(0);
window.__coppernicus = { ready: () => layerImage(S.rail, S.layer, S.field).then(() => true), state: S };
})();
