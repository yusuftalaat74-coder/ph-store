-- 0008 — asking for a credit limit to be exceeded, and who allowed it.
--
-- The gate (A10, R-038..R-044) already refuses a sub-basket that would take a
-- pharmacy past its limit, and `credit/gate.py` has always reported
-- `ADMIN_OVERRIDE` among the options. Nothing implemented it, so the refusal
-- was a wall: a pharmacist with a full basket was told "credit limit
-- exceeded" and given nothing to do about it. `order.credit_override_user_id`
-- and `credit_override_reason` have been sitting in the schema since the
-- first migration waiting for this.
--
-- One approval covers one request, one vendor, and an amount. It is consumed
-- when the order is created, so it buys one order and does not become a
-- standing licence; it expires on its own, so an approval nobody used in
-- time does not sit there as headroom the credit team has forgotten about.
--
-- Either side may decide, and the row records which: the distributor extends
-- the credit, and the platform carries the relationship, so both have a
-- legitimate claim on the decision and neither should be able to make it
-- anonymously.

CREATE TABLE IF NOT EXISTS credit_override (
    id                  TEXT PRIMARY KEY,
    request_id          TEXT NOT NULL REFERENCES request(id),
    vendor_id           TEXT NOT NULL REFERENCES vendor_account(id),
    pharmacy_id         TEXT NOT NULL REFERENCES pharmacy_account(id),
    facility_id         TEXT NOT NULL REFERENCES credit_facility(id),

    -- what was asked for, and what the gate said at the time. Both are kept
    -- because the second is the evidence for the first: an approver a day
    -- later should see the headroom the pharmacist was refused against, not
    -- only today's.
    amount_requested    NUMERIC(14,2) NOT NULL CHECK (amount_requested > 0),
    headroom_at_request NUMERIC(14,2) NOT NULL,
    exposure_at_request NUMERIC(14,2) NOT NULL,

    status              TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','APPROVED','DECLINED','USED','EXPIRED')),
    note                TEXT,

    requested_by_user_id TEXT NOT NULL REFERENCES app_user(id),
    requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ NOT NULL,

    decided_by_user_id   TEXT REFERENCES app_user(id),
    -- VENDOR or PLATFORM. Not derived from the role at read time: a person's
    -- memberships change, and the question "who allowed this" must still have
    -- the same answer in a year.
    decided_by_side      TEXT CHECK (decided_by_side IN ('VENDOR','PLATFORM')),
    decided_at           TIMESTAMPTZ,
    decision_reason      TEXT,

    used_order_id        TEXT REFERENCES "order"(id),
    used_at              TIMESTAMPTZ,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- a decision has an author and a moment, or it is not a decision
    CONSTRAINT ck_credit_override_decided_together CHECK (
        (status IN ('PENDING','EXPIRED'))
        OR (status = 'DECLINED' AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL
            AND decided_by_side IS NOT NULL)
        OR (status IN ('APPROVED','USED') AND decided_by_user_id IS NOT NULL
            AND decided_at IS NOT NULL AND decided_by_side IS NOT NULL)
    ),
    CONSTRAINT ck_credit_override_used_has_order CHECK (
        (status <> 'USED') OR (used_order_id IS NOT NULL AND used_at IS NOT NULL)
    )
);

-- One live ask per request and vendor. Without it a pharmacist tapping the
-- button twice raises two, and an approver answers one while the other stays
-- open looking unanswered.
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_credit_override
    ON credit_override (request_id, vendor_id)
    WHERE status IN ('PENDING','APPROVED');

CREATE INDEX IF NOT EXISTS ix_credit_override_vendor_status
    ON credit_override (vendor_id, status);
CREATE INDEX IF NOT EXISTS ix_credit_override_pharmacy_status
    ON credit_override (pharmacy_id, status);

INSERT INTO config_parameter (id, key, scope_type, scope_id, value, value_type,
                              owner_role, source_tag, created_at, updated_at)
VALUES ('cfg_cfg_credit_override_hours', 'CFG-CREDIT-OVERRIDE-HOURS', 'GLOBAL', NULL,
        '48', 'INT', 'PlatformAdmin', 'credit override (A10)', now(), now())
ON CONFLICT (id) DO NOTHING;
