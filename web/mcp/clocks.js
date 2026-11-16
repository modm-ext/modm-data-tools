// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// @ts-check
// The MCP app of clock_solve: the active path of the solution to its targets, read only, with the other inputs of its
// muxes dimmed. "all" shows the whole tree, fullscreen if the host has it.

import {connect} from "./host.js";
import {h} from "../shared/util.js";
import {activePath, focusOf, fmtHz, label} from "../clocks/model.js";
import {createClockGraph} from "../clocks/graph-view.js";

/** The tree from base64 of gzipped JSON. */
async function unpack(/** @type {string} */ text) {
  const bytes = Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
}

/** @type {ReturnType<typeof createClockGraph> | null} */
let graph = null;

await connect("modm-data clocks", async (main, view, app) => {
  const tree = await unpack(view.tree);
  const targets = new Map(Object.entries(view.targets).map(([id, hz]) => [id, Number(hz)]));
  const use = new Set(view.use);
  const path = activePath(tree, view.result, focusOf(tree, view.result, targets.keys(), use), {alternatives: true});
  const facts = [...targets].map(([id, hz]) => `${label(id)} ${fmtHz(hz)}`);
  const stage = h("div", {className: "mcp-stage"});
  main.append(h("p", {className: "mcp-caption"}, h("b", {}, view.partname), facts.length ? ` · ${facts.join(" · ")}` : ""), stage);

  graph?.destroy();
  graph = createClockGraph(stage, tree, {editable: false});
  let all = false;
  const show = () => graph?.update({result: view.result, targets, use,
    nodes: all ? null : path.nodes, alternatives: all ? undefined : path.alternatives});
  const button = (/** @type {string} */ text, /** @type {string} */ title, /** @type {() => void} */ onclick) =>
    h("button", {type: "button", textContent: text, title, onclick});
  const toggle = button("all", "the whole clock tree instead of the active path", () => {
    all = !all;
    toggle.setAttribute("aria-pressed", String(all));
    show();
  });
  graph.hud.prepend(toggle);
  if (app.getHostContext()?.availableDisplayModes?.includes("fullscreen")) {
    graph.hud.prepend(button("fullscreen", "fullscreen or back inline", async () => {
      const mode = document.documentElement.dataset.mode === "fullscreen" ? "inline" : "fullscreen";
      document.documentElement.dataset.mode = (await app.requestDisplayMode({mode})).mode;
      requestAnimationFrame(() => graph?.fit());
    }));
  }
  show();
});
