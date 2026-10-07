from datetime import date
from decimal import Decimal

from app.ingest.portal import parse_portal_json
from app.models import DocType, ImportKind
from tests.conftest import OWN_GSTIN, SUPPLIER_WB, as_json

GSTR2B = {"chksum": "x", "data": {
    "gstin": OWN_GSTIN, "rtnprd": "092026", "gendt": "14-10-2026",
    "docdata": {
        "b2b": [{"ctin": SUPPLIER_WB, "trdnm": "Supplier WB", "supfildt": "11-10-2026", "supprd": "092026",
                 "inv": [{"inum": "INV/889", "typ": "R", "dt": "02-09-2026", "val": 236000, "pos": "19",
                          "rev": "N", "itcavl": "Y", "rsn": "", "srctyp": "e-Invoice",
                          "items": [{"num": 1, "rt": 18, "txval": 200000, "cgst": 18000, "sgst": 18000, "cess": 0}]}]}],
        "cdnr": [{"ctin": SUPPLIER_WB, "trdnm": "Supplier WB", "supfildt": "11-10-2026", "supprd": "092026",
                  "nt": [{"ntnum": "CN-4", "typ": "C", "suptyp": "R", "dt": "20-09-2026", "val": 1180, "pos": "19",
                          "rev": "N", "itcavl": "N", "rsn": "P",
                          "items": [{"num": 1, "rt": 18, "txval": 1000, "cgst": 90, "sgst": 90, "cess": 0}]}]}],
        "impg": [{"refdt": "01-09-2026"}],
    },
}}

GSTR2A = {
    "gstin": OWN_GSTIN, "fp": "092026",
    "b2b": [{"ctin": SUPPLIER_WB, "cfs": "N", "cfs3b": "N", "inv": [
        {"chksum": "x", "inum": "INV/890", "idt": "15-09-2026", "val": 11800, "pos": "19", "rchrg": "N", "inv_typ": "R",
         "itms": [{"num": 1, "itm_det": {"rt": 18, "txval": 10000, "camt": 900, "samt": 900, "csamt": 0}}]}]}],
    "cdn": [],
}


def test_gstr2b_parses_invoices_and_notes():
    result = parse_portal_json(as_json(GSTR2B), ImportKind.GSTR2B)
    assert result.errors == []
    assert (result.gstin, result.period) == (OWN_GSTIN, "2026-09")
    inv, note = result.documents
    assert inv.invoice_no == "INV/889" and inv.invoice_date == date(2026, 9, 2)
    assert inv.taxable_value == Decimal("200000.00") and inv.cgst == Decimal("18000.00")
    assert inv.itc_available is True and inv.supplier_filing_period == "2026-09"
    assert note.doc_type is DocType.CREDIT_NOTE and note.itc_available is False
    assert "POS and supplier state" in note.itc_unavailable_reason
    assert any("impg" in w.message for w in result.warnings)


def test_gstr2a_parses_supplier_filing_status():
    result = parse_portal_json(as_json(GSTR2A), ImportKind.GSTR2A)
    assert result.errors == []
    (doc,) = result.documents
    assert doc.supplier_return_filed is False and doc.sgst == Decimal("900.00")


def test_wrong_kind_and_garbage():
    assert "expected GSTR2A" in parse_portal_json(as_json(GSTR2B), ImportKind.GSTR2A).errors[0].message
    assert "not a valid JSON" in parse_portal_json(b"<html>", ImportKind.GSTR2B).errors[0].message
