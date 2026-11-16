// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// The DOM-free helpers of web/shared/util.js.

import {test} from "node:test";
import assert from "node:assert/strict";
import {clamp, debounce, esc, hex, hexBits, highlight, num, plural, regex} from "../../web/shared/util.js";

test("regex: null when empty, undefined when invalid, else case insensitive", () => {
  assert.equal(regex(""), null);
  assert.equal(regex("("), undefined);
  const re = regex("usart\\d");
  assert.ok(re instanceof RegExp && re.test("USART2_TX"));
});

test("hex pads to the digits and writes upper case", () => {
  assert.equal(hex(1024, 4), "0x0400");
  assert.equal(hex(255), "0xFF");
  assert.equal(hex(0x40013800, 8), "0x40013800");
  assert.equal(hex(0x12345, 2), "0x12345");
});

test("hexBits pads to the digits of a value of this many bits", () => {
  assert.equal(hexBits(0xabcd, 32), "0x0000ABCD");
  assert.equal(hexBits(1, 1), "0x1");
  assert.equal(hexBits(1, 0), "0x1");
  assert.equal(hexBits(0xffffffffffn, 40), "0xFFFFFFFFFF");
});

test("plural, esc, highlight and clamp", () => {
  assert.equal(plural(1, "field"), "1 field");
  assert.equal(plural(1200, "field"), "1,200 fields");
  assert.equal(plural(2, "match", "matches"), "2 matches");
  assert.equal(esc(`<a href="x">&</a>`), "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;");
  assert.equal(esc(null), "");
  assert.equal(highlight("USART1 <TX>", /tx/i), "USART1 &lt;<mark>TX</mark>&gt;");
  assert.equal(highlight("CR1 CR2", /CR/g), "<mark>CR</mark>1 <mark>CR</mark>2");
  // Empty matches mark nothing and do not loop
  assert.equal(highlight("a<b", /x*/), "a&lt;b");
  assert.equal(highlight("a<b", null), "a&lt;b");
  assert.equal(highlight(null, /a/), "");
  assert.equal(clamp(5, 0, 3), 3);
  assert.equal(clamp(-1, 0, 3), 0);
  assert.equal(clamp(2, 0, 3), 2);
});

test("num separates thousands", () => {
  assert.equal(num(1234567), "1,234,567");
  assert.equal(num(12), "12");
});

test("debounce calls once per pause with the last arguments", (t) => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  const calls = [];
  const fn = debounce((...args) => calls.push(args), 100);
  fn(1);
  fn(2);
  t.mock.timers.tick(99);
  fn(3, "x");
  t.mock.timers.tick(99);
  assert.deepEqual(calls, []);
  t.mock.timers.tick(1);
  assert.deepEqual(calls, [[3, "x"]]);
  fn(4);
  t.mock.timers.tick(100);
  assert.deepEqual(calls, [[3, "x"], [4]]);
  const fallback = debounce(() => calls.push("default"));
  fallback();
  t.mock.timers.tick(149);
  assert.equal(calls.length, 2);
  t.mock.timers.tick(1);
  assert.equal(calls.length, 3);
});
