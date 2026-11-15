// The clock solver UI: one graph per device, every node live at its setting.
// The clock logic is the Python of modm_data.clocks, running in a Pyodide worker
import * as py from "./clocks-py.js";
import {fmtHz} from "./clocks-py.js";

// What the controls offer, read from the tree like solver.py's factors_of() and is_variable()
const factorsOf = (node) => node.domain ? node.domain.map((d) => d.factor ?? d.value)
  : node.range ? Array.from({length: node.range[1] - node.range[0] + 1}, (_, i) => node.range[0] + i) : [];
const isVariable = (node) => node.kind === "mux" ? node.inputs.length > 1
  : ["div", "mul", "mulfrac", "frac"].includes(node.kind) ? !node.rules && factorsOf(node).length > 1
  : node.kind === "choice" && (node.domain?.length ?? 0) > 1;
/** The setting a node stands at, as the last evaluation saw it. */
const choice = (id) => state.result?.[id]?.setting ?? state.tree.nodes[id]?.value ?? state.tree.nodes[id]?.default;

let apiIndex = null;
async function loadApi(device) {
  apiIndex ??= await fetch("data/rcc.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
  return apiIndex[device] ? fetch(`data/rcc/${apiIndex[device]}.json`).then((r) => r.json()) : null;
}

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...children);
  return e;
};
// CubeMX calls dividers devisors, the UI does not
const label = (text) => String(text).replace(/Devisor/g, "Divider").replace(/devisor/g, "divider");
const KIND = {mux: "mux", div: "divider", mul: "multiplier", mulfrac: "multiplier", frac: "fraction", fixed: "fixed",
  varied: "source", choice: "source", output: "output", peripheral: "peripheral"};
const mhz = (hz) => (hz === null || hz === undefined ? "" : String(Number((hz / 1e6).toPrecision(9))));

const state = {
  device: null, tree: null, solver: null,
  config: {},          // current choices by node id, options and modes by name
  locked: new Set(),   // nodes the solver must not move
  targets: new Map(),  // node id -> Hz
  use: new Set(),      // peripherals in use
  tolerance: 0,
  result: null, solved: null, positions: {},
};
let devices = {};

export async function start() {
  devices = await (await fetch("data/devices.json")).json();
  $("devices").append(...Object.keys(devices).map((d) => el("option", {value: d})));
  $("device").onchange = () => {
    const name = matchDevice($("device").value);
    if (name) load(name, {});
  };
  $("solve").onclick = solve;
  $("unlock").onclick = () => { state.locked.clear(); refresh(); };
  $("reset").onclick = () => {
    Object.assign(state, {config: {}, locked: new Set(), targets: new Map(), use: new Set(), solved: null});
    refresh();
  };
  $("maxperf").onclick = async () => { await maxPerf(); refresh(); };
  $("export").onclick = () => {
    const a = el("a", {href: URL.createObjectURL(new Blob([JSON.stringify(snapshot(), null, 1)], {type: "application/json"})),
      download: `${state.device}-clock.json`});
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  $("import").onclick = () => $("importFile").click();
  $("importFile").onchange = async () => {
    const file = $("importFile").files[0];
    $("importFile").value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!devices[data.device]) throw new Error(`unknown device ${data.device}`);
      await load(data.device, data);
    } catch (error) { $("status").innerHTML = `<span class="err">import: ${error.message}</span>`; }
  };
  $("tolerance").onchange = () => { state.tolerance = Number($("tolerance").value) || 0; save(); };
  const side = (hide) => {
    document.body.classList.toggle("noside", hide);
    $("sidebtn").textContent = hide ? "Show panel" : "Hide panel";
    try { localStorage.setItem("noside", hide ? "1" : ""); } catch {}
  };
  $("sidebtn").onclick = () => side(!document.body.classList.contains("noside"));
  // Drag the grip to resize the panel, the width is remembered like the panel itself
  const width = (px) => {
    document.body.style.setProperty("--side", `${Math.round(Math.min(innerWidth - 200, Math.max(240, px)))}px`);
  };
  $("grip").onpointerdown = (e) => {
    const grip = $("grip");
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("drag");
    grip.onpointermove = (m) => width(innerWidth - m.clientX);
    grip.onpointerup = () => {
      grip.onpointermove = grip.onpointerup = null;
      grip.classList.remove("drag");
      try { localStorage.setItem("side", document.body.style.getPropertyValue("--side")); } catch {}
    };
  };
  try { const saved = parseInt(localStorage.getItem("side")); if (saved) width(saved); } catch {}
  try { side(Boolean(localStorage.getItem("noside"))); } catch {}
  setupStage();
  const saved = await readHash();
  await load(saved?.device && devices[saved.device] ? saved.device : "STM32G071RBTx", saved ?? {});
}

