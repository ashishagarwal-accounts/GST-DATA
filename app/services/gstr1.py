"""Build GSTR-1 for a GSTIN and period from the imported sales register and the advance ledger.

Classification of each outward document
- Nil-rated / exempt / non-GST (by supply category, or a 0% line)  -> Table 8 (exemp)
- Exports                                    invoice -> 6A (exp)          note -> 9B CDNUR (EXPWP / EXPWOP)
- Registered customer (B2B, SEZ, deemed exp) invoice -> 4A/6B/6C (b2b)    note -> 9B CDNR (separate table)
- Unregistered, inter-state, value > B2CL_LIMIT  invoice -> 5 (b2cl)       note -> 9B CDNUR (B2CL)
- Unregistered, everything else              invoice -> 7 (b2cs)          note -> netted off in 7 (b2cs)
- Advances (if enabled for the GSTIN)        11A (at) / 11B (atadj) from the advance ledger
- Table 12 HSN summary, split B2B / B2C; Table 13 documents issued.

All amounts in a note are subtracted for credit notes and added for debit notes where tables are netted.
"""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.gstin import STATE_CODES
from app.models import (
    AdvanceEntry, AdvanceEntryType, Direction, DocType, Gstin, Invoice, InvoiceItem, SupplyCategory,
)
from app.services.advance_ledger import period_report, run_ledger

ZERO = Decimal("0.00")
B2CL_LIMIT = Decimal("100000")  # inter-state B2C invoices above this go to Table 5 (limit since 1 Aug 2024)
TAX = ("txval", "iamt", "camt", "samt", "csamt")

NIL_CATEGORIES = {SupplyCategory.NIL_RATED: "nil", SupplyCategory.EXEMPT: "exempt", SupplyCategory.NON_GST: "non_gst"}
EXPORT_TYPES = {SupplyCategory.EXPORT_WITH_PAYMENT: "WPAY", SupplyCategory.EXPORT_WITHOUT_PAYMENT: "WOPAY"}
CDNUR_EXPORT_TYPES = {SupplyCategory.EXPORT_WITH_PAYMENT: "EXPWP", SupplyCategory.EXPORT_WITHOUT_PAYMENT: "EXPWOP"}
B2B_TYPES = {
    SupplyCategory.TAXABLE: "Regular B2B",
    SupplyCategory.SEZ_WITH_PAYMENT: "SEZ supplies with payment",
    SupplyCategory.SEZ_WITHOUT_PAYMENT: "SEZ supplies without payment",
    SupplyCategory.DEEMED_EXPORT: "Deemed Exp",
}

SECTIONS = [
    # key, GSTR-1 table, title
    ("b2b", "4A, 4B, 6B, 6C", "B2B invoices (incl. SEZ, deemed exports)"),
    ("b2cl", "5", "B2C large - inter-state invoices above Rs 1 lakh"),
    ("b2cs", "7", "B2C small - net of B2C credit notes"),
    ("exp", "6A", "Exports"),
    ("cdnr", "9B", "Credit / debit notes - registered (B2B)"),
    ("cdnur", "9B", "Credit / debit notes - unregistered (B2CL, exports)"),
    ("exemp", "8", "Nil-rated, exempt and non-GST supplies"),
    ("at", "11A(1), 11A(2)", "Advances received, unadjusted at month-end"),
    ("atadj", "11B(1), 11B(2)", "Advances adjusted / refunded"),
    ("hsn_b2b", "12", "HSN summary - B2B"),
    ("hsn_b2c", "12", "HSN summary - B2C"),
    ("docs", "13", "Documents issued"),
]


def pos_label(code: str | None) -> str:
    return f"{code}-{STATE_CODES.get(code, '')}" if code else ""


def _zero() -> dict[str, Decimal]:
    return dict.fromkeys(TAX, ZERO)


def _item_amounts(item: InvoiceItem, sign: int = 1) -> dict[str, Decimal]:
    return {"txval": sign * item.taxable_value, "iamt": sign * item.igst, "camt": sign * item.cgst,
            "samt": sign * item.sgst, "csamt": sign * item.cess}


def _add(target: dict, amounts: dict) -> None:
    for k in TAX:
        target[k] += amounts[k]


def _by_rate(inv: Invoice) -> dict[Decimal, dict]:
    """Taxable lines of a document grouped by rate (nil / 0% lines excluded)."""
    rates: dict[Decimal, dict] = defaultdict(_zero)
    for item in inv.items:
        if item.tax_rate == 0:
            continue
        _add(rates[item.tax_rate], _item_amounts(item))
    return rates


