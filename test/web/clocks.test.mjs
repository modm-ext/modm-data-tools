// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// The clock tree model on a hand-made tree shaped like the `python -m modm_data.clocks` export:
// HSI/HSE -> PLLSource mux -> PLLM -> PLLN -> PLLR -> SysClkSource mux -> SYSCLK -> AHB prescaler -> HCLK,
// and a USART1 kernel clock mux that feeds an output that only clocks the USART1 peripheral.

import {test} from "node:test";
import assert from "node:assert/strict";
import {activePath, factorsOf, focusOf, fmtHz, fold, humanError, inUnit, isVariable, limitText, unitOf} from "../../web/clocks/model.js";

const domain = (prefix, ...factors) => factors.map((factor) => ({factor, macro: `${prefix}${factor}`}));

/** @returns {import("../../web/clocks/model.js").Tree} */
const tree = () => ({
  nodes: {
    HSIRC: {kind: "fixed", inputs: [], value: 16e6, outputs: ["PLLSource", "SysClkSource", "USART1Mult"]},
    HSEOSC: {kind: "varied", inputs: [], default: 8e6, min: 4e6, max: 48e6, outputs: ["PLLSource"],
      limits: [{max: 26e6, min: 4e6, when: ["var", "HSEOscillator"]}, {max: 48e6, min: 0}]},
    PLLSource: {kind: "mux", default: "RCC_PLLSOURCE_HSI", outputs: ["PLLM"], inputs: [
      {from: "HSIRC", macro: "RCC_PLLSOURCE_HSI"}, {from: "HSEOSC", macro: "RCC_PLLSOURCE_HSE"}]},
    PLLM: {kind: "div", default: 1, domain: domain("RCC_PLLM_DIV", 1, 2, 4), inputs: [{from: "PLLSource"}], outputs: ["PLLN"]},
    PLLN: {kind: "mul", default: 8, range: [8, 12], inputs: [{from: "PLLM"}], outputs: ["PLLR"],
      out_min: 96e6, out_max: 344e6, out_limits: [{out_min: 96e6, out_max: 344e6, when: ["var", "scale1"]},
        {out_min: 96e6, out_max: 128e6}]},
    PLLR: {kind: "div", default: 2, domain: domain("RCC_PLLR_DIV", 2, 4), inputs: [{from: "PLLN"}], outputs: ["SysClkSource"]},
    SysClkSource: {kind: "mux", default: "RCC_SYSCLKSOURCE_HSI", outputs: ["SysCLKOutput"], inputs: [
      {from: "HSIRC", macro: "RCC_SYSCLKSOURCE_HSI"}, {from: "PLLR", macro: "RCC_SYSCLKSOURCE_PLLCLK"}]},
    SysCLKOutput: {kind: "output", inputs: [{from: "SysClkSource"}], outputs: ["AHBPrescaler", "USART1Mult"], max: 64e6},
    AHBPrescaler: {kind: "div", default: 1, domain: domain("RCC_SYSCLK_DIV", 1, 2), inputs: [{from: "SysCLKOutput"}],
      outputs: ["AHBOutput"]},
    AHBOutput: {kind: "output", inputs: [{from: "AHBPrescaler"}], outputs: ["DMA1", "CRC"], max: 64e6},
    DMA1: {kind: "peripheral", inputs: [{from: "AHBOutput"}], outputs: []},
    CRC: {kind: "peripheral", inputs: [{from: "AHBOutput"}], outputs: []},
    // Inputs without a macro stand for their source node
    USART1Mult: {kind: "mux", default: "SysCLKOutput", outputs: ["USART1output"], inputs: [
      {from: "SysCLKOutput"}, {from: "HSIRC"}]},
    USART1output: {kind: "output", inputs: [{from: "USART1Mult"}], outputs: ["USART1"], min: 0, max: 64e6},
    USART1: {kind: "peripheral", inputs: [{from: "USART1output"}], outputs: []},
    // An output that feeds nothing, like MCO
    MCOPin: {kind: "output", inputs: [{from: "SysCLKOutput"}], outputs: []},
  },
});

const sorted = (set) => [...set].sort();