function matchDevice(text) {
  const key = text.trim().toUpperCase().replace(/^(?!STM32)/, "STM32");
  // Device names end in mixed case (STM32H743ZITx), compare without case
  return Object.keys(devices).find((d) => d.toUpperCase() === key) ??
    Object.keys(devices).find((d) => d.toUpperCase().startsWith(key)) ?? null;
}

export async function load(device, saved) {
  $("status").innerHTML = `<span class="sub">loading ${device}…</span>`;
  const text = await (await fetch(`data/tree/${devices[device]}.json`)).text();
  const tree = JSON.parse(text);
  state.api = await loadApi(device);
  await py.load(device, text, state.api);
  Object.assign(state, {
    device, tree, solved: null,
    config: saved.config ?? {}, locked: new Set(saved.locked ?? []),
    targets: new Map(Object.entries(saved.targets ?? {}).map(([k, v]) => [k, Number(v)])),
    use: new Set(saved.use ?? []), tolerance: Number(saved.tolerance ?? 0),
    positions: saved.positions ?? {},
  });
  $("device").value = device;
  $("tolerance").value = state.tolerance;
  build();
  const fresh = !Object.keys(state.positions).length;
  // A device opened fresh starts at its highest performance, a saved one (boot config included) stays as saved
  if (!saved.config) await maxPerf();
  await refresh();
  layout(fresh);
  requestAnimationFrame(fit);
}

/* ------------------------------------------------------------------ state */

const useSet = () => (new Set([...state.use, ...[...state.targets.keys()]
  .filter((id) => state.tree.nodes[id]?.kind === "peripheral")]));

let generation = 0;
async function refresh() {
  // Only the latest of overlapping refreshes paints
  const mine = ++generation;
  let result, code;
  try {
    result = await py.evaluate(state.config, useSet());
    code = await py.generate(state.config, useSet()).catch((error) => `// code generation failed: ${error.message}`);
  } catch (error) {
    if (mine === generation) $("status").innerHTML = `<span class="err">${error.message}</span>`;
    return;
  }
  if (mine !== generation) return;
  state.result = result;
  $("code").textContent = code;
  for (const id of boxes.keys()) paint(id);
  layout(false);
  sidePanel();
  save();
}

function snapshot() {
  return {device: state.device, config: state.config, locked: [...state.locked],
    targets: Object.fromEntries(state.targets), use: [...state.use], tolerance: state.tolerance};
}

// The hash is base64url of the deflated JSON: a few hundred characters instead of kilobytes of %XX
async function pipe(bytes, stream) {
  return new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer());
}

let saving = 0;
async function save() {
  const mine = ++saving;
  const z = await pipe(new TextEncoder().encode(JSON.stringify(snapshot())), new CompressionStream("deflate-raw"));
  const b64 = btoa(String.fromCharCode(...z)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  if (mine === saving) history.replaceState(null, "", "#" + b64);
}

async function readHash() {
  const hash = location.hash.slice(1);
  try {
    if (!hash) return null;
    const bin = atob(hash.replace(/-/g, "+").replace(/_/g, "/"));
    const z = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(await pipe(z, new DecompressionStream("deflate-raw"))));
  } catch { return null; }
}

/** A choice made by hand stays put: the solver may not move it until it is unlocked. */
function choose(id, value) {
  state.config[id] = value;
  state.locked.add(id);
  state.solved = null;
  refresh();
}

