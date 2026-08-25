/* Regression tests for renderLive(), the live progress panel.
 *
 *   node tests/renderlive.js
 *
 * The function is extracted from the shipped app.js and run against a stub
 * card: no browser, no cache, nothing to install. Born from two defects that
 * shipped unnoticed because nothing exercised this function — a declaration
 * order that threw on every transfer render, and a progress numerator that
 * ignored the files missing on the destination.
 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"), "utf8");

// extract function renderLive(...) { ... } with brace balancing
const start = src.indexOf("function renderLive(");
if (start < 0) throw new Error("renderLive not found in app.js");
let depth = 0, i = src.indexOf("{", start), end = -1;
for (; i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
}
const fnSrc = src.slice(start, end);

// helpers renderLive depends on
const count = (n) => (n ?? 0).toLocaleString("en-US");
const bytes = (n) => { if (!n) return "0 B"; const u = ["B","KB","MB","GB","TB"];
  let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : n < 10 ? 2 : 1)} ${u[i]}`; };
const duration = (s) => s == null ? "—" : s < 60 ? `${Math.round(s)} s`
  : `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, "0")}`;
const eta = (s) => (s == null || s === 0) ? "—" : duration(s);
const ticking = {};

const mkNode = () => ({
  textContent: "", innerHTML: "", hidden: false, style: {}, dataset: {},
  classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
  querySelector: () => null, querySelectorAll: () => [],
  appendChild(child) { (this._kids ||= []).push(child); },
});
global.document = { createElement: mkNode, getElementById: mkNode };

const renderLive = eval(`(${fnSrc})`);

function stubCard() {
  const store = {};
  return {
    querySelector(sel) { return (store[sel] ||= mkNode()); },
    querySelectorAll() { return []; },
    _get(sel) { return store[sel]; },
  };
}

const L = (o) => ({ checks: 0, total_checks: 0, local_total: 0, local_done: true,
  renames: 0, seen_copies: 0, seen_deletes: 0, listed: 0, bytes: 0, total_bytes: 0,
  speed: 0, elapsed: 100, checking: [], transferring: [], deletes: 0, ...o });

// A backup with a real gap: 204,257 local files, 179,095 of them present at
// the destination. `checks` stops there for good and the remaining 25,162
// are hashed for track-renames — the progress must keep moving anyway.
const gap = (copies) => L({ checks: 179095, total_checks: 179095,
  local_total: 204257, seen_copies: copies, listed: 390146 });

const scenarios = [
  ["analysis, gap barely started",  "analysis", gap(404),   null, "88%"],
  ["analysis, gap half listed",     "analysis", gap(9361),  null, "92%"],
  ["analysis, gap mostly listed",   "analysis", gap(19633), null, "97%"],
  ["analysis, gap fully listed",    "analysis", gap(25162), null, "99%"],
  ["analysis, taking inventory",    "analysis", L({listed:1200,local_total:50000,local_done:false}), null, null],
  ["analysis with moves",           "analysis", L({checks:1000,total_checks:1000,local_total:2000,renames:500,seen_copies:400}), null, "76%"],
  ["transfer by bytes",             "transfer", L({bytes:5e9,total_bytes:1e10,speed:2e7,eta:250,checks:100,local_total:200}), null, "50%"],
  ["transfer, verification pass",   "transfer", L({checks:120000,total_checks:120000,local_total:204257}), {moved:0,deletes:0,transfers:5}, "59%"],
  ["transfer, moves only",          "transfer", L({renames:300,checks:500,local_total:1000}), {moved:1000,deletes:0,transfers:0}, "30%"],
  ["transfer, deletions only",      "transfer", L({deletes:3,checks:500,local_total:1000,renames:1}), {moved:0,deletes:10,transfers:0}, "30%"],
  ["idle, panel hidden",            "idle",     L({}), null, null],
];

let failures = 0;
let n = 0;
for (const [label, phase, live, plan, expected] of scenarios) {
  const card = stubCard();
  const id = `p${++n}`;  // distinct id: the anti-backwards memo is per profile
  try {
    renderLive(card, id, phase, live, live.elapsed, plan);
    const what = card._get("[data-live-what]")?.textContent ?? "";
    const bar = card._get("[data-live-fill]")?.style?.width;
    if (expected !== null && bar !== expected) {
      failures++;
      console.log(`FAIL  ${label}\n        bar ${bar}, expected ${expected}`);
      continue;
    }
    const line = (card._get("[data-live-figures]")?._kids || [])
      .map((k) => k.innerHTML || k.textContent).join(" · ").replace(/<[^>]+>/g, "");
    console.log(`ok    ${label}  [${what}]${bar ? ` ${bar}` : ""}`);
    if (line) console.log(`        ${line}`);
  } catch (e) {
    failures++;
    console.log(`FAIL  ${label}\n        ${e.name}: ${e.message}`);
  }
}
console.log(failures === 0
  ? `\n${scenarios.length} scenarios passed`
  : `\n${failures} failure(s)`);
process.exit(failures === 0 ? 0 : 1);
