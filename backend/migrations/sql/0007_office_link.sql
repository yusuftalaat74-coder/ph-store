-- ============================================================================
-- 0005. PH OFFICE LINK (PH Office SPEC 5.9.1)
-- ============================================================================
--
-- Additive only. Nothing here changes how PH Store behaves while
-- ROVA_OFFICE_ENABLED=false: the two tables stay empty, every new column is
-- NULL / false / defaulted, and `current_exposure` only reads office_balance
-- once PH Office has written one. Rolling back the cutover is setting the
-- flag back to false (SPEC 1.4) — no migration involved.
--
-- Forward-only (A2.7).

CREATE TABLE integration_outbox (                       -- prefix obx_   Store -> Office
  id                 TEXT PRIMARY KEY,
  seq                BIGSERIAL UNIQUE,
  event_type         TEXT NOT NULL,
  aggregate_type     TEXT NOT NULL,
  aggregate_id       TEXT NOT NULL,
  aggregate_version  INTEGER NOT NULL,
  payload            JSONB NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('PENDING','SENT','FAILED','DEAD')) DEFAULT 'PENDING',
  attempts           INTEGER NOT NULL DEFAULT 0,
  next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error         TEXT NULL,
  sent_at            TIMESTAMPTZ NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_integration_outbox_pending ON integration_outbox (seq) WHERE status IN ('PENDING','FAILED');
CREATE INDEX ix_integration_outbox_aggregate ON integration_outbox (aggregate_type, aggregate_id, seq);

CREATE TABLE integration_inbound_event (                -- prefix ibx_   Office -> Store
  id                 TEXT PRIMARY KEY,
  event_id           TEXT NOT NULL UNIQUE,
  event_type         TEXT NOT NULL,
  seq                BIGINT NULL,
  aggregate_type     TEXT NOT NULL,
  aggregate_id       TEXT NOT NULL,
  aggregate_version  INTEGER NOT NULL,
  payload            JSONB NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('RECEIVED','PROCESSED','SKIPPED','FAILED')) DEFAULT 'RECEIVED',
  error              TEXT NULL,
  received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at       TIMESTAMPTZ NULL
);
CREATE INDEX ix_integration_inbound_aggregate ON integration_inbound_event (aggregate_type, aggregate_id, aggregate_version);

ALTER TABLE credit_facility
  ADD COLUMN office_balance NUMERIC(14,2) NULL,
  ADD COLUMN office_credit_limit NUMERIC(14,2) NULL,
  ADD COLUMN office_hold BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN office_synced_at TIMESTAMPTZ NULL;

ALTER TABLE pharmacy_account
  ADD COLUMN office_account_snapshot JSONB NULL;

ALTER TABLE invoice ADD COLUMN source TEXT NOT NULL DEFAULT 'VENDOR_UPLOAD'
  CHECK (source IN ('VENDOR_UPLOAD','OFFICE'));
ALTER TABLE invoice ADD COLUMN office_invoice_id TEXT NULL UNIQUE;
ALTER TABLE payment ADD COLUMN office_payment_id TEXT NULL UNIQUE;
ALTER TABLE credit_note ADD COLUMN office_credit_note_id TEXT NULL UNIQUE;

-- A payment mirrored from PH Office was recorded by an Office user, who has
-- no app_user row in PH Store (SPEC 5.9.5: no new Store user). The recorder
-- stays mandatory for every payment PH Store records itself.
ALTER TABLE payment ALTER COLUMN recorded_by_user_id DROP NOT NULL;
ALTER TABLE payment ADD CONSTRAINT ck_payment_recorder_or_office
  CHECK (recorded_by_user_id IS NOT NULL OR office_payment_id IS NOT NULL);