function setTarget(id, text) {
  const value = parseFloat(text);
  if (text.trim() === "" || !Number.isFinite(value)) state.targets.delete(id);
  else state.targets.set(id, value * 1e6);
  if (state.targets.size) solve(); else refresh();
}

/** Highest SYSCLK from the HSE, every peripheral in use if that works; everything else unlocked. */
async function maxPerf() {
  const t0 = performance.now();
  const r = await py.maxPerformance(state.config.HSEOSC ? {HSEOSC: state.config.HSEOSC} : {});
  const ms = Math.round(performance.now() - t0);
  if (!r) {
    state.solved = {ok: false, ms, unmet: [{id: null, why: "no HSE or SYSCLK in this tree"}], problems: []};
    return;
  }
  // Every peripheral ticked when that works, each one can be unticked afterwards
  const all = Object.keys(state.tree.nodes).filter((id) => state.tree.nodes[id].kind === "peripheral");
  Object.assign(state, {config: r.config, locked: new Set(), targets: new Map(), use: new Set(r.use === null ? all : [])});
  state.solved = {ok: true, ms, changed: Object.keys(r.config),
    note: `max performance from HSE: SYSCLK ${fmtHz(r.result.SysCLKOutput.freq)}, ${r.use === null ? "every peripheral ticked" : "not possible with every peripheral in use, none ticked"}`};
}

async function solve() {
  const t0 = performance.now();
  $("status").innerHTML = `<span class="sub">solving…</span>`;
  const r = await py.solve({config: state.config, locked: state.locked, targets: Object.fromEntries(state.targets),
    use: useSet(), tolerance: state.tolerance * 1e-6, budget: 3});
  const ms = Math.round(performance.now() - t0);
  if (r.ok) {
    const changed = Object.keys(r.config).filter((k) => String(r.config[k]) !== String(state.config[k] ?? ""));
    state.config = r.config;
    state.solved = {ok: true, ms, changed};
  } else {
    state.solved = {ok: false, ms, unmet: r.unmet, problems: r.problems, timeout: r.timeout};
  }
  refresh();
}

/* ------------------------------------------------------------------ nodes */

const boxes = new Map();
// Outputs that only feed peripherals, and the peripherals, render as lines inside the node that feeds them:
// a box each made the graph 3x taller and unreadable on big devices
let host = {}, feeds = new Map(), cards = new Set();
const owner = (id) => host[id] ?? id;

function build() {
  const world = $("world"), nodes = state.tree.nodes;
  for (const box of boxes.values()) box.remove();
  boxes.clear();
  host = {};
  feeds = new Map();
  const leafOut = (n) => n.kind === "output" && n.inputs.length === 1 && nodes[n.inputs[0].from] &&
    (n.outputs ?? []).every((o) => nodes[o]?.kind === "peripheral");
  for (const [id, n] of Object.entries(nodes)) {
    if (!leafOut(n)) continue;
    host[id] = n.inputs[0].from;
    // An output that feeds nothing is a line of its own
    if (!n.outputs?.length) feeds.set(host[id], [...(feeds.get(host[id]) ?? []), id]);
  }
  for (const [id, n] of Object.entries(nodes)) {
    if (n.kind !== "peripheral" || n.inputs.length !== 1 || !nodes[n.inputs[0].from]) continue;
    host[id] = owner(n.inputs[0].from);
    feeds.set(host[id], [...(feeds.get(host[id]) ?? []), id]);
  }
  // A kernel clock mux that only clocks peripherals is a card below the graph, its inputs are chips, no wires
  cards = new Set(Object.keys(nodes).filter((id) => !host[id] && nodes[id].kind === "mux" && nodes[id].inputs.length > 1 &&
    (nodes[id].outputs ?? []).every((o) => host[o])));
  for (const id of Object.keys(nodes)) {
    if (host[id]) continue;
    const box = el("div", {className: "node"});
    box.dataset.id = id;
    world.append(box);
    boxes.set(id, box);
  }
}

