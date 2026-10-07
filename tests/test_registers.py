from datetime import date
from decimal import Decimal

from app.ingest.registers import parse_register
from app.models import Direction, DocType, SupplyCategory
from tests.conftest import BAD_CHECKSUM, CUSTOMER_MH, CUSTOMER_WB, OWN_GSTIN, SUPPLIER_WB, xlsx

SALES_HEADER = ["Date", "Voucher No.", "Voucher Type", "Particulars", "GSTIN/UIN", "Place of Supply",
                "HSN/SAC", "Taxable Value", "IGST", "CGST", "SGST", "Invoice Value"]


def tally_sales(*rows) -> bytes:
    return xlsx([["Alcove Mall Road LLP"], ["Sales Register"], ["1-Sep-2026 to 30-Sep-2026"], [],
                 SALES_HEADER, *rows, [None, None, None, "Grand Total", None, None, None, 999999]])


def parse_sales(content, period="2026-09"):
    return parse_register(content, "sales.xlsx", direction=Direction.OUTWARD, own_gstin=OWN_GSTIN, period=period)


def test_tally_sales_register_groups_lines_and_infers_rates():
    result = parse_sales(tally_sales(
        ["01-09-2026", "S/001", "Sales", "Customer WB", CUSTOMER_WB, "19-West Bengal", "997212", 100000, 0, 9000, 9000, 118000],
        ["03-09-2026", "S/002", "Sales", "Customer MH", CUSTOMER_MH, None, "997212", 50000, 9000, 0, 0, 63200],
        ["03-09-2026", "S/002", "Sales", "Customer MH", CUSTOMER_MH, None, "998599", 4000, 200, 0, 0, 63200],
        ["10-09-2026", "CN/01", "Credit Note", "Customer WB", CUSTOMER_WB, "19", "997212", -10000, 0, -900, -900, -11800],
    ))
    assert result.errors == []
    assert result.header_row == 5
    assert [i.invoice_no for i in result.invoices] == ["S/001", "S/002", "CN/01"]

    s1, s2, cn = result.invoices
    assert s1.items[0].tax_rate == Decimal("18") and s1.place_of_supply == "19"
    assert s2.place_of_supply == "27"  # derived from customer GSTIN
    assert [i.tax_rate for i in s2.items] == [Decimal("18"), Decimal("5")]
    assert s2.total("taxable_value") == Decimal("54000.00") and s2.invoice_value == Decimal("63200.00")
    assert cn.doc_type is DocType.CREDIT_NOTE and cn.total("cgst") == Decimal("900.00")
    assert s1.source_rows == [6]


def test_unrecognisable_rate_is_an_error():
    result = parse_sales(tally_sales(
        ["01-09-2026", "S/001", "Sales", "X", CUSTOMER_WB, None, None, 10000, 0, 37, 37, 10074],
    ))
    assert any("cannot infer tax rate" in e.message for e in result.errors)


def test_validation_errors_block_and_report_rows():
    result = parse_sales(tally_sales(
        ["01-09-2026", "S/001", "Sales", "X", BAD_CHECKSUM, None, None, 1000, 0, 90, 90, 1180],
        ["01-09-2026", "S/002", "Sales", "X", CUSTOMER_WB, None, None, 1000, 180, 90, 90, 1180],
        ["01-09-2026", "S/003", "Sales", "X", CUSTOMER_WB, None, None, -1000, 0, -90, -90, -1180],
        ["bad date", "S/004", "Sales", "X", CUSTOMER_WB, None, None, 1000, 0, 90, 90, 1180],
        ["01-11-2026", "S/005", "Sales", "X", CUSTOMER_WB, None, None, 1000, 0, 90, 90, 1180],
    ))
    by_row = {e.row: e.message for e in result.errors}
    assert "checksum" in by_row[6]
    assert "both IGST and CGST/SGST" in by_row[7]
    assert "negative amounts" in by_row[8]
    assert "not a recognised date" in by_row[9]
    assert "after the end of 2026-09" in by_row[10]


def test_warnings_for_wrong_tax_type_and_b2c_without_pos():
    result = parse_sales(tally_sales(
        ["01-09-2026", "S/001", "Sales", "MH customer", CUSTOMER_MH, None, None, 1000, 0, 90, 90, 1180],
        ["02-09-2026", "S/002", "Sales", "Walk-in", None, None, None, 1000, 0, 90, 90, 1180],
    ))
    assert result.errors == []
    messages = " | ".join(w.message for w in result.warnings)
    assert "another state but CGST/SGST" in messages
    assert "assumed own state 19" in messages


def test_conflicting_invoice_level_fields():
    result = parse_sales(tally_sales(
        ["01-09-2026", "S/001", "Sales", "X", CUSTOMER_WB, None, None, 1000, 0, 90, 90, None],
        ["02-09-2026", "S/001", "Sales", "X", CUSTOMER_WB, None, None, 1000, 0, 90, 90, None],
    ))
    assert any("conflicting invoice date" in e.message for e in result.errors)


def test_missing_header():
    result = parse_sales(xlsx([["foo", "bar"], [1, 2]]))
    assert "no header row found" in result.errors[0].message


def test_purchase_register_prefers_supplier_invoice_number_and_reads_csv():
    csv_text = (
        "Date,Voucher No,Supplier Invoice No,Supplier Invoice Date,Party Name,GSTIN,Tax Rate,Taxable Value,"
        "CGST,SGST,Reverse Charge,ITC Eligible,Supply Type\n"
        f"05-09-2026,P-17,INV/889,02-09-2026,Supplier WB,{SUPPLIER_WB},18,\"2,00,000\",18000,18000,N,Y,\n"
        "06-09-2026,P-18,LEGAL-12,01-09-2026,Advocate,,18,50000,0,0,Y,Y,\n"
        f"07-09-2026,P-19,EX-1,03-09-2026,Supplier WB,{SUPPLIER_WB},0,1000,0,0,N,N,Exempt\n"
    )
    result = parse_register(csv_text.encode(), "purchases.csv", direction=Direction.INWARD,
                            own_gstin=OWN_GSTIN, period="2026-09")
    assert result.errors == []
    p1, rcm, exempt = result.invoices
    assert p1.invoice_no == "INV/889" and p1.invoice_date == date(2026, 9, 2)
    assert p1.total("taxable_value") == Decimal("200000.00") and p1.itc_eligible is True
    assert rcm.reverse_charge and rcm.counterparty_gstin is None
    assert exempt.supply_category is SupplyCategory.EXEMPT and exempt.itc_eligible is False
    assert "Voucher No" in result.unmapped_columns and "Date" in result.unmapped_columns
