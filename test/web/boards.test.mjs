// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// The board model on hand-made boards shaped like nucleo.json: a Nucleo-64 with the ARDUINO Uno headers and
// one short morpho header, and a second board that routes USART1 instead of USART2 to the same pins.

import {test} from "node:test";
import assert from "node:assert/strict";
import {analyze, connectors, hits, place, taken} from "../../web/boards/model.js";

const gpio = (signal, label = signal) => ({signal, kind: "gpio", label});
const power = (signal) => ({signal, kind: "power", label: signal});
const pins = (...list) => Object.fromEntries(list.map((pin, i) => [String(i + 1), pin]));

/** @returns {import("../../web/boards/model.js").Board} */
const nucleo = (usart = "USART2") => ({
  board: "NUCLEO-X123RE",
  connectors: {
    CN6: {type: "arduino", pins: pins(power("NC"), power("IOREF"), power("NRST"), power("3V3"), power("5V"),
      power("GND"), power("GND"), power("VIN"))},
    CN8: {type: "arduino", pins: pins(gpio("PA0", "A0"), gpio("PA1", "A1"), gpio("PA4", "A2"), gpio("PB0", "A3"),
      gpio("PC1", "A4"), gpio("PC0", "A5"))},
    CN9: {type: "arduino", pins: pins(gpio("PA3", "D0"), gpio("PA2", "D1"), gpio("PA10", "D2"), gpio("PB3", "D3"),
      gpio("PB5", "D4"), gpio("PB4", "D5"), gpio("PB10", "D6"), gpio("PA8", "D7"))},
    CN5: {type: "arduino", pins: pins(gpio("PA9", "D8"), gpio("PC7", "D9"), gpio("PB6", "D10"), gpio("PA7", "D11"),
      gpio("PA6", "D12"), gpio("PA5", "D13"), power("GND"), power("AVDD"), gpio("PB9", "D14"), gpio("PB8", "D15"))},
    CN7: {type: "morpho", side: "left", pins: pins(gpio("PC10"), gpio("PC11"), gpio("PC13"), gpio("PA13"))},
  },
  uses: {
    PA2: {use: "vcp", label: "USART_TX", signal: `${usart}_TX`},
    PA5: {use: "led", label: "LD2", signal: "GPIO_Output"},
    PA13: {use: "debug", label: "TMS", signal: "SYS_JTMS-SWDIO"},
    PC13: {use: "button", label: "B1", signal: "GPXTI13"},
  },
  bridges: [
    {ids: ["SB13", "SB14"], name: "ST-LINK-USART", gpios: ["PA2", "PA3"], pins: {}, states: []},
    {ids: ["SB21", "SB22"], name: "LED", gpios: ["PA5", "PB13"], pins: {SB21: ["PA5"], SB22: ["PB13"]}, states: []},
  ],
  signals: {
    PA2: {[`${usart}_TX`]: 7, TIM2_CH3: 1, ADC1_IN2: null},
    PA3: {[`${usart}_RX`]: 7, TIM2_CH4: 1},
    PA5: {SPI1_SCK: 5, TIM2_CH1: 1},
    PA9: {USART1_TX: 7},
  },
});

test("place puts the ARDUINO Uno, Nano and Zio headers next to the morpho rows", () => {
  const {connectors: c} = nucleo();
  assert.deepEqual(place(c.CN6, () => 0), {side: "left", row: 4});
  assert.deepEqual(place(c.CN8, () => 0), {side: "left", row: 13});
  assert.deepEqual(place(c.CN5, () => 0), {side: "right", row: 1, reverse: true, count: 10});
  assert.deepEqual(place(c.CN9, () => 0), {side: "right", row: 11, reverse: true});
  assert.deepEqual(place(c.CN7, () => 0), {side: "left", row: 0, morpho: true});
  const nano = {type: "arduino", pins: pins(...Array.from({length: 15}, (_, i) => gpio(`PB${i}`)))};
  assert.equal(place(nano, () => 0)?.side, "left");
  assert.equal(place(nano, () => 1)?.side, "right");
  const zio = (n) => ({type: "zio", pins: pins(...Array.from({length: n}, (_, i) => gpio(`PC${i}`)))});
  assert.deepEqual(place(zio(34), () => 0), {side: "right", row: 11});
  assert.equal(place(zio(12), () => 0), undefined);
  assert.equal(place({type: "stmod", pins: pins(gpio("PA0"))}, () => 0), undefined);
});