/** Renders one node: its frequency, its choice, its target, its limits and violations. */
function paint(id) {
  const node = state.tree.nodes[id], box = boxes.get(id), r = state.result[id] ?? {};
  const periph = node.kind === "peripheral";
  box.className = "node" + (periph ? " periph" : "") + (cards.has(id) ? " wide" : "") + (r.used ? " used" : "") +
    (state.locked.has(id) ? " locked" : "") + (state.targets.has(id) ? " target" : "") +
    (r.errors?.length && r.used ? " bad" : "");
  box.textContent = "";

  const head = el("div", {className: "head"}, el("span", {className: "name", textContent: label(id), title: label(id)}));
  if (isVariable(node)) {
    const locked = state.locked.has(id);
    head.append(el("button", {className: "lockbtn" + (locked ? " on" : ""), textContent: locked ? "🔒" : "🔓",
      title: locked ? "unlock, the solver may change it" : "lock, the solver keeps it",
      onclick: (e) => { e.stopPropagation(); locked ? state.locked.delete(id) : state.locked.add(id); refresh(); }}));
  } else head.append(el("span", {className: "kind", textContent: KIND[node.kind] ?? node.kind}));
  box.append(head);

  const hz = el("span", {className: "hz" + (r.freq === null || r.freq === undefined ? " none" : ""), textContent: fmtHz(r.freq)});

  // The target sits on every node, a peripheral also says whether it is in use
  const target = el("input", {className: "tgt" + (state.targets.has(id) ? " set" : ""), placeholder: "target MHz",
    value: state.targets.has(id) ? mhz(state.targets.get(id)) : "", title: "target frequency in MHz, Enter solves"});
  target.onchange = () => setTarget(id, target.value);
  const row = el("div", {className: "row", style: "justify-content:space-between"}, hz, el("span", {style: "flex:1"}), target);
  if (periph) {
    const use = el("input", {type: "checkbox", checked: state.use.has(id) || state.targets.has(id), title: "in use: its limits and usage flags count"});
    use.onchange = () => { use.checked ? state.use.add(id) : state.use.delete(id); refresh(); };
    row.append(el("label", {className: "lbl"}, use, "use"));
  }
  box.append(row);
  const control = controlOf(id, node);
  if (control) box.append(el("div", {className: "row"}, control));
  if (periph && node.bus) box.append(el("div", {className: "lim", textContent: `bus ${label(node.bus)} ${fmtHz(state.result[node.bus]?.freq)}`}));

  const limits = limitText(node, r);
  if (limits) box.append(el("div", {className: "lim", textContent: limits}));
  for (const error of (r.used ? r.errors ?? [] : [])) box.append(el("div", {className: "lim bad", textContent: "✗ " + humanError(error)}));
  paintFeeds(id, box, r);
}

/** The peripherals a node clocks, through an output or directly: one line each with its use checkbox. */
function paintFeeds(id, box, r) {
  const list = feeds.get(id);
  if (!list) return;
  // A shared output breaking a limit shows once under its own name, not once per peripheral
  const nodes = state.tree.nodes, grid = el("div", {className: "feeds"}), bad = new Set();
  for (const p of list.sort()) {
    if (nodes[p].kind === "output") {
      const rp = state.result[p] ?? {}, errors = (rp.used ? rp.errors ?? [] : []).map(humanError);
      grid.append(el("span", {className: "feed" + (errors.length ? " bad" : ""),
        title: [label(p), limitText(nodes[p], rp), ...errors].filter(Boolean).join("\n")}, "→ " + label(p)));
      for (const e of errors) bad.add(`${label(p)}: ${e}`);
      continue;
    }
    const via = nodes[p].inputs[0].from, rp = state.result[p] ?? {}, rv = host[via] ? state.result[via] ?? {} : {};
    const errors = [...(rv.used ? rv.errors ?? [] : []), ...(rp.used ? rp.errors ?? [] : [])].map(humanError);
    const limits = [host[via] ? limitText(nodes[via], rv) : "", limitText(nodes[p], rp)].filter(Boolean).join(" · ");
    const inUse = state.use.has(p) || state.targets.has(p);
    const use = el("input", {type: "checkbox", checked: inUse});
    use.onchange = () => { use.checked ? state.use.add(p) : state.use.delete(p); refresh(); };
    const freq = rp.freq !== r.freq && rp.freq ? ` ${fmtHz(rp.freq)}` : "";
    grid.append(el("label", {className: "feed" + (inUse ? " on" : "") + (errors.length ? " bad" : ""),
      title: [`${label(p)}: in use, its limits and usage flags count`, nodes[p].bus ? `bus ${label(nodes[p].bus)}` : "", limits, ...errors].filter(Boolean).join("\n")},
      use, label(p) + freq));
    for (const e of (rv.used ? rv.errors ?? [] : [])) bad.add(`${label(via)}: ${humanError(e)}`);
    for (const e of (rp.used ? rp.errors ?? [] : [])) bad.add(`${label(p)}: ${humanError(e)}`);
  }
  box.append(grid);
  for (const e of bad) box.append(el("div", {className: "lim bad", textContent: "✗ " + e}));
}

