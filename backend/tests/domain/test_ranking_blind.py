"""A17 'Ranking blindness' row — the fee-blind ranking test the architect
explicitly asked for: re-run a ranking with scrambled fee data and assert the
order is identical (executor manifest)."""
import ast
import inspect
from decimal import Decimal

from sqlalchemy import text

import rova.ranking.engine as engine_module
import rova.ranking.inputs as inputs_module
from rova.ranking.engine import Candidate, RankingInput, rank
from rova.ranking.inputs import build_ranking_input

FORBIDDEN_MODULES = {"rova.fees", "rova.billing", "rova.pricelist", "rova.accounts.agreements"}
FORBIDDEN_STRINGS = {"fee_schedule", "fee_event", "vendor_agreement", "platform_invoice", "price_list_row"}


def test_engine_module_static_imports_are_pure():
    tree = ast.parse(inspect.getsource(engine_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    allowed_prefixes = ("dataclasses", "decimal", "hashlib", "json", "rova.domain.enums")
    for name in imported:
        assert any(name == p or name.startswith(p + ".") for p in allowed_prefixes), \
            f"rova.ranking.engine imports forbidden module: {name}"
    for forbidden in FORBIDDEN_MODULES:
        assert forbidden not in imported


def test_inputs_module_does_not_import_forbidden_modules():
    tree = ast.parse(inspect.getsource(inputs_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in FORBIDDEN_MODULES:
        assert not any(name.startswith(forbidden) for name in imported)


def test_inputs_module_source_never_mentions_forbidden_tables():
    source = inspect.getsource(inputs_module)
    for word in FORBIDDEN_STRINGS:
        assert word not in source, f"rova.ranking.inputs mentions forbidden table: {word}"


def _candidate(vendor_id, **overrides):
    base = dict(
        vendor_id=vendor_id, offer_id=f"ofr_{vendor_id}", freshness_state="FRESH", qty_available=100,
        min_order_qty=None, expiry_horizon_days=300, price=Decimal("10.00"),
        fill_rate=Decimal("0.9"), on_time_dispatch_rate=Decimal("0.9"), promise_accuracy=Decimal("0.9"),
        score_value=Decimal("80"), median_promised_dispatch_hours=Decimal("10"),
        payment_terms_available=("UPFRONT",), credit_days=None, mov_amount=Decimal("0"), mov_reached=True,
    )
    base.update(overrides)
    return Candidate(**base)


def test_dynamic_scramble_produces_identical_ordering(session):
    """The dynamic half of the architect's test: build a real DB fixture with
    vendor_offer/vendor_score/fee_schedule/vendor_agreement rows, rank it,
    scramble every fee_schedule.rate_or_amount and vendor_agreement.founding_supplier,
    re-rank, and assert the sequence_snapshot is byte-identical."""
    session.execute(text(
        "INSERT INTO region (code, name, transit_window_hours) VALUES ('RB_REGION', 'R', 4) "
        "ON CONFLICT DO NOTHING"
    ))
    session.execute(text(
        "INSERT INTO organisation (id, tax_id, legal_name, legal_name_normalised, type) VALUES "
        "('org_rb_v1', '931', 'V1', 'v1', 'VENDOR'), ('org_rb_v2', '932', 'V2', 'v2', 'VENDOR'), "
        "('org_rb_p', '933', 'P', 'p', 'PHARMACY')"
    ))
    session.execute(text(
        "INSERT INTO vendor_account (id, organisation_id, region_code, trade_name, vendor_type, delivery_mode, "
        "mov_amount, status) VALUES "
        "('ven_rb_1', 'org_rb_v1', 'RB_REGION', 'V1', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'ACTIVE'), "
        "('ven_rb_2', 'org_rb_v2', 'RB_REGION', 'V2', 'IMPORTER_WHOLESALER', 'VENDOR_OWN_FLEET', 0, 'ACTIVE')"
    ))
    session.execute(text(
        "INSERT INTO pharmacy_account (id, organisation_id, region_code, licence_type, trade_name, address, "
        "latitude, longitude) VALUES ('pha_rb', 'org_rb_p', 'RB_REGION', 'A', 'P', 'X', 0, 0)"
    ))
    session.execute(text(
        "INSERT INTO index_product (id, inn, form, strength, pack_size, manufacturer, aim_status, "
        "regulated_price, reviewer_ref, search_text) VALUES "
        "('idx_rb', 'D', 'c', '1', '1', 'M', 'AUTHORISED', false, 'LOCAL:x', 'd')"
    ))
    for v, price in [("ven_rb_1", "10.00"), ("ven_rb_2", "8.00")]:
        session.execute(text(
            "INSERT INTO vendor_offer (id, vendor_id, index_product_id, regulated_price, qty_available, "
            "pack_size, expiry_horizon_days, price, stock_confirmed_at) VALUES "
            f"('ofr_rb_{v}', :v, 'idx_rb', false, 100, '1', 300, :price, now())"
        ), {"v": v, "price": price})
    session.execute(text(
        "INSERT INTO vendor_agreement (id, vendor_id, signed_at, document_ref, founding_supplier, status) "
        "VALUES ('vag_rb_1', 'ven_rb_1', now(), 'x', true, 'ACTIVE'), "
        "('vag_rb_2', 'ven_rb_2', now(), 'x', false, 'ACTIVE')"
    ))
    session.execute(text(
        "INSERT INTO app_user (id, phone, name, password_hash) VALUES ('usr_rb', '+258000777001', 'U', 'x')"
    ))
    session.execute(text(
        "INSERT INTO fee_schedule (id, type, payer, rate_or_amount, base, applies_to, scope_type, scope_id, "
        "effective_from, earning_event, legal_status, agreement_id, created_by_user_id) VALUES "
        "('fsc_rb_1', 'VENDOR_SHARE', 'VENDOR', 0.0100, 'NET_DELIVERED_VALUE', 'ALL_ORDERS', 'VENDOR', 'ven_rb_1', "
        "'2026-01-01', 'RECEIPT_ACCEPTED', 'OWNER_ACCEPTED', 'vag_rb_1', 'usr_rb')"
    ))

    inp1 = build_ranking_input(session, index_product_id="idx_rb", qty_requested=1, pharmacy_id="pha_rb",
                                strategy="FEWEST_VENDORS")
    result1 = rank(inp1)

    # scramble fee data
    session.execute(text("UPDATE fee_schedule SET rate_or_amount = 0.9900 WHERE id='fsc_rb_1'"))
    session.execute(text("UPDATE vendor_agreement SET founding_supplier = NOT founding_supplier"))

    inp2 = build_ranking_input(session, index_product_id="idx_rb", qty_requested=1, pharmacy_id="pha_rb",
                                strategy="FEWEST_VENDORS")
    result2 = rank(inp2)

    assert result1.snapshot == result2.snapshot
    assert [c.vendor_id for c in result1.ordered] == [c.vendor_id for c in result2.ordered]
    session.rollback()


def test_free_product_price_is_first_sort_key():
    cands = (
        _candidate("expensive", price=Decimal("50.00"), fill_rate=Decimal("1.0")),
        _candidate("cheap", price=Decimal("5.00"), fill_rate=Decimal("0.1")),
    )
    result = rank(RankingInput(index_product_id="p", regulated_price=False, qty_requested=1, pharmacy_id="ph",
                                strategy="FEWEST_VENDORS", candidates=cands))
    assert result.ordered[0].vendor_id == "cheap"  # R-021: price first for free products


def test_regulated_product_ignores_price_uses_fill_rate_first():
    cands = (
        _candidate("low_fill", price=None, fill_rate=Decimal("0.5")),
        _candidate("high_fill", price=None, fill_rate=Decimal("0.9")),
    )
    result = rank(RankingInput(index_product_id="p", regulated_price=True, qty_requested=1, pharmacy_id="ph",
                                strategy="FEWEST_VENDORS", candidates=cands))
    assert result.ordered[0].vendor_id == "high_fill"  # R-020


def test_excluded_vendor_is_filtered_r029():
    cands = (_candidate("v1"), _candidate("v2"))
    result = rank(RankingInput(index_product_id="p", regulated_price=False, qty_requested=1, pharmacy_id="ph",
                                strategy="FEWEST_VENDORS", candidates=cands, excluded_vendor_ids=frozenset({"v1"})))
    assert [c.vendor_id for c in result.ordered] == ["v2"]


def test_single_candidate_flag():
    result = rank(RankingInput(index_product_id="p", regulated_price=False, qty_requested=1, pharmacy_id="ph",
                                strategy="FEWEST_VENDORS", candidates=(_candidate("only"),)))
    assert result.single_candidate is True


def test_stale_offer_is_filtered_r014():
    cands = (_candidate("stale", freshness_state="STALE"), _candidate("fresh"))
    result = rank(RankingInput(index_product_id="p", regulated_price=False, qty_requested=1, pharmacy_id="ph",
                                strategy="FEWEST_VENDORS", candidates=cands))
    assert [c.vendor_id for c in result.ordered] == ["fresh"]