@dataclass
class Gstr1:
    gstin: Gstin
    period: str
    sections: dict[str, list[dict]]
    warnings: list[str]

    def summary(self) -> list[dict]:
        out = []
        for key, table, title in SECTIONS:
            rows = self.sections[key]
            totals = _zero()
            docs = set()
            for r in rows:
                sign = -1 if r.get("ntty") == "C" else 1
                for k in TAX:
                    totals[k] += sign * r.get(k, ZERO)
                docs.add(r.get("inum") or r.get("nt_num") or id(r))
            if key in ("exemp", "docs"):
                totals = _zero()
                if key == "exemp":
                    totals["txval"] = sum((r["nil"] + r["exempt"] + r["non_gst"] for r in rows), ZERO)
            out.append({"key": key, "table": table, "title": title, "records": len(docs) if rows else 0, **totals})
        return out

    def liability(self) -> dict[str, Decimal]:
        """Tax payable by the supplier from this return (reverse-charge B2B excluded, advances included)."""
        total = _zero()
        for key in ("b2b", "b2cl", "b2cs", "exp", "cdnr", "cdnur", "at"):
            for r in self.sections[key]:
                if r.get("rchrg") == "Y":
                    continue
                sign = -1 if r.get("ntty") == "C" else 1
                for k in TAX:
                    total[k] += sign * r.get(k, ZERO)
        for r in self.sections["atadj"]:
            for k in TAX:
                total[k] -= r.get(k, ZERO)
        total["tax"] = total["iamt"] + total["camt"] + total["samt"] + total["csamt"]
        return total


