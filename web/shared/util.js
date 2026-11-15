// Copyright 2026, Niklas Hauser
// SPDX-License-Identifier: MPL-2.0
// @ts-check

/** The element with this id. */
export const $ = (/** @type {string} */ id) => /** @type {HTMLElement} */ (document.getElementById(id));

/**
 * A new element with class names and text.
 * @param {string} tag
 * @param {string} [cls]
 * @param {string} [text]
 */
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * The case insensitive regular expression of a search field: null for an empty pattern,
 * undefined for an invalid one (mark the field with the class "bad").
 * @param {string} pattern
 * @returns {RegExp | null | undefined}
 */
export function regex(pattern) {
  if (!pattern) return null;
  try {
    return new RegExp(pattern, "i");
  } catch {
    return undefined;
  }
}

/**
 * Calls fn at most once per pause of ms milliseconds, e.g. while typing into a search field.
 * @template {(...args: any[]) => void} F
 * @param {F} fn
 * @param {number} [ms]
 */
export function debounce(fn, ms = 150) {
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer;
  return (/** @type {Parameters<F>} */ ...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/**
 * Fetches JSON, with a readable error for a missing file or a page opened without a server.
 * @param {string | URL} url
 */
export async function loadJSON(url) {
  let response;
  try {
    response = await fetch(url);
  } catch (error) {
    throw new Error(`Could not load ${url}: serve the page over HTTP, the data sits next to it`, {cause: error});
  }
  if (!response.ok) throw new Error(`Could not load ${url}: ${response.status} ${response.statusText}`);
  return response.json();
}

/**
 * Makes the buttons of a .modes group exclusive: apply(button.dataset[attribute]) on a click.
 * @param {HTMLElement} container
 * @param {string} attribute
 * @param {(value: string) => void} apply
 */
export function modes(container, attribute, apply) {
  container.addEventListener("click", (event) => {
    const button = /** @type {HTMLElement} */ (event.target).closest("button");
    if (!button) return;
    select(container, attribute, button.dataset[attribute] ?? "");
    apply(button.dataset[attribute] ?? "");
  });
}

/**
 * Presses the button of a .modes group with this value.
 * @param {HTMLElement} container
 * @param {string} attribute
 * @param {string} value
 */
export function select(container, attribute, value) {
  for (const button of container.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String((button.dataset[attribute] ?? "") === value));
  }
}

/** Numbers with thin thousands separators. */
export const num = (/** @type {number} */ value) => value.toLocaleString("en-US");

/** A number in hexadecimal, e.g. hex(1024, 4) is 0x0400. */
export const hex = (/** @type {number} */ value, digits = 0) => "0x" + value.toString(16).toUpperCase().padStart(digits, "0");
