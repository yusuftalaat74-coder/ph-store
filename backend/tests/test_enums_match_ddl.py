"""A2.3, A17 'Enums' row — every CHECK ... IN (...) list in the DDL equals
the corresponding StrEnum in rova/domain/enums.py."""
from pathlib import Path

from rova.domain.enums import ENUM_REGISTRY, parse_ddl_enum_checks

SQL_PATH = Path(__file__).resolve().parents[1] / "migrations" / "sql" / "0001_initial.sql"


def test_every_ddl_check_has_a_matching_enum():
    parsed = parse_ddl_enum_checks(SQL_PATH.read_text())
    assert len(parsed) >= 100  # sanity: the parser is actually finding the checks
    missing = []
    mismatched = []
    for table, column, values in parsed:
        cls = ENUM_REGISTRY.get((table, column))
        if cls is None:
            missing.append((table, column))
            continue
        if [m.value for m in cls] != values:
            mismatched.append((table, column, [m.value for m in cls], values))
    assert not missing, f"no enum registered for: {missing}"
    assert not mismatched, f"enum values differ from DDL: {mismatched}"
