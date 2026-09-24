/* Drive the real app in a real browser against the real API.

   Nothing here mocks anything: every number that appears on screen came out
   of Postgres through HTTP. The point is to catch what reading the file
   cannot — a helper that is called and never defined, a handler that reads
   an input the re-render already destroyed, a template that throws on a
   null, an RTL flip that hides the tab bar.

   It also checks the two promises the cart sprint is actually about:
     * closing the app does not lose the basket
     * the price on the screen is the price on the invoice

   Usage:  node scripts/drive_ui.js
   Needs:  a server on 8099 with the catalogue loaded, and psql for the one
           step that moves a price behind the app's back.
*/
const { chromium } = require("playwright");
const { execFileSync } = require("child_process");

const BASE = "http://127.0.0.1:8099/app/";
const PHONE = "+258840000011";
const PW = "rova-demo";
const DB = process.env.UI_DB || "rova_ui";

const errors = [];
const calls = [];
let n = 0;
// One step below refuses a checkout on purpose. The browser logs every
// non-2xx as a console error, and swallowing all of them would hide a real
// 500, so it is only ignored inside that step.
let expectRefusal = false;

function step(msg) { console.log(`  ${String(++n).padStart(2)}. ${msg}`); }
function note(msg) { console.log(`      ${msg}`); }
function check(cond, msg) { if (!cond) errors.push(msg); }

