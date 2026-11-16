// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// The peripheral variant model on a hand-made USART variant shaped like variants/<mode>/<group>.json.

import {test} from "node:test";
import assert from "node:assert/strict";
import {accessTag, describe, elements, hue, layout, matches, offset, parseHash, range, sortGroups, usedBy}
  from "../../web/variants/model.js";

/** @returns {import("../../web/variants/model.js").Variant} */
const variant = () => ({
  instances: 3, devices: [0, 2], names: ["USART1", "USART2", "USART3"], members: [0, 1, 2], renames: [],
  conflicts: {},
  features: [{name: "FIFO", elements: 2, registers: 0, instances: 2, members: [0, 1], devices: [0]},
    {elements: 1, registers: 1, instances: 1, members: [2], devices: [2]}],
  registers: [
    {offset: 0, width: 4, names: ["CR1"], feature: -1, access: ["read-write"], fields: [
      {position: 0, width: 1, names: ["UE"], feature: -1},
      {position: 5, width: 1, names: ["RXNEIE", "RXNEIE_RXFNEIE"], feature: 0},
      {position: 29, width: 1, names: ["FIFOEN"], feature: 0},
    ]},
    // Alternates of a register overlap: the mode field covers the bits of two flags
    {offset: 0x1c, offsets: [0x1c, 0x20], width: 2, names: ["ISR"], feature: 1, access: ["read-only"], fields: [
      {position: 0, width: 4, names: ["MODE"], feature: -1},
      {position: 1, width: 1, names: ["TC"], feature: 1, relocated: true},
      {position: 3, width: 2, names: ["BUSY"], feature: -1},
      {position: 8, width: 8, names: ["DATA"], feature: -1},
    ]},
    {offset: 0x28, width: 4, names: ["RQR"], feature: -1, access: ["write-only", "read-write"], fields: []},
  ],
});

test("parseHash reads #mode/group/variant and the older #group/variant", () => {
  assert.deepEqual(parseHash("#source/USART/2"), {mode: "source", group: "USART", variant: 2});
  assert.deepEqual(parseHash("#USART/1"), {mode: "binary", group: "USART", variant: 1});
  assert.deepEqual(parseHash("#USART"), {mode: "binary", group: "USART", variant: 0});
  assert.deepEqual(parseHash("#binary/ADC_Common/x"), {mode: "binary", group: "ADC_Common", variant: 0});
  assert.deepEqual(parseHash("#similar/I2C%201/3"), {mode: "similar", group: "I2C 1", variant: 3});
  assert.deepEqual(parseHash(""), {mode: "binary", group: "", variant: 0});
});

test("layout packs overlapping fields into lanes, most significant bit first", () => {
  const {rows, rowCount} = layout(variant());
  assert.deepEqual(rows.map((r) => [r.register.names[0], r.bits, r.y, r.height]),
    [["CR1", 32, 0, 1], ["ISR", 16, 1, 2], ["RQR", 32, 3, 1]]);
  assert.equal(rowCount, 4);
  const isr = rows[1].items.map(({field, lane, start}) => [field.names[0], lane, start]);
  assert.deepEqual(isr, [["DATA", 0, 0], ["BUSY", 0, 11], ["TC", 0, 14], ["MODE", 1, 12]]);
  assert.deepEqual(rows[0].items.map(({field, start}) => [field.names[0], start]), [["FIFOEN", 2], ["RXNEIE", 26], ["UE", 31]]);
  assert.equal(layout({...variant(), registers: []}).rowCount, 1);
});

test("matches registers and fields by any of their names", () => {
  const rows = layout(variant()).rows;
  assert.ok(matches(/^ISR$/, rows[1]));
  assert.ok(matches(/RXFNEIE/, rows[0]));
  assert.ok(!matches(/RXFNEIE/, rows[1]));
  assert.ok(!matches(/TXE/, rows[2]));
});

test("usedBy checks the devices of a variant against the pattern", () => {
  const group = {name: "USART", mode: "binary", instances: 3, devices: ["stm32f401re", "stm32g071rb", "stm32g474re"],
    variants: [variant()]};
  assert.ok(usedBy(group, variant(), /g474/));
  assert.ok(!usedBy(group, variant(), /g071/));
});

test("elements counts core and all registers and fields", () => {
  assert.deepEqual(elements(variant()), {core: 2 + 1 + 3, total: 3 + 3 + 4 + 0});
});

test("accessTag marks only registers that are not plain read-write", () => {
  const [cr1, isr, rqr] = variant().registers;
  assert.equal(accessTag(cr1), "");
  assert.equal(accessTag({...cr1, access: undefined}), "");
  assert.equal(accessTag(isr), "ro");
  assert.equal(accessTag(rqr), "wo/rw");
  assert.equal(accessTag({...cr1, access: ["writeOnce"]}), "writeOnce");
});

test("describe registers, fields and features", () => {
  const v = variant();
  const rows = layout(v).rows;
  assert.equal(describe({kind: "register", row: rows[0]}, v), "0x000 · 32 bit · 3 fields");
  assert.equal(describe({kind: "register", row: rows[1]}, v), "0x01c/0x020 · 16 bit · 4 fields · ro");
  const field = (row, name) => ({kind: "field", row, item: row.items.find((i) => i.field.names[0] === name)});
  assert.equal(describe(field(rows[0], "UE"), v), "[0] · core");
  assert.equal(describe(field(rows[0], "FIFOEN"), v), "[29] · feature 0: FIFO");
  assert.equal(describe(field(rows[1], "TC"), v), "[1] · feature 1 · relocated");
  assert.equal(describe(field(rows[1], "DATA"), v), "[15:8] · core");
  assert.equal(describe({kind: "feature", index: 0}, v), "FIFO");
  assert.equal(describe({kind: "feature", index: 1}, v), "");
});

test("formatting helpers", () => {
  assert.equal(offset(0x1c), "0x01c");
  assert.equal(offset(0x1234), "0x1234");
  assert.equal(range(4, 4), "7:4");
  assert.equal(range(3, 1), "3");
  assert.deepEqual([0, 1, 2].map(hue), [8, 146, 283]);
  const groups = [{name: "SPI", variants: [1]}, {name: "ADC", variants: [1, 2]}, {name: "I2C", variants: [1]}];
  assert.deepEqual(sortGroups(groups).map((g) => g.name), ["ADC", "I2C", "SPI"]);
});
