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

/* The real job, not a hand-written INSERT. A notice this walk conjured into
   the table would prove the screen renders and nothing about whether the
   thing that is supposed to produce it does. */
/* Abandon any open basket the way the app does, so SM-01 runs and slice B's
   hook closes the notices with it. Anything the hook misses shows up as a
   stale badge in the steps below, which is the point. */
async function resetState() {
  const carts = sql("SELECT r.id FROM request r JOIN pharmacy_account p ON p.id = r.pharmacy_id " +
                    "WHERE r.is_cart AND r.status='DRAFT'").split("\n").filter(Boolean);
  for (const id of carts) {
    execFileSync("python", ["-c",
      "import sys;sys.path.insert(0,'src');" +
      "from rova.core.db import get_sessionmaker;from rova.domain.hooks import wire;" +
      "from rova.domain.machines.registry import MACHINES;from rova.auth.principal import Principal;" +
      "from sqlalchemy import text;wire();s=get_sessionmaker()();" +
      `r=s.execute(text("SELECT pharmacy_id, created_by_user_id FROM request WHERE id='${id}'")).mappings().one();` +
      "p=Principal(user_id=r['created_by_user_id'], membership_id='x', organisation_id='x'," +
      " surface='PH', roles=frozenset({'PharmacyBuyer'}), pharmacy_id=r['pharmacy_id']," +
      " vendor_id=None, transporter_id=None);" +
      `MACHINES['SM-01'].apply(s, '${id}', 'ABANDON', p);s.commit()`],
      { cwd: "/home/claude/phstore/backend",
        env: { ...process.env,
               ROVA_DATABASE_URL: `postgresql+psycopg://postgres:postgres@localhost:5432/${DB}`,
               ROVA_JWT_SECRET: "dev-secret-please-be-32-characters-min", ROVA_ENV: "dev" } });
  }
}

