from tests.conftest import CUSTOMER_MH, CUSTOMER_WB, OWN_GSTIN, as_json, make_gstin, xlsx
from tests.test_portal import GSTR2B

HEADER = ["Invoice No", "Invoice Date", "Customer GSTIN", "Tax Rate", "Taxable Value", "IGST", "CGST", "SGST"]


def upload_sales(client, rows, period="2026-09", replace=False, gstin=OWN_GSTIN):
    return client.post("/api/imports/register", data={
        "gstin": gstin, "period": period, "kind": "sales_register", "source": "tally", "replace": str(replace).lower(),
    }, files={"file": ("sales.xlsx", xlsx([HEADER, *rows]))})


def test_entity_master(client):
    r = client.post("/api/entities", json={"name": "Alcove LLP", "pan": "AAKFA1234B"})
    assert r.status_code == 201
    entity_id = r.json()["id"]

    other_pan = make_gstin("19", "AABCZ9999Z")
    r = client.post(f"/api/entities/{entity_id}/gstins", json={"gstin": other_pan})
    assert r.status_code == 422 and "does not belong to PAN" in r.json()["detail"]

    r = client.post(f"/api/entities/{entity_id}/gstins", json={"gstin": OWN_GSTIN[:-1] + ("A" if OWN_GSTIN[-1] != "A" else "B")})
    assert r.status_code == 422 and "checksum" in r.text

    r = client.post(f"/api/entities/{entity_id}/gstins", json={"gstin": OWN_GSTIN.lower()})
    assert r.status_code == 201 and r.json()["state_code"] == "19"
    assert client.post(f"/api/entities/{entity_id}/gstins", json={"gstin": OWN_GSTIN}).status_code == 409

    r = client.patch(f"/api/entities/{entity_id}", json={"active": False})
    assert r.json()["active"] is False
    assert client.get("/api/entities").json() == []
    assert len(client.get("/api/entities?include_inactive=true").json()) == 1


def test_gstin_pan_copied_to_entity(client, own_gstin):
    (entity,) = client.get("/api/entities").json()
    assert entity["pan"] == own_gstin[2:12]


def test_register_import_replace_and_duplicates(client, own_gstin):
    rows = [["S/001", "01-09-2026", CUSTOMER_WB, 18, 1000, 0, 90, 90],
            ["S/002", "02-09-2026", CUSTOMER_MH, 18, 2000, 360, 0, 0]]
    r = upload_sales(client, rows)
    assert r.status_code == 201, r.text
    batch = r.json()["batch"]
    assert batch["record_count"] == 2 and batch["source"] == "tally"

    r = upload_sales(client, rows)
    assert r.status_code == 409 and "replace=true" in r.json()["errors"][0]["message"]

    r = upload_sales(client, rows[:1], replace=True)
    assert r.status_code == 201 and r.json()["replaced_batch_ids"] == [batch["id"]]
    invoices = client.get(f"/api/invoices?gstin={own_gstin}&period=2026-09&direction=outward").json()
    assert [i["invoice_no"] for i in invoices] == ["S/001"]
    assert invoices[0]["cgst"] == "90.00" and invoices[0]["items"][0]["tax_rate"] == "18.00"

    # Same invoice number re-appearing in October's register is rejected as a duplicate.
    r = upload_sales(client, [["S/001", "01-10-2026", CUSTOMER_WB, 18, 1000, 0, 90, 90]], period="2026-10")
    assert r.status_code == 409 and "already imported in period 2026-09" in r.json()["errors"][0]["message"]


def test_register_validation_errors_reject_whole_file(client, own_gstin):
    r = upload_sales(client, [["S/001", "01-09-2026", CUSTOMER_WB, 18, 1000, 0, 90, 90],
                              ["S/002", "02-09-2026", CUSTOMER_WB, 18, 1000, 180, 90, 90]])
    assert r.status_code == 422
    assert r.json()["errors"] == [{"row": 3, "field": "igst", "message": "row has both IGST and CGST/SGST"}]
    assert client.get("/api/imports").json() == []