function humanError(text) {
  return text.replace(/\d+(?:\.\d+)?(?:e[+-]\d+)?/g, (m) => (Number(m) >= 1000 ? fmtHz(Number(m)) : m));
}

/** The limits that apply in this configuration, from evaluate()'s bounds, else the static ones. */
function limitText(node, r) {
  const parts = [];
  const span = ([lo, hi]) => (lo && hi ? `${fmtHz(lo)}…${fmtHz(hi)}` : hi ? `≤ ${fmtHz(hi)}` : lo ? `≥ ${fmtHz(lo)}` : "");
  for (const prefix of ["", "out_"]) {
    let b = r.bounds?.[prefix];
    if (!b && (prefix === "out_" || ["fixed", "varied", "output"].includes(node.kind)))
      b = [node[`${prefix}min`] ?? null, node[`${prefix}max`] ?? null];
    const text = b ? span(b) : "";
    if (text) parts.push(text);
  }
  const variants = (node.limits?.length ?? 0) + (node.out_limits?.length ?? 0);
  return parts.length ? parts.join(" · ") + (variants > 1 ? " (depends on config)" : "") : "";
}

function controlOf(id, node) {
  const now = choice(id);
  if (cards.has(id)) {
    const chips = el("div", {className: "chips"});
    for (const input of node.inputs) {
      const macro = input.macro || input.from, sel = now === macro;
      chips.append(el("button", {className: "chip" + (sel ? " sel" : "") + (sel && state.result[id]?.used ? " ok" : ""),
        textContent: label(input.from), title: `${label(input.from)}${input.signal ? ` (${input.signal})` : ""}: ${fmtHz(state.result[input.from]?.freq)}`,
        onclick: () => choose(id, macro)}));
    }
    return chips;
  }
  if (node.kind === "mux") {
    const select = el("select", {title: node.param ?? ""});
    for (const input of node.inputs) {
      const macro = input.macro || input.from;
      select.append(el("option", {value: macro, textContent: label(input.from) + (input.signal && input.signal !== input.from ? ` (${input.signal})` : "")}));
    }
    select.value = now ?? "";
    select.onchange = () => choose(id, select.value);
    return select;
  }
  if (["div", "mul", "mulfrac", "frac"].includes(node.kind) && !node.rules) {
    const factors = factorsOf(node);
    const sign = node.kind === "div" ? "÷" : node.kind === "frac" ? "+" : "×";
    if (factors.length > 1 && (node.domain || factors.length <= 64)) {
      const select = el("select", {title: node.param ?? ""});
      for (const f of factors) select.append(el("option", {value: f, textContent: `${sign} ${f}${node.kind === "frac" ? "/8192" : ""}`}));
      select.value = now;
      select.onchange = () => choose(id, Number(select.value));
      return select;
    }
    if (factors.length > 64 && node.range) {
      const input = el("input", {type: "number", min: node.range[0], max: node.range[1], value: now ?? "",
        title: `${sign} ${node.range[0]}…${node.range[1]}`});
      input.onchange = () => choose(id, Math.min(node.range[1], Math.max(node.range[0], Math.round(Number(input.value)))));
      return el("span", {className: "row", style: "margin:0;flex:1"}, el("span", {className: "lbl", textContent: sign}), input);
    }
  }
  if (node.rules) {
    const factor = state.result[id]?.freq && state.result[node.inputs[0]?.from]?.freq
      ? state.result[id].freq / state.result[node.inputs[0].from].freq : null;
    return el("span", {className: "lbl", textContent: `follows config${factor ? `: × ${Number(factor.toPrecision(4))}` : ""}`});
  }
  if (node.kind === "varied") {
    const input = el("input", {type: "number", step: "any", value: mhz(now), title: limitText(node, {})});
    input.onchange = () => choose(id, Number(input.value) * 1e6);
    return el("span", {className: "row", style: "margin:0;flex:1"}, input, el("span", {className: "lbl", textContent: "MHz"}));
  }
  if (node.kind === "choice" && node.domain?.length) {
    const select = el("select");
    for (const o of node.domain) select.append(el("option", {value: o.value, textContent: fmtHz(o.value)}));
    select.value = now;
    select.onchange = () => choose(id, Number(select.value));
    return select;
  }
  return null;
}