function sql(q) {
  return execFileSync("psql", ["-h", "localhost", "-U", "postgres", "-d", DB, "-tAc", q],
                      { env: { ...process.env, PGPASSWORD: "postgres" } }).toString().trim();
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 420, height: 880 } });
  const page = await ctx.newPage();

  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    if (expectRefusal && /Failed to load resource/.test(m.text())) return;
    errors.push("console: " + m.text());
  });
  page.on("request", (r) => { if (r.url().includes("/v1/")) calls.push(r.method() + " " + r.url().replace(/^.*8099/, "")); });

  await page.goto(BASE, { waitUntil: "networkidle" });
  step("loaded, default language = " + (await page.getAttribute("html", "lang")));

  /* ---- language: three of them, one direction ------------------------- */
  for (const [code, label] of [["ar", "Arabic"], ["pt", "Portuguese"], ["en", "English"]]) {
    await page.click(`[data-lang="${code}"]`);
    await page.waitForTimeout(220);
    const dir = await page.getAttribute("html", "dir");
    step(`${label}: "${(await page.textContent(".sub")).trim()}"  dir=${dir}`);
    check(dir === "ltr", `${label} must keep the same layout direction as the rest`);
  }

  /* ---- sign in -------------------------------------------------------- */
  await page.fill("#ph", PHONE);
  await page.fill("#pw", PW);
  await page.click('[data-act="login"]');
  await page.waitForSelector(".hero, .grid", { timeout: 20000 });
  step("signed in");

  const heroNum = (await page.textContent(".hero .v")).trim();
  step("catalogue size on screen: " + heroNum);
  check(Number(heroNum) > 1000, "catalogue looks empty: " + heroNum);

  /* ---- the two filter rows, and that they combine --------------------- */
  const vendorChips = await page.$$eval('[data-act="setVendor"]', (e) => e.map((x) => x.textContent.trim()));
  const catChips = await page.$$eval('[data-act="setCategory"]', (e) => e.map((x) => x.textContent.trim()));
  step(`distributors: ${vendorChips.join(" | ")}`);
  step(`categories: ${catChips.slice(0, 6).join(" | ")}${catChips.length > 6 ? " …" : ""}`);
  check(vendorChips.length >= 2, "no distributor filter rendered");
  check(catChips.length >= 2, "no category filter rendered");

  // A category on its own has to reach every distributor's stock -- that is
  // the whole reason the two rows are separate questions. The *smallest*
  // category is picked on purpose: a big one fills the first page either way,
  // so the counts would match whether or not the filters actually combined.
  const smallest = catChips.length - 1;
  await page.click(`[data-act="setCategory"] >> nth=${smallest}`);
  await page.waitForTimeout(1500);
  const catOnly = await page.$$eval(".p", (e) => e.length);
  step(`smallest category alone -> ${catOnly} products`);
  check(catOnly > 0, "filtering by category alone returned nothing");

  await page.click('[data-act="setVendor"] >> nth=1');
  await page.waitForTimeout(1500);
  const both = await page.$$eval(".p", (e) => e.length);
  step(`that category + one distributor -> ${both} products`);
  check(both > 0, "the two filters together returned nothing");
  check(both <= catOnly, "narrowing by distributor somehow widened the result");

  // clear both
  await page.click('[data-act="setVendor"] >> nth=0');
  await page.waitForTimeout(900);
  await page.click('[data-act="setCategory"] >> nth=0');
  await page.waitForTimeout(1200);

  /* ---- search --------------------------------------------------------- */
  await page.fill("#q", "paracetamol");
  await page.click('[data-act="search"]');
  await page.waitForTimeout(1600);
  const hits = await page.$$eval(".p", (e) => e.length);
  step(`search "paracetamol" -> ${hits} products`);
  check(hits > 0, "search returned nothing");

  /* ---- the card: name, price, discount, dial -------------------------- */
  const card = await page.$eval(".p", (e) => ({
    name: (e.querySelector(".nm") || {}).textContent,
    price: (e.querySelector(".pr") || {}).textContent,
    off: (e.querySelector(".off") || {}).textContent || null,
    plus: !!e.querySelector('[data-act="add"]'),
  }));
  step(`first card: ${card.name} · ${card.price} · discount ${card.off || "—"}`);
  check(card.plus, "the product card has no way to add the product");

  /* ---- add from the card: the `+` becomes the dial, then reload ------- */
  await page.click('.p [data-act="add"]');
  await page.waitForTimeout(1000);
  // the same card now has to carry the `− n +` dial, in place of the `+`
  const dial = await page.$eval(".p", (e) => ({
    minus: !!e.querySelector('[data-act="dec"]'),
    plus: !!e.querySelector('[data-act="inc"]'),
    qty: (e.querySelector(".cnt .n") || {}).textContent,
  }));
  step(`card after adding: dial ${dial.minus && dial.plus ? "− " + dial.qty + " +" : "MISSING"}`);
  check(dial.minus && dial.plus, "the card did not turn into a quantity dial");
  check(dial.qty === "1", "the dial shows " + dial.qty + " after one tap");
  const barBefore = (await page.textContent(".cartbar")).trim();
  step("cart bar after one tap: " + barBefore);
  check(/\d/.test(barBefore), "adding from the card did not reach the cart");

  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(1800);
  const barAfter = await page.textContent(".cartbar").catch(() => "(gone)");
  step("cart bar after a full reload: " + barAfter.trim());
  check(barAfter.trim() === barBefore, "the cart did not survive a reload — that is the sprint");

  /* ---- the cart screen ------------------------------------------------ */
  await page.click('[data-act="goCart"]');
  await page.waitForTimeout(1200);
  const row = await page.$eval(".card .row", (e) => e.textContent.replace(/\s+/g, " ").trim());
  step("cart row: " + row.slice(0, 90));
  const btn = (await page.textContent('[data-act="checkout"]')).trim();
  step("button reads: " + btn);

  /* ---- move a price behind the app's back ----------------------------- */
  // via the DOM and the database rather than the page's own state: the app
  // keeps `S` closed over inside its script, and a test that reaches into a
  // module's privates stops testing the thing the pharmacist uses.
  const lineId = await page.getAttribute('[data-act="incLine"]', "data-line");
  const lineProduct = sql(`SELECT index_product_id FROM request_line WHERE id='${lineId}'`);
  const seen = sql(`SELECT price_seen FROM request_line WHERE id='${lineId}'`);
  const offer = sql(`SELECT id FROM vendor_offer WHERE index_product_id='${lineProduct}' ` +
                    `AND freshness_state='FRESH' ORDER BY price LIMIT 1`);
  const original = sql(`SELECT price FROM vendor_offer WHERE id='${offer}'`);
  sql(`UPDATE vendor_offer SET price = price + 25 WHERE id='${offer}'`);
  try {
    // Deliberately NO reload: this is the case the guard exists for. The
    // screen is showing the old price and the pharmacist taps "place order",
    // so the server refuses and the app has to recover on its own. An
    // earlier version of this script reloaded first, which meant the client
    // acknowledged the new number and the refusal path never ran at all.
    expectRefusal = true;
    await page.click('[data-act="checkout"]');
    await page.waitForTimeout(2500);
    expectRefusal = false;
    const refused = await page.$(".err");
    const wasShown = await page.$eval(".was", (e) => e.textContent.trim()).catch(() => null);
    const newBtn = (await page.textContent('[data-act="checkout"]')).trim();
    const nowShown = await page.$eval(".row .price", (e) => e.textContent.trim()).catch(() => null);
    step(`price moved ${original} -> ${Number(original) + 25} while the screen showed the old one:`);
    note(`refused and recovered on its own: ${!!refused}`);
    note(`row now reads "${wasShown}" then ${nowShown}`);
    note(`button now "${newBtn}" (was "${btn}")`);
    check(!!refused, "the order went through at a price that was never shown");
    check(wasShown !== null, "after the refusal the old price was not shown");
    check(newBtn !== btn, "the button did not change to ask for agreement");
    check(!/rql_/.test(await page.textContent(".err")), "the refusal showed raw line ids");
    note(`price_seen was ${seen}`);

    /* ---- and the second tap, now agreeing, goes through --------------- */
    await page.click('[data-act="checkout"]');
    await page.waitForSelector(".ok", { timeout: 25000 });
    const flash = (await page.textContent(".ok")).trim();
    step("ORDER: " + flash);
    check(/\d/.test(flash), "checkout produced no order line");
    // the agreed figure, formatted the way the app formats money
    const agreed = (Number(original) + 25).toFixed(2).replace(".", ",");
    check(flash.replace(/\s/g, "").includes(agreed.replace(/\s/g, "")),
          `the order total is not the agreed price: expected ${agreed} in "${flash}"`);
  } finally {
    sql(`UPDATE vendor_offer SET price = ${original} WHERE id='${offer}'`);
  }

  /* ---- the cart is empty again, and says so --------------------------- */
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(1600);
  const barGone = await page.$(".cartbar");
  step("cart bar after ordering: " + (barGone ? "still there" : "gone"));
  check(!barGone, "the ordered cart is still showing as a cart");

  /* ---- the order he just placed is in the orders tab ------------------
     The cart is excluded from that list by a flag that is never cleared, so
     a filter written as "not a cart" rather than "not an open cart" hid
     every order the pharmacy had ever placed from the app. Nothing else in
     this walk would have noticed. */
  await page.click('[data-tab="orders"]');
  await page.waitForTimeout(1800);
  const orderRows = await page.$$eval(".card .row", (e) =>
    e.map((x) => x.textContent.replace(/\s+/g, " ").trim()).filter((t) => /RQ-/.test(t)));
  step(`orders tab: ${orderRows.length} request(s), first "${(orderRows[0] || "").slice(0, 60)}"`);
  check(orderRows.length > 0, "the order he just placed is not in the orders tab");
  check(!orderRows.some((t) => /Draft|Rascunho/i.test(t)),
        "a draft cart is showing in the order history");

  /* ---- the WhatsApp lane still works ---------------------------------- */
  await page.click('[data-tab="list"]');
  await page.waitForTimeout(500);
  await page.fill("#paste", "Paracetamol 500\nAmoxicilina 500 mg 21");
  await page.click('[data-act="paste"]');
  await page.waitForTimeout(3000);
  const normText = await page
    .$eval("#normblock", (e) => e.textContent.replace(/\s+/g, " ").slice(0, 120))
    .catch(() => "(no normalisation block)");
  step("paste -> " + normText);

  /* ---- every tab, in Arabic -------------------------------------------
     The language chips live on the account screen once signed in, so the
     switch happens there and not wherever the walk happens to have got to. */
  await page.click('[data-tab="panel"]');
  await page.waitForTimeout(900);
  await page.click('[data-lang="ar"]');
  await page.waitForTimeout(500);
  check((await page.getAttribute("html", "lang")) === "ar",
        "switching to Arabic after signing in did not take");
  for (const tab of ["store", "list", "orders", "chat", "panel"]) {
    await page.click(`[data-tab="${tab}"]`);
    await page.waitForTimeout(900);
    const h = (await page.textContent("h1").catch(() => "—")).trim();
    const tabs = await page.$$eval(".tabbar button span", (e) => e.map((x) => x.textContent.trim()));
    step(`ar/${tab}: "${h}"  tabs=[${tabs.join(", ")}]`);
  }
  await page.screenshot({ path: "/tmp/claude-0/store-ar.png" });

  await page.click('[data-tab="panel"]');
  await page.waitForTimeout(700);
  await page.click('[data-lang="en"]');
  await page.waitForTimeout(400);
  await page.click('[data-tab="store"]');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: "/tmp/claude-0/store-en.png" });

  console.log("\n  API calls made: " + calls.length);
  console.log("  JavaScript errors: " + errors.length);
  errors.forEach((e) => console.log("    ! " + e));

  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch((e) => {
  console.error("DRIVE FAILED: " + e.message);
  process.exit(2);
});
