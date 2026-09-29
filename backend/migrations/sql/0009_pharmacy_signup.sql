-- 0009 — a pharmacy can create its own account from the phone.
--
-- What it can type at a counter is less than what the reviewer knows after
-- reading the Alvará, so the columns the reviewer fills become nullable and
-- the approval guard (and the CHECK below) requires them instead.
--
-- Every statement is safe to run twice: DROP NOT NULL on a nullable column
-- is a no-op, constraints are dropped before they are (re)added, and the new
-- table and its indexes are IF NOT EXISTS.

ALTER TABLE organisation ALTER COLUMN tax_id DROP NOT NULL;          -- NUIT optional at signup (uq_organisation_tax tolerates NULLs)
ALTER TABLE pharmacy_account ALTER COLUMN latitude  DROP NOT NULL;   -- nothing reads these outside onboarding/seed
ALTER TABLE pharmacy_account ALTER COLUMN longitude DROP NOT NULL;

ALTER TABLE licence ALTER COLUMN number       DROP NOT NULL;
ALTER TABLE licence ALTER COLUMN issue_date   DROP NOT NULL;
ALTER TABLE licence ALTER COLUMN expiry_date  DROP NOT NULL;
ALTER TABLE licence ALTER COLUMN document_ref DROP NOT NULL;         -- the reviewer can attach it later (§2.6)

-- the inline CHECK (expiry_date > issue_date) from 0001 references two
-- columns, so Postgres named it `licence_check` (checked with \d licence;
-- not `licence_expiry_date_check`). It would reject nothing new — a NULL
-- passes a CHECK — but it is replaced by a named one that says what it means.
ALTER TABLE licence DROP CONSTRAINT IF EXISTS licence_check;
ALTER TABLE licence DROP CONSTRAINT IF EXISTS licence_expiry_date_check;
ALTER TABLE licence DROP CONSTRAINT IF EXISTS ck_licence_dates;
ALTER TABLE licence ADD CONSTRAINT ck_licence_dates
  CHECK (issue_date IS NULL OR expiry_date IS NULL OR expiry_date > issue_date);

-- a licence may only be VALID/EXPIRING_SOON/RENEWED/EXPIRED with real dates
-- and a number: a reviewer approves what was read off the document.
ALTER TABLE licence DROP CONSTRAINT IF EXISTS ck_licence_valid_has_dates;
ALTER TABLE licence ADD CONSTRAINT ck_licence_valid_has_dates
  CHECK (status IN ('SUBMITTED','UNDER_REVIEW','REJECTED')
         OR (number IS NOT NULL AND issue_date IS NOT NULL AND expiry_date IS NOT NULL));

-- who tried to sign up, from where, with what result. Read by the rate
-- limiter, which has to live in the database: the API runs two workers.
CREATE TABLE IF NOT EXISTS signup_attempt (               -- prefix sga_
  id            TEXT PRIMARY KEY,
  phone         TEXT NOT NULL,                             -- normalised E.164, or the raw string when it could not be normalised
  ip            TEXT NOT NULL,
  outcome       TEXT NOT NULL CHECK (outcome IN ('CREATED','DUPLICATE_PHONE','INVALID','RATE_LIMITED')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_signup_attempt_phone ON signup_attempt (phone, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_signup_attempt_ip    ON signup_attempt (ip, created_at DESC);