/* ------------------------------------------------------------------ wires */

/** The wires between boxes: lines inside a box have none. */
function edges() {
  const out = [];
  for (const [id, node] of Object.entries(state.tree.nodes))
    for (const input of node.inputs)
      if (state.tree.nodes[input.from] && !host[id]) out.push({from: owner(input.from), to: id, input});
  return out;
}

function paintWires() {
  const svg = $("wires");
  svg.textContent = "";
  const ns = "http://www.w3.org/2000/svg";
  for (const {from, to, input} of edges()) {
    const a = state.positions[from], b = state.positions[to];
    if (a?.x === undefined || b?.x === undefined) continue;
    const node = state.tree.nodes[to];
    // A mux carries only the input it has selected, everything else carries its input
    const selected = node.kind !== "mux" || choice(to) === (input.macro || input.from);
    if (cards.has(to)) continue;
    const live = selected && state.result[to]?.used && state.result[from]?.used;
    const x1 = a.x + a.w, y1 = a.y + 14, x2 = b.x, y2 = b.y + 14, mid = Math.max(x1 + 20, (x1 + x2) / 2);
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", `M${x1},${y1} C${mid},${y1} ${x2 - (mid - x1)},${y2} ${x2},${y2}`);
    path.setAttribute("class", live ? "live" : selected ? "sel" : "");
    svg.append(path);
  }
}

/* ----------------------------------------------------------------- layout */

/** Layered left to right with dagre; the sinks (kernel clock muxes and the like) wrap into columns on the right. */
function layout(auto = true) {
  // Measured at zoom 1 and then fixed: the browser's minimum font size makes boxes taller when zoomed out
  $("world").style.zoom = 1;
  for (const box of boxes.values()) box.style.height = "";
  for (const [id, box] of boxes) state.positions[id] = {...(state.positions[id] ?? {}), w: box.offsetWidth, h: box.offsetHeight};
  $("world").style.zoom = view.k;
  if (auto) {
    const g = new dagre.graphlib.Graph();
    g.setGraph({rankdir: "LR", nodesep: 10, ranksep: 46, marginx: 20, marginy: 20});
    g.setDefaultEdgeLabel(() => ({}));
    const ids = [...boxes.keys()], rest = ids.filter((id) => !cards.has(id));
    for (const id of rest) g.setNode(id, {width: state.positions[id].w, height: state.positions[id].h});
    for (const {from, to} of edges()) if (!cards.has(to)) g.setEdge(from, to);
    dagre.layout(g);
    for (const id of rest) {
      const n = g.node(id), p = state.positions[id];
      p.x = n.x - p.w / 2;
      p.y = n.y - p.h / 2;
    }
    // Kernel clock cards in rows below the graph, as wide as the graph
    const bottom = Math.max(0, ...rest.map((id) => state.positions[id].y + state.positions[id].h)) + 40;
    const width = Math.max(1100, ...rest.map((id) => state.positions[id].x + state.positions[id].w));
    let x = 20, y = bottom, row = 0;
    for (const id of [...cards].sort()) {
      const p = state.positions[id];
      if (x + p.w > width && x > 20) { x = 20; y += row + 8; row = 0; }
      Object.assign(p, {x, y});
      x += p.w + 8;
      row = Math.max(row, p.h);
    }
  }
  place();
}

