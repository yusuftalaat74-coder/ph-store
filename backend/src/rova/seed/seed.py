"""`rova seed` (A16, reduced scope — see the executor manifest for exactly
what is cut against the full A16 fixture set). Idempotent: every row is
inserted with `ON CONFLICT DO NOTHING` against a fixed, deterministic id or
natural key, so a second run changes no row count (B1.3).

Every price, limit, MOV and fee amount below carries a `-- fixture:` / notes
comment naming its provenance, per A2.24/A16. None of CONTEXT.md's seven
named unknowns (AOV, order frequency, courier tariff, WhatsApp penetration,
software adoption, Maputo pharmacy count, credit-terms market rate) is given
a value anywhere in this module.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from rova.auth.principal import Principal
from rova.auth.security import hash_password
from rova.core.clock import now
from rova.core.db import get_sessionmaker
from rova.domain.machines.registry import MACHINES
from rova.ordering.checkout import checkout as run_checkout


def _exec(session: Session, sql: str, params: dict | None = None):
    return session.execute(text(sql), params or {})


def _upsert(session: Session, table: str, id_col: str, row: dict) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    _exec(session, f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) ON CONFLICT ({id_col}) DO NOTHING', row)


def seed_config_only(session: Session) -> None:
    # regions — transit_window_hours: Maputo/Matola 4h [assume: A-167]; other 24h [assume: A-BE-07]
    for code, name, hours in [
        ("MAPUTO_CIDADE", "Maputo Cidade", 4),
        ("MATOLA", "Matola", 4),
        ("OUTRA", "Outra região", 24),
    ]:
        _upsert(session, "region", "code", {"code": code, "name": name, "transit_window_hours": hours})

    # config_parameter — reduced subset of §16/§A14 actually consumed by the
    # implemented services in this session, each with a source_tag (A2.24).
    # NOTE (executor manifest, this session): every source_tag below was
    # re-verified against مواصفة_المنتج_v1.0.md §16/A19's own A-xx table
    # (lines ~12624/338-351) and addendum-v11.md §A19 (lines ~1539-1768) by
    # grep, since an earlier pass through this list had assigned several
    # rows the wrong A-number (or, worse, the wrong *value* — the two
    # corrected below, CFG-SLA-ACCEPTANCE-HOURS and CFG-VERIFICATION-SLA-
    # HOURS, actually disagreed with the spec's own stated figure, not just
    # its citation) and had claimed "no source value" for two rows that the
    # spec does in fact give a value+tag for. Fixed in place rather than
    # left diverged, per this session's brief.
    cfg_rows = [
        ("CFG-LOGIN-LOCKOUT-ATTEMPTS", "5", "INT", "design target A-153"),
        ("CFG-DEFAULT-ALLOCATION-STRATEGY", "FEWEST_VENDORS", "ENUM", "design target A-92"),
        ("CFG-NEAR-EXPIRY-DAYS", "90", "INT", "design target A-02"),
        ("CFG-RETURN-WINDOW-DAYS", "7", "INT", "design target A-08"),
        # was seeded as 72h/"A-08"; spec §16 gives CFG-OFFER-FRESHNESS-TTL-HOURS
        # its own row: «هدف تصميمي A-01» — 24 (مواصفة_المنتج_v1.0.md line 12624).
        ("CFG-OFFER-FRESHNESS-TTL-HOURS", "24", "INT", "design target A-01"),
        ("CFG-SLA-QUOTATION-HOURS", "24", "INT", "design target A-04"),
        # was seeded as 4h/"A-01" (a stray copy of the reroute-ask-timeout
        # value below); spec's own A-05 row states 12h for vendor sub-order
        # acceptance (مواصفة_المنتج_v1.0.md lines 342/12628).
        ("CFG-SLA-ACCEPTANCE-HOURS", "12", "INT", "design target A-05"),
        ("CFG-SLA-DISPATCH-HOURS", "24", "INT", "design target A-06"),
        # SM-04 reroute-ask timeout (§7 group 7, §16); default behaviour on
        # expiry is LINE_DROPPED per the same spec row.
        ("CFG-REROUTE-ASK-TIMEOUT-HOURS", "4", "INT", "design target A-03"),
        ("CFG-HEALTH-SCORE-FORMULA", "v1", "TEXT", "A-BE-01"),
        # was seeded as 40/"A-15"; spec's A-09 row states 60 of 100
        # (مواصفة_المنتج_v1.0.md line 12633).
        ("CFG-HEALTH-SUSPEND-THRESHOLD", "60", "DECIMAL", "design target A-09"),
        ("CFG-ETA-VENDOR-VARIANCE-HOURS", "2", "INT", "A-BE-06"),
        # price-list import (A12, مواصفة_استيراد_قائمة_الأسعار.md §4) — the two
        # confidence thresholds are literal values from the import spec
        # ("as in v1.0"), not invented here.
        ("CFG-INGESTION-CONFIDENCE-AUTOMATCH", "0.90", "DECIMAL", "design target A-120"),
        ("CFG-INGESTION-CONFIDENCE-REVIEW-FLOOR", "0.55", "DECIMAL", "design target A-121"),
        # grace window before a newly-published price list takes effect
        # (A12.2 step 5). This WAS tagged "assume: A-BE-09 (no source
        # value)", which was simply wrong: addendum-v11.md §A19 gives this
        # key its own row, A-165 = 24h (also مواصفة_المنتج_v1.0.md line
        # 707/1544) — the same value already seeded, only the false
        # "no source" tag was. Fixed in place, not left diverged (B fix,
        # item 4's "CFG-PRICELIST-EFFECTIVE-GRACE-HOURS bad tag").
        ("CFG-PRICELIST-EFFECTIVE-GRACE-HOURS", "24", "INT", "design target A-165"),
        # was tagged "assume: A-BE-10 (no source value)"; spec's A-07 row
        # gives this key a real source and value, 3 (مواصفة_المنتج_v1.0.md
        # line 12629 / A19 line 344) — the value already matched, only the
        # false "no source" tag was wrong.
        ("CFG-DELIVERY-MAX-ATTEMPTS", "3", "INT", "design target A-07"),
        # SM-09 dispute-resolution SLA (§7 group 10, §16).
        ("CFG-DISPUTE-RESOLUTION-DAYS", "5", "INT", "design target A-10"),
        ("CFG-DISPUTE-ASSIGNMENT-HOURS", "24", "INT", "design target A-141"),
        # was seeded as 48h/"A-14"; spec's A-140 row states 120h
        # (مواصفة_المنتج_v1.0.md line 12655/A19 line 56).
        ("CFG-VERIFICATION-SLA-HOURS", "120", "INT", "design target A-140"),
        # was tagged "assume: A-BE-11 (no source value)"; spec's A-143 row
        # gives this key a real source and value, 72 (already matched, only
        # the false "no source" tag was wrong).
        ("CFG-INVOICE-UPLOAD-SLA-HOURS", "72", "INT", "design target A-143"),
        # SM-08 licence/expiry-warning window, shared by VendorAccount and
        # PharmacyAccount (§6 SM-08, §16).
        ("CFG-LICENCE-EXPIRY-WARNING-DAYS", "30", "INT", "design target A-94"),
        # SM-22 mode-switch gate-freshness and post-flip observation window
        # (addendum-v11.md §A19, lines 1539-1540).
        ("CFG-SWITCH-GATE-MAX-AGE-HOURS", "72", "INT", "design target A-160"),
        ("CFG-SWITCH-OBSERVATION-DAYS", "14", "INT", "design target A-161"),
        # SM-24 support-thread human-handover-response SLA (addendum-v11.md
        # §A19, line 1555).
        ("CFG-SUPPORT-HANDOVER-SLA-MINUTES", "5", "INT", "design target A-176"),

        # ---- item 4 (backend-review-r1.md): §16 names 41 keys; only the 18
        # above were seeded. The 23 below fill the gap — every value/unit/
        # A-number is copied verbatim from مواصفة_المنتج_v1.0.md §16 (lines
        # ~12624-12665), re-checked against A19's own A-xx table, not
        # invented. Grouped here in the same §16 row order as the source
        # table so a future diff against §16 is a straight top-to-bottom
        # read, not a hunt.
        ("CFG-HEALTH-SCORE-WINDOW-DAYS", "30", "INT", "design target A-09"),
        ("CFG-CONFIRMATION-TIMEOUT-HOURS", "48", "INT", "design target A-11"),
        ("CFG-HUB-CONSOLIDATION-ENABLED", "false", "BOOL", "design target A-93"),
        ("CFG-SMS-DAILY-CAP-PER-RECIPIENT", "5", "INT", "design target A-80"),
        ("CFG-PUSH-ACTIVE-WINDOW-HOURS", "72", "INT", "design target A-81"),
        ("CFG-APP-INSTALL-SIZE-TARGET-MB", "25", "INT", "design target A-83"),
        ("CFG-CATALOGUE-SYNC-INTERVAL-HOURS", "6", "INT", "design target A-84"),
        ("CFG-IMAGE-UPLOAD-TARGET-KB", "200", "INT", "design target A-85"),
        ("CFG-ORDER-DATA-BUDGET-TARGET-KB", "150", "INT", "design target A-86"),
        ("CFG-OFFLINE-QUEUE-MAX-AGE-DAYS", "14", "INT", "design target A-87"),
        ("CFG-SEARCH-MAX-EDIT-DISTANCE", "2", "INT", "design target A-88"),
        ("CFG-SESSION-IDLE-TIMEOUT-MINUTES", "30", "INT", "design target A-89"),
        ("CFG-MOBILE-SESSION-IDLE-TIMEOUT-HOURS", "12", "INT", "design target A-89"),
        ("CFG-COURIER-MAX-BOUND-DEVICES", "1", "INT", "design target A-91"),
        ("CFG-COURIER-LOCATION-PING-INTERVAL-SECONDS", "60", "INT", "design target A-95"),
        ("CFG-RETENTION-PRESCRIPTION-PHOTO-HOURS", "72", "INT", "design target A-90"),
        ("CFG-RETENTION-VOICE-NOTE-DAYS", "30", "INT", "design target A-90"),
        ("CFG-RETENTION-AUDIT-LOG-YEARS", "5", "INT", "design target A-90"),
        ("CFG-RETENTION-DSR-RECORD-DAYS", "1825", "INT", "design target A-90"),
        ("CFG-ADMIN-APPROVAL-TIMEOUT-HOURS", "24", "INT", "design target A-142"),
        ("CFG-CATALOGUE-CACHE-TTL-DAYS", "7", "INT", "design target A-150"),
        ("CFG-OFFLINE-QUEUE-MAX-ACTIONS", "200", "INT", "design target A-151"),
        ("CFG-SYNC-MAX-RETRY-ATTEMPTS", "5", "INT", "design target A-152"),

        # ---- CFG-LOGIN-LOCKOUT-MINUTES: §16 gives CFG-LOGIN-LOCKOUT-ATTEMPTS
        # (above) its own row but names no companion *duration* key anywhere
        # in §16/addendum §A19; only the assumption register's A-BE-02 gives
        # a lockout-duration figure (15 min), the same status as this
        # session's own CFG-HEALTH-SCORE-FORMULA/CFG-ETA-VENDOR-VARIANCE-HOURS
        # additions. Registered so rova/auth/router.py can resolve it through
        # the config service instead of a hard-coded constant (item 4).
        ("CFG-LOGIN-LOCKOUT-MINUTES", "15", "INT", "assume: A-BE-02"),

        # ---- CFG-PRICELIST-MAPPING-AUTO-SUGGEST-THRESHOLD: six-defect repair
        # round "also fix" item — rova/pricelist/mapping.py::suggest_mapping
        # used to hard-code this as a Python constant (AUTO_SUGGEST_THRESHOLD
        # = 70) with no §16 key behind it (no spec row names a column-header
        # label-similarity threshold; CFG-INGESTION-CONFIDENCE-AUTOMATCH/
        # -REVIEW-FLOOR above are a different threshold, §4's row-level
        # product-matching confidence). Same value, only moved into config
        # (A2.10) — same status as CFG-LOGIN-LOCKOUT-MINUTES just above.
        ("CFG-PRICELIST-MAPPING-AUTO-SUGGEST-THRESHOLD", "70", "INT", "backend-introduced (moved constant)"),
    ]
    for key, value, vtype, source in cfg_rows:
        _upsert(session, "config_parameter", "id", {
            "id": f"cfg_{key.lower().replace('-', '_')}", "key": key, "scope_type": "GLOBAL", "scope_id": None,
            "value": value, "value_type": vtype, "owner_role": "PlatformAdmin", "source_tag": source,
        })

    # terms_version
    _upsert(session, "terms_version", "id", {
        "id": "trm_pharmacy_v1", "code": "PHARMACY_TERMS", "version": 1,
        "mentions_pharmacy_service_fee": True,
        "service_fee_amount": Decimal("150.00"),  # SEED-FIXTURE placeholder; real amount is OD-141 (no source, CONTEXT.md)
        "mentions_multi_vendor": True, "mentions_pms_aggregation": False,
        "body_ref": "terms/pharmacy_terms_v1_pt.txt",
    })
    _upsert(session, "terms_version", "id", {
        "id": "trm_vendor_v1", "code": "VENDOR_TERMS", "version": 1,
        "mentions_pharmacy_service_fee": False, "service_fee_amount": None,
        "mentions_multi_vendor": True, "mentions_pms_aggregation": False,
        "body_ref": "terms/vendor_terms_v1_pt.txt",
    })
    _upsert(session, "terms_version", "id", {
        "id": "trm_pms_v1", "code": "PMS_LINK_TERMS", "version": 1,
        "mentions_pharmacy_service_fee": False, "service_fee_amount": None,
        "mentions_multi_vendor": False, "mentions_pms_aggregation": True,
        "body_ref": "terms/pms_link_terms_v1_pt.txt",
    })

    # mode_switch — the three GLOBAL rows (A7)
    _upsert(session, "mode_switch", "id", {
        "id": "mds_vendor_mode", "switch_key": "VENDOR_MODE", "scope_type": "GLOBAL", "scope_id": None,
        "current_value": "SINGLE",
    })
    _upsert(session, "mode_switch", "id", {
        "id": "mds_fee_model", "switch_key": "FEE_MODEL", "scope_type": "GLOBAL", "scope_id": None,
        "current_value": "DELIVERY_FEE,PHARMACY_SERVICE_FEE,VENDOR_SHARE",
    })
    _upsert(session, "mode_switch", "id", {
        "id": "mds_primary_interface", "switch_key": "PRIMARY_INTERFACE", "scope_type": "GLOBAL", "scope_id": None,
        "current_value": "CLASSIC",
    })
    session.commit()


PASSWORD = "rova-demo"


def _user(session: Session, uid: str, phone: str, name: str) -> None:
    _upsert(session, "app_user", "id", {
        "id": uid, "phone": phone, "name": name, "locale": "pt", "password_hash": hash_password(PASSWORD),
    })


def _membership(session: Session, mid: str, uid: str, org_id: str | None, roles: list[str]) -> None:
    _upsert(session, "membership", "id", {"id": mid, "user_id": uid, "organisation_id": org_id, "role_codes": roles})


def seed_domain(session: Session) -> None:
    seed_config_only(session)

    # ---- platform staff ----
    _user(session, "usr_platform_admin", "+258840000090", "Admin Plataforma")
    _membership(session, "mem_platform_admin", "usr_platform_admin", None, ["PlatformAdmin"])
    _user(session, "usr_compliance", "+258840000091", "Oficial de Compliance")
    _membership(session, "mem_compliance", "usr_compliance", None, ["ComplianceOfficer"])
    # A16 names every platform role a user; only PlatformAdmin/ComplianceOfficer
    # were seeded in the prior session — added here since the ingestion,
    # price-list-review and ops-console endpoints in this session's scope
    # need a real OpsReviewer/IndexPharmacist/SupportAgent/PlatformFinance
    # principal to exercise end to end (genuine seed gap, fixed).
    _user(session, "usr_ops", "+258840000092", "Revisor de Operações")
    _membership(session, "mem_ops", "usr_ops", None, ["OpsReviewer"])
    _user(session, "usr_index_pharmacist", "+258840000093", "Farmacêutico do Índice")
    _membership(session, "mem_index_pharmacist", "usr_index_pharmacist", None, ["IndexPharmacist"])
    _user(session, "usr_support", "+258840000094", "Agente de Suporte")
    _membership(session, "mem_support", "usr_support", None, ["SupportAgent"])
    _user(session, "usr_platform_finance", "+258840000095", "Finanças da Plataforma")
    _membership(session, "mem_platform_finance", "usr_platform_finance", None, ["PlatformFinance"])

    # ---- vendor: Ahmed Distribuição, Lda ----
    _upsert(session, "organisation", "id", {
        "id": "org_ahmed", "tax_id": "400000001", "legal_name": "Ahmed Distribuição, Lda",
        "legal_name_normalised": "ahmed distribuicao lda", "type": "VENDOR",
    })
    _upsert(session, "vendor_account", "id", {
        "id": "ven_ahmed", "organisation_id": "org_ahmed", "region_code": "MAPUTO_CIDADE",
        "trade_name": "Ahmed Distribuição", "vendor_type": "IMPORTER_WHOLESALER",
        "delivery_mode": "VENDOR_OWN_FLEET",
        "mov_amount": Decimal("5000.00"),  # fixture, vendor-declared (OD-13: no platform default)
        "acceptance_mode": "AUTO_ACCEPT_FULL", "sourcing_attestation": True, "locale": "ar",
        "status": "ONBOARDING",
    })
    _upsert(session, "licence", "id", {
        "id": "lic_ahmed_alvara", "holder_type": "VENDOR", "holder_id": "ven_ahmed",
        "type": "WHOLESALE_ALVARA", "number": "ALV-2025-0001", "issuer": "ANARME",
        "issue_date": date(2025, 1, 1), "expiry_date": date(2027, 6, 30),
        "document_ref": "licences/ven_ahmed/alvara.pdf", "status": "VALID",
    })
    _upsert(session, "vendor_agreement", "id", {
        "id": "vag_ahmed", "vendor_id": "ven_ahmed", "signed_at": now(), "document_ref": "agreements/ven_ahmed.pdf",
        "founding_supplier": True, "multi_vendor_clause_ack": True,
        "exclusivity_until": date.today() + timedelta(days=180),
        "preferential_rate_until": date.today() + timedelta(days=180),
        "badge_until": date.today() + timedelta(days=180),  # [assume: A-163] six months from seed date
        "status": "ACTIVE",
    })
    _user(session, "usr_ahmed_admin", "+258840000001", "Ahmed — Administrador")
    _membership(session, "mem_ahmed_admin", "usr_ahmed_admin", "org_ahmed", ["VendorAdmin"])
    _user(session, "usr_ahmed_desk", "+258840000002", "Ahmed — Balcão de Encomendas")
    _membership(session, "mem_ahmed_desk", "usr_ahmed_desk", "org_ahmed", ["VendorOrderDesk"])
    _user(session, "usr_ahmed_finance", "+258840000003", "Ahmed — Financeiro")
    _membership(session, "mem_ahmed_finance", "usr_ahmed_finance", "org_ahmed", ["VendorFinance"])
    _user(session, "usr_ahmed_picker", "+258840000004", "Ahmed — Picking")
    _membership(session, "mem_ahmed_picker", "usr_ahmed_picker", "org_ahmed", ["VendorPicker"])
    # dispatcher/courier for the VENDOR_OWN_FLEET delivery_job lifecycle
    # (SM-05) — added this session so the end-to-end order loop (item 2,
    # backend-review-r1.md) has real actors to drive assign/start/attempts
    # with, both in seed's own order-flow demo and for a pilot pharmacy.
    _user(session, "usr_ahmed_dispatcher", "+258840000005", "Ahmed — Despacho")
    _membership(session, "mem_ahmed_dispatcher", "usr_ahmed_dispatcher", "org_ahmed", ["Dispatcher"])
    _user(session, "usr_ahmed_courier", "+258840000006", "Ahmed — Estafeta")
    _membership(session, "mem_ahmed_courier", "usr_ahmed_courier", "org_ahmed", ["Courier"])
    session.commit()

    # second vendor, deliberately left in ONBOARDING so a SINGLE->MULTI gate
    # (G4: >= 2 ACTIVE vendors) demonstrably fails (A16).
    _upsert(session, "organisation", "id", {
        "id": "org_mozpharma", "tax_id": "400000002", "legal_name": "MozPharma Demo, Lda",
        "legal_name_normalised": "mozpharma demo lda", "type": "VENDOR",
    })
    _upsert(session, "vendor_account", "id", {
        "id": "ven_mozpharma", "organisation_id": "org_mozpharma", "region_code": "MATOLA",
        "trade_name": "MozPharma Demo", "vendor_type": "DISTRIBUTOR", "delivery_mode": "LICENSED_TRANSPORTER",
        "mov_amount": Decimal("3000.00"), "acceptance_mode": "MANUAL_CONFIRM", "status": "ONBOARDING",
    })
    session.commit()

    # approve Ahmed through the real SM-06 machine (proves the FSM, not a
    # raw status write)
    ahmed_row = session.execute(text("SELECT status FROM vendor_account WHERE id='ven_ahmed'")).scalar()
    if ahmed_row == "ONBOARDING":
        MACHINES["SM-06"].apply(session, "ven_ahmed", "APPROVE",
                                 Principal(user_id="usr_compliance", membership_id="mem_compliance",
                                           organisation_id=None, roles=frozenset({"ComplianceOfficer"}),
                                           surface="OP", pharmacy_id=None, vendor_id=None, transporter_id=None))
        session.commit()

    # ---- pharmacies ----
    _upsert(session, "organisation", "id", {
        "id": "org_baixa", "tax_id": "500000001", "legal_name": "Farmácia Central da Baixa",
        "legal_name_normalised": "farmacia central da baixa", "type": "PHARMACY",
    })
    _upsert(session, "pharmacy_account", "id", {
        "id": "pha_baixa", "organisation_id": "org_baixa", "region_code": "MAPUTO_CIDADE",
        "licence_type": "A", "trade_name": "Farmácia Central da Baixa", "address": "Av. 25 de Setembro, Maputo",
        "latitude": Decimal("-25.969200"), "longitude": Decimal("32.573200"), "status": "ONBOARDING",
    })
    _upsert(session, "licence", "id", {
        "id": "lic_baixa_retail", "holder_type": "PHARMACY", "holder_id": "pha_baixa", "type": "RETAIL_A",
        "number": "RET-2025-0101", "issuer": "ANARME", "issue_date": date(2025, 1, 1),
        "expiry_date": date(2027, 1, 1), "document_ref": "licences/pha_baixa/retail.pdf", "status": "VALID",
    })
    _user(session, "usr_baixa_admin", "+258840000010", "Baixa — Administradora")
    _membership(session, "mem_baixa_admin", "usr_baixa_admin", "org_baixa", ["PharmacyAdmin"])
    _user(session, "usr_baixa_buyer", "+258840000011", "Baixa — Compradora")
    _membership(session, "mem_baixa_buyer", "usr_baixa_buyer", "org_baixa", ["PharmacyBuyer"])
    _user(session, "usr_baixa_receiver", "+258840000012", "Baixa — Recepção")
    _membership(session, "mem_baixa_receiver", "usr_baixa_receiver", "org_baixa", ["PharmacyReceiver"])
    session.commit()

    row = session.execute(text("SELECT status FROM pharmacy_account WHERE id='pha_baixa'")).scalar()
    if row == "ONBOARDING":
        MACHINES["SM-07"].apply(session, "pha_baixa", "APPROVE",
                                 Principal(user_id="usr_compliance", membership_id="mem_compliance",
                                           organisation_id=None, roles=frozenset({"ComplianceOfficer"}),
                                           surface="OP", pharmacy_id=None, vendor_id=None, transporter_id=None))
        session.commit()

    # Baixa consents to PHARMACY_TERMS v1 (unlocks the R-147 pharmacy fee)
    _upsert(session, "consent_record", "id", {
        "id": "cns_baixa_terms", "subject_type": "PHARMACY_ACCOUNT", "subject_id": "pha_baixa",
        "purpose": "PHARMACY_TERMS", "terms_version_id": "trm_pharmacy_v1",
        "granted_by_user_id": "usr_baixa_admin",
    })

    # second pharmacy — deliberately WITHOUT consent, demonstrating R-147
    _upsert(session, "organisation", "id", {
        "id": "org_costa", "tax_id": "500000002", "legal_name": "Farmácia Costa do Sol",
        "legal_name_normalised": "farmacia costa do sol", "type": "PHARMACY",
    })
    _upsert(session, "pharmacy_account", "id", {
        "id": "pha_costa", "organisation_id": "org_costa", "region_code": "MAPUTO_CIDADE",
        "licence_type": "POSTO_DE_VENDA", "trade_name": "Farmácia Costa do Sol", "address": "Costa do Sol, Maputo",
        "latitude": Decimal("-25.925000"), "longitude": Decimal("32.622000"), "status": "LICENCE_EXPIRING",
    })
    _upsert(session, "licence", "id", {
        "id": "lic_costa_retail", "holder_type": "PHARMACY", "holder_id": "pha_costa", "type": "POSTO_DE_VENDA",
        "number": "RET-2025-0102", "issuer": "ANARME", "issue_date": date(2024, 1, 1),
        "expiry_date": date.today() + timedelta(days=20), "document_ref": "licences/pha_costa/retail.pdf",
        "status": "EXPIRING_SOON",
    })
    session.commit()

    # ---- catalogue: a reduced real-INN subset (A16 names 163; this session
    # seeds 10 regulated + 2 free-price, honestly reduced — see manifest) ----
    products = [
        ("idx_paracetamol_500", "Paracetamol", "Comprimido", "500 mg", "20", "Generic Labs", True),
        ("idx_ibuprofeno_400", "Ibuprofeno", "Comprimido", "400 mg", "20", "Generic Labs", True),
        ("idx_amoxicilina_500", "Amoxicilina", "Cápsula", "500 mg", "21", "Generic Labs", True),
        ("idx_azitromicina_500", "Azitromicina", "Comprimido", "500 mg", "3", "Generic Labs", True),
        ("idx_ciprofloxacina_500", "Ciprofloxacina", "Comprimido", "500 mg", "10", "Generic Labs", True),
        ("idx_metronidazol_400", "Metronidazol", "Comprimido", "400 mg", "20", "Generic Labs", True),
        ("idx_diclofenac_50", "Diclofenac", "Comprimido", "50 mg", "20", "Generic Labs", True),
        ("idx_aas_100", "Ácido Acetilsalicílico", "Comprimido", "100 mg", "30", "Generic Labs", True),
        ("idx_smx_tmp", "Sulfametoxazol + Trimetoprim", "Comprimido", "800/160 mg", "10", "Generic Labs", True),
        ("idx_vitc_1000", "Vitamina C", "Comprimido efervescente", "1000 mg", "20", "Generic Labs", False),
        ("idx_alcool_gel", "Álcool gel 70%", "Gel", "70%", "500 ml", "Higiene Lda", False),
        ("idx_luvas_nitrilo", "Luvas de nitrilo M", "Caixa", "M", "100", "Higiene Lda", False),
    ]
    for pid, inn, form, strength, pack, manufacturer, regulated in products:
        search = f"{inn} {form} {strength} {pack}".lower()
        _upsert(session, "index_product", "id", {
            "id": pid, "inn": inn, "form": form, "strength": strength, "pack_size": pack,
            "manufacturer": manufacturer, "aim_status": "AUTHORISED", "regulated_price": regulated,
            "review_status": "PUBLISHED", "reviewer_ref": "LOCAL:usr_compliance", "search_text": search,
        })
        if regulated:
            # fixture wholesale/PVP values — illustrative, NOT the official
            # Diploma Ministerial 21/2017 figures (no public per-product table exists)
            _upsert(session, "price_reference", "id", {
                "id": f"prf_{pid}", "index_product_id": pid,
                "pvp_price": Decimal("120.00"), "wholesale_derived_price": Decimal("85.00"),
                "cif_basis": Decimal("60.00"), "effective_from": date(2026, 1, 1),
                "source_document": "SEED-FIXTURE: illustrative value, not the official Diploma Ministerial 21/2017 figure",
            })
            offer_price = None
        else:
            offer_price = Decimal("95.00")  # fixture free-price offer
        _upsert(session, "vendor_offer", "id", {
            "id": f"ofr_ahmed_{pid}", "vendor_id": "ven_ahmed", "index_product_id": pid,
            "regulated_price": regulated, "qty_available": 500, "pack_size": pack, "min_order_qty": None,
            "expiry_horizon_days": 365, "price": offer_price, "stock_confirmed_at": now(),
            "freshness_state": "FRESH",
        })
    session.commit()

    # ---- credit facility ----
    _upsert(session, "credit_facility", "id", {
        "id": "crf_ahmed_baixa", "vendor_id": "ven_ahmed", "pharmacy_id": "pha_baixa",
        "limit_amount": Decimal("50000.00"),  # fixture
        "opening_balance": Decimal("0.00"), "terms_days": 30, "status": "ACTIVE",
    })

    # ---- fee schedules (A16; notes carry provenance per A2.24) ----
    _upsert(session, "fee_schedule", "id", {
        "id": "fsc_vendor_share_ahmed", "type": "VENDOR_SHARE", "payer": "VENDOR",
        "rate_or_amount": Decimal("0.0250"), "base": "NET_DELIVERED_VALUE",
        "applies_to": "INCREMENTAL_ORDERS_ONLY", "scope_type": "VENDOR", "scope_id": "ven_ahmed",
        "effective_from": date(2026, 1, 1), "earning_event": "RECEIPT_ACCEPTED", "legal_status": "OWNER_ACCEPTED",
        "agreement_id": "vag_ahmed",
        "notes": "owner negotiation target 2-3% (CONTEXT-V11 unit economics model); base=NET_DELIVERED_VALUE in "
                 "this seed rather than the spec's DECLARED_MARGIN because the price-list vendor_cost pipeline is "
                 "out of this session's reduced scope (see manifest)",
        "created_by_user_id": "usr_platform_admin",
    })
    _upsert(session, "fee_schedule", "id", {
        "id": "fsc_pharmacy_service_fee", "type": "PHARMACY_SERVICE_FEE", "payer": "PHARMACY",
        "rate_or_amount": Decimal("150.00"), "base": None, "applies_to": "ALL_ORDERS",
        "scope_type": "GLOBAL", "scope_id": None, "effective_from": date(2026, 1, 1),
        "earning_event": "RECEIPT_ACCEPTED", "legal_status": "STANDARD", "agreement_id": None,
        "notes": "SEED-FIXTURE placeholder; the real amount is OD-141 / field test T9 — no source (CONTEXT.md)",
        "created_by_user_id": "usr_platform_admin",
    })
    _upsert(session, "fee_schedule", "id", {
        "id": "fsc_delivery_maputo", "type": "PER_DELIVERY_JOB", "payer": "PHARMACY",
        "rate_or_amount": Decimal("250.00"), "base": None, "applies_to": "ALL_ORDERS",
        "scope_type": "REGION", "scope_id": "MAPUTO_CIDADE", "effective_from": date(2026, 1, 1),
        "earning_event": "DELIVERY_DELIVERED", "legal_status": "STANDARD", "agreement_id": None,
        "notes": "fixture; delivery cost per order 250 MZN from the project economic model is a cost input, "
                 "not a validated market price (CONTEXT.md unit economics)",
        "created_by_user_id": "usr_platform_admin",
    })
    session.commit()

    # ---- one full order lifecycle through the real services (A16 "orders")
    _seed_order_flow(session)


def _seed_order_flow(session: Session) -> None:
    """Item-2 fix (backend-review-r1.md): this demo order now goes through
    every step the real HTTP API uses — SM-04 `CONFIRM_FULL` (never a raw
    `order_line` UPDATE of `confirmed_qty`/`fulfilment_status`), pick
    (`traceability_event(PICKED)`), SM-05 `ASSIGN`/`START`/`ATTEMPT_DELIVERED`
    (never a bare SM-03 `DELIVERED`), and a real `receipt`/`receipt_line`
    before `RECEIPT_ACCEPT` — the same machinery `tests/api/test_orders_flow.py`
    drives over HTTP."""
    existing = session.execute(text("SELECT id FROM request WHERE number = 'RQ-SEED-000001'")).scalar()
    if existing:
        return

    buyer = Principal(user_id="usr_baixa_buyer", membership_id="mem_baixa_buyer", organisation_id="org_baixa",
                       roles=frozenset({"PharmacyBuyer"}), surface="PH", pharmacy_id="pha_baixa",
                       vendor_id=None, transporter_id=None)
    desk = Principal(user_id="usr_ahmed_desk", membership_id="mem_ahmed_desk", organisation_id="org_ahmed",
                      roles=frozenset({"VendorOrderDesk"}), surface="VN", pharmacy_id=None,
                      vendor_id="ven_ahmed", transporter_id=None)
    dispatcher = Principal(user_id="usr_ahmed_dispatcher", membership_id="mem_ahmed_dispatcher",
                            organisation_id="org_ahmed", roles=frozenset({"Dispatcher"}), surface="VN",
                            pharmacy_id=None, vendor_id="ven_ahmed", transporter_id=None)
    courier = Principal(user_id="usr_ahmed_courier", membership_id="mem_ahmed_courier",
                         organisation_id="org_ahmed", roles=frozenset({"Courier"}), surface="VN",
                         pharmacy_id=None, vendor_id="ven_ahmed", transporter_id=None)
    receiver = Principal(user_id="usr_baixa_receiver", membership_id="mem_baixa_receiver",
                          organisation_id="org_baixa", roles=frozenset({"PharmacyReceiver"}), surface="PH",
                          pharmacy_id="pha_baixa", vendor_id=None, transporter_id=None)

    from rova.core.ids import new_id
    rid = new_id("req")
    session.execute(
        text(
            "INSERT INTO request (id, number, pharmacy_id, mode, channel, status, allocation_strategy, "
            "created_by_user_id) VALUES (:id, 'RQ-SEED-000001', 'pha_baixa', 'CATALOGUE', 'APP', 'DRAFT', "
            "'FEWEST_VENDORS', 'usr_baixa_buyer')"
        ),
        {"id": rid},
    )
    for pid, qty in [("idx_paracetamol_500", 10), ("idx_alcool_gel", 3)]:
        session.execute(
            text("INSERT INTO request_line (id, request_id, index_product_id, qty_requested, match_status) "
                 "VALUES (:id, :r, :p, :q, 'RESOLVED')"),
            {"id": new_id("rql"), "r": rid, "p": pid, "q": qty},
        )
    session.commit()

    result = run_checkout(session, request_id=rid, actor=buyer)
    session.commit()
    order_id = result["orders"][0]["id"]

    lines = session.execute(text("SELECT id, ordered_qty FROM order_line WHERE order_id=:o"), {"o": order_id}).mappings().all()
    for l in lines:
        MACHINES["SM-04"].apply(session, l["id"], "CONFIRM_FULL", desk)
    MACHINES["SM-03"].apply(session, order_id, "ACCEPT", desk)
    session.commit()

    # pick: batch/lot/expiry/seals + traceability_event(PICKED) per seal, so
    # the DISPATCH guard (R-054) passes and Track & Trace has real capture
    # rows (attribute columns, not a status write — same as the /pick endpoint).
    for l in lines:
        seal = f"SEAL-{l['id']}"
        exp = date.today() + timedelta(days=365)
        session.execute(
            text("UPDATE order_line SET batch_number='B-2026-01', lot_number='L-01', "
                 "expiry_date=:exp, seal_ids=ARRAY[:seal], picked_at=:now WHERE id=:id"),
            {"exp": exp, "seal": seal, "now": now(), "id": l["id"]},
        )
        session.execute(
            text("INSERT INTO traceability_event (id, order_line_id, seal_id, batch_number, lot_number, "
                 "expiry_date, event_type, actor_user_id, actor_role) VALUES "
                 "(:id, :ol, :seal, 'B-2026-01', 'L-01', :exp, 'PICKED', 'usr_ahmed_picker', 'VendorPicker')"),
            {"id": new_id("trc"), "ol": l["id"], "seal": seal, "exp": exp},
        )
    session.commit()

    MACHINES["SM-03"].apply(session, order_id, "DISPATCH", desk)
    session.commit()

    delivery_job = session.execute(
        text("SELECT id FROM delivery_job WHERE order_id=:o"), {"o": order_id}
    ).mappings().one()
    MACHINES["SM-05"].apply(session, delivery_job["id"], "ASSIGN", dispatcher,
                             courier_user_id="usr_ahmed_courier", transporter_id=None)
    MACHINES["SM-05"].apply(session, delivery_job["id"], "START", courier)
    session.execute(
        text("INSERT INTO delivery_attempt (id, delivery_job_id, attempt_no, outcome, courier_user_id, "
             "proof_signature_or_code, proof_captured_at) VALUES "
             "(:id, :job, 1, 'DELIVERED', 'usr_ahmed_courier', 'SIGNED-SEED-DEMO', :now)"),
        {"id": new_id("dla"), "job": delivery_job["id"], "now": now()},
    )
    MACHINES["SM-05"].apply(session, delivery_job["id"], "ATTEMPT_DELIVERED", courier)
    session.commit()

    receipt_id = new_id("rcp")
    session.execute(
        text("INSERT INTO receipt (id, order_id, received_by_user_id) VALUES (:id, :o, 'usr_baixa_receiver')"),
        {"id": receipt_id, "o": order_id},
    )
    for l in lines:
        confirmed_qty = session.execute(
            text("SELECT confirmed_qty FROM order_line WHERE id=:id"), {"id": l["id"]}
        ).scalar()
        session.execute(
            text("INSERT INTO receipt_line (id, receipt_id, order_line_id, accepted_qty) VALUES (:id, :r, :ol, :q)"),
            {"id": new_id("rcl"), "r": receipt_id, "ol": l["id"], "q": confirmed_qty},
        )
    MACHINES["SM-03"].apply(session, order_id, "RECEIPT_ACCEPT", receiver)
    session.commit()


def run_seed(config_only: bool = False) -> None:
    from rova.domain.hooks import wire
    wire()  # register fee-accrual hooks (rova.main:create_app does this for the API; the CLI needs it too)
    session = get_sessionmaker()()
    try:
        if config_only:
            seed_config_only(session)
        else:
            seed_domain(session)
        session.commit()
    finally:
        session.close()
