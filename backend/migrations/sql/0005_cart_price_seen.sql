-- 0005 — what the pharmacist saw when they put a line in the cart.
--
-- The cart is a `DRAFT` request, so it can sit for days while prices move
-- underneath it. These two columns record the price and the vendor at the
-- moment the line was added, so the cart can say "this went up" instead of
-- quietly repricing and surprising the pharmacist at checkout.
--
-- Both nullable: a line that arrived from a WhatsApp paste has no price when
-- it is created, and a line whose product is still unresolved has no vendor
-- to name. NULL here means "nothing was shown yet", not "free".

ALTER TABLE request_line
  ADD COLUMN price_seen     NUMERIC(14,2) NULL,
  ADD COLUMN vendor_seen_id TEXT NULL REFERENCES vendor_account(id),
  ADD CONSTRAINT ck_request_line_price_seen_positive
      CHECK (price_seen IS NULL OR price_seen >= 0);
