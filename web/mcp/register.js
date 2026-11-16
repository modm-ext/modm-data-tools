// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// @ts-check
// The MCP app of decode_register: the decoded registers drawn like in the reference manual.

import {connect} from "./host.js";
import {el, plural} from "../shared/util.js";
import {renderRegister} from "../svd/register-view.js";

await connect("modm-data register", (main, /** @type {{registers: any[], total: number}} */ view) => {
  for (const {register, value} of view.registers) main.append(renderRegister(register, {value}));
  const more = view.total - view.registers.length;
  if (more > 0) main.append(el("p", "mcp-note", `${plural(more, "more register")} only in the text`));
});
