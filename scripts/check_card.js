/* The card at a real phone width, with something in the cart — the state
   that overflowed. Measured, not eyeballed: every element inside a card has
   to stay inside the card's box. */
const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 360, height: 800 }, deviceScaleFactor: 3 });
  const errs = []; p.on("pageerror", e => errs.push(e.message));
  await p.goto("http://127.0.0.1:8099/app/", { waitUntil: "networkidle" });
  await p.fill("#ph", "+258840000011"); await p.fill("#pw", "rova-demo");
  await p.click('[data-act="login"]'); await p.waitForSelector(".p", { timeout: 20000 });

  // put the first two in the cart so the counter is showing
  const adds = await p.$$('.p .rnd[data-act="add"]');
  await adds[0].click(); await p.waitForTimeout(600);
  const adds2 = await p.$$('.p .rnd[data-act="add"]');
  await adds2[0].click(); await p.waitForTimeout(600);
  const inc = await p.$$('.p .rnd[data-act="inc"]');
  await inc[0].click(); await p.waitForTimeout(600);

  const overflow = await p.$$eval(".p", cards => cards.map((c, i) => {
    const cb = c.getBoundingClientRect();
    const bad = [];
    c.querySelectorAll(".pr, .was, .cnt, .rnd, .nm").forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.left < cb.left - 0.5 || r.right > cb.right + 0.5)
        bad.push(el.className + " " + Math.round(r.left - cb.left) + ".." + Math.round(r.right - cb.right));
    });
    return bad.length ? { card: i, bad } : null;
  }).filter(Boolean));

  console.log("  cards on screen: " + (await p.$$eval(".p", e => e.length)));
  console.log("  overflowing elements: " + (overflow.length ? JSON.stringify(overflow) : "none"));
  console.log("  JS errors: " + errs.length);
  await p.screenshot({ path: "/home/claude/card-phone.png", clip: { x: 0, y: 150, width: 360, height: 560 } });
  await b.close();
  process.exit(overflow.length || errs.length ? 1 : 0);
})();