function runJobs() {
  execFileSync("python", ["-m", "rova.cli", "jobs", "tick"], {
    cwd: "/home/claude/phstore/backend",
    env: {
      ...process.env,
      ROVA_DATABASE_URL: `postgresql+psycopg://postgres:postgres@localhost:5432/${DB}`,
      ROVA_JWT_SECRET: "dev-secret-please-be-32-characters-min",
      ROVA_ENV: "dev",
    },
  });
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

  // Start from a known basket. The walk is meant to be repeatable, and a
  // cart or an unread notice left behind by the previous run changed what
  // several of these steps were actually testing — a card already in the
  // basket shows the dial instead of the `+`, and a second line makes the
  // checkout flash carry a sum rather than one total.
  // Through the machine, not around it: an `UPDATE request SET status` here
  // skipped SM-01 and the `on_cart_left_draft` hook, and then had to mark the
  // notices read by hand to cover for what it had skipped — which is the
  // hook's own job and exactly what this walk is supposed to be exercising.
  await resetState();
  // Every notice from an earlier run, marked read. The one this walk checks
  // is written during it, by the real tick, a few steps below — so this
  // starts the inbox at zero rather than asserting against whatever the last
  // run happened to leave behind. The basket goes through SM-01 ABANDON
  // above, so slice B's own hook runs rather than being worked around.
  sql("UPDATE notification SET status='READ', read_at=now() WHERE read_at IS NULL");
  step("cleared the previous walk's basket and inbox");

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
  // One line, so the checkout flash carries one total and can be compared
  // against the price that was agreed. A leftover line from an earlier step
  // made that assertion compare a unit price against a two-line sum.
  for (let i = 0; i < 6; i++) {
    const extra = await page.$$('[data-act="dropCartLine"]');
    if (extra.length <= 1) break;
    await extra[extra.length - 1].click();
    await page.waitForTimeout(900);
  }
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
    // The agreed unit price times the quantity, formatted the way the app
    // formats money. Comparing the unit price alone passed only because the
    // basket happened to hold one of everything.
    const qty = Number(sql(`SELECT qty_requested FROM request_line WHERE id='${lineId}'`)) || 1;
    const agreed = ((Number(original) + 25) * qty).toFixed(2).replace(".", ",");
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

  /* ---- the basket he walked away from --------------------------------
     Put something in the cart, age it past CFG-CART-IDLE-HOURS in the
     database, run the real tick, and see what the app does with it. */
  await page.click('[data-tab="store"]');
  await page.waitForTimeout(1200);
  await page.click('.p [data-act="add"]');
  await page.waitForTimeout(1000);
  const idleCart = sql("SELECT id FROM request WHERE is_cart AND status='DRAFT' " +
                       "ORDER BY created_at DESC LIMIT 1");
  sql(`UPDATE request SET updated_at = now() - interval '30 hours' WHERE id='${idleCart}'`);
  sql(`UPDATE request_line SET updated_at = now() - interval '30 hours', ` +
      `created_at = now() - interval '30 hours' WHERE request_id='${idleCart}'`);
  runJobs();
  step("ran the tick against a 30-hour-old basket");

  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  const badge = await page.$eval(".bell .badge", (e) => e.textContent.trim()).catch(() => null);
  const reminder = await page.$eval(".reminder", (e) => e.textContent.replace(/\s+/g, " ").trim())
    .catch(() => null);
  step(`badge on the bell: ${badge || "(none)"}`);
  step(`reminder line: ${reminder || "(none)"}`);
  check(badge === "1", `the idle-basket notice never reached the app (badge=${badge})`);
  check(reminder !== null, "the store does not mention the basket he left");
  // An amount, not merely "some digit" — a day-of-month satisfied that. The
  // app writes money as `1 044,00 MT`, so that is what is looked for.
  check(/\d[\d\s]*,\d{2}\s*MT/.test(reminder || ""),
        `the reminder does not show what the basket comes to: "${reminder}"`);

  await page.click('[data-act="goNotifs"]');
  await page.waitForTimeout(1500);
  const notice = await page.$eval(".card .row", (e) => e.textContent.replace(/\s+/g, " ").trim())
    .catch(() => null);
  step(`notice reads: ${notice}`);
  check(notice !== null && !/N-CART-IDLE/.test(notice),
        "the notice shows a raw event code instead of a sentence");
  const noticed = sql("SELECT payload->'first_items'->>0 FROM notification " +
                      `WHERE event_code='N-CART-IDLE' AND payload->>'request_id'='${idleCart}'`);
  check(noticed && (notice || "").includes(noticed),
        `the notice does not name what is in the basket (expected "${noticed}")`);
  check(/\d/.test(notice || "") && /·/.test(notice || ""),
        "the notice does not say how many, or when");

  // tapping it opens the basket, and the badge is gone afterwards
  await page.click(".card .row[data-act='readNotice']");
  await page.waitForTimeout(1800);
  const landedOn = (await page.textContent("h1")).trim();
  step(`tapping the notice landed on: "${landedOn}"`);
  const cartTitles = ["Cart", "Carrinho", "السلة"];
  check(cartTitles.includes(landedOn),
        `tapping the notice did not open the basket, it opened "${landedOn}"`);
  await page.click('[data-act="goStore"]');
  await page.waitForTimeout(1200);
  const badgeOnStore = await page.$eval(".bell .badge", (e) => e.textContent.trim()).catch(() => null);
  step(`badge after reading: ${badgeOnStore || "(gone)"}`);
  check(badgeOnStore === null, "the notice stayed unread after he opened it");

  // and a basket touched just now gets no reminder: the line is for one he
  // walked away from, not a restatement of the bar at the bottom
  await page.click('.p [data-act="inc"], .p [data-act="add"]');
  await page.waitForTimeout(1200);
  const freshReminder = await page.$(".reminder");
  step(`reminder for a basket touched just now: ${freshReminder ? "shown" : "none"}`);
  check(!freshReminder, "a basket he is using right now is being 'reminded' about");

  /* ---- the shelves, and ordering something again ----------------------
     The pilot pharmacies have one or two orders, so "what you usually buy"
     is deliberately hidden and only the discount shelf is expected. That is
     the sprint's own rule, not a gap — a shelf that claims to know his
     habits after one purchase is a lie with a friendly face. */
  await page.click('[data-tab="store"]');
  await page.waitForTimeout(2500);
  const shelfTitles = await page.$$eval(".shelf-h", (e) => e.map((x) => x.textContent.trim()));
  const railCards = await page.$$eval(".rail .p.mini", (e) => e.length);
  step(`shelves: ${shelfTitles.join(" | ") || "(none)"} — ${railCards} card(s)`);
  check(shelfTitles.length > 0, "no shelf appeared on the store home");
  check(railCards > 0, "a shelf is showing with nothing on it");
  // Every card on the *discount* shelf has to carry the discount it
  // promises. The other shelves are about his habits and his past prices, so
  // a card without a badge there is not padding.
  const offShelf = await page.evaluate(() => {
    const heads = [...document.querySelectorAll(".shelf-h")];
    const head = heads[heads.length - 1 - [...heads].reverse()
      .findIndex((h) => /discount|desconto|خصم/i.test(h.textContent))];
    if (!head || !head.nextElementSibling) return null;
    return [...head.nextElementSibling.querySelectorAll(".p.mini")]
      .map((x) => !!x.querySelector(".off"));
  });
  step(`discount badges on that shelf: ${(offShelf || []).filter(Boolean).length}/${(offShelf || []).length}`);
  check(offShelf && offShelf.length > 0, "the biggest-discount shelf did not render");
  check((offShelf || []).every(Boolean),
        "the biggest-discount shelf is padded with rows that have none");

  // the reminder must keep its place above them
  const order = await page.evaluate(() => {
    const el = (sel) => document.querySelector(sel);
    const rem = el(".reminder"), sh = el(".shelf-h");
    if (!rem || !sh) return "n/a";
    return rem.compareDocumentPosition(sh) & Node.DOCUMENT_POSITION_FOLLOWING ? "above" : "below";
  });
  step(`the basket reminder sits ${order} the shelves`);
  check(order !== "below", "the shelves pushed the basket reminder out of sight");

  // and adding from a shelf goes into the same basket
  const beforeAdd = await page.textContent(".cartbar").catch(() => "");
  // The same card as the button — reading `.pr` from the first mini card and
  // clicking the first `add` picked two different products whenever the
  // first was already in the basket and showing its dial instead.
  //
  // And specifically a shelf card that is NOT in the grid. That is the whole
  // point: the lookup that recorded the price he saw searched the grid and
  // the product page only, so it found nothing for a shelf card and the
  // server stored its own number instead. A card that happens to appear in
  // both passes either way and proves nothing.
  const shelfCard = await page.evaluate(() => {
    const inGrid = new Set([...document.querySelectorAll(".grid .p [data-act]")]
      .map((b) => b.dataset.id).filter(Boolean));
    const cards = [...document.querySelectorAll(".rail .p.mini")]
      .filter((c) => c.querySelector('[data-act="add"]'));
    const pick = cards.find((c) => !inGrid.has(c.querySelector('[data-act="add"]').dataset.id))
              || cards[0];
    if (!pick) return null;
    return { id: pick.querySelector('[data-act="add"]').dataset.id,
             price: (pick.querySelector(".pr") || {}).textContent.trim(),
             offGrid: !inGrid.has(pick.querySelector('[data-act="add"]').dataset.id) };
  });
  check(!!shelfCard, "no shelf card offers a way to add it");
  check(shelfCard && shelfCard.offGrid,
        "every shelf card is also in the grid, so this step cannot see the defect it is for");
  const shelfPrice = shelfCard.price, shelfId = shelfCard.id;
  // Move the price out from under the shelf before tapping. Without this the
  // step cannot fail: the server's own figure equals the card's on this data,
  // so a cart that recorded the server's number instead of the pharmacist's
  // would look identical. The gap this guards is time, not ranking.
  const shelfOffer = sql(`SELECT id FROM vendor_offer WHERE index_product_id='${shelfId}' ` +
                         "AND freshness_state='FRESH' ORDER BY price LIMIT 1");
  const shelfOriginal = sql(`SELECT price FROM vendor_offer WHERE id='${shelfOffer}'`);
  sql(`UPDATE vendor_offer SET price = price + 37 WHERE id='${shelfOffer}'`);
  await page.click(`.rail .p.mini [data-act="add"][data-id="${shelfId}"]`);
  await page.waitForTimeout(1400);
  const afterAdd = await page.textContent(".cartbar").catch(() => "");
  step(`cart bar: "${beforeAdd.trim()}" -> "${afterAdd.trim()}"`);
  check(afterAdd !== beforeAdd, "adding from a shelf did not reach the cart");

  // The claim, not just the line. A shelf card is almost never in the grid,
  // so `+` here used to send no price at all and the server recorded its own
  // as though it were the one on screen — the defect the whole price
  // mechanism exists to prevent, arriving through a new door.
  const storedSeen = sql("SELECT price_seen FROM request_line rl JOIN request r ON r.id = rl.request_id " +
                         `WHERE r.is_cart AND r.status='DRAFT' AND rl.index_product_id='${shelfId}'`);
  sql(`UPDATE vendor_offer SET price = ${shelfOriginal} WHERE id='${shelfOffer}'`);
  step(`shelf showed ${shelfPrice}; the server would have said ` +
       `${(Number(shelfOriginal) + 37).toFixed(2)}; the basket recorded ${storedSeen || "(nothing)"}`);
  check(!!storedSeen, "adding from a shelf recorded no price the pharmacist was shown");
  check(Number(storedSeen) === Number(shelfOriginal),
        `the basket recorded ${storedSeen} — the server's own figure — rather than the ` +
        `${shelfOriginal} the shelf was showing him`);

  /* order it all again */
  const ordersBeforeReorder = Number(sql("SELECT count(*) FROM \"order\""));
  await page.click('[data-tab="orders"]');
  await page.waitForTimeout(1600);
  await page.click(".card.tap");
  await page.waitForTimeout(1400);
  const hasReorder = await page.$('[data-act="reorder"]');
  check(!!hasReorder, "a past order has no way to be ordered again");
  await page.click('[data-act="reorder"]');
  await page.waitForTimeout(2000);
  const afterReorder = (await page.textContent("h1")).trim();
  const reorderRows = await page.$$eval(".card .row .cnt .n", (e) => e.map((x) => x.textContent.trim()));
  step(`"add all" landed on "${afterReorder}" with quantities [${reorderRows.join(", ")}]`);
  check(["Cart", "Carrinho", "السلة"].includes(afterReorder),
        "adding a past order did not open the basket");
  check(reorderRows.length > 0, "the basket is empty after adding a whole order to it");
  // The real question is whether an order row appeared, not what the flash
  // says. Greping the flash for "RQ-" could never match the sentence the app
  // actually writes, so it was a check that could not fail.
  const ordersAfterReorder = Number(sql("SELECT count(*) FROM \"order\""));
  check(ordersAfterReorder === ordersBeforeReorder,
        `"add all" created ${ordersAfterReorder - ordersBeforeReorder} order(s); it must only fill the basket`);

  /* ---- the credit limit, and the two ways past it ---------------------
     R-044 used to be the end of the conversation: a full basket and nothing
     to press. The pilot's own facility is Ahmed Distribuição against
     Farmácia Central da Baixa, so it is squeezed to something a real basket
     exceeds and then put back. */
  // Its own facility, against a distributor whose offers carry prices: the
  // pilot's existing one is with Ahmed, whose seed catalogue is the regulated
  // products, and a regulated offer has no price on it by construction
  // (R-009) — so a basket from Ahmed could never test a credit limit.
  const pharmacyId = sql("SELECT pa.id FROM pharmacy_account pa " +
                         "JOIN membership m ON m.organisation_id = pa.organisation_id " +
                         `JOIN app_user u ON u.id = m.user_id WHERE u.phone='${PHONE}' LIMIT 1`);
  const vendorOfFacility = sql(
    "SELECT vendor_id FROM vendor_offer WHERE freshness_state='FRESH' AND price IS NOT NULL " +
    "GROUP BY vendor_id ORDER BY count(*) DESC LIMIT 1");
  const facility = "crf_walk_credit";
  try {
    sql(`DELETE FROM credit_override WHERE facility_id='${facility}'`);
    sql(`DELETE FROM credit_facility WHERE id='${facility}'`);
    sql("INSERT INTO credit_facility (id, vendor_id, pharmacy_id, limit_amount, " +
        "opening_balance, terms_days, mov_waived, status) VALUES " +
        `('${facility}', '${vendorOfFacility}', '${pharmacyId}', 1.00, 0, 30, false, 'ACTIVE')`);
    await resetState();
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(2200);

    // a basket from that distributor alone
    // a product only that distributor offers, so the ranking cannot route
    // the line around the facility and out of the test
    const prod = sql("SELECT o.index_product_id FROM vendor_offer o " +
                     `WHERE o.vendor_id='${vendorOfFacility}' AND o.freshness_state='FRESH' ` +
                     "AND o.price IS NOT NULL AND (SELECT count(*) FROM vendor_offer x " +
                     "  WHERE x.index_product_id=o.index_product_id AND x.freshness_state='FRESH')=1 " +
                     "ORDER BY o.price DESC LIMIT 1");
    // The basket is put together through the API rather than by hunting for
    // one particular product in a grid of three thousand: this step is about
    // what happens at the limit, and the search is exercised above.
    const added = await page.evaluate(async (id) => {
      const tok = localStorage.getItem("token");
      const r = await fetch("/v1/cart/lines", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + tok },
        body: JSON.stringify({ lines: [{ index_product_id: id, qty_requested: 3 }] }),
      });
      return r.status;
    }, prod);
    check(added === 200, `could not build a basket for the credit step (HTTP ${added})`);
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(2000);
    await page.click('[data-act="goCart"]');
    await page.waitForTimeout(1400);
    expectRefusal = true;
    await page.click('[data-act="checkout"]');
    await page.waitForTimeout(2500);
    expectRefusal = false;

    const wall = await page.$eval("#creditblock", (e) => e.textContent.replace(/\s+/g, " ").trim())
      .catch(() => null);
    step("over the credit limit, the cart says: " + (wall ? wall.slice(0, 110) : "(nothing)"));
    check(!!wall, "the credit refusal is still a wall with nothing to press");
    check(/\d[\d\s]*,\d{2}\s*MT/.test(wall || ""),
          "the refusal does not say how much headroom there is or what the order costs");
    check(!!(await page.$('[data-act="askCredit"]')), "no way to ask for permission");
    check(!!(await page.$('[data-act="payCash"]')), "no way to pay cash instead");

    // he asks
    await page.fill("#creditnote", "cliente a espera");
    await page.click('[data-act="askCredit"]');
    await page.waitForTimeout(2000);
    const pendingRow = sql("SELECT status FROM credit_override ORDER BY requested_at DESC LIMIT 1");
    step("asked for permission -> " + pendingRow);
    check(pendingRow === "PENDING", "asking produced no pending permission");

    // the distributor's order desk answers, from its own session
    // Its own context: a second page in the pharmacist's shares his
    // localStorage, so it opens already signed in as him.
    const deskCtx = await browser.newContext({ viewport: { width: 420, height: 880 } });
    const deskPage = await deskCtx.newPage();
    await deskPage.goto(BASE, { waitUntil: "networkidle" });
    // Somebody who can actually answer for THIS distributor. A desk hand at
    // another wholesaler gets a 404, which is the point of that rule and not
    // a way to drive this one.
    const decider = sql(
      "SELECT u.phone FROM app_user u JOIN membership m ON m.user_id = u.id " +
      "JOIN vendor_account v ON v.organisation_id = m.organisation_id " +
      `WHERE v.id='${vendorOfFacility}' ` +
      "AND m.role_codes && ARRAY['VendorOrderDesk','VendorAdmin','VendorFinance'] LIMIT 1") ||
      sql("SELECT u.phone FROM app_user u JOIN membership m ON m.user_id = u.id " +
          "WHERE m.role_codes && ARRAY['PlatformFinance','PlatformAdmin'] LIMIT 1");
    check(!!decider, "nobody in the seed data can answer a credit request");
    await deskPage.fill("#ph", decider);
    await deskPage.fill("#pw", PW);
    await deskPage.click('[data-act="login"]');
    await deskPage.waitForTimeout(2500);
    const covId = sql("SELECT id FROM credit_override ORDER BY requested_at DESC LIMIT 1");
    const approved = await deskPage.evaluate(async (id) => {
      const tok = localStorage.getItem("token");
      const r = await fetch("/v1/credit-overrides/" + id + "/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + tok },
        body: JSON.stringify({ reason: "cliente antigo" }),
      });
      return r.status;
    }, covId);
    await deskCtx.close();
    const side = sql("SELECT decided_by_side FROM credit_override ORDER BY requested_at DESC LIMIT 1");
    step(`answered by ${decider} (HTTP ${approved}), recorded as ${side || "(nothing)"}`);
    check(approved === 200, `the decider could not approve it (HTTP ${approved})`);
    check(side === "VENDOR" || side === "PLATFORM",
          "the permission does not record which side allowed it");

    // and now the same basket goes through, on credit, with his name on it
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(2000);
    await page.click('[data-act="goCart"]');
    await page.waitForTimeout(1200);
    const ordersBeforeCredit = Number(sql('SELECT count(*) FROM "order"'));
    await page.click('[data-act="checkout"]');
    await page.waitForSelector(".ok", { timeout: 25000 });
    step("ORDER on overridden credit: " + (await page.textContent(".ok")).trim());
    check(Number(sql('SELECT count(*) FROM "order"')) > ordersBeforeCredit,
          "the approved permission did not let the order through");
    const stamped = sql('SELECT credit_override_user_id FROM "order" ' +
                        "ORDER BY created_at DESC LIMIT 1");
    const terms = sql('SELECT payment_terms FROM "order" ORDER BY created_at DESC LIMIT 1');
    step(`the order records: terms=${terms}, allowed by ${stamped || "(nobody)"}`);
    check(!!stamped, "the order does not say who allowed it past the limit");
    check(terms === "CREDIT_N_DAYS", "it went through as cash rather than on the credit allowed");
    check(sql("SELECT status FROM credit_override ORDER BY requested_at DESC LIMIT 1") === "USED",
          "the permission was not spent, so it could be spent again");
  } finally {
    sql(`UPDATE credit_override SET facility_id=facility_id WHERE facility_id='${facility}'`);
    sql(`DELETE FROM credit_override WHERE facility_id='${facility}'`);
    sql(`DELETE FROM credit_facility WHERE id='${facility}'`);
  }

  /* ---- the WhatsApp lane, and its price screen ------------------------
     This lane used to go from "confirm" straight to a placed order with no
     figure ever on screen — the one place in the app where a pharmacy's
     money moved blind. */
  await page.click('[data-tab="list"]');
  await page.waitForTimeout(500);
  await page.fill("#paste", "Paracetamol 500\nAmoxicilina 500 mg 21");
  await page.click('[data-act="paste"]');
  await page.waitForTimeout(3000);
  const normText = await page
    .$eval("#normblock", (e) => e.textContent.replace(/\s+/g, " ").slice(0, 120))
    .catch(() => "(no normalisation block)");
  step("paste -> " + normText);

  // resolve whatever the matcher could not decide, then confirm the list
  for (let i = 0; i < 8; i++) {
    const choice = await page.$('#normblock [data-act="resolve"]');
    if (!choice) break;
    await choice.click();
    await page.waitForTimeout(1200);
  }
  for (let i = 0; i < 8; i++) {
    const drop = await page.$('#normblock [data-act="dropline"]');
    const done = await page.$('#normblock [data-act="normdone"]');
    if (done || !drop) break;
    await drop.click();
    await page.waitForTimeout(1200);
  }
  const canConfirm = await page.$('[data-act="normdone"]');
  if (canConfirm) {
    const ordersBeforeList = Number(sql('SELECT count(*) FROM "order"'));
    await page.click('[data-act="normdone"]');
    await page.waitForTimeout(2500);

    const quote = await page.$eval("#quoteblock", (e) => e.textContent.replace(/\s+/g, " ").trim())
      .catch(() => null);
    step("confirming the list showed: " + (quote ? quote.slice(0, 110) : "(nothing)"));
    check(!!quote, "confirming a pasted list showed no prices at all");
    check(/\d[\d\s]*,\d{2}\s*MT/.test(quote || ""),
          "the confirmation screen names no price");
    check(Number(sql('SELECT count(*) FROM "order"')) === ordersBeforeList,
          "confirming the list bought something before he had seen a price");

    // a line nobody can supply has to have a way out, on this screen
    for (let i = 0; i < 8; i++) {
      const stuck = await page.$('#quoteblock [data-act="dropQuoteLine"]');
      if (!stuck) break;
      await stuck.click();
      await page.waitForTimeout(1500);
    }
    const stillBlocked = await page.$('[data-act="quoteAccept"][disabled]');
    step(`after clearing what cannot be supplied, the order button is ${stillBlocked ? "still disabled" : "live"}`);
    check(!stillBlocked, "the price screen is a dead end: blocked, with no way to unblock it");

    // Move a price out from under this screen too. The lane's whole defect
    // was that it could bill a figure he had never seen; showing him one and
    // then billing a different one would be the same defect wearing a
    // confirmation screen.
    const qLine = await page.getAttribute('#quoteblock [data-act="dropQuoteLine"], #quoteblock .row', "data-line")
      .catch(() => null);
    const qProd = sql("SELECT rl.index_product_id FROM request_line rl JOIN request r ON r.id = rl.request_id " +
                      "WHERE r.status='AWAITING_CONFIRMATION' ORDER BY rl.created_at LIMIT 1");
    const qOffer = qProd ? sql(`SELECT id FROM vendor_offer WHERE index_product_id='${qProd}' ` +
                               "AND freshness_state='FRESH' ORDER BY price LIMIT 1") : "";
    if (qOffer) {
      const qWas = sql(`SELECT price FROM vendor_offer WHERE id='${qOffer}'`);
      sql(`UPDATE vendor_offer SET price = price + 19 WHERE id='${qOffer}'`);
      expectRefusal = true;
      await page.click('[data-act="quoteAccept"]');
      await page.waitForTimeout(3000);
      const qBanner = await page.$("#quoteblock .err, .err");
      const qBtn = (await page.textContent('[data-act="quoteAccept"]')).trim();
      step(`price moved under the confirmation screen: refused=${!!qBanner}, button now "${qBtn}"`);
      check(!!qBanner, "the pasted list was billed a price he had never been shown");
      check(Number(sql('SELECT count(*) FROM "order"')) === ordersBeforeList,
            "a moved price still produced an order");
      sql(`UPDATE vendor_offer SET price = ${qWas} WHERE id='${qOffer}'`);
      await page.waitForTimeout(500);
      // the retry can refuse once more if the restore lands mid-flight, so
      // the console stays forgiving until the screen settles
      await page.click('[data-act="quoteAccept"]').catch(() => {});
      await page.waitForTimeout(2500);
      expectRefusal = false;
    }

    // and it is his tap, on the prices he was shown, that buys
    await page.click('[data-act="quoteAccept"]').catch(() => {});
    await page.waitForSelector(".ok", { timeout: 25000 });
    const listFlash = (await page.textContent(".ok")).trim();
    step("LIST ORDER: " + listFlash);
    check(Number(sql('SELECT count(*) FROM "order"')) > ordersBeforeList,
          "agreeing to the prices did not place the order");
  } else {
    step("the pasted list had nothing the matcher could resolve; price screen not driven");
  }

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