def build_gstr1(session: Session, gstin: Gstin, period: str) -> Gstr1:
    own = gstin.state_code
    sections: dict[str, list[dict]] = {key: [] for key, _, _ in SECTIONS}
    warnings: list[str] = []
    b2cs: dict[tuple, dict] = defaultdict(_zero)
    nil: dict[str, dict] = {d: {"nil": ZERO, "exempt": ZERO, "non_gst": ZERO} for d in (
        "Inter-State supplies to registered persons", "Intra-State supplies to registered persons",
        "Inter-State supplies to unregistered persons", "Intra-State supplies to unregistered persons")}
    hsn: dict[str, dict[tuple, dict]] = {"hsn_b2b": {}, "hsn_b2c": {}}
    docs: dict[DocType, list[Invoice]] = defaultdict(list)

    invoices = session.scalars(
        select(Invoice).options(selectinload(Invoice.items))
        .where(Invoice.gstin_id == gstin.id, Invoice.period == period, Invoice.direction == Direction.OUTWARD)
        .order_by(Invoice.invoice_date, Invoice.invoice_no)
    ).all()

    for inv in invoices:
        docs[inv.doc_type].append(inv)
        is_note = inv.doc_type is not DocType.INVOICE
        ntty = "C" if inv.doc_type is DocType.CREDIT_NOTE else "D"
        sign = -1 if inv.doc_type is DocType.CREDIT_NOTE else 1
        registered = bool(inv.counterparty_gstin)
        pos = inv.place_of_supply or own
        interstate = pos != own
        cat = inv.supply_category
        common = {"pos": pos_label(pos), "val": inv.invoice_value}
        doc_ref = ({"nt_num": inv.invoice_no, "nt_dt": inv.invoice_date, "ntty": ntty} if is_note
                   else {"inum": inv.invoice_no, "idt": inv.invoice_date})

        # Table 12: every line, B2B or B2C by registration
        bucket = hsn["hsn_b2b" if registered else "hsn_b2c"]
        for item in inv.items:
            uqc = item.uqc or ("NA" if (item.hsn or "").startswith("99") else "OTH")  # services (SAC 99xx) take NA
            key = (item.hsn or "", uqc, item.tax_rate)
            row = bucket.setdefault(key, {"hsn": item.hsn or "", "desc": item.description or "", "uqc": uqc,
                                          "qty": ZERO, "val": ZERO, "rt": item.tax_rate, **_zero()})
            amounts = _item_amounts(item, sign)
            _add(row, amounts)
            row["qty"] += sign * (item.quantity or ZERO)
            row["val"] += sum(amounts.values(), ZERO) + sign * item.land_value
            if not item.hsn:
                warnings.append(f"{inv.invoice_no}: line without HSN/SAC - shown under a blank HSN in Table 12")

        # Table 8: nil-rated / exempt / non-GST (whole document by category, or 0% lines of a taxable one)
        nil_desc = (f"{'Inter' if interstate else 'Intra'}-State supplies to "
                    f"{'registered' if registered else 'unregistered'} persons")
        if cat in NIL_CATEGORIES:
            nil[nil_desc][NIL_CATEGORIES[cat]] += sign * inv.taxable_value
            continue
        for item in inv.items:
            if item.tax_rate == 0 and cat is SupplyCategory.TAXABLE:
                nil[nil_desc]["nil"] += sign * item.taxable_value

        rates = _by_rate(inv)
        if not rates:
            continue

        if cat in EXPORT_TYPES:
            for rt, amt in rates.items():
                if is_note:
                    sections["cdnur"].append({"ur_typ": CDNUR_EXPORT_TYPES[cat], **doc_ref, **common, "rt": rt, **amt})
                else:
                    sections["exp"].append({"exp_typ": EXPORT_TYPES[cat], **doc_ref, **common, "rt": rt, **amt})
            continue

        if registered:
            inv_typ = B2B_TYPES.get(cat, "Regular B2B")
            if cat is SupplyCategory.TAXABLE and not interstate and any(a["iamt"] for a in rates.values()):
                inv_typ = "Intra-State supplies attracting IGST"
            for rt, amt in rates.items():
                row = {"ctin": inv.counterparty_gstin, "name": inv.counterparty_name or "", **doc_ref, **common,
                       "rchrg": "Y" if inv.reverse_charge else "N", "inv_typ": inv_typ, "rt": rt, **amt}
                sections["cdnr" if is_note else "b2b"].append(row)
            continue

        # unregistered
        large = interstate and inv.invoice_value > B2CL_LIMIT
        if large:
            for rt, amt in rates.items():
                if is_note:
                    sections["cdnur"].append({"ur_typ": "B2CL", **doc_ref, **common, "rt": rt, **amt})
                else:
                    sections["b2cl"].append({**doc_ref, **common, "rt": rt, **amt})
            continue
        for rt, amt in rates.items():  # B2C credit notes are adjusted from the B2CS demand
            _add(b2cs[(pos, rt)], {k: sign * v for k, v in amt.items()})

    for (pos, rt), amt in sorted(b2cs.items()):
        if any(amt.values()):
            sections["b2cs"].append({"typ": "OE", "sply_ty": "INTRA" if pos == own else "INTER",
                                     "pos": pos_label(pos), "rt": rt, **amt})
            if amt["txval"] < 0:
                warnings.append(f"B2CS {pos_label(pos)} at {rt}%: credit notes exceed sales this month "
                                f"(net {amt['txval']}); the portal does not accept negative B2CS values")

    sections["exemp"] = [{"desc": d, **v} for d, v in nil.items() if any(v.values())]
    for key, rows in hsn.items():
        sections[key] = [r for _, r in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][2]))]

    natures = {DocType.INVOICE: "Invoices for outward supply", DocType.CREDIT_NOTE: "Credit Note",
               DocType.DEBIT_NOTE: "Debit Note"}
    for doc_type, items in docs.items():
        numbers = [i.invoice_no for i in items]
        sections["docs"].append({"nature": natures[doc_type], "from": numbers[0], "to": numbers[-1],
                                 "total": len(numbers), "cancelled": 0})

    if gstin.advance_tax_enabled:
        rep = period_report(run_ledger(session, gstin), period)
        for src, dest in (("table_11a", "at"), ("table_11b", "atadj")):
            sections[dest] = [{"pos": pos_label(r["place_of_supply"]), "sply_ty": r["supply_type"], "rt": r["rate"],
                               "gross": r["amount"], "txval": r["taxable_value"], "iamt": r["igst"],
                               "camt": r["cgst"], "samt": r["sgst"], "csamt": ZERO} for r in rep[src]]
        warnings += [f"Advances: {w}" for w in rep["warnings"]]

        # Table 13 also lists receipt vouchers (advances) and refund vouchers issued in the period.
        vouchers = session.scalars(
            select(AdvanceEntry).where(AdvanceEntry.gstin_id == gstin.id, AdvanceEntry.period == period,
                                       AdvanceEntry.entry_type.in_([AdvanceEntryType.ADVANCE, AdvanceEntryType.REFUND]))
            .order_by(AdvanceEntry.entry_date, AdvanceEntry.voucher_no)
        ).all()
        for entry_type, nature in ((AdvanceEntryType.ADVANCE, "Receipt voucher"), (AdvanceEntryType.REFUND, "Refund voucher")):
            numbers = list(dict.fromkeys(v.voucher_no for v in vouchers if v.entry_type is entry_type))
            if numbers:
                sections["docs"].append({"nature": nature, "from": numbers[0], "to": numbers[-1],
                                         "total": len(numbers), "cancelled": 0})

    for row in sections["hsn_b2b"] + sections["hsn_b2c"]:
        if not row["hsn"]:
            warnings.append("Table 12 has lines without HSN/SAC; the portal rejects a blank HSN - add it in the register")
            break

    return Gstr1(gstin, period, sections, list(dict.fromkeys(warnings)))