test("activePath follows the defaults without a result", () => {
  const {nodes, alternatives} = activePath(tree(), {}, ["USART1"]);
  assert.deepEqual(sorted(nodes), ["HSIRC", "SysCLKOutput", "SysClkSource", "USART1", "USART1Mult", "USART1output"]);
  assert.equal(alternatives.size, 0);
});

test("activePath follows the settings of the result through the PLL", () => {
  const result = {SysClkSource: {setting: "RCC_SYSCLKSOURCE_PLLCLK"}, PLLSource: {setting: "RCC_PLLSOURCE_HSE"}};
  const {nodes} = activePath(tree(), result, ["DMA1"]);
  assert.deepEqual(sorted(nodes), ["AHBOutput", "AHBPrescaler", "DMA1", "HSEOSC", "PLLM", "PLLN", "PLLR", "PLLSource",
    "SysCLKOutput", "SysClkSource"]);
});

test("activePath with alternatives adds only the other inputs, not their sources or nodes on the path", () => {
  const result = {SysClkSource: {setting: "RCC_SYSCLKSOURCE_PLLCLK"}, USART1Mult: {setting: "HSIRC"}};
  let {nodes, alternatives} = activePath(tree(), result, ["USART1"], {alternatives: true});
  assert.deepEqual(sorted(nodes), ["HSIRC", "USART1", "USART1Mult", "USART1output"]);
  assert.deepEqual(sorted(alternatives), ["SysCLKOutput"]);
  // With SYSCLK on the path too, the other USART1 input and the other SysClkSource input (HSI) are active
  ({nodes, alternatives} = activePath(tree(), result, ["USART1", "DMA1"], {alternatives: true}));
  assert.ok(nodes.has("SysCLKOutput") && nodes.has("PLLR") && nodes.has("HSIRC"));
  assert.deepEqual(sorted(alternatives), ["HSEOSC"]);
});

test("activePath ignores unknown focus ids and a mux without any setting", () => {
  const t = tree();
  delete t.nodes.USART1Mult.default;
  const {nodes} = activePath(t, {}, ["USART1", "NOPE"]);
  assert.deepEqual(sorted(nodes), ["USART1", "USART1Mult", "USART1output"]);
});

test("focusOf prefers targets and peripherals in use, then used peripherals, then everything used", () => {
  const t = tree();
  assert.deepEqual(sorted(focusOf(t, {}, ["SysCLKOutput", "NOPE"], ["USART1"])), ["SysCLKOutput", "USART1"]);
  const result = {SysCLKOutput: {used: true}, USART1: {used: true}, CRC: {used: false}};
  assert.deepEqual(sorted(focusOf(t, result, ["NOPE"])), ["USART1"]);
  assert.deepEqual(sorted(focusOf(t, {HSIRC: {used: true}, SysCLKOutput: {used: true}})), ["HSIRC", "SysCLKOutput"]);
  assert.equal(focusOf(t, {}).size, 0);
});

test("fold moves leaf outputs and peripherals into their host and kernel muxes into cards", () => {
  const {host, feeds, cards, owner} = fold(tree());
  assert.equal(host.USART1output, "USART1Mult");
  assert.equal(host.USART1, "USART1Mult");
  assert.equal(owner("USART1"), "USART1Mult");
  assert.equal(owner("PLLN"), "PLLN");
  // AHBOutput feeds only peripherals, so it folds into the prescaler, and so do its peripherals
  assert.equal(host.AHBOutput, "AHBPrescaler");
  assert.deepEqual(feeds.get("AHBPrescaler"), ["DMA1", "CRC"]);
  // An output that feeds nothing becomes a line of its own
  assert.deepEqual(feeds.get("SysCLKOutput"), ["MCOPin"]);
  // SysCLKOutput feeds a divider, so it stays a box
  assert.equal(host.SysCLKOutput, undefined);
  assert.deepEqual(sorted(cards), ["USART1Mult"]);
  assert.equal(fold(tree(), {cards: false}).cards.size, 0);
});

