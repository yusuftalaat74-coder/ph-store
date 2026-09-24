-- 0007 — one idle-basket notice per basket, enforced by the database.
--
-- `cart_idle` checked for an existing notice and then inserted one. Two ticks
-- overlapping — a cron that fires while the previous run is still going, or a
-- hand-run `rova jobs tick` next to the scheduled one — both read "none" and
-- both write, and the pharmacist gets the same sentence twice. The module's
-- own docstring promised every job was safe to run concurrently with itself,
-- which for this one was true only by luck.
--
-- The same index answers the guard's own question. Without it that check is a
-- sequential scan of `notification` for every idle cart on every tick, which
-- is carts x notifications and grows with both.

DELETE FROM notification a
USING notification b
WHERE a.event_code = 'N-CART-IDLE' AND b.event_code = 'N-CART-IDLE'
  AND a.payload->>'request_id' = b.payload->>'request_id'
  AND (a.created_at, a.id) > (b.created_at, b.id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_idle_notice_per_cart
    ON notification ((payload->>'request_id'))
    WHERE event_code = 'N-CART-IDLE';
