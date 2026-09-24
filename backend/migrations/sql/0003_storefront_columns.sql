-- 0003 — two columns the storefront needs, both nullable so nothing existing
-- has to change.
--
-- `vendor_offer.list_price` is the public selling price the vendor's own
-- price list prints beside what the pharmacy pays (`PVP` at Medimport,
-- `Público c/IVA` at Medis). It is captured, never computed: the margin a
-- pharmacist sees on the shelf has to be the vendor's own published figure,
-- not something this system derived. It is deliberately NOT tied to
-- `price_reference`, which carries state-fixed prices and their source
-- document; a vendor's list price is neither.
--
-- `index_product.image_url` is where a real product photo goes when one
-- exists. Until then the client draws a placeholder from the product name,
-- so the grid never shows a picture of a medicine that is not that medicine.

ALTER TABLE vendor_offer
  ADD COLUMN list_price NUMERIC(14,2) NULL,
  ADD CONSTRAINT ck_vendor_offer_list_price_positive CHECK (list_price IS NULL OR list_price >= 0),
  -- a list price below what the pharmacy pays is a data error, not a discount
  ADD CONSTRAINT ck_vendor_offer_list_price_above_price
      CHECK (list_price IS NULL OR price IS NULL OR list_price >= price);

ALTER TABLE index_product
  ADD COLUMN image_url TEXT NULL;
