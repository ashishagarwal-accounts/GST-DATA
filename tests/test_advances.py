from decimal import Decimal

import pytest

from app.ingest.advances import split_inclusive
from tests.conftest import xlsx

D = Decimal
ADV_HEADER = ["Date", "Voucher No", "Type", "Customer Name", "Customer GSTIN", "Service Type", "Amount (incl. GST)",
              "Against Voucher No"]
SALES_HEADER = ["Invoice No", "Invoice Date", "Customer Name", "Service Type", "Tax Rate", "Taxable Value",
                "IGST", "CGST", "SGST"]


def test_split_inclusive_with_and_without_land_deduction():
    flat = split_inclusive(D("1000000"), D("18"), land_deduction=True, interstate=False)
    assert flat.taxable_value == D("595238.10")
    assert (flat.cgst, flat.sgst) == (D("53571.43"), D("53571.43"))
    cam = split_inclusive(D("59000"), D("18"), land_deduction=False, interstate=True)
    assert (cam.taxable_value, cam.igst, cam.cgst) == (D("50000.00"), D("9000.00"), D("0"))


@pytest.fixture
def adv_gstin(client, own_gstin):
    (entity,) = client.get("/api/entities").json()
    gid = entity["gstins"][0]["id"]
    for name, land in (("Flat Sale", True), ("CAM", False)):
        r = client.post(f"/api/gstins/{gid}/service-types", json={"name": name, "tax_rate": 18, "land_deduction": land})
        assert r.status_code == 201, r.text
    return own_gstin, gid


def upload(client, gstin, kind, period, rows, header=ADV_HEADER, replace=False):
    return client.post("/api/imports/register", files={"file": ("f.xlsx", xlsx([header, *rows]))},
                       data={"gstin": gstin, "period": period, "kind": kind, "replace": str(replace).lower()})


def report(client, gstin, period):
    r = client.get(f"/api/advances/report?gstin={gstin}&period={period}")
    assert r.status_code == 200, r.text
    return r.json()


def test_advances_require_enabling(client, adv_gstin):
    gstin, _ = adv_gstin
    r = upload(client, gstin, "advance_register", "2026-08", [["05-08-2026", "R1", "Advance", "X", "", "CAM", 1000, ""]])
    assert r.status_code == 422 and "not enabled" in r.json()["errors"][0]["message"]


