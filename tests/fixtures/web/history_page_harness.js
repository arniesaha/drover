// Boots history.html's real script against a minimal stub DOM under node.
//
// Usage: node history_page_harness.js <page.html> <scenario.json>
// The scenario lists canned HTTP responses (matched by URL prefix, consumed
// in order) and a sequence of steps. The harness prints a JSON trace of every
// fetch, history call and the DOM state observed after each step. There is no
// jsdom in this repo; the stub implements only what the page touches.
"use strict";
const fs = require("fs");
const vm = require("vm");

const [pagePath, scenarioPath] = process.argv.slice(2);
const page = fs.readFileSync(pagePath, "utf8");
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

class ClassList {
  constructor(initial) { this.set = new Set((initial || "").split(/\s+/).filter(Boolean)); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : Boolean(force);
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
}

function element(id, classes) {
  const listeners = {};
  let html = "";
  const el = {
    id,
    // One backing store, as in a real DOM: textContent is the markup's text.
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v); },
    get textContent() { return html.replace(/<[^>]*>/g, ""); },
    set textContent(v) { html = String(v).replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c])); },
    value: "",
    style: {},
    dataset: {},
    attributes: {},
    children: [],
    classList: new ClassList(classes),
    scrollTop: 0,
    scrollHeight: 0,
    get className() { return [...this.classList.set].join(" "); },
    set className(v) { this.classList = new ClassList(v); },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch(type, event) { for (const fn of listeners[type] || []) fn(event); },
    focus() {},
    remove() { this.removed = true; },
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll() { return this.children.filter((c) => !c.removed); },
    insertAdjacentHTML(_, html) { this.innerHTML = html + this.innerHTML; this.scrollHeight += 100; },
    getBoundingClientRect() { return {top: 120 - win.scrollY}; },
  };
  return el;
}

const ids = {
  q: [], clear: ["hidden"], "chips-state": [], "chips-host": [], "chips-harness": [],
  "chips-repo": [], "repo-picker": [], "chips-date": [], since: [], until: [], list: [],
  sentinel: [], notice: [], drawer: ["drawer", "hidden"], "drawer-title": [],
  "drawer-meta": [], "drawer-console": [], "drawer-close": [], "drawer-body": [],
  older: ["hidden"], messages: [], "drawer-status": [], retry: [],
};
const els = Object.fromEntries(Object.entries(ids).map(([id, c]) => [id, element(id, c.join(" "))]));
const dateChips = [1, 7, 30].map((d) => Object.assign(element(`days-${d}`), {dataset: {days: String(d)}}));

const trace = {fetches: [], history: [], steps: []};
let clock = Date.parse(scenario.now || "2026-10-02T12:00:00Z");
const timers = [];
let timerSeq = 0;
const responses = scenario.responses.map((r) => ({...r}));

function respond(url) {
  const index = responses.findIndex((r) => url.startsWith(r.url));
  if (index < 0) return {status: 404, body: {error: "no canned response"}, headers: {}};
  return responses[index].repeat ? responses[index] : responses.splice(index, 1)[0];
}

