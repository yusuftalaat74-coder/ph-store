-- ============================================================================
-- 0. INFRASTRUCTURE
-- ============================================================================

CREATE OR REPLACE FUNCTION rova_forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'append-only table: % (R-060, R-092, §14.3)', TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

-- region: the scope unit for PER_REGION config, fees, mode switches. Seeded, not user-created in v1.
CREATE TABLE region (                                   -- no id prefix: natural key
  code        TEXT PRIMARY KEY,                         -- 'MAPUTO_CIDADE', 'MATOLA', 'OUTRA'
  name        TEXT NOT NULL,
  transit_window_hours INTEGER NOT NULL CHECK (transit_window_hours > 0),  -- CFG-TRANSIT-WINDOW-HOURS default holder
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- config_parameter (E-048): every CFG-* key, with scope precedence VENDOR > REGION > GLOBAL.
CREATE TABLE config_parameter (                         -- prefix cfg_
  id          TEXT PRIMARY KEY,
  key         TEXT NOT NULL CHECK (key LIKE 'CFG-%'),
  scope_type  TEXT NOT NULL CHECK (scope_type IN ('GLOBAL','REGION','VENDOR')),
  scope_id    TEXT NULL,                                -- region.code or vendor_account.id; NULL iff GLOBAL
  value       TEXT NOT NULL,                            -- canonical string; parsed by value_type
  value_type  TEXT NOT NULL CHECK (value_type IN ('INT','DECIMAL','BOOL','ENUM','TEXT')),
  owner_role  TEXT NOT NULL,                            -- role code allowed to change it (§16)
  source_tag  TEXT NOT NULL,                            -- e.g. 'design target A-05' — every value carries provenance
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_config_parameter_scope CHECK ((scope_type = 'GLOBAL') = (scope_id IS NULL)),
  CONSTRAINT uq_config_parameter UNIQUE (key, scope_type, scope_id)
);

-- audit_event (E-047): append-only compliance record. actor NULL = SYSTEM.
CREATE TABLE audit_event (                              -- prefix aud_
  id             TEXT PRIMARY KEY,
  actor_user_id  TEXT NULL,                             -- FK added after app_user
  actor_role     TEXT NOT NULL,                         -- role code or 'SYSTEM'
  action_code    TEXT NOT NULL CHECK (action_code IN (
      -- §14.3 v1.0
      'CREDIT_LIMIT_OVERRIDE','MOV_WAIVER','SOURCING_ATTESTATION_CHANGE','PATIENT_DATA_ACCESS',
      'DATA_SUBJECT_REQUEST_HANDLED','CONFIG_PARAMETER_CHANGE','ROLE_MEMBERSHIP_CHANGE','LICENCE_DECISION',
      'PRICE_DEVIATION_RESOLUTION','ALLOCATION_MANUAL_OVERRIDE','FEE_SCHEDULE_CHANGE','COURIER_DEVICE_REBIND',
      'INVOICE_WRITTEN_OFF','CONSENT_WITHDRAWN','COMPLIANCE_HALT','ORDER_LINE_QTY_CORRECTION',
      -- addendum §A12
      'MODE_SWITCH_FLIPPED','MODE_SWITCH_ROLLED_BACK','RANKING_AUDIT_RESULT','SCHEMA_REJECTED','PRICELIST_PUBLISHED',
      -- backend additions (A2.12)
      'LOGIN_LOCKOUT','ACCOUNT_SUSPENDED','ACCOUNT_REINSTATED','ACCOUNT_CLOSED','CONSENT_GRANTED',
      'VENDOR_AGREEMENT_ACTIVATED','EXCLUSIVITY_WAIVED','ERASURE_EXECUTED','VERIFICATION_DECISION')),
  subject_type   TEXT NOT NULL,                         -- table name
  subject_id     TEXT NOT NULL,
  rule_ref       TEXT NULL,                             -- 'R-041' etc.
  metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_event_subject ON audit_event (subject_type, subject_id, occurred_at DESC);
CREATE INDEX ix_audit_event_action ON audit_event (action_code, occurred_at DESC);
CREATE TRIGGER trg_audit_event_append_only BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION rova_forbid_mutation();

-- state_transition: one row per applied state-machine transition, for every machine (A4).
CREATE TABLE state_transition (                         -- prefix stt_
  id             TEXT PRIMARY KEY,
  machine        TEXT NOT NULL CHECK (machine ~ '^SM-(0[1-9]|1[0-2]|2[0-4])$'),
  subject_type   TEXT NOT NULL,
  subject_id     TEXT NOT NULL,
  from_state     TEXT NULL,                             -- NULL on creation (initial state)
  to_state       TEXT NOT NULL,
  trigger        TEXT NOT NULL,                         -- trigger code from the machine table
  actor_user_id  TEXT NULL,
  actor_role     TEXT NOT NULL,
  notes          JSONB NOT NULL DEFAULT '{}'::jsonb,    -- guard evidence, reason codes
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_state_transition_subject ON state_transition (subject_type, subject_id, occurred_at);
CREATE TRIGGER trg_state_transition_append_only BEFORE UPDATE OR DELETE ON state_transition
  FOR EACH ROW EXECUTE FUNCTION rova_forbid_mutation();

-- idempotency_key: replay store for POSTs that carry Idempotency-Key (A15.3).
CREATE TABLE idempotency_key (
  key            TEXT NOT NULL,
  principal_id   TEXT NOT NULL,                         -- membership id or pms_link id
  request_hash   TEXT NOT NULL,                         -- sha256 of method+path+body
  status_code    INTEGER NOT NULL,
  response_body  JSONB NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (key, principal_id)
);

-- terms_version: a published version of terms the pharmacy/vendor must accept (SH-07). Needed by R-147, G7, R-161.
CREATE TABLE terms_version (                            -- prefix trm_
  id                            TEXT PRIMARY KEY,
  code                          TEXT NOT NULL CHECK (code IN ('PHARMACY_TERMS','VENDOR_TERMS','PMS_LINK_TERMS')),
  version                       INTEGER NOT NULL CHECK (version > 0),
  mentions_pharmacy_service_fee BOOLEAN NOT NULL DEFAULT false,   -- R-147
  service_fee_amount            NUMERIC(14,2) NULL,               -- the amount those terms name; NULL when not mentioned
  mentions_multi_vendor         BOOLEAN NOT NULL DEFAULT false,   -- G7
  mentions_pms_aggregation      BOOLEAN NOT NULL DEFAULT false,   -- R-161
  body_ref                      TEXT NOT NULL,                    -- storage path of the Portuguese text
  published_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_terms_version UNIQUE (code, version),
  CONSTRAINT ck_terms_fee_amount CHECK (mentions_pharmacy_service_fee = (service_fee_amount IS NOT NULL))
);

-- ============================================================================
-- 1. ACCOUNTS AND IDENTITY
-- ============================================================================

-- organisation (E-001). The platform is NOT an organisation (no 'PLATFORM' value — makes Invoice.issuer = platform impossible).
CREATE TABLE organisation (                             -- prefix org_
  id                     TEXT PRIMARY KEY,
  tax_id                 TEXT NOT NULL,                 -- NUIT
  legal_name             TEXT NOT NULL,
  legal_name_normalised  TEXT NOT NULL,                 -- lower, diacritics folded, spaces collapsed — R-107 conflict check
  type                   TEXT NOT NULL CHECK (type IN ('PHARMACY','VENDOR','TRANSPORTER')),
  country                TEXT NOT NULL DEFAULT 'MZ',
  status                 TEXT NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED','CLOSED')) DEFAULT 'ACTIVE',
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_organisation_tax UNIQUE (country, tax_id)
);
CREATE INDEX ix_organisation_name ON organisation (legal_name_normalised);

-- app_user (E-005). Personal data lives here and only here (phone, name) — A3.4 privacy boundary.
CREATE TABLE app_user (                                 -- prefix usr_
  id                  TEXT PRIMARY KEY,
  phone               TEXT NOT NULL,                    -- E.164; after erasure: 'erased:<id>'
  name                TEXT NOT NULL,                    -- after erasure: 'ERASED'
  locale              TEXT NOT NULL CHECK (locale IN ('pt','ar')) DEFAULT 'pt',
  password_hash       TEXT NOT NULL,                    -- argon2id
  failed_login_count  INTEGER NOT NULL DEFAULT 0,
  locked_until        TIMESTAMPTZ NULL,                 -- CFG-LOGIN-LOCKOUT-ATTEMPTS
  erased_at           TIMESTAMPTZ NULL,                 -- R-120 erasure executed
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_app_user_phone UNIQUE (phone)
);
ALTER TABLE audit_event ADD CONSTRAINT fk_audit_event_actor FOREIGN KEY (actor_user_id) REFERENCES app_user(id);
ALTER TABLE state_transition ADD CONSTRAINT fk_state_transition_actor FOREIGN KEY (actor_user_id) REFERENCES app_user(id);

-- membership (E-007). organisation_id NULL = platform staff. AssistedPharmacy has NO membership (flag on pharmacy_account).
CREATE TABLE membership (                               -- prefix mem_
  id               TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL REFERENCES app_user(id),
  organisation_id  TEXT NULL REFERENCES organisation(id),
  role_codes       TEXT[] NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED')) DEFAULT 'ACTIVE',
  revoked_at       TIMESTAMPTZ NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_membership_user_org UNIQUE (user_id, organisation_id),
  CONSTRAINT ck_membership_roles_known CHECK (role_codes <@ ARRAY[
      'PharmacyAdmin','PharmacyBuyer','PharmacyReceiver',
      'VendorAdmin','VendorOrderDesk','VendorPicker','VendorFinance',
      'Courier','Dispatcher',
      'OpsReviewer','IndexPharmacist','ComplianceOfficer','PlatformAdmin','SupportAgent','PlatformFinance']::text[]
      AND cardinality(role_codes) > 0),
  -- platform roles only without an organisation; organisation roles only with one (§3)
  CONSTRAINT ck_membership_platform_vs_org CHECK (
      (organisation_id IS NULL AND role_codes <@ ARRAY['OpsReviewer','IndexPharmacist','ComplianceOfficer','PlatformAdmin','SupportAgent','PlatformFinance']::text[])
   OR (organisation_id IS NOT NULL AND NOT (role_codes && ARRAY['OpsReviewer','IndexPharmacist','ComplianceOfficer','PlatformAdmin','SupportAgent','PlatformFinance']::text[])))
);
CREATE INDEX ix_membership_org ON membership (organisation_id) WHERE status = 'ACTIVE';

-- auth_session: refresh-token record; one row per device login (A9).
CREATE TABLE auth_session (                             -- prefix ses_
  id                  TEXT PRIMARY KEY,
  user_id             TEXT NOT NULL REFERENCES app_user(id),
  membership_id       TEXT NULL REFERENCES membership(id),   -- NULL until a membership is selected
  surface             TEXT NOT NULL CHECK (surface IN ('PH','VN','VW','CR','OP')),
  refresh_token_hash  TEXT NOT NULL,
  issued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at          TIMESTAMPTZ NOT NULL,             -- idle timeout per surface, CFG-SESSION-*
  revoked_at          TIMESTAMPTZ NULL,
  CONSTRAINT uq_auth_session_token UNIQUE (refresh_token_hash)
);
CREATE INDEX ix_auth_session_user ON auth_session (user_id) WHERE revoked_at IS NULL;

-- pharmacy_account (E-002)
CREATE TABLE pharmacy_account (                         -- prefix pha_
  id                          TEXT PRIMARY KEY,
  organisation_id             TEXT NOT NULL REFERENCES organisation(id),
  region_code                 TEXT NOT NULL REFERENCES region(code),
  licence_type                TEXT NOT NULL CHECK (licence_type IN ('A','B','C','POSTO_DE_VENDA','HEALTH_UNIT')),   -- R-113
  trade_name                  TEXT NOT NULL,            -- 'Farmácia Polana'
  address                     TEXT NOT NULL,
  latitude                    NUMERIC(9,6) NOT NULL,
  longitude                   NUMERIC(9,6) NOT NULL,
  status                      TEXT NOT NULL CHECK (status IN ('ONBOARDING','ACTIVE','LICENCE_EXPIRING','SUSPENDED','REJECTED','CLOSED')) DEFAULT 'ONBOARDING',  -- SM-07
  suspension_cause            TEXT NULL CHECK (suspension_cause IN ('LICENCE_EXPIRED','HEALTH_SCORE','MANUAL_INCIDENT')),
  auto_reroute                BOOLEAN NOT NULL DEFAULT false,
  buyer_approval_threshold    NUMERIC(14,2) NULL CHECK (buyer_approval_threshold > 0),   -- R-122; NULL = no gate
  default_allocation_strategy TEXT NULL CHECK (default_allocation_strategy IN ('FEWEST_VENDORS','FASTEST_DISPATCH','BEST_TERMS')),  -- R-024
  is_assisted                 BOOLEAN NOT NULL DEFAULT false,    -- AssistedPharmacy: no logins, OpsReviewer acts
  assisted_channel_ref        TEXT NULL,                -- WhatsApp number / phone used for confirmations when is_assisted
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_pharmacy_org UNIQUE (organisation_id),
  CONSTRAINT ck_pharmacy_suspension CHECK ((status = 'SUSPENDED') = (suspension_cause IS NOT NULL))
);
CREATE INDEX ix_pharmacy_region_status ON pharmacy_account (region_code, status);

-- vendor_account (E-003, extended §A12: locale). fee_agreement_id is gone: vendor_agreement.vendor_id is UNIQUE instead.
CREATE TABLE vendor_account (                           -- prefix ven_
  id                    TEXT PRIMARY KEY,
  organisation_id       TEXT NOT NULL REFERENCES organisation(id),
  region_code           TEXT NOT NULL REFERENCES region(code),
  trade_name            TEXT NOT NULL,                  -- 'Ahmed Distribuição'
  vendor_type           TEXT NOT NULL CHECK (vendor_type IN ('IMPORTER_WHOLESALER','DISTRIBUTOR')),
  delivery_mode         TEXT NOT NULL CHECK (delivery_mode IN ('VENDOR_OWN_FLEET','LICENSED_TRANSPORTER','PLATFORM_COORDINATED_COURIER')),
  mov_amount            NUMERIC(14,2) NOT NULL CHECK (mov_amount >= 0),   -- vendor-declared; no platform default (OD-13)
  acceptance_mode       TEXT NOT NULL CHECK (acceptance_mode IN ('MANUAL_CONFIRM','AUTO_ACCEPT_FULL')) DEFAULT 'AUTO_ACCEPT_FULL',  -- R-128
  sourcing_attestation  BOOLEAN NOT NULL DEFAULT false, -- Art. 40, R-112 (audited on change)
  locale                TEXT NOT NULL CHECK (locale IN ('pt','ar')) DEFAULT 'pt',   -- OD-146
  status                TEXT NOT NULL CHECK (status IN ('ONBOARDING','ACTIVE','LICENCE_EXPIRING','SUSPENDED','REJECTED','CLOSED')) DEFAULT 'ONBOARDING',  -- SM-06
  suspension_cause      TEXT NULL CHECK (suspension_cause IN ('LICENCE_EXPIRED','HEALTH_SCORE','MANUAL_INCIDENT')),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_vendor_org UNIQUE (organisation_id),
  CONSTRAINT ck_vendor_suspension CHECK ((status = 'SUSPENDED') = (suspension_cause IS NOT NULL))
);
CREATE INDEX ix_vendor_status ON vendor_account (status);

-- transporter_account (E-004)
CREATE TABLE transporter_account (                      -- prefix trn_
  id               TEXT PRIMARY KEY,
  organisation_id  TEXT NULL REFERENCES organisation(id),   -- NULL when platform-coordinated pool
  name             TEXT NOT NULL,
  pool_type        TEXT NOT NULL CHECK (pool_type IN ('LICENSED_CARRIER','PLATFORM_COORDINATED_POOL')),
  licence_id       TEXT NULL,                           -- FK added after licence (OD-03)
  status           TEXT NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')) DEFAULT 'ACTIVE',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_transporter_org CHECK ((pool_type = 'PLATFORM_COORDINATED_POOL') = (organisation_id IS NULL))
);

-- licence (E-008). Renewal = new row linked by successor_id; never edit dates.
CREATE TABLE licence (                                  -- prefix lic_
  id             TEXT PRIMARY KEY,
  holder_type    TEXT NOT NULL CHECK (holder_type IN ('VENDOR','PHARMACY','TRANSPORTER')),
  holder_id      TEXT NOT NULL,                         -- vendor_account.id | pharmacy_account.id | transporter_account.id (polymorphic; app-checked)
  type           TEXT NOT NULL CHECK (type IN ('WHOLESALE_ALVARA','RETAIL_A','RETAIL_B','RETAIL_C','POSTO_DE_VENDA','TRANSPORTER_LICENCE')),
  number         TEXT NOT NULL,
  issuer         TEXT NOT NULL DEFAULT 'ANARME',
  issue_date     DATE NOT NULL,
  expiry_date    DATE NOT NULL CHECK (expiry_date > issue_date),
  document_ref   TEXT NOT NULL,                         -- storage path of the scan
  status         TEXT NOT NULL CHECK (status IN ('SUBMITTED','UNDER_REVIEW','VALID','EXPIRING_SOON','REJECTED','RENEWED','EXPIRED')) DEFAULT 'SUBMITTED',  -- SM-08
  successor_id   TEXT NULL REFERENCES licence(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_licence_holder ON licence (holder_type, holder_id, status);
CREATE INDEX ix_licence_expiry ON licence (expiry_date) WHERE status IN ('VALID','EXPIRING_SOON');
ALTER TABLE transporter_account ADD CONSTRAINT fk_transporter_licence FOREIGN KEY (licence_id) REFERENCES licence(id);

-- verification_case (E-009)
CREATE TABLE verification_case (                        -- prefix vcs_
  id               TEXT PRIMARY KEY,
  subject_type     TEXT NOT NULL CHECK (subject_type IN ('VENDOR_ONBOARDING','PHARMACY_ONBOARDING','LICENCE_RENEWAL','CONFLICT_CHECK')),
  organisation_id  TEXT NOT NULL REFERENCES organisation(id),
  licence_id       TEXT NULL REFERENCES licence(id),
  reviewer_user_id TEXT NULL REFERENCES app_user(id),
  decision         TEXT NOT NULL CHECK (decision IN ('PENDING','APPROVED','REJECTED','RESUBMISSION_REQUESTED')) DEFAULT 'PENDING',
  decision_notes   TEXT NULL,
  opened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at       TIMESTAMPTZ NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_verification_decided CHECK ((decision = 'PENDING') = (decided_at IS NULL))
);
CREATE INDEX ix_verification_pending ON verification_case (opened_at) WHERE decision = 'PENDING';   -- R-123

-- vendor_agreement (E-061)
CREATE TABLE vendor_agreement (                         -- prefix vag_
  id                       TEXT PRIMARY KEY,
  vendor_id                TEXT NOT NULL REFERENCES vendor_account(id),
  signed_at                TIMESTAMPTZ NOT NULL,
  document_ref             TEXT NOT NULL,
  founding_supplier        BOOLEAN NOT NULL DEFAULT false,
  multi_vendor_clause_ack  BOOLEAN NOT NULL DEFAULT false,   -- R-143, G2
  exclusivity_until        DATE NULL,                   -- G3
  preferential_rate_until  DATE NULL,                   -- R-143 auto-revert
  badge_until              DATE NULL,
  waived_by_user_id        TEXT NULL REFERENCES app_user(id),
  waived_at                TIMESTAMPTZ NULL,
  status                   TEXT NOT NULL CHECK (status IN ('DRAFT','ACTIVE','EXPIRED','TERMINATED')) DEFAULT 'DRAFT',
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_vendor_agreement_vendor UNIQUE (vendor_id)          -- 1:1 per addendum
);

-- vendor_pharmacy_relationship (E-062): dated snapshot, immutable after declared_at (app enforces no UPDATE except nothing).
CREATE TABLE vendor_pharmacy_relationship (             -- prefix vpr_
  id            TEXT PRIMARY KEY,
  vendor_id     TEXT NOT NULL REFERENCES vendor_account(id),
  pharmacy_id   TEXT NOT NULL REFERENCES pharmacy_account(id),
  declared_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,                   -- declared_at + CFG-PREEXISTING-RELATIONSHIP-MONTHS
  evidence_ref  TEXT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_vpr UNIQUE (vendor_id, pharmacy_id)
);

-- consent_record (E-050). subject = the pharmacy (terms) or a user (personal-data purposes).
CREATE TABLE consent_record (                           -- prefix cns_
  id                  TEXT PRIMARY KEY,
  subject_type        TEXT NOT NULL CHECK (subject_type IN ('USER','PHARMACY_ACCOUNT','VENDOR_ACCOUNT')),
  subject_id          TEXT NOT NULL,
  purpose             TEXT NOT NULL,                    -- 'PHARMACY_TERMS', 'PMS_LINK', 'VENDOR_TERMS', 'CONTACT_PROCESSING'
  terms_version_id    TEXT NULL REFERENCES terms_version(id),
  granted_by_user_id  TEXT NULL REFERENCES app_user(id),     -- NULL for assisted pharmacies (OpsReviewer records channel ref)
  channel_ref         TEXT NULL,                        -- message id / call ref for assisted consent
  granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  withdrawn_at        TIMESTAMPTZ NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_consent_subject ON consent_record (subject_type, subject_id, purpose) WHERE withdrawn_at IS NULL;

-- data_subject_request (E-051)
CREATE TABLE data_subject_request (                     -- prefix dsr_
  id                  TEXT PRIMARY KEY,
  subject_user_id     TEXT NOT NULL REFERENCES app_user(id),
  type                TEXT NOT NULL CHECK (type IN ('ACCESS','EXPORT','ERASURE')),
  status              TEXT NOT NULL CHECK (status IN ('RECEIVED','IN_PROGRESS','FULFILLED','REJECTED')) DEFAULT 'RECEIVED',
  handled_by_user_id  TEXT NULL REFERENCES app_user(id),     -- ComplianceOfficer
  result_ref          TEXT NULL,                        -- export file path, or erasure summary
  received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  fulfilled_at        TIMESTAMPTZ NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 2. CATALOGUE (mirror of the governed Index)
-- ============================================================================

-- index_product (E-010, E-073 mirror). regulated_price is immutable once referenced: children FK to (id, regulated_price) with no cascade.
CREATE TABLE index_product (                            -- prefix idx_
  id                 TEXT PRIMARY KEY,
  inn                TEXT NOT NULL,
  brand_name         TEXT NULL,
  form               TEXT NOT NULL,                     -- 'comprimido', 'cápsula', 'xarope' …
  strength           TEXT NOT NULL,
  pack_size          TEXT NOT NULL,                     -- '20', '100', '1' — text as in the spec
  manufacturer       TEXT NOT NULL,
  barcode            TEXT NULL,
  therapeutic_class  TEXT NULL,                         -- ATC level 3 when the Index supplies it
  aim_status         TEXT NOT NULL CHECK (aim_status IN ('AUTHORISED','NOT_AUTHORISED','PENDING')),
  regulated_price    BOOLEAN NOT NULL,                  -- R-007
  review_status      TEXT NOT NULL CHECK (review_status IN ('DRAFT','PUBLISHED','RETIRED')) DEFAULT 'DRAFT',
  reviewer_ref       TEXT NOT NULL,                     -- Index-side approver id (R-166)
  search_text        TEXT NOT NULL,                     -- normalised concat of inn/brand/form/strength for LIKE search (A6.4)
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_index_product_id_regulated UNIQUE (id, regulated_price),
  CONSTRAINT ck_index_published_authorised CHECK (review_status <> 'PUBLISHED' OR aim_status = 'AUTHORISED')   -- R-001
);
CREATE INDEX ix_index_product_search ON index_product (search_text text_pattern_ops) WHERE review_status = 'PUBLISHED';
CREATE INDEX ix_index_product_barcode ON index_product (barcode) WHERE barcode IS NOT NULL;

-- product_alias (E-011). Approval happens in the Index; the platform mirrors and proposes (§A10).
CREATE TABLE product_alias (                            -- prefix als_
  id                TEXT PRIMARY KEY,
  index_product_id  TEXT NOT NULL REFERENCES index_product(id),
  alias_text        TEXT NOT NULL,
  alias_normalised  TEXT NOT NULL,
  alias_type        TEXT NOT NULL CHECK (alias_type IN ('COLLOQUIAL','MISSPELLING','BRAND','ABBREVIATION')),
  status            TEXT NOT NULL CHECK (status IN ('PROPOSED','ACTIVE','RETIRED')) DEFAULT 'PROPOSED',   -- R-003: only ACTIVE participates in matching
  approved_by_ref   TEXT NULL,                          -- Index approver; NOT NULL when ACTIVE (app-checked)
  proposal_count    INTEGER NOT NULL DEFAULT 1,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_product_alias UNIQUE (index_product_id, alias_normalised)
);
CREATE INDEX ix_product_alias_norm ON product_alias (alias_normalised) WHERE status = 'ACTIVE';

-- substitution (E-012)
CREATE TABLE substitution (                             -- prefix sub_
  id                     TEXT PRIMARY KEY,
  source_product_id      TEXT NOT NULL REFERENCES index_product(id),
  substitute_product_id  TEXT NOT NULL REFERENCES index_product(id),
  rationale              TEXT NOT NULL,
  reviewer_ref           TEXT NOT NULL,
  status                 TEXT NOT NULL CHECK (status IN ('ACTIVE','RETIRED')) DEFAULT 'ACTIVE',
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_substitution_distinct CHECK (source_product_id <> substitute_product_id),
  CONSTRAINT uq_substitution UNIQUE (source_product_id, substitute_product_id)
);

-- price_reference (E-013): versioned, never edited; new row + superseded_by (R-006).
CREATE TABLE price_reference (                          -- prefix prf_
  id                       TEXT PRIMARY KEY,
  index_product_id         TEXT NOT NULL REFERENCES index_product(id),
  pvp_price                NUMERIC(14,2) NOT NULL CHECK (pvp_price >= 0),
  wholesale_derived_price  NUMERIC(14,2) NOT NULL CHECK (wholesale_derived_price >= 0),   -- R-008, R-012: captured, not computed
  cif_basis                NUMERIC(14,2) NULL CHECK (cif_basis >= 0),
  effective_from           DATE NOT NULL,
  source_document          TEXT NOT NULL,               -- 'Diploma Ministerial 21/2017 …' or 'SEED-FIXTURE: illustrative, not an official value'
  superseded_by            TEXT NULL REFERENCES price_reference(id),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_price_reference_product ON price_reference (index_product_id, effective_from DESC);

-- external_ref (E-073): identity bridge to Index and PMS.
CREATE TABLE external_ref (                             -- prefix xrf_
  id              TEXT PRIMARY KEY,
  entity_type     TEXT NOT NULL CHECK (entity_type IN ('INDEX_PRODUCT','SUBSTITUTION','PRODUCT_ALIAS','PHARMACY_ACCOUNT','REQUEST','ORDER','RECEIPT')),
  local_id        TEXT NOT NULL,
  system          TEXT NOT NULL CHECK (system IN ('INDEX','PMS')),
  external_id     TEXT NOT NULL,
  mapping_source  TEXT NOT NULL CHECK (mapping_source IN ('PUSHED_BY_SYSTEM','MATCHED_ON_SYNC','MANUAL')),
  mapped_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_external_ref_ext UNIQUE (system, entity_type, external_id),
  CONSTRAINT uq_external_ref_local UNIQUE (system, entity_type, local_id)
);

-- vendor_offer (E-014). THE load-bearing constraint: regulated products cannot carry a price.
CREATE TABLE vendor_offer (                             -- prefix ofr_
  id                   TEXT PRIMARY KEY,
  vendor_id            TEXT NOT NULL REFERENCES vendor_account(id),
  index_product_id     TEXT NOT NULL,
  regulated_price      BOOLEAN NOT NULL,                -- copied from the product and locked by the composite FK below
  qty_available        INTEGER NOT NULL CHECK (qty_available >= 0),
  pack_size            TEXT NOT NULL,
  min_order_qty        INTEGER NULL CHECK (min_order_qty > 0),
  expiry_horizon_days  INTEGER NOT NULL CHECK (expiry_horizon_days >= 0),
  price                NUMERIC(14,2) NULL CHECK (price >= 0),
  stock_confirmed_at   TIMESTAMPTZ NOT NULL,
  freshness_state      TEXT NOT NULL CHECK (freshness_state IN ('FRESH','STALE','WITHDRAWN')) DEFAULT 'FRESH',   -- SM-12
  withdrawn_at         TIMESTAMPTZ NULL,
  source_row_id        TEXT NULL,                       -- price_list_row that created/last updated it; FK added later
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_vendor_offer_product_regulated FOREIGN KEY (index_product_id, regulated_price)
      REFERENCES index_product (id, regulated_price),   -- no ON UPDATE CASCADE: flipping regulated_price is impossible while offers exist
  CONSTRAINT ck_vendor_offer_regulated_no_price CHECK (regulated_price = false OR price IS NULL),   -- R-009
  CONSTRAINT ck_vendor_offer_free_has_price CHECK (regulated_price = true OR freshness_state = 'WITHDRAWN' OR price IS NOT NULL)   -- R-010
);
-- one live offer per vendor × product (withdrawn rows are history)
CREATE UNIQUE INDEX uq_vendor_offer_live ON vendor_offer (vendor_id, index_product_id) WHERE freshness_state <> 'WITHDRAWN';
CREATE INDEX ix_vendor_offer_product_fresh ON vendor_offer (index_product_id) WHERE freshness_state = 'FRESH';   -- ranking candidate fetch
CREATE INDEX ix_vendor_offer_stale_scan ON vendor_offer (stock_confirmed_at) WHERE freshness_state = 'FRESH';   -- freshness job

-- ============================================================================
-- 3. VENDOR PRICE LISTS (§A6)
-- ============================================================================

CREATE TABLE vendor_column_mapping (                    -- prefix vcm_   (E-065)
  id                 TEXT PRIMARY KEY,
  vendor_id          TEXT NOT NULL REFERENCES vendor_account(id),
  fingerprint        TEXT NOT NULL,                     -- sha256(normalised header row + column count), R-155
  mapping            JSONB NOT NULL,                    -- {"canonical_field": "<file column header>" | "UNMAPPED"} for every canonical field
  sheet_selector     TEXT NULL,
  header_row_index   INTEGER NOT NULL CHECK (header_row_index >= 0),
  unit_conventions   JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {"decimal_separator": ",", "date_format": "DD/MM/YYYY"}
  status             TEXT NOT NULL CHECK (status IN ('ACTIVE','EXPIRED')) DEFAULT 'ACTIVE',
  created_by_user_id TEXT NOT NULL REFERENCES app_user(id),
  last_used_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_vcm_active_fingerprint ON vendor_column_mapping (vendor_id, fingerprint) WHERE status = 'ACTIVE';

CREATE TABLE price_list_version (                       -- prefix plv_   (E-066)
  id                 TEXT PRIMARY KEY,
  vendor_id          TEXT NOT NULL REFERENCES vendor_account(id),
  version_number     INTEGER NOT NULL CHECK (version_number > 0),
  source_file_ref    TEXT NOT NULL,                     -- immutable stored original
  source_file_name   TEXT NOT NULL,
  fingerprint        TEXT NOT NULL,
  mapping_id         TEXT NULL REFERENCES vendor_column_mapping(id),   -- NULL while MAPPING_NEEDED
  uploaded_by_user_id TEXT NOT NULL REFERENCES app_user(id),
  uploaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  partial_update     BOOLEAN NOT NULL DEFAULT false,    -- R-156
  qty_only           BOOLEAN NOT NULL DEFAULT false,    -- zero grace when the file carries quantities only (§A6)
  effective_from     TIMESTAMPTZ NULL,                  -- set on VALIDATED→SCHEDULED
  status             TEXT NOT NULL CHECK (status IN ('UPLOADED','MAPPING_NEEDED','VALIDATING','VALIDATED','SCHEDULED','LIVE','SUPERSEDED','REJECTED')) DEFAULT 'UPLOADED',  -- SM-21
  row_count          INTEGER NOT NULL DEFAULT 0,
  accepted_rows      INTEGER NOT NULL DEFAULT 0,
  rejected_rows      INTEGER NOT NULL DEFAULT 0,
  warning_rows       INTEGER NOT NULL DEFAULT 0,
  pending_rows       INTEGER NOT NULL DEFAULT 0,
  supersedes_id      TEXT NULL REFERENCES price_list_version(id),
  report_ref         TEXT NULL,                         -- xlsx report path once VALIDATED/REJECTED
  went_live_at       TIMESTAMPTZ NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_plv_vendor_version UNIQUE (vendor_id, version_number)
);
CREATE UNIQUE INDEX uq_plv_one_live ON price_list_version (vendor_id) WHERE status = 'LIVE';
CREATE INDEX ix_plv_scheduled ON price_list_version (effective_from) WHERE status = 'SCHEDULED';

CREATE TABLE price_list_row (                           -- prefix plr_   (E-067)
  id                   TEXT PRIMARY KEY,
  version_id           TEXT NOT NULL REFERENCES price_list_version(id),
  row_index            INTEGER NOT NULL CHECK (row_index >= 0),   -- the vendor's own row number in the file
  raw_values           JSONB NOT NULL,                  -- the row exactly as received
  vendor_sku           TEXT NULL,
  index_product_id     TEXT NULL REFERENCES index_product(id),
  match_confidence     NUMERIC(5,4) NULL CHECK (match_confidence BETWEEN 0 AND 1),
  candidate_product_ids JSONB NOT NULL DEFAULT '[]'::jsonb,   -- top-3 when PENDING_REVIEW
  price_to_pharmacy    NUMERIC(14,2) NULL,
  price_to_public      NUMERIC(14,2) NULL,
  discount_pct         NUMERIC(7,4) NULL,
  vendor_cost          NUMERIC(14,2) NULL,              -- CONFIDENTIAL, R-146: never leaves vendor/PlatformFinance surfaces
  qty_available        INTEGER NULL,
  pack_size            TEXT NULL,
  expiry_horizon_days  INTEGER NULL,
  min_order_qty        INTEGER NULL,
  outcome              TEXT NOT NULL CHECK (outcome IN ('ACCEPTED','REJECTED','WARNING','PENDING_REVIEW')),
  outcome_reason       TEXT NULL CHECK (outcome_reason IN ('UNMATCHED_PRODUCT','AMBIGUOUS_MATCH','REGULATED_PRICE_DEVIATION',
                          'DISCOUNT_ON_REGULATED','NOT_AUTHORISED','MISSING_REQUIRED','INVALID_NUMBER','DUPLICATE_ROW',
                          'PACK_SIZE_MISMATCH','PVP_DEVIATION_WARNING','PRODUCT_NOT_PUBLISHED')),
  outcome_detail       TEXT NULL,                       -- Portuguese sentence for the report
  resolved_by_user_id  TEXT NULL REFERENCES app_user(id),
  resolved_at          TIMESTAMPTZ NULL,
  resulting_offer_id   TEXT NULL REFERENCES vendor_offer(id),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_plr_version_row UNIQUE (version_id, row_index)
);
CREATE INDEX ix_plr_version_outcome ON price_list_row (version_id, outcome);
ALTER TABLE vendor_offer ADD CONSTRAINT fk_vendor_offer_source_row FOREIGN KEY (source_row_id) REFERENCES price_list_row(id);

-- ============================================================================
-- 4. ORDERING
-- ============================================================================

CREATE SEQUENCE request_number_seq;

-- request (E-015)
CREATE TABLE request (                                  -- prefix req_
  id                   TEXT PRIMARY KEY,
  number               TEXT NOT NULL,                   -- 'RQ-2026-000123'
  pharmacy_id          TEXT NOT NULL REFERENCES pharmacy_account(id),
  mode                 TEXT NOT NULL CHECK (mode IN ('CATALOGUE','RFQ','ASSISTED')),
  channel              TEXT NOT NULL CHECK (channel IN ('APP','WHATSAPP_TEXT','VOICE_NOTE','PHOTO','FILE','PHONE_CALL','PMS')),   -- R-168
  status               TEXT NOT NULL CHECK (status IN ('DRAFT','AWAITING_ADMIN_APPROVAL','NORMALIZING','AWAITING_CONFIRMATION','CONFIRMED','IN_FULFILMENT','CLOSED','CANCELLED')),  -- SM-01
  allocation_strategy  TEXT NOT NULL CHECK (allocation_strategy IN ('FEWEST_VENDORS','FASTEST_DISPATCH','BEST_TERMS')),
  confirmation_ref     TEXT NULL,                       -- 'channel|iso-timestamp|message_ref' — R-115; NOT NULL once CONFIRMED (app-checked)
  confirmed_at         TIMESTAMPTZ NULL,
  created_by_user_id   TEXT NULL REFERENCES app_user(id),      -- NULL for assisted / PMS
  acting_ops_user_id   TEXT NULL REFERENCES app_user(id),      -- OpsReviewer acting for an assisted pharmacy
  pms_idempotency_key  TEXT NULL,                       -- R-168
  raw_payload_ref      TEXT NULL,                       -- ingestion source (text stored inline in JSONB below for text channels)
  raw_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_request_number UNIQUE (number),
  CONSTRAINT uq_request_pms_key UNIQUE (pharmacy_id, pms_idempotency_key)
);
CREATE INDEX ix_request_pharmacy ON request (pharmacy_id, created_at DESC);
CREATE INDEX ix_request_status ON request (status);

-- request_line (E-016). index_product_id is NULL only for unresolved ingestion lines.
CREATE TABLE request_line (                             -- prefix rql_
  id                     TEXT PRIMARY KEY,
  request_id             TEXT NOT NULL REFERENCES request(id) ON DELETE CASCADE,   -- cascade only reachable while DRAFT (A2.7)
  index_product_id       TEXT NULL REFERENCES index_product(id),
  qty_requested          INTEGER NOT NULL CHECK (qty_requested > 0),
  line_kind              TEXT NOT NULL CHECK (line_kind IN ('CATALOGUE','RFQ')) DEFAULT 'CATALOGUE',   -- R-033
  match_status           TEXT NOT NULL CHECK (match_status IN ('AUTO','ASK','UNRESOLVED','RESOLVED')) DEFAULT 'AUTO',  -- §9.1
  confidence             NUMERIC(5,4) NULL CHECK (confidence BETWEEN 0 AND 1),
  source_span            TEXT NULL,
  candidate_product_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
  substitution_id        TEXT NULL REFERENCES substitution(id),     -- R-004: explicit acceptance
  origin_line_id         TEXT NULL REFERENCES request_line(id),     -- remainder line created by reroute (SM-04)
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_request_line_resolved_has_product CHECK (match_status IN ('ASK','UNRESOLVED') OR index_product_id IS NOT NULL)
);
CREATE INDEX ix_request_line_request ON request_line (request_id);

-- quotation / quotation_line (E-017/E-018)
CREATE TABLE quotation (                                -- prefix quo_
  id             TEXT PRIMARY KEY,
  request_id     TEXT NOT NULL REFERENCES request(id),
  vendor_id      TEXT NOT NULL REFERENCES vendor_account(id),
  status         TEXT NOT NULL CHECK (status IN ('INVITED','SUBMITTED','ACCEPTED','DECLINED','EXPIRED')) DEFAULT 'INVITED',  -- SM-02
  invited_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitted_at   TIMESTAMPTZ NULL,
  expires_at     TIMESTAMPTZ NOT NULL,                  -- SlaTimer(QUOTATION)
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_quotation_request_vendor UNIQUE (request_id, vendor_id)
);
CREATE TABLE quotation_line (                           -- prefix qul_
  id                   TEXT PRIMARY KEY,
  quotation_id         TEXT NOT NULL REFERENCES quotation(id),
  request_line_id      TEXT NOT NULL REFERENCES request_line(id),
  index_product_id     TEXT NOT NULL,
  regulated_price      BOOLEAN NOT NULL,
  offered_qty          INTEGER NOT NULL CHECK (offered_qty >= 0),
  price                NUMERIC(14,2) NULL CHECK (price >= 0),
  expiry_horizon_days  INTEGER NOT NULL CHECK (expiry_horizon_days >= 0),
  accepted             BOOLEAN NOT NULL DEFAULT false,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_quotation_line_product_regulated FOREIGN KEY (index_product_id, regulated_price) REFERENCES index_product (id, regulated_price),
  CONSTRAINT ck_quotation_line_regulated_no_price CHECK (regulated_price = false OR price IS NULL),   -- E-018, R-009
  CONSTRAINT uq_quotation_line UNIQUE (quotation_id, request_line_id)
);

-- "order" (E-020, extended §A7/§A3). pharmacy_id denormalised for isolation.
CREATE TABLE "order" (                                  -- prefix ord_
  id                       TEXT PRIMARY KEY,
  number                   TEXT NOT NULL,               -- 'RQ-2026-000123-V01'
  request_id               TEXT NOT NULL REFERENCES request(id),
  vendor_id                TEXT NOT NULL REFERENCES vendor_account(id),
  pharmacy_id              TEXT NOT NULL REFERENCES pharmacy_account(id),
  status                   TEXT NOT NULL CHECK (status IN ('PENDING_ACCEPTANCE','ACCEPTED','REJECTED','DISPATCHED','DELIVERED_PENDING_RECEIPT',
                               'RECEIPT_ACCEPTED','DISPUTED','RETURN_IN_PROGRESS','CANCELLED','CLOSED')) DEFAULT 'PENDING_ACCEPTANCE',  -- SM-03
  cancel_reason            TEXT NULL CHECK (cancel_reason IN ('PHARMACY','ACCEPTANCE_TIMEOUT','DISPATCH_TIMEOUT','DELIVERY_EXHAUSTED',
                               'LICENCE_REVOKED','REGULATED_PRICE_INCIDENT','COUNTERFEIT_SUSPECTED','NOTHING_TO_DISPATCH')),
  payment_terms            TEXT NOT NULL CHECK (payment_terms IN ('UPFRONT','COD','CREDIT_N_DAYS')),
  credit_days              INTEGER NULL CHECK (credit_days > 0),
  delivery_mode            TEXT NOT NULL CHECK (delivery_mode IN ('VENDOR_OWN_FLEET','LICENSED_TRANSPORTER','PLATFORM_COORDINATED_COURIER')),
  accepted_at              TIMESTAMPTZ NULL,
  promised_dispatch_at     TIMESTAMPTZ NULL,            -- §A7
  dispatched_at            TIMESTAMPTZ NULL,
  credit_override_user_id  TEXT NULL REFERENCES app_user(id),   -- R-041
  credit_override_reason   TEXT NULL,
  compliance_flag          BOOLEAN NOT NULL DEFAULT false,      -- continued after a party was suspended (R-108/R-110)
  goods_total              NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (goods_total >= 0),   -- Σ order_line.unit_price × confirmed_qty (ordered_qty before acceptance); NEVER includes any fee (R-097)
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_order_number UNIQUE (number),
  CONSTRAINT ck_order_cancel_reason CHECK ((status = 'CANCELLED') = (cancel_reason IS NOT NULL)),
  CONSTRAINT ck_order_credit_days CHECK ((payment_terms = 'CREDIT_N_DAYS') = (credit_days IS NOT NULL))
);
CREATE INDEX ix_order_vendor_status ON "order" (vendor_id, status);
CREATE INDEX ix_order_pharmacy_status ON "order" (pharmacy_id, status);
CREATE INDEX ix_order_request ON "order" (request_id);

-- order_line (E-021). Price is frozen here at confirmation (R-157); source is structurally tied to regulated_price.
CREATE TABLE order_line (                               -- prefix orl_
  id                 TEXT PRIMARY KEY,
  order_id           TEXT NOT NULL REFERENCES "order"(id),
  request_line_id    TEXT NOT NULL REFERENCES request_line(id),
  index_product_id   TEXT NOT NULL,
  regulated_price    BOOLEAN NOT NULL,
  ordered_qty        INTEGER NOT NULL CHECK (ordered_qty > 0),
  confirmed_qty      INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_qty >= 0),
  unit_price         NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0),
  price_source       TEXT NOT NULL CHECK (price_source IN ('PRICE_REFERENCE','VENDOR_OFFER','QUOTATION')),
  price_source_id    TEXT NOT NULL,                     -- price_reference.id | vendor_offer.id | quotation_line.id
  fulfilment_status  TEXT NOT NULL CHECK (fulfilment_status IN ('LINE_PENDING','LINE_FULL','LINE_SHORT','LINE_REROUTED','LINE_DROPPED')) DEFAULT 'LINE_PENDING',  -- SM-04
  short_reason       TEXT NULL CHECK (short_reason IN ('PARTIAL_ACCEPTANCE','QTY_CORRECTION','LICENCE_LAPSE','COMPLIANCE_HALT')),
  batch_number       TEXT NULL,
  lot_number         TEXT NULL,
  expiry_date        DATE NULL,
  seal_ids           TEXT[] NOT NULL DEFAULT '{}',      -- Track & Trace seals, set at dispatch (R-054)
  near_expiry_ack_user_id TEXT NULL REFERENCES app_user(id),   -- R-059
  picked_at          TIMESTAMPTZ NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_order_line_product_regulated FOREIGN KEY (index_product_id, regulated_price) REFERENCES index_product (id, regulated_price),
  CONSTRAINT ck_order_line_price_source CHECK (
      (regulated_price = true  AND price_source = 'PRICE_REFERENCE') OR
      (regulated_price = false AND price_source IN ('VENDOR_OFFER','QUOTATION'))),   -- R-008/R-011: a regulated line's price can only come from PriceReference
  CONSTRAINT ck_order_line_confirmed_le_ordered CHECK (confirmed_qty <= ordered_qty),   -- R-053
  CONSTRAINT uq_order_line_request_line UNIQUE (request_line_id)   -- a request line lands on exactly one order line (remainders are new request lines)
);
CREATE INDEX ix_order_line_order ON order_line (order_id);

-- allocation (E-019, extended R-151/R-153)
CREATE TABLE allocation (                               -- prefix alc_
  id                   TEXT PRIMARY KEY,
  request_line_id      TEXT NOT NULL REFERENCES request_line(id),
  vendor_id            TEXT NOT NULL REFERENCES vendor_account(id),
  order_line_id        TEXT NULL REFERENCES order_line(id),
  reason_code          TEXT NOT NULL CHECK (reason_code IN ('RANKING_DEFAULT','PHARMACY_OVERRIDE','AUTO_REROUTE','MANUAL_OPS')),
  override_reason      TEXT NULL,                       -- free text, required for PHARMACY_OVERRIDE (R-025)
  ops_override_code    TEXT NULL CHECK (ops_override_code IN ('PHARMACY_REQUESTED_BY_PHONE','OFFER_DATA_ERROR','COMPLIANCE_INSTRUCTION')),  -- R-153
  strategy             TEXT NOT NULL CHECK (strategy IN ('FEWEST_VENDORS','FASTEST_DISPATCH','BEST_TERMS')),
  sequence_snapshot    JSONB NOT NULL,                  -- ordered candidates with the 9 factor values (R-023)
  inputs_fingerprint   TEXT NOT NULL,                   -- sha256 of the RankingInput actually used (R-151)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_allocation_override_reason CHECK (reason_code <> 'PHARMACY_OVERRIDE' OR override_reason IS NOT NULL),
  CONSTRAINT ck_allocation_ops_code CHECK ((reason_code = 'MANUAL_OPS') = (ops_override_code IS NOT NULL))
);
CREATE INDEX ix_allocation_line ON allocation (request_line_id);
CREATE INDEX ix_allocation_vendor_time ON allocation (vendor_id, created_at DESC);

-- demand_gap (E-063)
CREATE TABLE demand_gap (                               -- prefix dgp_
  id                  TEXT PRIMARY KEY,
  index_product_id    TEXT NULL REFERENCES index_product(id),    -- NULL when cause = PRODUCT_NOT_IN_INDEX
  raw_product_text    TEXT NULL,                        -- what the pharmacy asked for, when not in the index
  pharmacy_id         TEXT NOT NULL REFERENCES pharmacy_account(id),
  region_code         TEXT NOT NULL REFERENCES region(code),
  qty_requested       INTEGER NOT NULL CHECK (qty_requested > 0),
  cause               TEXT NOT NULL CHECK (cause IN ('NO_FRESH_OFFER','RFQ_UNANSWERED','LINE_DROPPED_AFTER_SHORT','PRODUCT_NOT_IN_INDEX')),
  request_line_id     TEXT NULL REFERENCES request_line(id),
  occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_by_offer_id TEXT NULL REFERENCES vendor_offer(id),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_demand_gap_product CHECK ((cause = 'PRODUCT_NOT_IN_INDEX') = (index_product_id IS NULL))
);
CREATE INDEX ix_demand_gap_product_region ON demand_gap (index_product_id, region_code, occurred_at DESC);

-- ============================================================================
-- 5. FULFILMENT
-- ============================================================================

CREATE TABLE delivery_job (                             -- prefix dlv_   (E-029)
  id              TEXT PRIMARY KEY,
  order_id        TEXT NOT NULL REFERENCES "order"(id),
  vendor_id       TEXT NOT NULL REFERENCES vendor_account(id),
  pharmacy_id     TEXT NOT NULL REFERENCES pharmacy_account(id),
  performed_by    TEXT NOT NULL CHECK (performed_by IN ('VENDOR_OWN_FLEET','LICENSED_TRANSPORTER','PLATFORM_COORDINATED_COURIER')),
  transporter_id  TEXT NULL REFERENCES transporter_account(id),
  courier_user_id TEXT NULL REFERENCES app_user(id),
  status          TEXT NOT NULL CHECK (status IN ('CREATED','ASSIGNED','IN_TRANSIT','DELIVERED','FAILED_RETRY_PENDING','RETURNED_TO_VENDOR','CANCELLED')) DEFAULT 'CREATED',  -- SM-05
  attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  fee_event_id    TEXT NULL,                            -- delivery fee event (R-099); FK added after fee_event
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_delivery_job_order ON delivery_job (order_id);
CREATE INDEX ix_delivery_job_status ON delivery_job (status);

CREATE TABLE delivery_attempt (                         -- prefix dla_   (E-030 + folded E-031)
  id                      TEXT PRIMARY KEY,
  delivery_job_id         TEXT NOT NULL REFERENCES delivery_job(id),
  attempt_no              INTEGER NOT NULL CHECK (attempt_no > 0),
  attempted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome                 TEXT NOT NULL CHECK (outcome IN ('DELIVERED','CLOSED','REFUSED','WRONG_ADDRESS','NO_AUTHORISED_RECEIVER')),   -- R-065
  courier_user_id         TEXT NOT NULL REFERENCES app_user(id),
  proof_signature_or_code TEXT NULL,
  proof_photo_ref         TEXT NULL,
  proof_captured_at       TIMESTAMPTZ NULL,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_delivery_attempt_no UNIQUE (delivery_job_id, attempt_no),
  CONSTRAINT ck_delivered_needs_proof CHECK (outcome <> 'DELIVERED' OR (proof_captured_at IS NOT NULL AND (proof_signature_or_code IS NOT NULL OR proof_photo_ref IS NOT NULL)))   -- R-069
);

CREATE TABLE receipt (                                  -- prefix rcp_   (E-032)
  id                   TEXT PRIMARY KEY,
  order_id             TEXT NOT NULL REFERENCES "order"(id),
  received_by_user_id  TEXT NULL REFERENCES app_user(id),      -- NULL for assisted pharmacies (channel ref instead)
  channel_ref          TEXT NULL,
  received_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_receipt_order UNIQUE (order_id)
);
CREATE TABLE receipt_line (                             -- prefix rcl_   (E-033)
  id             TEXT PRIMARY KEY,
  receipt_id     TEXT NOT NULL REFERENCES receipt(id),
  order_line_id  TEXT NOT NULL REFERENCES order_line(id),
  accepted_qty   INTEGER NOT NULL CHECK (accepted_qty >= 0),
  rejected_qty   INTEGER NOT NULL DEFAULT 0 CHECK (rejected_qty >= 0),
  reason_code    TEXT NULL CHECK (reason_code IN ('WRONG_ITEM','DAMAGED','SHORT','EXPIRED','NEAR_EXPIRY')),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_receipt_line UNIQUE (receipt_id, order_line_id),
  CONSTRAINT ck_receipt_line_reason CHECK (rejected_qty = 0 OR reason_code IS NOT NULL)   -- R-072
);

CREATE TABLE dispute (                                  -- prefix dsp_   (E-034 + folded E-035)
  id                   TEXT PRIMARY KEY,
  order_id             TEXT NOT NULL REFERENCES "order"(id),
  receipt_line_id      TEXT NULL REFERENCES receipt_line(id),
  invoice_line_id      TEXT NULL,                       -- FK added after invoice_line
  type                 TEXT NOT NULL CHECK (type IN ('RECEIPT_DISCREPANCY','REGULATED_PRICE_INCIDENT','FREE_PRICE_MISMATCH')),
  raised_by            TEXT NOT NULL CHECK (raised_by IN ('PharmacyReceiver','PharmacyAdmin','ComplianceOfficer','SYSTEM')),
  status               TEXT NOT NULL CHECK (status IN ('OPEN','UNDER_REVIEW','ESCALATED','RESOLVED')) DEFAULT 'OPEN',   -- SM-09
  assigned_to_user_id  TEXT NULL REFERENCES app_user(id),
  assigned_at          TIMESTAMPTZ NULL,
  outcome              TEXT NULL CHECK (outcome IN ('CREDIT_NOTE','REPLACEMENT','REJECTED','ESCALATED_TO_COMPLIANCE')),
  resolved_by_user_id  TEXT NULL REFERENCES app_user(id),
  resolved_at          TIMESTAMPTZ NULL,
  notes                TEXT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_dispute_subject CHECK ((receipt_line_id IS NOT NULL) <> (invoice_line_id IS NOT NULL)),
  CONSTRAINT ck_dispute_resolved CHECK ((status = 'RESOLVED') = (outcome IS NOT NULL AND resolved_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_dispute_open_per_receipt_line ON dispute (receipt_line_id) WHERE status <> 'RESOLVED' AND receipt_line_id IS NOT NULL;   -- R-077

CREATE SEQUENCE rma_number_seq;
CREATE TABLE "return" (                                 -- prefix rtn_   (E-036)
  id                    TEXT PRIMARY KEY,
  order_id              TEXT NOT NULL REFERENCES "order"(id),
  rma_number            TEXT NOT NULL,                  -- 'RMA-2026-000012'
  status                TEXT NOT NULL CHECK (status IN ('REQUESTED','APPROVED','REJECTED','GOODS_IN_TRANSIT','RECEIVED_BY_VENDOR','CLOSED')) DEFAULT 'REQUESTED',  -- SM-10
  requested_by_user_id  TEXT NULL REFERENCES app_user(id),
  origin                TEXT NOT NULL CHECK (origin IN ('PHARMACY_RMA','DISPUTE_RESOLUTION','DELIVERY_EXHAUSTED')),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_return_rma UNIQUE (rma_number)
);
CREATE TABLE return_line (                              -- prefix rtl_   (E-037)
  id             TEXT PRIMARY KEY,
  return_id      TEXT NOT NULL REFERENCES "return"(id),
  order_line_id  TEXT NOT NULL REFERENCES order_line(id),
  qty            INTEGER NOT NULL CHECK (qty > 0),
  reason_code    TEXT NOT NULL CHECK (reason_code IN ('DAMAGED','EXPIRED','NEAR_EXPIRY','WRONG_ITEM','OTHER')),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE traceability_event (                       -- prefix trc_   (E-038) append-only
  id             TEXT PRIMARY KEY,
  order_line_id  TEXT NOT NULL REFERENCES order_line(id),
  seal_id        TEXT NOT NULL,
  batch_number   TEXT NOT NULL,
  lot_number     TEXT NOT NULL,
  expiry_date    DATE NOT NULL,
  event_type     TEXT NOT NULL CHECK (event_type IN ('PICKED','DISPATCHED','RECEIVED','RETURNED')),
  actor_user_id  TEXT NULL REFERENCES app_user(id),
  actor_role     TEXT NOT NULL,
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_traceability_line ON traceability_event (order_line_id, occurred_at);
CREATE UNIQUE INDEX uq_traceability_seal_dispatched ON traceability_event (seal_id) WHERE event_type = 'DISPATCHED';   -- R-057: a seal is dispatched once
CREATE TRIGGER trg_traceability_append_only BEFORE UPDATE OR DELETE ON traceability_event
  FOR EACH ROW EXECUTE FUNCTION rova_forbid_mutation();

CREATE TABLE eta_estimate (                             -- prefix eta_   (E-068)
  id              TEXT PRIMARY KEY,
  order_id        TEXT NOT NULL REFERENCES "order"(id),
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  order_state     TEXT NOT NULL,
  delivery_state  TEXT NULL,
  earliest_at     TIMESTAMPTZ NOT NULL,
  latest_at       TIMESTAMPTZ NOT NULL,
  basis           TEXT NOT NULL CHECK (basis IN ('SLA_BOUNDS','VENDOR_HISTORY','VENDOR_PROMISE','COURIER_LIVE')),
  components      JSONB NOT NULL,                       -- [{"name":"acceptance","from":…,"to":…,"source":"CFG-SLA-ACCEPTANCE-HOURS"}, …]
  status          TEXT NOT NULL CHECK (status IN ('PROVISIONAL','COMMITTED','IN_TRANSIT_LIVE','REALISED','MISSED','VOID')) DEFAULT 'PROVISIONAL',   -- SM-20
  realised_at     TIMESTAMPTZ NULL,
  error_minutes   INTEGER NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_eta_range CHECK (latest_at >= earliest_at)
);
CREATE UNIQUE INDEX uq_eta_one_open_per_order ON eta_estimate (order_id) WHERE status IN ('PROVISIONAL','COMMITTED','IN_TRANSIT_LIVE');   -- R-158

CREATE TABLE sla_timer (                                -- prefix sla_   (E-044)
  id            TEXT PRIMARY KEY,
  policy_type   TEXT NOT NULL CHECK (policy_type IN ('QUOTATION','ACCEPTANCE','DISPATCH','DISPUTE_RESOLUTION','REROUTE_ASK','CONFIRMATION','ADMIN_APPROVAL','HUMAN_HANDOVER_RESPONSE')),
  subject_type  TEXT NOT NULL,                          -- 'quotation' | 'order' | 'order_line' | 'dispute' | 'request' | 'support_thread'
  subject_id    TEXT NOT NULL,
  starts_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('RUNNING','CANCELLED','EXPIRED')) DEFAULT 'RUNNING',
  config_source TEXT NOT NULL,                          -- which CFG row (key + scope) produced the duration
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_sla_timer_running ON sla_timer (policy_type, subject_type, subject_id) WHERE status = 'RUNNING';
CREATE INDEX ix_sla_timer_due ON sla_timer (expires_at) WHERE status = 'RUNNING';

-- ============================================================================
-- 6. CREDIT AND BILLING (the vendor's fiscal documents)
-- ============================================================================

CREATE TABLE credit_facility (                          -- prefix crf_   (E-025)
  id                           TEXT PRIMARY KEY,
  vendor_id                    TEXT NOT NULL REFERENCES vendor_account(id),
  pharmacy_id                  TEXT NOT NULL REFERENCES pharmacy_account(id),
  limit_amount                 NUMERIC(14,2) NOT NULL CHECK (limit_amount >= 0),
  opening_balance              NUMERIC(14,2) NOT NULL DEFAULT 0,
  opening_balance_attested_at  TIMESTAMPTZ NULL,
  terms_days                   INTEGER NULL CHECK (terms_days > 0),
  mov_waived                   BOOLEAN NOT NULL DEFAULT false,   -- R-037
  status                       TEXT NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')) DEFAULT 'ACTIVE',
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_credit_facility_pair UNIQUE (vendor_id, pharmacy_id),
  CONSTRAINT ck_credit_opening_attested CHECK (opening_balance = 0 OR opening_balance_attested_at IS NOT NULL)   -- R-038
);

CREATE TABLE ledger_entry (                             -- prefix led_   (E-026) append-only
  id                  TEXT PRIMARY KEY,
  credit_facility_id  TEXT NOT NULL REFERENCES credit_facility(id),
  entry_type          TEXT NOT NULL CHECK (entry_type IN ('INVOICE','PAYMENT','CREDIT_NOTE','ADJUSTMENT')),
  reference_type      TEXT NOT NULL CHECK (reference_type IN ('invoice','payment','credit_note','invoice_write_off')),
  reference_id        TEXT NOT NULL,
  amount              NUMERIC(14,2) NOT NULL,           -- signed: INVOICE +, PAYMENT −, CREDIT_NOTE −, ADJUSTMENT ±
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ledger_facility ON ledger_entry (credit_facility_id, created_at);
CREATE TRIGGER trg_ledger_append_only BEFORE UPDATE OR DELETE ON ledger_entry
  FOR EACH ROW EXECUTE FUNCTION rova_forbid_mutation();

-- invoice (E-022): the vendor's fiscal document. issuer is a vendor by FK — the platform has no row to point at.
CREATE TABLE invoice (                                  -- prefix inv_
  id                     TEXT PRIMARY KEY,
  order_id               TEXT NOT NULL REFERENCES "order"(id),
  issuer_vendor_id       TEXT NOT NULL REFERENCES vendor_account(id),   -- never the platform (R-097 structural)
  pharmacy_id            TEXT NOT NULL REFERENCES pharmacy_account(id),
  vendor_invoice_number  TEXT NOT NULL,
  total_amount           NUMERIC(14,2) NOT NULL CHECK (total_amount >= 0),   -- Σ lines; goods only
  status                 TEXT NOT NULL CHECK (status IN ('UPLOADED','PRICE_MATCH_OK','PRICE_DEVIATION_FLAGGED','AWAITING_PAYMENT','PARTIALLY_PAID','PAID','WRITTEN_OFF')) DEFAULT 'UPLOADED',  -- SM-11
  price_match_flag       TEXT NOT NULL CHECK (price_match_flag IN ('OK','DEVIATION_ABOVE','DEVIATION_BELOW','NOT_APPLICABLE','PENDING')) DEFAULT 'PENDING',
  document_ref           TEXT NULL,
  uploaded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  finalised_at           TIMESTAMPTZ NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_invoice_vendor_number UNIQUE (issuer_vendor_id, vendor_invoice_number)
);
CREATE UNIQUE INDEX uq_invoice_live_per_order ON invoice (order_id) WHERE status <> 'WRITTEN_OFF';   -- Order 1:1 Invoice
CREATE TABLE invoice_line (                             -- prefix ivl_   (E-023)
  id              TEXT PRIMARY KEY,
  invoice_id      TEXT NOT NULL REFERENCES invoice(id),
  order_line_id   TEXT NOT NULL REFERENCES order_line(id),
  invoiced_price  NUMERIC(14,2) NOT NULL CHECK (invoiced_price >= 0),
  invoiced_qty    INTEGER NOT NULL CHECK (invoiced_qty >= 0),
  match_result    TEXT NULL CHECK (match_result IN ('OK','DEVIATION_ABOVE','DEVIATION_BELOW','NOT_APPLICABLE')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_invoice_line UNIQUE (invoice_id, order_line_id)
);
ALTER TABLE dispute ADD CONSTRAINT fk_dispute_invoice_line FOREIGN KEY (invoice_line_id) REFERENCES invoice_line(id);

CREATE TABLE credit_note (                              -- prefix crn_   (E-024)
  id           TEXT PRIMARY KEY,
  vendor_id    TEXT NOT NULL REFERENCES vendor_account(id),
  pharmacy_id  TEXT NOT NULL REFERENCES pharmacy_account(id),
  order_id     TEXT NOT NULL REFERENCES "order"(id),
  return_id    TEXT NULL REFERENCES "return"(id),
  dispute_id   TEXT NULL REFERENCES dispute(id),
  amount       NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  approved_by_compliance_user_id TEXT NULL REFERENCES app_user(id),   -- R-082 exception without physical return
  issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_credit_note_origin CHECK (return_id IS NOT NULL OR dispute_id IS NOT NULL)
);

CREATE TABLE payment (                                  -- prefix pay_   (E-027)
  id            TEXT PRIMARY KEY,
  pharmacy_id   TEXT NOT NULL REFERENCES pharmacy_account(id),
  vendor_id     TEXT NOT NULL REFERENCES vendor_account(id),
  method        TEXT NOT NULL CHECK (method IN ('MOBILE_MONEY','BANK_TRANSFER','CASH_ON_DELIVERY')),
  collected_by  TEXT NOT NULL CHECK (collected_by IN ('VENDOR_DIRECT','PLATFORM_COLLECTED')) DEFAULT 'VENDOR_DIRECT',   -- R-088: PLATFORM_COLLECTED rejected by the API in v1
  amount        NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  external_ref  TEXT NULL,                              -- M-Pesa / e-Mola / mKesh / bank reference
  recorded_by_user_id TEXT NOT NULL REFERENCES app_user(id),
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE payment_allocation (                       -- prefix pal_   (E-028)
  id          TEXT PRIMARY KEY,
  payment_id  TEXT NOT NULL REFERENCES payment(id),
  invoice_id  TEXT NOT NULL REFERENCES invoice(id),
  amount      NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_payment_allocation UNIQUE (payment_id, invoice_id)
);
-- R-090 / R-091 (Σ allocations ≤ payment.amount; Σ per invoice ≤ outstanding) are enforced in the service inside the same transaction
-- with the payment/invoice rows locked FOR UPDATE.

-- ============================================================================
-- 7. PLATFORM FEES (structurally separate from group 6 — no FK crosses the line)
-- ============================================================================

CREATE TABLE fee_schedule (                             -- prefix fsc_   (E-039 as redesigned in §A3)
  id                 TEXT PRIMARY KEY,
  type               TEXT NOT NULL CHECK (type IN ('FLAT_PER_ORDER','PER_DELIVERY_JOB','SUBSCRIPTION_MONTHLY','PERCENT_OF_NET_DELIVERED','VENDOR_SHARE','PHARMACY_SERVICE_FEE')),
  payer              TEXT NOT NULL CHECK (payer IN ('VENDOR','PHARMACY')),
  rate_or_amount     NUMERIC(14,4) NOT NULL CHECK (rate_or_amount >= 0),   -- rate (0.0250) for percentage types, MZN amount for flat types
  base               TEXT NULL CHECK (base IN ('NET_DELIVERED_VALUE','DECLARED_MARGIN')),
  applies_to         TEXT NOT NULL CHECK (applies_to IN ('ALL_ORDERS','INCREMENTAL_ORDERS_ONLY')) DEFAULT 'ALL_ORDERS',
  scope_type         TEXT NOT NULL CHECK (scope_type IN ('GLOBAL','REGION','VENDOR','PHARMACY')),
  scope_id           TEXT NULL,
  effective_from     DATE NOT NULL,
  effective_to       DATE NULL,
  earning_event      TEXT NOT NULL CHECK (earning_event IN ('RECEIPT_ACCEPTED','ORDER_CLOSED','DELIVERY_DELIVERED','PERIOD_END')),
  legal_status       TEXT NOT NULL CHECK (legal_status IN ('OWNER_ACCEPTED','STANDARD')),
  agreement_id       TEXT NULL REFERENCES vendor_agreement(id),
  notes              TEXT NULL,                         -- provenance of the number ('owner negotiation target 2–3%', 'SEED-FIXTURE placeholder, OD-141')
  created_by_user_id TEXT NOT NULL REFERENCES app_user(id),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_fee_scope CHECK ((scope_type = 'GLOBAL') = (scope_id IS NULL)),
  CONSTRAINT ck_fee_vendor_share_base CHECK (type <> 'VENDOR_SHARE' OR base IS NOT NULL),
  CONSTRAINT ck_fee_vendor_payer_agreement CHECK (payer <> 'VENDOR' OR agreement_id IS NOT NULL),   -- §A3: required when payer = VENDOR
  CONSTRAINT ck_fee_payer_type CHECK (
      (type IN ('VENDOR_SHARE','PERCENT_OF_NET_DELIVERED') AND payer = 'VENDOR') OR
      (type = 'PHARMACY_SERVICE_FEE' AND payer = 'PHARMACY') OR
      (type IN ('FLAT_PER_ORDER','PER_DELIVERY_JOB','SUBSCRIPTION_MONTHLY'))),
  CONSTRAINT ck_fee_effective CHECK (effective_to IS NULL OR effective_to >= effective_from)
  -- ranking_influence = NONE is not a column: there is nothing to store. It is enforced by A6 (the ranking engine cannot import this table).
);
CREATE INDEX ix_fee_schedule_scope ON fee_schedule (scope_type, scope_id, effective_from);

-- order_fee_schedule: the schedules captured at Order creation (Order.fee_schedule_ids[] in §A3), frozen against later changes (R-157).
CREATE TABLE order_fee_schedule (
  order_id         TEXT NOT NULL REFERENCES "order"(id),
  fee_schedule_id  TEXT NOT NULL REFERENCES fee_schedule(id),
  PRIMARY KEY (order_id, fee_schedule_id)
);

CREATE TABLE fee_event (                                -- prefix fev_   (E-040 extended)
  id                   TEXT PRIMARY KEY,
  fee_schedule_id      TEXT NOT NULL REFERENCES fee_schedule(id),
  payer_org_id         TEXT NOT NULL REFERENCES organisation(id),
  request_id           TEXT NULL REFERENCES request(id),
  order_id             TEXT NULL REFERENCES "order"(id),
  delivery_job_id      TEXT NULL REFERENCES delivery_job(id),
  base_amount          NUMERIC(14,2) NOT NULL CHECK (base_amount >= 0),   -- confidential when base = DECLARED_MARGIN (R-146)
  amount               NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
  attribution          TEXT NULL CHECK (attribution IN ('INCREMENTAL','PRE_EXISTING')),
  status               TEXT NOT NULL CHECK (status IN ('ACCRUED','INVOICED','REVERSED')) DEFAULT 'ACCRUED',
  occurred_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  reversed_at          TIMESTAMPTZ NULL,
  reversal_reason      TEXT NULL,
  platform_invoice_id  TEXT NULL,                       -- FK added below
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_fee_event_exactly_one_subject CHECK (
      (request_id IS NOT NULL)::int + (order_id IS NOT NULL)::int + (delivery_job_id IS NOT NULL)::int = 1)
);
CREATE INDEX ix_fee_event_payer_status ON fee_event (payer_org_id, status, occurred_at);
CREATE INDEX ix_fee_event_order ON fee_event (order_id);
ALTER TABLE delivery_job ADD CONSTRAINT fk_delivery_job_fee_event FOREIGN KEY (fee_event_id) REFERENCES fee_event(id);

CREATE SEQUENCE platform_invoice_number_seq;
CREATE TABLE platform_invoice (                         -- prefix pin_   (E-041). One payer per document. NO reference to invoice/invoice_line.
  id            TEXT PRIMARY KEY,
  number        TEXT NOT NULL,                          -- 'ROVA-2026-000007'
  payer_org_id  TEXT NOT NULL REFERENCES organisation(id),
  period_start  DATE NOT NULL,
  period_end    DATE NOT NULL,
  amount        NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
  status        TEXT NOT NULL CHECK (status IN ('ISSUED','SETTLED')) DEFAULT 'ISSUED',
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  settled_at    TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_platform_invoice_number UNIQUE (number),
  CONSTRAINT ck_platform_invoice_period CHECK (period_end >= period_start)
);
ALTER TABLE fee_event ADD CONSTRAINT fk_fee_event_platform_invoice FOREIGN KEY (platform_invoice_id) REFERENCES platform_invoice(id);

-- ============================================================================
-- 8. GOVERNANCE: MODE SWITCHES, RANKING ISOLATION, VENDOR SCORE
-- ============================================================================

CREATE TABLE mode_switch (                              -- prefix mds_   (E-060)
  id                   TEXT PRIMARY KEY,
  switch_key           TEXT NOT NULL CHECK (switch_key IN ('VENDOR_MODE','FEE_MODEL','PRIMARY_INTERFACE')),
  scope_type           TEXT NOT NULL CHECK (scope_type IN ('GLOBAL','REGION','PHARMACY','VENDOR')),
  scope_id             TEXT NULL,
  current_value        TEXT NOT NULL,                   -- VENDOR_MODE: SINGLE|MULTI; FEE_MODEL: sorted comma-joined subset of {DELIVERY_FEE,PHARMACY_SERVICE_FEE,VENDOR_SHARE}; PRIMARY_INTERFACE: CONVERSATIONAL|CLASSIC
  previous_value       TEXT NULL,
  proposed_value       TEXT NULL,
  status               TEXT NOT NULL CHECK (status IN ('STEADY','PROPOSED','GATE_CHECKING','READY','FLIPPED','ROLLED_BACK')) DEFAULT 'STEADY',   -- SM-22
  gate_results         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{"gate_id":"G1","passed":true,"evidence_ref":"…","checked_at":"…"}]
  gates_checked_at     TIMESTAMPTZ NULL,
  proposed_by_user_id  TEXT NULL REFERENCES app_user(id),
  flipped_by_user_id   TEXT NULL REFERENCES app_user(id),
  flipped_at           TIMESTAMPTZ NULL,
  observation_ends_at  TIMESTAMPTZ NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_mode_switch_scope CHECK ((scope_type = 'GLOBAL') = (scope_id IS NULL)),
  CONSTRAINT ck_mode_switch_value CHECK (
      (switch_key = 'VENDOR_MODE' AND current_value IN ('SINGLE','MULTI')) OR
      (switch_key = 'PRIMARY_INTERFACE' AND current_value IN ('CONVERSATIONAL','CLASSIC')) OR
      (switch_key = 'FEE_MODEL' AND current_value ~ '^(DELIVERY_FEE)?(,?PHARMACY_SERVICE_FEE)?(,?VENDOR_SHARE)?$' AND current_value <> '')),
  CONSTRAINT uq_mode_switch UNIQUE (switch_key, scope_type, scope_id)
);
-- There is no UPDATE path to current_value outside rova/modes/service.py::flip (R-140); a test greps for it.

CREATE TABLE ranking_isolation_audit (                  -- prefix ria_   (E-064)
  id                           TEXT PRIMARY KEY,
  run_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
  window_start                 TIMESTAMPTZ NOT NULL,
  window_end                   TIMESTAMPTZ NOT NULL,
  sample_size                  INTEGER NOT NULL CHECK (sample_size >= 0),
  allocations_sampled          JSONB NOT NULL,          -- ["alc_…", …]
  divergences                  INTEGER NOT NULL CHECK (divergences >= 0),
  manual_ops_share             NUMERIC(7,4) NOT NULL,
  manual_ops_top_vendor_share  NUMERIC(7,4) NOT NULL,
  auto_top_vendor_share        NUMERIC(7,4) NOT NULL,   -- comparison base for R-153
  result                       TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
  evidence_ref                 TEXT NOT NULL,           -- stored recomputation log path
  triggered_by                 TEXT NOT NULL CHECK (triggered_by IN ('SCHEDULE','MANUAL','GATE')),
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ria_run ON ranking_isolation_audit (run_at DESC);

CREATE TABLE vendor_score (                             -- prefix vsc_   (E-042 extended R-160)
  id                       TEXT PRIMARY KEY,
  vendor_id                TEXT NOT NULL REFERENCES vendor_account(id),
  window_start             TIMESTAMPTZ NOT NULL,
  window_end               TIMESTAMPTZ NOT NULL,
  fill_rate                NUMERIC(7,4) NOT NULL CHECK (fill_rate BETWEEN 0 AND 1),
  on_time_dispatch_rate    NUMERIC(7,4) NOT NULL CHECK (on_time_dispatch_rate BETWEEN 0 AND 1),
  promise_accuracy         NUMERIC(7,4) NOT NULL CHECK (promise_accuracy BETWEEN 0 AND 1),
  eta_accuracy             NUMERIC(7,4) NOT NULL CHECK (eta_accuracy BETWEEN 0 AND 1),
  quotation_sla_breaches   INTEGER NOT NULL CHECK (quotation_sla_breaches >= 0),
  sample_orders            INTEGER NOT NULL CHECK (sample_orders >= 0),
  score_value              NUMERIC(7,2) NOT NULL CHECK (score_value BETWEEN 0 AND 100),
  formula_ref              TEXT NOT NULL,               -- 'CFG-HEALTH-SCORE-FORMULA@v1'
  computed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_vendor_score_latest ON vendor_score (vendor_id, computed_at DESC);

-- ============================================================================
-- 9. FEDERATION: PMS
-- ============================================================================

CREATE TABLE pms_link (                                 -- prefix pml_   (E-072)
  id                  TEXT PRIMARY KEY,
  pharmacy_id         TEXT NOT NULL REFERENCES pharmacy_account(id),
  pms_tenant_id       TEXT NOT NULL,
  api_key_hash        TEXT NOT NULL,                    -- sha256 of the key issued to the PMS for this link
  status              TEXT NOT NULL CHECK (status IN ('PENDING_CONSENT','LINKED','SUSPENDED','REVOKED')) DEFAULT 'PENDING_CONSENT',   -- SM-23
  consent_record_id   TEXT NULL REFERENCES consent_record(id),    -- NOT NULL once LINKED (app-checked)
  product_key_mode    TEXT NOT NULL CHECK (product_key_mode IN ('INDEX_ID','MAPPED')) DEFAULT 'INDEX_ID',   -- OD-143
  linked_at           TIMESTAMPTZ NULL,
  last_inbound_at     TIMESTAMPTZ NULL,
  last_outbound_at    TIMESTAMPTZ NULL,
  revoked_at          TIMESTAMPTZ NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_pms_link_key UNIQUE (api_key_hash)
);
CREATE UNIQUE INDEX uq_pms_link_live ON pms_link (pharmacy_id) WHERE status <> 'REVOKED';

-- dispensing_aggregate (E-071): the ONLY inbound dispensing schema. No other column may ever be added (R-169).
CREATE TABLE dispensing_aggregate (                     -- prefix dsa_
  id                TEXT PRIMARY KEY,
  pms_link_id       TEXT NOT NULL REFERENCES pms_link(id),
  index_product_id  TEXT NOT NULL REFERENCES index_product(id),
  period_date       DATE NOT NULL,
  dispensed_units   INTEGER NOT NULL CHECK (dispensed_units >= 0),
  stock_on_hand     INTEGER NULL CHECK (stock_on_hand >= 0),
  received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_dispensing UNIQUE (pms_link_id, index_product_id, period_date)   -- re-sends overwrite the same day (idempotent)
);

-- ============================================================================
-- 10. SUPPORT THREAD AND NOTIFICATIONS
-- ============================================================================

CREATE TABLE support_thread (                           -- prefix sth_   (E-077)
  id                TEXT PRIMARY KEY,
  pharmacy_id       TEXT NOT NULL REFERENCES pharmacy_account(id),
  active_handler    TEXT NOT NULL CHECK (active_handler IN ('BOT','ESCALATING','HUMAN')) DEFAULT 'BOT',   -- SM-24
  active_ticket_id  TEXT NULL,                          -- FK added after ticket
  last_activity_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_support_thread_pharmacy UNIQUE (pharmacy_id)   -- one thread per pharmacy, ever (R-179)
);

CREATE TABLE ticket (                                   -- prefix tkt_   (E-049 extended §A15)
  id                     TEXT PRIMARY KEY,
  thread_id              TEXT NOT NULL REFERENCES support_thread(id),
  subject_org_id         TEXT NOT NULL REFERENCES organisation(id),
  raised_by_user_id      TEXT NULL REFERENCES app_user(id),
  status                 TEXT NOT NULL CHECK (status IN ('OPEN','IN_PROGRESS','RESOLVED','CLOSED')) DEFAULT 'OPEN',
  related_order_id       TEXT NULL REFERENCES "order"(id),
  requires_vendor_relay  BOOLEAN NOT NULL DEFAULT false,   -- R-180
  opened_by_agent_user_id TEXT NULL REFERENCES app_user(id),
  resolved_at            TIMESTAMPTZ NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE support_thread ADD CONSTRAINT fk_support_thread_ticket FOREIGN KEY (active_ticket_id) REFERENCES ticket(id);
CREATE INDEX ix_ticket_open ON ticket (status, created_at) WHERE status IN ('OPEN','IN_PROGRESS');

CREATE TABLE thread_message (                           -- prefix thm_   (backend addition; the thread's content)
  id              TEXT PRIMARY KEY,
  thread_id       TEXT NOT NULL REFERENCES support_thread(id),
  sender_type     TEXT NOT NULL CHECK (sender_type IN ('PHARMACY','AGENT','SYSTEM')),
  sender_user_id  TEXT NULL REFERENCES app_user(id),
  channel         TEXT NOT NULL CHECK (channel IN ('APP','WHATSAPP_TEXT','PHONE_SUMMARY')),
  body            TEXT NOT NULL,
  ticket_id       TEXT NULL REFERENCES ticket(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_thread_message_thread ON thread_message (thread_id, created_at DESC);

CREATE TABLE notification (                             -- prefix ntf_   (E-045, used as the outbox)
  id                 TEXT PRIMARY KEY,
  event_code         TEXT NOT NULL,                     -- 'N-07', 'N-VENDOR_FEED_UPLOAD_RESULT', …
  recipient_role     TEXT NULL,
  recipient_user_id  TEXT NULL REFERENCES app_user(id),
  recipient_org_id   TEXT NULL REFERENCES organisation(id),
  channel            TEXT NOT NULL CHECK (channel IN ('PUSH','SMS','WHATSAPP','IN_APP','EMAIL','PMS_WEBHOOK')),
  payload            JSONB NOT NULL DEFAULT '{}'::jsonb,
  rendered_text      TEXT NULL,                         -- Portuguese, from templates_pt.py
  status             TEXT NOT NULL CHECK (status IN ('QUEUED','SENT','FAILED','READ')) DEFAULT 'QUEUED',
  sent_at            TIMESTAMPTZ NULL,
  read_at            TIMESTAMPTZ NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_notification_recipient ON notification (recipient_user_id, created_at DESC);
CREATE INDEX ix_notification_queued ON notification (created_at) WHERE status = 'QUEUED';