test("connectors gives comparable positions, also for the two Nano headers and paired standards", () => {
  const board = nucleo();
  const positions = Object.fromEntries(connectors(board).map(({name, position}) => [name, position]));
  assert.deepEqual(positions, {CN6: "arduino left 4", CN8: "arduino left 13", CN9: "arduino right 11",
    CN5: "arduino right 1", CN7: "morpho left 0"});
  const header = () => ({type: "arduino", pins: pins(...Array.from({length: 15}, (_, i) => gpio(`PB${i}`)))});
  board.connectors = {CN3: header(), CN4: header(), P1: {type: "pmod", pins: {}}, P2: {type: "pmod", pins: {}},
    X9: {type: "other", pins: {}}};
  assert.deepEqual(connectors(board).map(({position}) => position),
    ["arduino left 0", "arduino right 0", "pmod 1", "pmod 2", "X9"]);
});

test("taken with the free modes", () => {
  const board = nucleo();
  const {CN9, CN5, CN7} = board.connectors;
  const [pa2, pa5, pc13, pa10] = [CN9.pins[2], CN5.pins[6], CN7.pins[3], CN9.pins[3]];
  for (const pin of [pa2, pa5, pc13, pa10]) assert.ok(!taken(board, pin, ""));
  assert.ok(taken(board, pa2, "free") && taken(board, pc13, "free"));
  assert.ok(!taken(board, pa10, "free"));
  // A solder bridge may free PA2 and PA5, but nothing frees the button
  assert.ok(!taken(board, pa2, "bridge") && !taken(board, pa5, "bridge"));
  assert.ok(taken(board, pc13, "bridge"));
  assert.ok(!taken(board, CN5.pins[7], "free"));
});

test("hits matches signals, labels, board uses, bridges and device functions", () => {
  const board = nucleo();
  const pa2 = board.connectors.CN9.pins[2];
  assert.deepEqual(hits(board, pa2, /usart/i), {names: ["USART_TX", "USART2_TX"], functions: ["USART2_TX"]});
  assert.deepEqual(hits(board, pa2, /^D1$/), {names: ["D1"], functions: []});
  assert.deepEqual(hits(board, pa2, /^SB1/), {names: ["SB13", "SB14"], functions: []});
  // The bridge group tells its pins apart, so only SB21 belongs to PA5
  assert.deepEqual(hits(board, board.connectors.CN5.pins[6], /^SB2/).names, ["SB21"]);
  assert.deepEqual(hits(board, board.connectors.CN6.pins[2], /IOREF/), {names: ["IOREF"], functions: []});
});

test("analyze counts and marks matching pins, skips taken ones", () => {
  const board = nucleo();
  let result = analyze(board, /TIM2_CH/);
  assert.equal(result.count, 3);
  assert.deepEqual([...result.marks.keys()], ["CN9.1", "CN9.2", "CN5.6"]);
  assert.deepEqual(result.marks.get("CN9.2"), {names: [], functions: ["TIM2_CH3"]});
  result = analyze(board, /TIM2_CH/, {free: "free"});
  assert.deepEqual([...result.marks.keys()], ["CN9.1"]);
  result = analyze(board, /TIM2_CH/, {free: "bridge"});
  assert.equal(result.count, 3);
  assert.equal(analyze(board, /NOTHING/).count, 0);
});

test("analyze without a filter keys the whole pinout", () => {
  const key = analyze(nucleo(), null).key.split("|");
  assert.equal(key.length, 36);
  assert.ok(key.includes("arduino right 11.2=PA2"));
  assert.ok(analyze(nucleo(), null, {free: "free"}).key.includes("arduino right 11.2=used"));
  assert.equal(analyze(nucleo(), null).count, 0);
});

test("analyze compat modes: name, generic function, position", () => {
  const usart2 = nucleo("USART2");
  const usart1 = nucleo("USART1");
  const pattern = /USART\d_[TR]X/;
  assert.notEqual(analyze(usart2, pattern).key, analyze(usart1, pattern).key);
  assert.equal(analyze(usart2, pattern, {compat: "function"}).key, analyze(usart1, pattern, {compat: "function"}).key);
  assert.ok(analyze(usart2, pattern, {compat: "function"}).key.includes("arduino right 11.2=USART_TX"));
  assert.ok(analyze(usart2, pattern).key.includes("arduino right 11.2=USART2_TX"));
  // PA9 has USART1_TX on both boards, the position alone is the same anyway
  assert.equal(analyze(usart2, /PA[239]$/, {compat: "position"}).key, analyze(usart1, /PA[239]$/, {compat: "position"}).key);
  assert.ok(analyze(usart2, /PA[239]$/, {compat: "position"}).key.includes("arduino right 1.1="));
  // A pin that matches by name only keys its signal
  assert.ok(analyze(usart2, /^D1$/).key.includes("arduino right 11.2=PA2"));
});
