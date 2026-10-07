from decimal import Decimal

import pytest

from tests.conftest import CUSTOMER_WB, xlsx

D = Decimal
SALES = ["Invoice No", "Invoice Date", "Customer Name", "Customer GSTIN", "Service Type", "Cost Centre", "Tax Rate",
         "Taxable Value", "IGST", "CGST", "SGST"]
PURCHASES = ["Supplier Invoice No", "Supplier Invoice Date", "Supplier Name", "Supplier GSTIN", "Cost Centre",
             "Taxable Value", "CGST", "SGST", "ITC Eligible"]


@pytest.fixture
def setup(client, own_gstin):
    (entity,) = client.get("/api/entities").json()
    gid = entity["gstins"][0]["id"]
    for name in ("Mall Road", "Tower A", "Tower B"):
        assert client.post(f"/api/gstins/{gid}/cost-centres", json={"name": name}).status_code == 201
    client.post(f"/api/gstins/{gid}/service-types", json={"name": "Flat Sale", "tax_rate": 18, "land_deduction": True})
    client.post(f"/api/gstins/{gid}/service-types", json={"name": "Rent", "tax_rate": 18})
    return own_gstin, gid


def upload(client, gstin, kind, header, rows, period="2026-09"):
    return client.post("/api/imports/register", files={"file": ("f.xlsx", xlsx([header, *rows]))},
                       data={"gstin": gstin, "period": period, "kind": kind})


def test_cost_centre_master(client, setup):
    _, gid = setup
    r = client.post(f"/api/gstins/{gid}/cost-centres", json={"name": "  mall   road "})
    assert r.status_code == 409
    centres = client.get(f"/api/gstins/{gid}/cost-centres").json()
    assert [c["name"] for c in centres] == ["Mall Road", "Tower A", "Tower B"]
    r = client.patch(f"/api/cost-centres/{centres[2]['id']}", json={"name": "Tower A"})
    assert r.status_code == 409
    r = client.patch(f"/api/cost-centres/{centres[2]['id']}", json={"code": "TB", "active": False})
    assert r.json()["code"] == "TB" and r.json()["active"] is False


def test_unknown_cost_centre_rejects_file(client, setup):
    gstin, _ = setup
    r = upload(client, gstin, "sales_register", SALES,
               [["S/1", "01-09-2026", "Tenant", CUSTOMER_WB, "Rent", "Tower Z", 18, 1000, 0, 90, 90]])
    assert r.status_code == 422
    assert "unknown cost centre 'Tower Z'" in r.json()["errors"][0]["message"]


def test_report_splits_and_ties_to_gstin(client, setup):
    gstin, _ = setup
    r = upload(client, gstin, "sales_register", SALES, [
        ["S/1", "01-09-2026", "Tenant", CUSTOMER_WB, "Rent", "Mall Road", 18, 100000, 0, 9000, 9000],
        ["S/2", "02-09-2026", "Rahul Sharma", None, "Flat Sale", "Tower A", 18, 89285.71, 0, 5357.14, 5357.15],
        ["S/3", "03-09-2026", "Walk-in", None, None, None, 18, 10000, 0, 900, 900],
    ])
    assert r.status_code == 201, r.text
    assert any("1 line(s) have no cost centre" in w["message"] for w in r.json()["batch"]["warnings"])
    r = upload(client, gstin, "purchase_register", PURCHASES, [
        ["P/1", "02-09-2026", "Guard Co", CUSTOMER_WB, "Mall Road", 1000, 90, 90, "Y"],
        ["P/2", "03-09-2026", "Caterer", CUSTOMER_WB, "Tower A", 500, 45, 45, "N"],
    ])
    assert r.status_code == 201, r.text

    rep = client.get(f"/api/reports/cost-centres?gstin={gstin}&period=2026-09").json()
    rows = {r["name"]: r for r in rep["rows"]}
    assert set(rows) == {"Mall Road", "Tower A", "Tower B", "Unassigned"}
    assert (rows["Mall Road"]["output_tax"], rows["Mall Road"]["itc"]) == ("18000.00", "180.00")
    assert rows["Tower A"]["output_tax"] == "10714.29" and rows["Tower A"]["itc"] == "0.00"  # ITC ineligible
    assert rows["Tower A"]["outward_taxable"] == "59523.81"  # two-thirds after land deduction
    assert rows["Unassigned"]["output_tax"] == "1800.00"
    assert rows["Tower B"]["output_tax"] == "0.00"
    dash = client.get("/api/dashboard?period=2026-09").json()
    assert rep["totals"]["output_tax"] == dash["totals"]["outward"]["tax"]
    assert rep["totals"]["itc"] == dash["totals"]["itc_books"]["tax"]

    s2 = next(i for i in client.get(f"/api/invoices?gstin={gstin}&period=2026-09&direction=outward").json()
              if i["invoice_no"] == "S/2")
    assert s2["items"][0]["cost_centre_name"] == "Tower A"

    x = client.get(f"/api/reports/cost-centres.xlsx?gstin={gstin}&period=2026-09")
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_advance_does_not_cross_cost_centres(client, setup):
    gstin, gid = setup
    client.patch(f"/api/gstins/{gid}", json={"advance_tax_enabled": True})
    header = ["Date", "Voucher No", "Type", "Customer Name", "Service Type", "Cost Centre", "Amount"]
    r = upload(client, gstin, "advance_register", header, [
        ["01-09-2026", "R/B", "Advance", "Rahul Sharma", "Flat Sale", "Tower B", 100000],
        ["02-09-2026", "R/A", "Advance", "Rahul Sharma", "Flat Sale", "Tower A", 100000],
    ])
    assert r.status_code == 201, r.text
    # Invoice for Tower A: oldest-first would take R/B (older) but it belongs to Tower B.
    r = upload(client, gstin, "sales_register", SALES,
               [["S/9", "20-09-2026", "Rahul Sharma", None, "Flat Sale", "Tower A", 18, 89285.71, 0, 5357.14, 5357.15]])
    assert r.status_code == 201, r.text
    ledger = {row["voucher_no"]: row for row in client.get(f"/api/advances/ledger?gstin={gstin}").json()}
    assert ledger["R/B"]["adjusted"] == "0.00" and ledger["R/B"]["cost_centre"] == "Tower B"
    assert ledger["R/A"]["adjusted"] == "100000.00"

    rows = {r["name"]: r for r in client.get(f"/api/reports/cost-centres?gstin={gstin}&period=2026-09").json()["rows"]}
    assert D(rows["Tower B"]["advance_11a"]) == D("10714.29")  # R/B fully unadjusted
    assert D(rows["Tower A"]["advance_11a"]) == D("0.00")      # R/A adjusted within the month