function place() {
  let w = 0, h = 0;
  for (const [id, box] of boxes) {
    const p = state.positions[id];
    if (p.x === undefined) continue;
    box.style.left = `${p.x}px`;
    box.style.top = `${p.y}px`;
    box.style.height = `${p.h}px`;
    w = Math.max(w, p.x + p.w);
    h = Math.max(h, p.y + p.h);
  }
  $("world").style.width = `${w + 40}px`;
  $("world").style.height = `${h + 40}px`;
  $("wires").setAttribute("width", w + 40);
  $("wires").setAttribute("height", h + 40);
  paintWires();
}

/* -------------------------------------------------------------- pan, zoom */

// Pan with a transform on the wrapper, zoom with CSS zoom on the world: scale() blurs in Safari
const view = {x: 0, y: 0, k: 0.8};
// At most one style update per frame: wheel and pointer events come faster than the browser paints
let viewFrame = 0;
const applyView = () => {
  viewFrame ||= requestAnimationFrame(() => {
    viewFrame = 0;
    $("pan").style.transform = `translate(${view.x}px, ${view.y}px)`;
    $("world").style.zoom = view.k;
  });
};

function zoomAt(k, px, py) {
  k = Math.min(2.5, Math.max(0.15, k));
  view.x = px - (px - view.x) * (k / view.k);
  view.y = py - (py - view.y) * (k / view.k);
  view.k = k;
  applyView();
}

function fit() {
  const stage = $("stage").getBoundingClientRect(), world = $("world");
  const w = parseFloat(world.style.width) || 1000, h = parseFloat(world.style.height) || 800;
  view.k = Math.min(1.2, Math.max(0.15, Math.min(stage.width / w, stage.height / h)));
  view.x = (stage.width - w * view.k) / 2;
  view.y = Math.max(0, (stage.height - h * view.k) / 2);
  applyView();
}

function setupStage() {
  const stage = $("stage");
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(view.k * Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
  }, {passive: false});
  let drag = null;
  stage.addEventListener("pointerdown", (e) => {
    const head = e.target.closest(".head");
    if (head && !e.target.closest("button")) {
      const id = head.parentElement.dataset.id;
      drag = {id, x: e.clientX, y: e.clientY, p: {...state.positions[id]}};
    } else if (!e.target.closest(".node")) {
      drag = {pan: true, x: e.clientX - view.x, y: e.clientY - view.y};
      stage.classList.add("panning");
    } else return;
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (drag.pan) {
      view.x = e.clientX - drag.x;
      view.y = e.clientY - drag.y;
      applyView();
    } else {
      const p = state.positions[drag.id];
      p.x = drag.p.x + (e.clientX - drag.x) / view.k;
      p.y = drag.p.y + (e.clientY - drag.y) / view.k;
      boxes.get(drag.id).style.left = `${p.x}px`;
      boxes.get(drag.id).style.top = `${p.y}px`;
      paintWires();
    }
  });
  const end = () => { drag = null; stage.classList.remove("panning"); };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
  const center = () => { const r = stage.getBoundingClientRect(); return [r.width / 2, r.height / 2]; };
  $("zoomIn").onclick = () => zoomAt(view.k * 1.25, ...center());
  $("zoomOut").onclick = () => zoomAt(view.k / 1.25, ...center());
  $("fit").onclick = fit;
  $("relayout").onclick = () => { layout(true); fit(); };
  applyView();
}

/* ------------------------------------------------------------------- side */

