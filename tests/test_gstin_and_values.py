from datetime import date, datetime
from decimal import Decimal

import pytest

from app.gstin import gstin_error, parse_state_code
from app.ingest.values import gstn_period_to_iso, parse_date, parse_decimal, period_bounds


@pytest.mark.parametrize("gstin", ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4"])
def test_known_valid_gstins(gstin):
    assert gstin_error(gstin) is None


def test_gstin_errors():
    assert "checksum" in gstin_error("27AAPFU0939F1ZA")
    assert "state code" in gstin_error("28AAPFU0939F1ZV")
    assert "15-character" in gstin_error("27AAPFU0939F1Z")


@pytest.mark.parametrize("value,code", [("27", "27"), (27, "27"), ("27-Maharashtra", "27"),
                                        ("west bengal", "19"), ("9", "09"), ("Other Countries", "96")])
def test_state_codes(value, code):
    assert parse_state_code(value) == code


def test_bad_state():
    with pytest.raises(ValueError):
        parse_state_code("Atlantis")


@pytest.mark.parametrize("value,expected", [
    ("1,23,456.789", Decimal("123456.79")), ("(500)", Decimal("-500.00")), (12.5, Decimal("12.50")),
    ("₹ 1,000", Decimal("1000.00")), ("", None), ("-", None),
])
def test_parse_decimal(value, expected):
    assert parse_decimal(value) == expected


@pytest.mark.parametrize("value", ["05-09-2026", "05/09/2026", "5-Sep-26", "2026-09-05", datetime(2026, 9, 5), 46270])
def test_parse_date(value):
    assert parse_date(value) == date(2026, 9, 5)


def test_periods():
    assert period_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert gstn_period_to_iso("092026") == "2026-09"
