"""Generate realistic sample input files in samples/ for manual and smoke testing.

All GSTINs are fictitious (valid checksum, made-up PANs). Period: September 2026.
The purchase register and GSTR-2B deliberately disagree in places, for the reconciliation module:
  SEC/889  matches exactly
  HK/77    taxable value differs (60,000 in books vs 61,000 in 2B)
  EL-45    matches (inter-state, IGST)
  AMC/31   in books, missing from 2B (supplier has not filed)
  MISC/5   in 2B, missing from books
  LF-9     reverse charge (unregistered advocate), never in 2B
"""
import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.gstin import gstin_check_char  # noqa: E402

OUT = ROOT / "samples"


def gstin(state: str, pan: str) -> str:
    first14 = f"{state}{pan}1Z"
    return first14 + gstin_check_char(first14)


OWN = gstin("19", "ZZZFD0001Z")          # "Alcove Demo LLP", West Bengal
TENANT_A = gstin("19", "AABCT1001A")
TENANT_B = gstin("19", "AABCT1002B")
TENANT_C = gstin("27", "AABCT1003C")      # Maharashtra -> IGST
TENANT_D = gstin("19", "AABCT1004D")
SECURITY = gstin("19", "AABCS2001E")
HOUSEKEEPING = gstin("19", "AABCH2002F")
ELECTRICAL = gstin("27", "AABCE2003G")
CATERING = gstin("19", "AABCC2004H")
LIFT_AMC = gstin("19", "AABCL2005J")
MISC_VENDOR = gstin("19", "AABCM2006K")


def save(rows: list[list], name: str, header_row: int) -> None:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    for cell in ws[header_row]:
        cell.font = Font(bold=True)
    ws.title = "Register"
    wb.save(OUT / name)


def sales_register() -> None:
    header = ["Date", "Particulars", "Voucher Type", "Voucher No.", "GSTIN/UIN", "Place of Supply", "HSN/SAC",
              "Service Type", "Taxable Value", "IGST", "CGST", "SGST", "Invoice Value"]
    rows = [
        ["01-Sep-2026", "Tenant A Retail Pvt Ltd", "Sales", "S/26-27/101", TENANT_A, "19-West Bengal", "997212", None, 500000, 0, 45000, 45000, 590000],
        ["01-Sep-2026", "Tenant B Foods Pvt Ltd", "Sales", "S/26-27/102", TENANT_B, "19-West Bengal", "997212", None, 300000, 0, 27000, 27000, 448400],
        ["01-Sep-2026", "Tenant B Foods Pvt Ltd", "Sales", "S/26-27/102", TENANT_B, "19-West Bengal", "998599", "CAM", 80000, 0, 7200, 7200, 448400],
        ["05-Sep-2026", "Tenant C Apparel Ltd", "Sales", "S/26-27/103", TENANT_C, "27-Maharashtra", "997212", None, 200000, 36000, 0, 0, 236000],
        ["10-Sep-2026", "Cash - Event Booking", "Sales", "S/26-27/104", None, None, "997212", None, 50000, 0, 4500, 4500, 59000],
        ["15-Sep-2026", "Tenant A Retail Pvt Ltd", "Credit Note", "CN/26-27/01", TENANT_A, "19-West Bengal", "997212", None, -50000, 0, -4500, -4500, -59000],
        ["20-Sep-2026", "Tenant D Services LLP", "Sales", "S/26-27/105", TENANT_D, "19-West Bengal", "997212", None, 25000, 0, 2250, 2250, 29500],
        # 1,00,000 incl. GST for a flat: register shows the full consideration; the tool taxes two-thirds at 18%
        ["25-Sep-2026", "Rahul Sharma", "Sales", "S/26-27/106", None, "19-West Bengal", "995411", "Flat Sale", 89285.71, 0, 5357.14, 5357.15, 100000],
    ]
    # Cost centre per invoice: the mall vs the Tower A residential project
    centre = {"S/26-27/106": "Tower A"}
    header.insert(8, "Cost Centre")
    rows = [r[:8] + [centre.get(r[3], "Mall Road")] + r[8:] for r in rows]
    save([["Alcove Demo LLP"], ["Sales Register"], ["1-Sep-2026 to 30-Sep-2026"], [], header, *rows],
         "sales_register_2026-09.xlsx", header_row=5)