function sidePanel() {
  const r = state.result, s = state.solved;
  const status = $("status");
  if (!s) status.innerHTML = state.targets.size ? `<span class="sub">targets set, press Solve</span>` : "";
  else if (s.ok) status.innerHTML = `<span class="ok">${s.note ?? "solved"} (${s.ms} ms, ${s.changed.length} settings)</span>`;
  else status.innerHTML = `<span class="err">no solution (${s.ms} ms)</span>`;

  const box = $("result");
  box.textContent = "";
  const errors = Object.entries(r).filter(([, v]) => v.used && v.errors?.length);
  const fl = r.FLatency?.setting, opts = r.options?.setting ?? {};
  box.append(el("div", {className: "sub", textContent: [state.device, state.tree.tree, state.tree.rcc, `${Object.keys(state.tree.nodes).length} nodes`].filter(Boolean).join(" · ")}));
  box.append(el("div", {innerHTML: `flash: <b>${fl ?? "—"}</b>` + (opts.PWR_Regulator_Voltage_Scale ? ` · scale <b>${opts.PWR_Regulator_Voltage_Scale.replace(/^PWR_REGULATOR_VOLTAGE_/, "")}</b>` : "") +
    (opts.PWREXT_OverDrive ? ` · over-drive <b>${opts.PWREXT_OverDrive.replace(/^PWREXT_OverDrive_/, "")}</b>` : "")}));
  if (s && !s.ok) {
    const items = s.unmet.map((u) => u.id ? `${label(u.id)} ${fmtHz(state.targets.get(u.id))}: ${u.why}${u.closest ? `, closest ${fmtHz(u.closest)}` : ""}` : u.why);
    box.append(el("div", {className: "err", textContent: "Targets not met:"}), el("ul", {className: "list err"}, ...items.map((t) => el("li", {textContent: t}))));
    if (s.problems.length) box.append(el("div", {className: "sub", textContent: "best attempt breaks:"}),
      el("ul", {className: "list"}, ...s.problems.slice(0, 8).map((t) => el("li", {textContent: humanError(t)}))));
  }
  if (errors.length) box.append(el("div", {className: "err", textContent: `${errors.length} nodes break a limit`}),
    el("ul", {className: "list err"}, ...errors.slice(0, 12).map(([id, v]) => el("li", {textContent: `${label(id)}: ${v.errors.map(humanError).join(", ")}`}))));
  else box.append(el("div", {className: "ok", textContent: "all limits met"}));
  if (state.targets.size) box.append(el("div", {className: "sub", textContent: "targets: " +
    [...state.targets].map(([id, f]) => `${label(id)} ${fmtHz(f)} → ${fmtHz(r[id]?.freq)}`).join(", ")}));

  // Options that no node stands for and the oscillator modes; derived ones follow the clocks unless set
  const opt = $("options");
  opt.textContent = "";
  for (const [name, values] of Object.entries(state.tree.choices ?? {})) {
    const derived = name in (state.tree.derived ?? {});
    const select = el("select");
    select.append(el("option", {value: "", textContent: derived ? `auto: ${short(opts[name])}` : `default: ${short(state.tree.options[name])}`}));
    for (const v of values) select.append(el("option", {value: v, textContent: short(v)}));
    select.value = state.config[name] ?? "";
    select.onchange = () => { if (select.value) state.config[name] = select.value; else delete state.config[name]; state.solved = null; refresh(); };
    opt.append(el("div", {className: "opt"}, el("span", {textContent: name}), select));
  }
  for (const [name, modes] of Object.entries(state.tree.modes ?? {})) {
    const select = el("select");
    select.append(el("option", {value: "", textContent: "unset"}));
    for (const m of Object.keys(modes)) select.append(el("option", {value: m, textContent: m}));
    select.value = state.config[name] ?? "";
    select.onchange = () => { if (select.value) state.config[name] = select.value; else delete state.config[name]; refresh(); };
    opt.append(el("div", {className: "opt"}, el("span", {textContent: `${name} mode`}), select));
  }

}

const short = (macro) => String(macro ?? "").replace(/^(PWR_REGULATOR_VOLTAGE_|PWREXT_OverDrive_|RCC_\w+?SOURCE_|RCC_)/, "");