def test_full_advance_cycle(client, adv_gstin):
    gstin, gid = adv_gstin
    assert client.patch(f"/api/gstins/{gid}", json={"advance_tax_enabled": True}).json()["advance_tax_enabled"]

    # Go-live August 2026 with one unadjusted advance from January.
    r = upload(client, gstin, "advance_opening", "2026-08",
               [["15-01-2026", "RCPT/0457", "", "Priya Das", "", "Flat Sale", 500000, ""]])
    assert r.status_code == 201, r.text

    # August: two advances; part of the CAM advance is invoiced the same month.
    r = upload(client, gstin, "advance_register", "2026-08", [
        ["05-08-2026", "RCPT/101", "Advance", "Rahul Sharma", "", "Flat Sale", 1000000, ""],
        ["10-08-2026", "RCPT/102", "Advance", "Tenant A Retail", "", "CAM", 59000, ""],
        ["11-08-2026", "BANK/INT", "Other", "Bank interest", "", "", 500, ""],
    ])
    assert r.status_code == 201, r.text
    assert r.json()["batch"]["record_count"] == 2
    r = upload(client, gstin, "sales_register", "2026-08", [["S/1", "20-08-2026", "Tenant A Retail", "CAM", 18, 30000, 0, 2700, 2700]],
               header=SALES_HEADER)
    assert r.status_code == 201, r.text

    aug = report(client, gstin, "2026-08")
    by_rate = {(row["place_of_supply"], row["rate"]): row for row in aug["table_11a"]}
    (row,) = by_rate.values()  # flat + CAM both 18%, intra-state -> one 11A row
    # flat 595238.10 + unadjusted CAM 20000 (50000 received, 30000 invoiced in the same month)
    assert D(row["taxable_value"]) == D("615238.10")
    assert D(aug["tax_11a"]) == D("107142.86") + D("3600.00")
    assert aug["table_11b"] == []  # same-month adjustment is reported in neither table
    assert D(aug["adjusted_same_month"]["amount"]) == D("35400.00")

    # September: partial refund of RCPT/101, a new advance, an invoice to Priya (eats the opening balance)
    # and an invoice to Rahul pinned by rule to the new advance RCPT/103 instead of oldest-first RCPT/101.
    r = upload(client, gstin, "advance_register", "2026-09", [
        ["10-09-2026", "PYMT/7", "Refund", "Rahul Sharma", "", "Flat Sale", -200000, "RCPT/101"],
        ["12-09-2026", "RCPT/103", "Advance", "Rahul Sharma", "", "Flat Sale", 300000, ""],
    ])
    assert r.status_code == 201, r.text
    # Register carries the full consideration; the tool applies the land deduction (8,00,000 and 1,00,000 incl. GST).
    r = upload(client, gstin, "sales_register", "2026-09", [
        ["S/2", "25-09-2026", "Priya Das", "Flat Sale", 18, 714285.71, 0, 42857.14, 42857.15],
        ["S/3", "28-09-2026", "Rahul Sharma", "Flat Sale", 18, 89285.71, 0, 5357.14, 5357.15],
    ], header=SALES_HEADER)
    assert r.status_code == 201, r.text
    assert r.json()["batch"]["warnings"] == []
    s3 = next(i for i in client.get(f"/api/invoices?gstin={gstin}&period=2026-09&direction=outward").json()
              if i["invoice_no"] == "S/3")
    assert (s3["items"][0]["taxable_value"], s3["items"][0]["land_value"]) == ("59523.81", "29761.90")
    assert s3["taxable_value"] == "59523.81" and s3["items"][0]["tax_rate"] == "18.00"
    r = client.post("/api/advances/rules", json={"gstin": gstin, "invoice_no": "S/3", "advance_voucher_no": "RCPT/103"})
    assert r.status_code == 201

    sep = report(client, gstin, "2026-09")
    assert sep["warnings"] == []
    # 11B: whole opening balance (tax 53571.43) + refund of 1/5 of RCPT/101 (tax ~21428.57; CGST and
    # SGST are pro-rated separately, so the total may differ by a paisa)
    assert abs(D(sep["tax_11b"]) - (D("53571.43") + D("21428.57"))) <= D("0.02")
    # 11A: RCPT/103 less the ~1,00,000 adjusted against S/3 in the same month
    assert D("21400") < D(sep["tax_11a"]) < D("21430")
    assert D(sep["refunded"]) == D("200000.00")

    ledger = {row["voucher_no"]: row for row in client.get(f"/api/advances/ledger?gstin={gstin}").json()}
    assert D(ledger["RCPT/101"]["balance"]) == D("800000.00")
    assert D(ledger["RCPT/0457"]["balance"]) == D("0.00")
    assert ledger["RCPT/103"]["allocations"][0]["manual"] is True
    assert ledger["RCPT/0457"]["allocations"][0]["reference"] == "S/2"

    # Without the rule, oldest-first takes RCPT/101 and S/3 moves into 11B.
    rule_id = client.get(f"/api/advances/rules?gstin={gstin}").json()[0]["id"]
    assert client.delete(f"/api/advances/rules/{rule_id}").status_code == 204
    assert D(report(client, gstin, "2026-09")["tax_11b"]) > D(sep["tax_11b"])

    dash = client.get("/api/dashboard?period=2026-09").json()
    assert dash["advances"]["net_tax"] == str(D(report(client, gstin, "2026-09")["net_tax"]))


def test_advance_validation(client, adv_gstin):
    gstin, gid = adv_gstin
    client.patch(f"/api/gstins/{gid}", json={"advance_tax_enabled": True})
    r = upload(client, gstin, "advance_register", "2026-09", [
        ["05-09-2026", "R1", "Advance", "X", "", "Parking", 1000, ""],
        ["05-10-2026", "R2", "Advance", "X", "", "CAM", 1000, ""],
        ["05-09-2026", "R3", "Gift", "X", "", "CAM", 1000, ""],
    ])
    msgs = {e["row"]: e["message"] for e in r.json()["errors"]}
    assert "unknown service type 'Parking'" in msgs[2]
    assert "after the end of 2026-09" in msgs[3]
    assert "unknown type 'Gift'" in msgs[4]

    r = upload(client, gstin, "sales_register", "2026-09", [["S/1", "20-09-2026", "X", "Parking", 18, 100, 0, 9, 9]],
               header=SALES_HEADER)
    assert r.status_code == 422 and "unknown service type 'Parking'" in r.json()["errors"][0]["message"]


def test_refund_exceeding_open_advance_is_flagged(client, adv_gstin):
    gstin, gid = adv_gstin
    client.patch(f"/api/gstins/{gid}", json={"advance_tax_enabled": True})
    upload(client, gstin, "advance_register", "2026-09", [
        ["01-09-2026", "R1", "Advance", "X", "", "CAM", 1180, ""],
        ["02-09-2026", "P1", "Refund", "X", "", "CAM", 2000, ""],
    ])
    rep = report(client, gstin, "2026-09")
    assert any("exceeds the open CAM advances" in w for w in rep["warnings"])
    assert rep["table_11a"] == [] and rep["table_11b"] == []  # received and refunded in the same month


def test_templates_download(client):
    r = client.get("/api/templates/advance_register.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert client.get("/api/templates/nope.xlsx").status_code == 404
