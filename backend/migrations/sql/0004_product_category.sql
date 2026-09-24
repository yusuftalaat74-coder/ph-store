-- 0004 — a browsing category for every product.
--
-- `therapeutic_class` is reserved for real ATC data from the Index and is
-- left alone: a dosage-form group is not a therapeutic class, and storing
-- one in the other's column would quietly make every later ATC query wrong.
--
-- The value is derived at import from whatever the source actually said —
-- the register's own category where there is one, otherwise the dosage form
-- or the wording of the vendor's line — so the filter covers the whole
-- catalogue instead of only the rows a register happened to classify.

ALTER TABLE index_product ADD COLUMN category TEXT NULL;

CREATE INDEX ix_index_product_category ON index_product (category)
  WHERE review_status = 'PUBLISHED';