test("factorsOf reads domains, choice values and ranges", () => {
  const nodes = tree().nodes;
  assert.deepEqual(factorsOf(nodes.PLLM), [1, 2, 4]);
  assert.deepEqual(factorsOf(nodes.PLLN), [8, 9, 10, 11, 12]);
  assert.deepEqual(factorsOf({kind: "choice", inputs: [], domain: [{value: "A"}, {value: "B"}]}), ["A", "B"]);
  assert.deepEqual(factorsOf(nodes.HSIRC), []);
});

test("isVariable", () => {
  const nodes = tree().nodes;
  assert.ok(isVariable(nodes.PLLSource));
  assert.ok(isVariable(nodes.PLLM));
  assert.ok(isVariable(nodes.PLLN));
  assert.ok(!isVariable({kind: "mux", inputs: [{from: "HSIRC"}]}));
  assert.ok(!isVariable({kind: "div", inputs: [], domain: [{factor: 2}]}));
  // Factors set by rules, like the timer clock multiplier, are not a choice
  assert.ok(!isVariable({kind: "mul", inputs: [], range: [1, 2], rules: [{factor: 2}]}));
  assert.ok(isVariable({kind: "choice", inputs: [], domain: [{value: "A"}, {value: "B"}]}));
  assert.ok(!isVariable({kind: "choice", inputs: [], domain: [{value: "A"}]}));
  assert.ok(!isVariable(nodes.HSIRC));
  assert.ok(!isVariable(nodes.SysCLKOutput));
});

test("fmtHz", () => {
  assert.equal(fmtHz(170e6), "170 MHz");
  assert.equal(fmtHz(32768), "32.768 kHz");
  assert.equal(fmtHz(1.5e9), "1.5 GHz");
  assert.equal(fmtHz(999), "999 Hz");
  assert.equal(fmtHz(0), "0 Hz");
  assert.equal(fmtHz(-2e6), "-2 MHz");
  assert.equal(fmtHz(16e6 / 3), "5.333333 MHz");
  for (const value of [null, undefined, NaN, Infinity]) assert.equal(fmtHz(value), "—");
});

test("limitText uses the bounds of the evaluation, else the static limits of sources and outputs", () => {
  const nodes = tree().nodes;
  assert.equal(limitText(nodes.SysCLKOutput, {}), "≤ 64 MHz");
  // The limits of a source depend on the configuration
  assert.equal(limitText(nodes.HSEOSC, {}), "4 MHz…48 MHz (depends on config)");
  assert.equal(limitText(nodes.HSEOSC, {bounds: {"": [4e6, 26e6]}}), "4 MHz…26 MHz (depends on config)");
  // A divider or multiplier has no static input limits, only output ones
  assert.equal(limitText(nodes.PLLN, {}), "96 MHz…344 MHz (depends on config)");
  assert.equal(limitText(nodes.PLLN, {bounds: {"": [2.66e6, null], out_: [96e6, 128e6]}}),
    "≥ 2.66 MHz · 96 MHz…128 MHz (depends on config)");
  assert.equal(limitText(nodes.PLLM, {}), "");
  // A minimum of 0 is no limit
  assert.equal(limitText(nodes.USART1output, {}), "≤ 64 MHz");
});

test("humanError writes frequencies with a unit and leaves small numbers alone", () => {
  assert.equal(humanError("PLLN output 400000000.0 > 344000000.0"), "PLLN output 400 MHz > 344 MHz");
  assert.equal(humanError("SYSCLK 1.7e+08 out of range"), "SYSCLK 170 MHz out of range");
  assert.equal(humanError("PLLN factor 87 not in 8..86"), "PLLN factor 87 not in 8..86");
  assert.equal(humanError("LSE 32768"), "LSE 32.768 kHz");
});

test("input fields of slow clocks in kHz", () => {
  assert.deepEqual(unitOf(32000), ["kHz", 1e3]);
  assert.deepEqual(unitOf(8e6), ["MHz", 1e6]);
  assert.deepEqual(unitOf(undefined), ["MHz", 1e6]);
  assert.equal(inUnit(32768, 1e3), "32.768");
  assert.equal(inUnit(170e6, 1e6), "170");
  assert.equal(inUnit(undefined, 1e6), "");
});
