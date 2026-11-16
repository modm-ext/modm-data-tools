// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// @ts-check
// The part of the MCP apps that talks to the host (Claude desktop, claude.ai, ...) through the ext-apps App client:
// the host's theme and display mode on <html>, and every tool result to a render function. The server bundles this
// with the views into one page, see src/modm_data_tools/lookup/apps.py.

import {App} from "https://unpkg.com/@modelcontextprotocol/ext-apps@1.7.5/dist/src/app-with-deps.js";
import {el} from "../shared/util.js";

/** The key of the view data in the _meta of a tool result. */
export const KEY = "modm-data/view";

/**
 * Connects to the host and calls render with a fresh <main> and the view data of every tool result; a result
 * without view data (an error) shows its text. The size changes reach the host by themselves (autoResize).
 * @param {string} name
 * @param {(main: HTMLElement, data: any, app: App) => void | Promise<void>} render
 */
export async function connect(name, render) {
  const app = new App({name, version: "1.0.0"});
  const main = document.body.appendChild(el("main", "mcp-app"));
  const context = (/** @type {any} */ ctx) => {
    // theme.css follows data-theme, the styles of the clock path follow data-mode
    if (ctx?.theme) document.documentElement.dataset.theme = ctx.theme;
    if (ctx?.displayMode) document.documentElement.dataset.mode = ctx.displayMode;
  };
  app.addEventListener("hostcontextchanged", context);
  app.addEventListener("toolresult", async (/** @type {any} */ result) => {
    const data = result._meta?.[KEY];
    main.replaceChildren();
    try {
      if (data) return await render(main, data, app);
    } catch (error) {
      main.append(el("p", "mcp-error", `cannot show the result: ${/** @type {Error} */ (error).message}`));
    }
    main.append(el("pre", "mcp-text", (result.content ?? []).map((/** @type {any} */ c) => c.text ?? "").join("\n")));
  });
  await app.connect();
  context(app.getHostContext());
  return app;
}
