/* Drive the real app in a real browser against the real API.
   Nothing here mocks anything: every number that appears on screen came out
   of Postgres through HTTP. The point is to catch what reading the file
   cannot — a handler that reads an input the re-render already destroyed,
   a template that throws on a null, an RTL flip that hides the tab bar. */
const { chromium } = require("playwright");

const BASE = "http://127.0.0.1:8099/app/";
const PHONE = "+258840000011";
const PW = "rova-demo";

const errors = [];
const calls = [];

function step(n, msg){ console.log(`  ${String(n).padStart(2)}. ${msg}`); }

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 420, height: 880 } });

  page.on("pageerror", e => errors.push("pageerror: " + e.message));
  page.on("console", m => { if (m.type() === "error") errors.push("console: " + m.text()); });
  page.on("request", r => { if (r.url().includes("/v1/")) calls.push(r.method() + " " + r.url().replace(/^.*8099/, "")); });

  let n = 0;
  await page.goto(BASE, { waitUntil: "networkidle" });
  step(++n, "loaded, default language = " + await page.getAttribute("html", "lang"));

  // --- language round trip, before anything else ---
  await page.click('[data-lang="ar"]');
  await page.waitForTimeout(250);
  const dir = await page.getAttribute("html", "dir");
  step(++n, "switched to Arabic, layout dir stays " + dir);
  if (dir !== "ltr") errors.push("Arabic must keep the same layout direction as the rest");
  const arabicTitle = (await page.textContent("h1")).trim();
  step(++n, "title in Arabic: " + arabicTitle);

  await page.click('[data-lang="pt"]');
  await page.waitForTimeout(200);
  step(++n, "Portuguese: " + (await page.textContent(".sub")).trim());
  await page.click('[data-lang="en"]');
  await page.waitForTimeout(200);
  step(++n, "back to English: " + (await page.textContent(".sub")).trim());

  // --- login ---
  await page.fill("#ph", PHONE);
  await page.fill("#pw", PW);
  await page.click('[data-act="login"]');
  await page.waitForSelector(".shelves, .hero", { timeout: 15000 });
  step(++n, "signed in");

  // --- the store home ---
  const heroNum = (await page.textContent(".hero .v")).trim();
  const shelves = await page.$$eval(".shelf .n", els => els.slice(0, 6).map(e => e.textContent.trim()));
  step(++n, "catalogue size on screen: " + heroNum);
  step(++n, "shelves: " + shelves.join(" | "));
  if (Number(heroNum) < 1000) errors.push("catalogue looks empty: " + heroNum);

  // --- browse a shelf ---
  await page.click(".shelf");
  await page.waitForTimeout(1200);
  const browsed = await page.$$eval(".card.tap .price", els => els.slice(0, 4).map(e => e.textContent.trim()));
  step(++n, "shelf prices: " + (browsed.join(" | ") || "(none)"));
  if (!browsed.length) errors.push("browsing a shelf produced no priced products");

  // --- search ---
  await page.click('[data-act="clearResults"]');
  await page.waitForTimeout(400);
  await page.fill("#q", "paracetamol");
  await page.click('[data-act="search"]');
  await page.waitForTimeout(1500);
  const hits = await page.$$eval(".card.tap", els => els.length);
  step(++n, "search 'paracetamol' -> " + hits + " products");
  if (!hits) errors.push("search returned nothing");

  // --- open a product, read the vendor lines ---
  await page.click(".card.tap");
  await page.waitForTimeout(1200);
  const productName = (await page.textContent("h1")).trim();
  const vendors = await page.$$eval(".vendorline", els =>
    els.map(e => e.textContent.replace(/\s+/g, " ").trim().slice(0, 70)));
  step(++n, "product: " + productName);
  vendors.forEach(v => step("  ", "vendor line: " + v));
  if (!vendors.length) errors.push("the product page showed no vendor");

  // --- add to cart and order ---
  await page.click('[data-act="add"]');
  await page.waitForTimeout(500);
  await page.click('[data-act="goCart"]');
  await page.waitForTimeout(600);
  step(++n, "cart: " + (await page.textContent("h1")).trim());
  await page.click('[data-act="checkout"]');
  await page.waitForSelector(".ok", { timeout: 20000 });
  const flash = (await page.textContent(".ok")).trim();
  step(++n, "ORDER: " + flash);
  if (!/\d/.test(flash)) errors.push("checkout produced no order line");

  // --- the WhatsApp lane still works ---
  await page.click('[data-tab="list"]');
  await page.waitForTimeout(400);
  await page.fill("#paste", "Paracetamol 500\nAmoxicilina 500 mg 21");
  await page.click('[data-act="paste"]');
  await page.waitForTimeout(2500);
  const normText = await page.$eval("#normblock", e => e.textContent.replace(/\s+/g, " ").slice(0, 140))
    .catch(() => "(no normalisation block)");
  step(++n, "paste -> " + normText);

  // --- Arabic, logged in, every tab ---
  await page.click('[data-tab="panel"]');
  await page.waitForTimeout(800);
  await page.click('[data-lang="ar"]');
  await page.waitForTimeout(300);
  for (const tab of ["store", "list", "orders", "chat", "panel"]) {
    await page.click(`[data-tab="${tab}"]`);
    await page.waitForTimeout(900);
    const h = (await page.textContent("h1").catch(() => "—")).trim();
    const tabs = await page.$$eval(".tabbar button span", els => els.map(e => e.textContent.trim()));
    step(++n, `ar/${tab}: "${h}"  tabs=[${tabs.join(", ")}]`);
  }
  await page.screenshot({ path: "/home/claude/store-ar.png", fullPage: false });

  await page.click('[data-lang="en"]').catch(() => {});
  await page.click('[data-tab="store"]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: "/home/claude/store-en.png", fullPage: false });

  console.log("\n  API calls made: " + calls.length);
  console.log("  JavaScript errors: " + errors.length);
  errors.forEach(e => console.log("    ! " + e));

  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error("DRIVE FAILED: " + e.message); process.exit(2); });
