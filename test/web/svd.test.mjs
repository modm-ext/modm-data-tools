// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// The SVD helpers and the filter on a hand-built device. Node has no DOMParser, so parseSvd() itself is
// only tested in the browser, the test checks that it fails on that early.

import {test} from "node:test";
import assert from "node:assert/strict";
import {bitRange, bitsValue, countOf, fieldValue, filterDevice, offsetText, parseNumber, parseSvd, sizeText}
  from "../../web/svd/parse.js";

/** A device like parseSvd() returns: USART1 with CR1 (UE, M[1:0]) and a cluster with one register. */
function device() {
  const nodes = [];
  const make = (type, name, parent, props = {}) => {
    const node = {id: nodes.length, type, name, path: parent ? `${parent.path}.${name}` : name, desc: "",
      children: [], parent, ...props};
    nodes.push(node);
    if (parent) parent.children.push(node);
    return node;
  };
  const usart = make("peripheral", "USART1", null, {address: 0x40013800, desc: "Universal synchronous receiver"});
  const cr1 = make("register", "CR1", usart, {offset: 0, size: 32, desc: "Control register 1"});
  make("field", "UE", cr1, {lsb: 0, msb: 0, width: 1, desc: "USART enable"});
  make("field", "M", cr1, {lsb: 12, msb: 13, width: 2, desc: "Word length"});
  const fifo = make("cluster", "FIFO", usart, {offset: 0x40});
  make("register", "DR", fifo, {offset: 0x40, size: 32, desc: "Data"});
  const gpio = make("peripheral", "GPIOA", null, {address: 0x48000000});
  make("register", "MODER", gpio, {offset: 0, size: 32});
  const stats = {peripheral: 2, cluster: 1, register: 3, field: 2, enum: 0};
  return {name: "TEST", peripherals: [usart, gpio], nodes, stats};
}

const names = (dev, ids) => [...ids].map((id) => dev.nodes[id].path).sort();

test("parseSvd needs a DOMParser, which node lacks", {skip: typeof DOMParser !== "undefined"}, () => {
  assert.throws(() => parseSvd("<device/>"), ReferenceError);
});

test("parseNumber reads SVD numbers", () => {
  assert.equal(parseNumber("0x40013800"), 0x40013800);
  assert.equal(parseNumber(" 0XFF "), 255);
  assert.equal(parseNumber("0b101"), 5);
  assert.equal(parseNumber("#1x1"), 5);
  assert.equal(parseNumber("#12"), null);
  assert.equal(parseNumber("32"), 32);
  assert.equal(parseNumber("4k"), 4096);
  assert.equal(parseNumber("2M"), 2097152);
  assert.equal(parseNumber("1g"), 1073741824);
  assert.equal(parseNumber("-1"), null);
  assert.equal(parseNumber("abc"), null);
  assert.equal(parseNumber(null), null);
  assert.equal(parseNumber(undefined), null);
});

test("fieldValue takes the bits of a field out of a register value", () => {
  assert.equal(fieldValue(0x3000, 12, 2), 3);
  assert.equal(fieldValue(0xffffffff, 31, 1), 1);
  assert.equal(fieldValue(0xffffffff, 0, 32), 0xffffffff);
  assert.equal(fieldValue(0n, 0, 4), 0);
  assert.equal(fieldValue(null, 0, 1), null);
});

test("number formats", () => {
  assert.equal(offsetText(0x1c), "+0x1C");
  assert.equal(offsetText(0x400), "+0x400");
  assert.equal(bitsValue(1, 1), "1");
  assert.equal(bitsValue(5, 4), "0b0101");
  assert.equal(bitsValue(0x1f, 5), "0x1F");
  assert.equal(bitRange({lsb: 3, msb: 3}), "[3]");
  assert.equal(bitRange({lsb: 4, msb: 7}), "[7:4]");
  assert.equal(sizeText(1), "1 byte");
  assert.equal(sizeText(1024), "1 KiB");
  assert.equal(sizeText(1536), "1.5 KiB");
  assert.equal(sizeText(2097152), "2 MiB");
});

test("countOf counts descendants of a type", () => {
  const dev = device();
  assert.equal(countOf(dev.peripherals[0], "register"), 2);
  assert.equal(countOf(dev.peripherals[0], "field"), 2);
  assert.equal(countOf(dev.peripherals[1], "field"), 0);
});

test("filterDevice matches names and keeps the ancestors", () => {
  const dev = device();
  const f = filterDevice(dev, "^ue$");
  assert.deepEqual(names(dev, f.matched), ["USART1.CR1.UE"]);
  assert.deepEqual(names(dev, f.ancestors), ["USART1", "USART1.CR1"]);
  assert.deepEqual(names(dev, f.keep), ["USART1", "USART1.CR1", "USART1.CR1.UE"]);
  assert.equal(filterDevice(dev, "^ue$", {cased: true}).matched.size, 0);
});

test("filterDevice matches dotted paths only when the match ends in the last segment", () => {
  const dev = device();
  assert.deepEqual(names(dev, filterDevice(dev, "USART1.CR1.UE").matched), ["USART1.CR1.UE"]);
  assert.deepEqual(names(dev, filterDevice(dev, "CR1").matched), ["USART1.CR1"]);
  assert.deepEqual(names(dev, filterDevice(dev, "1\\.CR").matched), ["USART1.CR1"]);
  assert.deepEqual(names(dev, filterDevice(dev, "FIFO\\.D").matched), ["USART1.FIFO.DR"]);
});

test("filterDevice searches descriptions only when asked", () => {
  const dev = device();
  assert.equal(filterDevice(dev, "word length").matched.size, 0);
  assert.deepEqual(names(dev, filterDevice(dev, "word length", {desc: true}).matched), ["USART1.CR1.M"]);
  assert.throws(() => filterDevice(dev, "("), SyntaxError);
});
