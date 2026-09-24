-- 0006 — the cart says it is a cart, and a pharmacy has only one.
--
-- Until now "this request is the cart" was inferred: status DRAFT, mode
-- CATALOGUE, channel APP. Every one of those is also true of a request made
-- through `POST /v1/requests`, so the two were the same thing to the
-- database and merely different in intent. That guess cost real behaviour:
-- two taps on `+` before the first reply landed created two such requests,
-- `find_cart` returned the newer, and the older cart's lines were invisible
-- on the cart screen while still being counted in the assistant summary and
-- listed in the orders tab.
--
-- A column the cart sets on purpose ends the guessing, and a partial unique
-- index over it makes "the pharmacy's cart" true rather than hopeful. A
-- pharmacy may still have as many ordinary drafts as it likes; only carts
-- are limited to one, and only while they are still DRAFT.

ALTER TABLE request ADD COLUMN IF NOT EXISTS is_cart boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN request.is_cart IS
    'Set only by the cart endpoints. A pharmacy has at most one open cart '
    '(uq_one_cart_per_pharmacy); any number of other DRAFT requests is fine.';

-- Carts already created by the deployed cart code, which had no flag to set.
-- The newest catalogue draft per pharmacy is the one `find_cart` was
-- returning, so it is the one that keeps being the cart.
CREATE TEMP TABLE _carts AS
SELECT id, pharmacy_id,
       row_number() OVER (PARTITION BY pharmacy_id ORDER BY created_at DESC) AS rn
FROM request
WHERE status = 'DRAFT' AND mode = 'CATALOGUE' AND channel = 'APP';

UPDATE request SET is_cart = true
WHERE id IN (SELECT id FROM _carts WHERE rn = 1);

-- The losers of the double-tap race are not ordinary drafts and must not be
-- left as such: the old mode+channel guess at least kept them out of the
-- order list and the open-request count, and without this they would surface
-- as "Draft" rows the app has no screen to open. Their lines go to the cart
-- the pharmacist can actually see, because a line he put in a basket is not
-- ours to drop.
UPDATE request_line rl
SET request_id = keep.id
FROM _carts lost
JOIN _carts keep ON keep.pharmacy_id = lost.pharmacy_id AND keep.rn = 1
WHERE lost.rn > 1 AND rl.request_id = lost.id
  -- not where the kept cart already carries that product, so the merge
  -- cannot create the duplicate lines the cart itself prevents
  AND NOT EXISTS (SELECT 1 FROM request_line other
                  WHERE other.request_id = keep.id
                    AND other.index_product_id = rl.index_product_id
                    AND other.line_kind = 'CATALOGUE');

-- Whatever is left in a superseded cart is a duplicate of a line already in
-- the kept one, so the cart itself is abandoned. `DRAFT -> CANCELLED` on
-- ABANDON is a real SM-01 transition, and it is written to the append-only
-- log here rather than having a migration move a status behind the machine's
-- back.
INSERT INTO state_transition
    (id, machine, subject_type, subject_id, from_state, to_state, trigger,
     actor_user_id, actor_role, notes)
SELECT 'stt_' || replace(gen_random_uuid()::text, '-', ''), 'SM-01', 'request', id,
       'DRAFT', 'CANCELLED', 'ABANDON', NULL, 'SYSTEM',
       '{"reason": "superseded cart; its lines were folded into the pharmacy''s visible cart (migration 0006)"}'::jsonb
FROM _carts WHERE rn > 1;

UPDATE request SET status = 'CANCELLED' WHERE id IN (SELECT id FROM _carts WHERE rn > 1);

DROP TABLE _carts;

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_cart_per_pharmacy
    ON request (pharmacy_id)
    WHERE is_cart AND status = 'DRAFT';