const win = {
  scrollY: 0,
  innerHeight: 800,
  scrollTo(_, y) { win.scrollY = y; },
  addEventListener(type, fn) { (win.listeners[type] = win.listeners[type] || []).push(fn); },
  listeners: {},
};
const location = {
  search: scenario.search || "",
  pathname: "/ui/history",
  set href(v) { trace.navigated = v; },
};
function setUrl(url) {
  const q = url.indexOf("?");
  location.search = q >= 0 ? url.slice(q) : "";
}
const storage = new Map(Object.entries(scenario.sessionStorage || {}));
const context = {
  console,
  URLSearchParams,
  JSON,
  Math,
  Date: class extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return clock; }
  },
  Number, String, Boolean, Array, Object, Set, Map, Promise, RegExp,
  encodeURIComponent, decodeURIComponent,
  window: win,
  location,
  document: {
    getElementById: (id) => els[id],
    querySelectorAll: (sel) => (sel.includes("data-days") ? dateChips : []),
    createElement: () => element("created"),
    addEventListener: (type, fn) => { (win.listeners["doc:" + type] = win.listeners["doc:" + type] || []).push(fn); },
    body: {classList: new ClassList()},
  },
  history: {
    pushState: (s, _, url) => { trace.history.push(["push", url]); setUrl(url); },
    replaceState: (s, _, url) => { trace.history.push(["replace", url]); setUrl(url); },
    back: () => { trace.history.push(["back"]); },
  },
  sessionStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, v),
    removeItem: (k) => storage.delete(k),
  },
  performance: {getEntriesByType: () => [{type: scenario.navigation || "navigate"}]},
  IntersectionObserver: class { constructor(fn) { win.intersect = fn; } observe() {} },
  requestAnimationFrame: (fn) => { fn(); return 1; },
  setTimeout: (fn, ms) => { const id = ++timerSeq; timers.push({id, at: clock + (ms || 0), fn}); return id; },
  clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
  fetch: async (url) => {
    trace.fetches.push(url);
    const r = respond(url);
    const headers = r.headers || {};
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      redirected: false,
      headers: {get: (k) => headers[k] ?? null},
      json: async () => r.body,
    };
  },
};
context.globalThis = context;
vm.createContext(context);

async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
}

async function advance(ms) {
  const until = clock + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const next = timers[0];
    if (!next || next.at > until) break;
    timers.shift();
    clock = next.at;
    next.fn();
    await settle();
  }
  clock = until;
}

function observe(label) {
  const list = els.list.innerHTML;
  trace.steps.push({
    label,
    rows: (list.match(/class="row"/g) || []).length,
    days: [...list.matchAll(/class="day"[^>]*>([^<]*)</g)].map((m) => m[1]),
    ids: [...list.matchAll(/data-id="([^"]*)"/g)].map((m) => m[1]),
    listHeight: els.list.style.height,
    notice: els.notice.textContent.trim(),
    noticeHidden: els.notice.classList.contains("hidden"),
    clearHidden: els.clear.classList.contains("hidden"),
    stateChips: els["chips-state"].children.filter((c) => !c.removed).length,
    hostChips: els["chips-host"].children.filter((c) => !c.removed).map((c) => c.innerHTML),
    drawerOpen: !els.drawer.classList.contains("hidden"),
    drawerTitle: els["drawer-title"].textContent,
    messages: (els.messages.innerHTML.match(/class="msg/g) || []).length,
    olderHidden: els.older.classList.contains("hidden"),
    drawerStatus: els["drawer-status"].textContent,
    timers: timers.map((t) => t.at - clock),
    snapshot: storage.get("drover.history.v1") || null,
  });
}

(async () => {
  for (const script of scripts) vm.runInContext(script, context);
  await settle();
  observe("boot");
  for (const step of scenario.steps || []) {
    if (step.scroll !== undefined) {
      win.scrollY = step.scroll;
      for (const fn of win.listeners.scroll || []) fn();
    }
    if (step.intersect) win.intersect([{isIntersecting: true}]);
    if (step.type !== undefined) {
      els.q.value = step.type;
      els.q.dispatch("input", {target: els.q});
    }
    if (step.clickRow !== undefined) {
      const target = {closest: () => ({dataset: {id: step.clickRow}})};
      els.list.dispatch("click", {target, button: 0, preventDefault() {}});
    }
    if (step.clickChip) {
      const [group, index] = step.clickChip;
      els[group].children.filter((c) => !c.removed)[index].onclick();
    }
    if (step.click) els[step.click].onclick();
    if (step.pagehide) for (const fn of win.listeners.pagehide || []) fn();
    if (step.advance) await advance(step.advance);
    await settle();
    observe(step.label || "step");
  }
  process.stdout.write(JSON.stringify(trace));
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