def advance_files() -> None:
    header = ["Date", "Voucher No", "Type", "Customer Name", "Customer GSTIN", "Service Type", "Amount (incl. GST)",
              "Place of Supply", "Against Voucher No", "Narration"]
    rows = [
        ["05-09-2026", "RCPT/101", "Advance", "Rahul Sharma", None, "Flat Sale", 1000000, None, None, "Booking - Flat 3B"],
        ["08-09-2026", "RCPT/104", "Other", "Tenant A Retail Pvt Ltd", TENANT_A, None, 531000, None, None, "Rent received against S/26-27/101"],
        ["10-09-2026", "RCPT/102", "Advance", "Tenant A Retail Pvt Ltd", TENANT_A, "CAM", 59000, None, None, "CAM advance Oct-Dec"],
        ["15-09-2026", "BANK/INT", "Other", "Bank interest", None, None, 1250, None, None, None],
        ["20-09-2026", "PYMT/7", "Refund", "Rahul Sharma", None, "Flat Sale", -200000, None, "RCPT/101", "Partial cancellation - parking"],
    ]
    centre = {"RCPT/101": "Tower A", "RCPT/102": "Mall Road", "PYMT/7": "Tower A"}
    header.insert(6, "Cost Centre")
    rows = [r[:6] + [centre.get(r[1])] + r[6:] for r in rows]
    save([["Alcove Demo LLP"], ["Bank Book - Advances"], ["1-Sep-2026 to 30-Sep-2026"], [], header, *rows],
         "advance_register_2026-09.xlsx", header_row=5)

    header = ["Original Receipt Date", "Original Receipt No", "Customer Name", "Customer GSTIN", "Service Type",
              "Unadjusted Amount (incl. GST)", "Place of Supply"]
    rows = [
        ["15-01-2026", "RCPT/0457", "Priya Das", None, "Flat Sale", 500000, None],
        ["20-08-2026", "ADV/88", "Tenant B Foods Pvt Ltd", TENANT_B, "CAM", 23600, None],
    ]
    centre = {"RCPT/0457": "Tower A", "ADV/88": "Mall Road"}
    header.insert(5, "Cost Centre")
    rows = [r[:5] + [centre.get(r[1])] + r[5:] for r in rows]
    save([header, *rows], "advance_opening_2026-09.xlsx", header_row=1)


def purchase_register() -> None:
    header = ["Date", "Voucher No", "Supplier Invoice No", "Supplier Invoice Date", "Party Name", "GSTIN/UIN",
              "HSN/SAC", "Taxable Value", "IGST", "CGST", "SGST", "Reverse Charge", "ITC Eligible"]
    rows = [
        ["03-09-2026", "P-201", "SEC/889", "02-09-2026", "Shield Security Services", SECURITY, "998525", 100000, 0, 9000, 9000, "N", "Y"],
        ["04-09-2026", "P-202", "HK/77", "03-09-2026", "Sparkle Housekeeping", HOUSEKEEPING, "998533", 60000, 0, 5400, 5400, "N", "Y"],
        ["05-09-2026", "P-203", "EL-45", "04-09-2026", "Volt Electricals (Mumbai)", ELECTRICAL, "995461", 80000, 14400, 0, 0, "N", "Y"],
        ["07-09-2026", "P-204", "LF-9", "06-09-2026", "Adv. R. Sen (Legal fees)", None, "998211", 50000, 0, 0, 0, "Y", "Y"],
        ["09-09-2026", "P-205", "CT-12", "08-09-2026", "Tasty Caterers", CATERING, "996331", 20000, 0, 500, 500, "N", "N"],
        ["13-09-2026", "P-206", "AMC/31", "12-09-2026", "Up-Lift Elevators", LIFT_AMC, "998717", 40000, 0, 3600, 3600, "N", "Y"],
    ]
    centre = {"SEC/889": "Mall Road", "HK/77": "Mall Road", "EL-45": "Tower A", "CT-12": "Tower A",
              "AMC/31": "Mall Road"}  # LF-9 (legal fees) left untagged -> Unassigned
    header.insert(7, "Cost Centre")
    rows = [r[:7] + [centre.get(r[2])] + r[7:] for r in rows]
    save([["Alcove Demo LLP"], ["Purchase Register"], ["1-Sep-2026 to 30-Sep-2026"], [], header, *rows],
         "purchase_register_2026-09.xlsx", header_row=5)


def _2b_inv(inum, dt, txval, igst=0, cgst=0, sgst=0, rt=18):
    return {"inum": inum, "typ": "R", "dt": dt, "val": txval + igst + cgst + sgst, "pos": "19", "rev": "N",
            "itcavl": "Y", "rsn": "", "diffprcnt": 1, "srctyp": "",
            "items": [{"num": 1, "rt": rt, "txval": txval, "igst": igst, "cgst": cgst, "sgst": sgst, "cess": 0}]}


def _supplier(ctin, name, *invs):
    return {"ctin": ctin, "trdnm": name, "supfildt": "11-10-2026", "supprd": "092026", "inv": list(invs)}


def gstr2b() -> None:
    data = {"chksum": "sample", "data": {
        "gstin": OWN, "rtnprd": "092026", "version": "1.0", "gendt": "14-10-2026",
        "docdata": {"b2b": [
            _supplier(SECURITY, "Shield Security Services", _2b_inv("SEC/889", "02-09-2026", 100000, cgst=9000, sgst=9000)),
            _supplier(HOUSEKEEPING, "Sparkle Housekeeping", _2b_inv("HK/77", "03-09-2026", 61000, cgst=5490, sgst=5490)),
            _supplier(ELECTRICAL, "Volt Electricals", {**_2b_inv("EL-45", "04-09-2026", 80000, igst=14400), "pos": "19"}),
            _supplier(CATERING, "Tasty Caterers", _2b_inv("CT-12", "08-09-2026", 20000, cgst=500, sgst=500, rt=5)),
            _supplier(MISC_VENDOR, "Metro Supplies", _2b_inv("MISC/5", "18-09-2026", 10000, cgst=900, sgst=900)),
        ]},
    }}
    (OUT / "gstr2b_2026-09.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    sales_register()
    purchase_register()
    advance_files()
    gstr2b()
    print(f"Own GSTIN for samples: {OWN}")
    for f in sorted(OUT.iterdir()):
        print(f"  {f.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