def test_unknown_gstin_and_bad_period(client, own_gstin):
    assert upload_sales(client, [], gstin=CUSTOMER_WB).status_code == 404
    assert upload_sales(client, [], period="09-2026").status_code == 422


def test_portal_import_checks_gstin_and_period(client, own_gstin):
    def upload(period):
        return client.post("/api/imports/portal", data={"gstin": own_gstin, "period": period, "kind": "gstr2b"},
                           files={"file": ("2b.json", as_json(GSTR2B))})

    r = upload("2026-08")
    assert r.status_code == 422 and "period 2026-09, not 2026-08" in r.json()["errors"][0]["message"]
    r = upload("2026-09")
    assert r.status_code == 201 and r.json()["batch"]["record_count"] == 2

    batch_id = r.json()["batch"]["id"]
    assert client.delete(f"/api/imports/{batch_id}").status_code == 204
    assert client.get("/api/imports").json() == []


def test_dashboard_summarises_imports_and_signed_totals(client, own_gstin):
    upload_sales(client, [["S/001", "01-09-2026", CUSTOMER_WB, 18, 1000, 0, 90, 90],
                          ["S/002", "02-09-2026", CUSTOMER_MH, 18, 2000, 360, 0, 0]])
    purchases = xlsx([["Invoice No", "Invoice Date", "Supplier GSTIN", "Taxable Value", "CGST", "SGST", "ITC Eligible", "Doc Type"],
                      ["P/1", "01-09-2026", CUSTOMER_WB, 1000, 90, 90, "Y", "Invoice"],
                      ["P/2", "02-09-2026", CUSTOMER_WB, 500, 45, 45, "N", "Invoice"],
                      ["CN/1", "03-09-2026", CUSTOMER_WB, 100, 9, 9, "Y", "Credit Note"]])
    r = client.post("/api/imports/register", data={"gstin": own_gstin, "period": "2026-09", "kind": "purchase_register"},
                    files={"file": ("p.xlsx", purchases)})
    assert r.status_code == 201, r.text

    d = client.get("/api/dashboard?period=2026-09").json()
    (row,) = d["gstins"]
    assert row["imports"]["sales_register"]["record_count"] == 2
    assert row["imports"]["gstr2b"] is None
    assert d["totals"]["outward"]["tax"] == "540.00"
    assert d["totals"]["itc_books"]["tax"] == "162.00"  # 180 eligible - 18 credit note; ineligible P/2 excluded
    assert client.get("/api/periods").json() == ["2026-09"]
    assert client.get("/api/dashboard?period=Sep").status_code == 422


def test_web_ui_is_served(client):
    assert "Alcove" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_period_ranges(client, own_gstin):
    upload_sales(client, [["S/001", "01-09-2026", CUSTOMER_WB, 18, 1000, 0, 90, 90]], period="2026-09")
    upload_sales(client, [["S/002", "01-10-2026", CUSTOMER_WB, 18, 2000, 0, 180, 180]], period="2026-10")

    d = client.get("/api/dashboard?from=2026-08&to=2026-10").json()
    assert d["months"] == ["2026-08", "2026-09", "2026-10"] and d["period"] is None
    assert d["totals"]["outward"]["tax"] == "540.00"
    status = d["gstins"][0]["imports"]["sales_register"]
    assert (status["months"], status["of"], status["record_count"]) == (2, 3, 2)

    inv = client.get(f"/api/invoices?gstin={own_gstin}&from=2026-09&to=2026-10&direction=outward").json()
    assert [i["invoice_no"] for i in inv] == ["S/001", "S/002"]
    assert len(client.get(f"/api/imports?gstin={own_gstin}&from=2026-10&to=2026-10").json()) == 1
    rep = client.get(f"/api/reports/cost-centres?gstin={own_gstin}&from=2026-09&to=2026-10").json()
    assert (rep["from"], rep["to"], rep["totals"]["output_tax"]) == ("2026-09", "2026-10", "540.00")

    assert client.get("/api/dashboard?from=2026-10&to=2026-09").status_code == 422
    assert client.get("/api/dashboard?from=2020-01&to=2026-09").status_code == 422  # over 36 months
    assert client.get("/api/dashboard").status_code == 422
