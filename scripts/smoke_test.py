"""End-to-end check of entity master + ingestion against real MySQL, using the sample files.

Runs on TEST_DATABASE_URL (alcove_gst_test), which is wiped at the start of every run.
Your real database (DATABASE_URL) is never touched.

    .venv\\Scripts\\python scripts\\make_samples.py
    .venv\\Scripts\\python scripts\\smoke_test.py
"""
import json
from decimal import Decimal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Invoice, PortalDocument  # noqa: E402

SAMPLES = ROOT / "samples"
PERIOD = "2026-09"
DEMO_EMAIL, DEMO_PASSWORD = "demo@alcove.local", "Demo#2026"  # demo/test database only
results: list[tuple[bool, str]] = []


def check(ok, label: str, detail: str = "") -> None:
    ok = bool(ok)
    results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))


def show_issues(title: str, issues: list[dict]) -> None:
    if issues:
        print(f"      {title}:")
        for i in issues:
            where = f"row {i['row']}: " if i.get("row") else ""
            print(f"        - {where}{i['message']}")


def main() -> None:
    if not settings.test_database_url:
        sys.exit("TEST_DATABASE_URL is not set in .env -- run scripts\\setup_mysql.py first")
    if settings.test_database_url == settings.database_url:
        sys.exit("TEST_DATABASE_URL must differ from DATABASE_URL (the test DB is wiped)")
    if not (SAMPLES / "sales_register_2026-09.xlsx").exists():
        sys.exit("samples/ is missing -- run scripts\\make_samples.py first")

    engine = create_engine(settings.test_database_url, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app import migrate
    migrate.stamp(settings.test_database_url)  # so the app can open this DB without re-running migrations
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        with Session() as s:
            yield s

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    print(f"Database: {engine.url.render_as_string(hide_password=True)} (wiped)\n")

    own = json.loads((SAMPLES / "gstr2b_2026-09.json").read_text())["data"]["gstin"]

    def upload(endpoint, kind, path, replace=False, gstin=own, period=PERIOD):
        data = {"gstin": gstin, "period": period, "kind": kind, "replace": str(replace).lower()}
        if endpoint == "register":
            data["source"] = "tally"
        return client.post(f"/api/imports/{endpoint}", data=data, files={"file": (path.name, path.read_bytes())})

    print("0. Sign-in")
    check(client.get("/api/entities").status_code == 401, "API refuses requests without signing in")
    r = client.post("/api/auth/setup", json={"name": "Demo Admin", "email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    check(r.status_code == 201, f"first administrator created ({DEMO_EMAIL})", r.text)
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    check(r.status_code == 200 and client.get("/api/auth/me").status_code == 200, "sign in with email and password")

    print("\n1. Entity master")
    r = client.post("/api/entities", json={"name": "Alcove Demo LLP", "notes": "smoke test"})
    check(r.status_code == 201, "create entity", r.text)
    entity_id = r.json()["id"]
    r = client.post(f"/api/entities/{entity_id}/gstins", json={"gstin": own, "legal_name": "Alcove Demo LLP"})
    check(r.status_code == 201, f"add GSTIN {own}", r.text)
    check(client.get(f"/api/entities/{entity_id}").json()["pan"] == own[2:12], "PAN derived from GSTIN")
    r = client.post(f"/api/entities/{entity_id}/gstins", json={"gstin": own[:-1] + ("A" if own[-1] != "A" else "B")})
    check(r.status_code == 422, "reject GSTIN with bad checksum")
    gstin_id = client.get(f"/api/entities/{entity_id}").json()["gstins"][0]["id"]
    r = client.patch(f"/api/gstins/{gstin_id}", json={"advance_tax_enabled": True})
    check(r.status_code == 200 and r.json()["advance_tax_enabled"], "enable GST on advances")
    for name, sac, land in (("Flat Sale", "995411", True), ("CAM", "998599", False)):
        r = client.post(f"/api/gstins/{gstin_id}/service-types",
                        json={"name": name, "sac": sac, "tax_rate": 18, "land_deduction": land})
        check(r.status_code == 201, f"service type {name}{' (land deduction)' if land else ''}", r.text)
    for name in ("Mall Road", "Tower A"):
        r = client.post(f"/api/gstins/{gstin_id}/cost-centres", json={"name": name})
        check(r.status_code == 201, f"cost centre {name}", r.text)

    print("\n2. Sales register (Tally layout)")
    r = upload("register", "sales_register", SAMPLES / "sales_register_2026-09.xlsx")
    body = r.json()
    check(r.status_code == 201, "import accepted", json.dumps(body)[:500])
    if r.status_code == 201:
        check(body["batch"]["record_count"] == 7, f"7 documents grouped from 8 lines (got {body['batch']['record_count']})")
        print(f"      header found on row {body['details']['header_row']}; columns: {body['details']['column_map']}")
        show_issues("warnings", body["batch"]["warnings"])

    print("\n3. Purchase register")
    r = upload("register", "purchase_register", SAMPLES / "purchase_register_2026-09.xlsx")
    body = r.json()
    check(r.status_code == 201, "import accepted", json.dumps(body)[:500])
    if r.status_code == 201:
        check(body["batch"]["record_count"] == 6, "6 purchase invoices")
        check(body["details"]["column_map"]["invoice_no"] == "Supplier Invoice No", "supplier invoice no used, not voucher no")
        show_issues("warnings", body["batch"]["warnings"])

    print("\n4. GSTR-2B JSON")
    r = upload("portal", "gstr2b", SAMPLES / "gstr2b_2026-09.json")
    check(r.status_code == 201 and r.json()["batch"]["record_count"] == 5, "5 supplier documents loaded", r.text[:500])

    print("\n5. Safety rules")
    r = upload("register", "sales_register", SAMPLES / "sales_register_2026-09.xlsx")
    check(r.status_code == 409, "re-upload without replace is refused")
    r = upload("register", "sales_register", SAMPLES / "sales_register_2026-09.xlsx", replace=True)
    check(r.status_code == 201 and r.json()["replaced_batch_ids"], "re-upload with replace swaps the batch")
    r = upload("register", "sales_register", SAMPLES / "sales_register_2026-09.xlsx", period="2026-10")
    check(r.status_code == 409 and "already imported in period 2026-09" in r.text, "same invoices in October rejected as duplicates")
    r = upload("portal", "gstr2b", SAMPLES / "gstr2b_2026-09.json", period="2026-08")
    check(r.status_code == 422 and "not 2026-08" in r.text, "2B for wrong period rejected")
    r = upload("register", "sales_register", SAMPLES / "purchase_register_2026-09.xlsx", period="2026-08", replace=True)
    check(r.status_code == 422, "purchase file uploaded as August sales is rejected",
          "accepted -- dates outside period should have errored")
    if r.status_code == 422:
        show_issues("errors returned to user", r.json()["errors"][:3])

    print("\n6. Advances (GSTR-1 Tables 11A / 11B)")
    r = upload("register", "advance_opening", SAMPLES / "advance_opening_2026-09.xlsx")
    check(r.status_code == 201 and r.json()["batch"]["record_count"] == 2, "opening balances loaded (go-live 2026-09)", r.text[:500])
    r = upload("register", "advance_register", SAMPLES / "advance_register_2026-09.xlsx")
    body = r.json()
    check(r.status_code == 201 and body["batch"]["record_count"] == 3, "bank book: 2 advances + 1 refund, 'Other' rows skipped", r.text[:500])
    rep = client.get(f"/api/advances/report?gstin={own}&period={PERIOD}").json()
    check(rep["warnings"] == [], "no ledger warnings", "; ".join(rep["warnings"]))
    # 11B: Tenant B's opening CAM advance (23,600 incl. 3,600 tax) used by invoice S/26-27/102
    check(rep["tax_11b"] == "3600.00", f"11B tax 3,600.00 (got {rep['tax_11b']})")
    for row in rep["table_11a"]:
        print(f"      11A  POS {row['place_of_supply']} {row['rate']}%  taxable {row['taxable_value']}  "
              f"CGST {row['cgst']}  SGST {row['sgst']}")
    ledger = {row["voucher_no"]: row for row in client.get(f"/api/advances/ledger?gstin={own}").json()}
    check(ledger["RCPT/101"]["refunded"] == "200000.00", "refund PYMT/7 applied against RCPT/101")
    check(ledger["ADV/88"]["balance"] == "0.00" and ledger["RCPT/0457"]["balance"] == "500000.00",
          "oldest-first used Tenant B's CAM opening; Priya's flat advance untouched")

    print("\n7. GSTR-1")
    r = client.get(f"/api/gstr1?gstin={own}&period={PERIOD}")
    check(r.status_code == 200, "GSTR-1 built", r.text[:300])
    if r.status_code == 200:
        g = r.json()
        for s in g["summary"]:
            if s["records"]:
                print(f"      {s['table']:<15} {s['title']:<52} {s['records']:>3} docs  taxable {s['txval']:>12}")
        print(f"      Tax payable from GSTR-1: {g['liability']['tax']}")
        check({row["inum"] for row in g["sections"]["b2b"]} >= {"S/26-27/101", "S/26-27/103"}, "B2B invoices in Table 4A")
        check(g["sections"]["b2cs"] and g["sections"]["at"], "B2C flat sale in B2CS, advances in 11A")
    r = client.get(f"/api/gstr1/export.xlsx?gstin={own}&period={PERIOD}")
    check(r.status_code == 200 and r.content[:2] == b"PK", "GSTR-1 Excel export")
    r = client.get(f"/api/gstr1/export.json?gstin={own}&period={PERIOD}")
    check(r.status_code == 200, "GSTR-1 portal JSON export", r.text[:300])
    if r.status_code == 200:
        j = r.json()
        print(f"      JSON sections: {', '.join(k for k in j if k not in ('gstin', 'fp', 'version', 'hash'))}")
        docs = {d["doc_num"]: d["docs"][0]["totnum"] for d in j.get("doc_issue", {}).get("doc_det", [])}
        check(j["fp"] == "092026" and "at" in j and "txpd" in j, "JSON has fp 092026 and advances (at / txpd)")
        check(docs.get(6) == 2 and docs.get(8) == 1, f"Table 13 lists 2 receipt vouchers and 1 refund voucher (got {docs})")

    print("\n8. GST by cost centre")
    r = client.get(f"/api/reports/cost-centres?gstin={own}&period={PERIOD}")
    check(r.status_code == 200, "cost centre report built", r.text[:300])
    if r.status_code == 200:
        rep = r.json()
        for row in rep["rows"]:
            print(f"      {row['name']:<12} output tax {row['output_tax']:>11}  11A {row['advance_11a']:>10}  "
                  f"11B {row['advance_11b']:>9}  ITC {row['itc']:>10}  net {row['net']:>11}")
        dash = client.get(f"/api/dashboard?period={PERIOD}").json()
        check(rep["totals"]["output_tax"] == dash["totals"]["outward"]["tax"]
              and rep["totals"]["itc"] == dash["totals"]["itc_books"]["tax"], "cost centre rows add up to the GSTIN totals")
        check({row["name"] for row in rep["rows"]} == {"Mall Road", "Tower A", "Unassigned"},
              "untagged legal-fees purchase shows as Unassigned")

    print("\n9. Data stored in MySQL")
    with Session() as s:
        def totals(model, *where):
            return s.execute(select(func.count(), func.sum(model.taxable_value), func.sum(model.igst),
                                    func.sum(model.cgst), func.sum(model.sgst)).where(*where)).one()

        print(f"      {'':28}{'docs':>5}{'taxable':>14}{'IGST':>12}{'CGST':>12}{'SGST':>12}")
        for label, model, where in [
            ("Sales register", Invoice, (Invoice.direction == "outward",)),
            ("Purchase register", Invoice, (Invoice.direction == "inward",)),
            ("GSTR-2B", PortalDocument, ()),
        ]:
            n, tx, ig, cg, sg = totals(model, *where)
            print(f"      {label:28}{n:>5}{tx:>14,.2f}{ig:>12,.2f}{cg:>12,.2f}{sg:>12,.2f}")
        n, tx, *_ = totals(Invoice, Invoice.direction == "outward")
        # 6 invoices + 1 credit note, all stored as positive amounts: 1,205,000 incl. the 50,000 note + 59,523.81 flat
        check(n == 7 and tx == Decimal("1264523.81"), "sales totals exact to the paisa (Decimal round-trip)", f"{n} docs, {tx}")

    passed = sum(ok for ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
