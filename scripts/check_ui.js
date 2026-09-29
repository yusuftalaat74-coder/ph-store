#!/usr/bin/env node
/* Static checks over apps/ui/index.html.
 *
 * Why this exists: `priceLine` was called from the cart view and defined
 * nowhere. The page parsed, the API tests were green, and the cart screen
 * threw a ReferenceError the moment it rendered a line. Nothing in the suite
 * could see it, because nothing in the suite runs the page.
 *
 * These are the three mistakes that survive a passing backend:
 *   1. a helper that is called but never defined
 *   2. a `data-act` with no handler in ACT
 *   3. a `t("key")` missing from one of the three languages
 *
 * Exits non-zero with the list. No browser, no network, ~50ms.
 */
const fs = require("fs");
const path = require("path");

const FILE = path.join(__dirname, "..", "apps", "ui", "index.html");
const html = fs.readFileSync(FILE, "utf8");
const script = (html.match(/<script>([\s\S]*)<\/script>/) || [])[1];
if (!script) fail(["no <script> block found"]);

const problems = [];

/* ---- 1. called but never defined ---------------------------------------
   Deliberately coarse: collect every `foo(` that is not preceded by a dot,
   drop anything declared in the file or built into the language. A false
   positive here costs one line in KNOWN; a false negative costs a broken
   screen. */
/* Comments and string literals have to go first: the page is built by
   concatenating HTML strings, so `'<button data-act="add">'` and a Portuguese
   comment about `linha()` both look exactly like calls to this scan. */
const code = script
  .replace(/\/\*[\s\S]*?\*\//g, " ")
  .replace(/(^|[^:"'\\])\/\/[^\n]*/g, "$1 ")
  .replace(/`(?:[^`\\]|\\.)*`/g, '""')
  .replace(/'(?:[^'\\\n]|\\.)*'/g, '""')
  .replace(/"(?:[^"\\\n]|\\.)*"/g, '""');

const defined = new Set();
for (const m of code.matchAll(/function\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
for (const m of code.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g)) defined.add(m[1]);
// object shorthand methods, which is how every ACT handler is written
for (const m of code.matchAll(/^\s+(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/gm)) defined.add(m[1]);
// Parameters, including arrow-function ones. A parameter that holds a
// callback is called like any other function, and without this the checker
// reported every one of them as undefined -- the kind of noise that gets a
// checker switched off.
//
// Be honest about the cost: this scan has no notion of scope, so every
// parameter name in the file joins one global whitelist. Short ones -- `p`,
// `e`, `l`, `x` -- are now names this checker will never report, anywhere.
// It still catches what it was written for (a helper called and never
// defined, which is how `priceLine` and `filterBar` shipped broken), and
// real scope analysis means a parser, which is a different tool.
for (const m of code.matchAll(/(?:function\s*[A-Za-z_$\w]*|^\s+(?:async\s+)?[A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{/gm)) {
  for (const part of m[1].split(",")) {
    const name = part.trim().split(/[\s=]/)[0].replace(/^\.\.\./, "");
    if (/^[A-Za-z_$][\w$]*$/.test(name)) defined.add(name);
  }
}
for (const m of code.matchAll(/\(([^()]*)\)\s*=>/g)) {
  for (const part of m[1].split(",")) {
    const name = part.trim().split(/[\s=]/)[0].replace(/^\.\.\./, "");
    if (/^[A-Za-z_$][\w$]*$/.test(name)) defined.add(name);
  }
}

const KNOWN = new Set([
  "if", "for", "while", "switch", "catch", "return", "typeof", "function",
  "await", "new", "throw", "else", "do", "try", "String", "Number", "Boolean",
  "Object", "Array", "JSON", "Math", "Date", "Promise", "Error", "Set", "Map",
  "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "decodeURIComponent",
  "setTimeout", "setInterval", "clearTimeout", "requestAnimationFrame",
  "fetch", "alert", "confirm", "RegExp", "Intl", "console", "URLSearchParams",
  "URL", "localStorage", "document", "window", "Element", "Event", "FormData",
]);

// `[^.\\w$\\\\]` also skips regex escapes like `\\B(` inside a literal
for (const m of code.matchAll(/(^|[^.\w$\\])([A-Za-z_$][\w$]*)\s*\(/g)) {
  const fn = m[2];
  if (!defined.has(fn) && !KNOWN.has(fn)) {
    problems.push(`called but never defined: ${fn}()`);
  }
}

/* ---- 2. a data-act with no handler ------------------------------------- */
const actBody = code.match(/const ACT\s*=\s*\{([\s\S]*?)\n\};/);
if (!actBody) {
  problems.push("could not find the ACT handler table");
} else {
  const handlers = new Set(
    [...actBody[1].matchAll(/^\s{2}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(/gm)].map((m) => m[1]),
  );
  // Only literal names. A handful of chips build the name at runtime
  // (`data-act="' + act + '"`); those are not checked here, which is the one
  // gap in this pass and the reason the handler names stay short and few.
  for (const m of html.matchAll(/data-act="([^"]+)"/g)) {
    if (!/^[A-Za-z_$][\w$]*$/.test(m[1])) continue;
    if (!handlers.has(m[1])) problems.push(`data-act="${m[1]}" has no handler in ACT`);
  }
}

/* ---- 3. a translation key missing from a language ----------------------
   A missing key renders the raw key to a pharmacist, which is worse than an
   English fallback, and it is invisible until someone switches language. */
const langs = {};
for (const m of script.matchAll(/^\s{2}(en|pt|ar):\s*\{([\s\S]*?)\n\s{2}\},/gm)) {
  langs[m[1]] = new Set([...m[2].matchAll(/([a-z_0-9]+)\s*:/g)].map((k) => k[1]));
}
const names = Object.keys(langs);
if (names.length !== 3) {
  problems.push(`expected 3 language tables, found ${names.length}: ${names.join(", ")}`);
} else {
  const used = new Set([...script.matchAll(/\bt\("([a-z_0-9]+)"\)/g)].map((m) => m[1]));
  for (const key of used) {
    const missing = names.filter((l) => !langs[l].has(key));
    if (missing.length) problems.push(`t("${key}") missing from: ${missing.join(", ")}`);
  }
}

function fail(list) {
  console.error("check_ui: " + list.length + " problem(s)");
  for (const p of list) console.error("  - " + p);
  process.exit(1);
}

const unique = [...new Set(problems)];
if (unique.length) fail(unique);
console.log("check_ui: ok — helpers, data-act handlers and all three languages line up");
