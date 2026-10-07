"""Cost-centre-wise GST tracking.

Returns are filed per GSTIN; this splits a GSTIN's period figures by the cost centre (branch / project)
each register line or advance is tagged with. Untagged lines fall into "Unassigned", so the cost centre
rows always add up to the GSTIN totals.
"""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CostCentre, Direction, DocType, Gstin, Invoice, InvoiceItem
from app.services.advance_ledger import run_ledger, table_11_contributions

ZERO = Decimal("0.00")
FIELDS = ("outward_taxable", "output_tax", "inward_taxable", "itc", "advance_11a", "advance_11b", "net")


def cost_centre_lookup(session: Session, gstin_id: int) -> dict[str, int]:
    """Active cost centres of a GSTIN by lower-cased name."""
    return {c.name.lower(): c.id for c in session.scalars(
        select(CostCentre).where(CostCentre.gstin_id == gstin_id, CostCentre.active.is_(True)))}


def cost_centre_report(session: Session, gstin: Gstin, months: list[str]) -> dict:
    centres = session.scalars(select(CostCentre).where(CostCentre.gstin_id == gstin.id).order_by(CostCentre.name)).all()
    rows: dict[int | None, dict] = defaultdict(lambda: dict.fromkeys(FIELDS, ZERO))
    docs: dict[int | None, dict[str, set]] = defaultdict(lambda: {"outward": set(), "inward": set()})

    lines = session.execute(
        select(InvoiceItem, Invoice.id, Invoice.direction, Invoice.doc_type, Invoice.itc_eligible)
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.gstin_id == gstin.id, Invoice.period.in_(months))
    ).all()
    for item, invoice_id, direction, doc_type, itc_eligible in lines:
        sign = -1 if doc_type is DocType.CREDIT_NOTE else 1
        tax = sign * (item.igst + item.cgst + item.sgst + item.cess)
        row = rows[item.cost_centre_id]
        if direction is Direction.OUTWARD:
            row["outward_taxable"] += sign * item.taxable_value
            row["output_tax"] += tax
            docs[item.cost_centre_id]["outward"].add(invoice_id)
        else:
            row["inward_taxable"] += sign * item.taxable_value
            if itc_eligible is not False:
                row["itc"] += tax
            docs[item.cost_centre_id]["inward"].add(invoice_id)

    if gstin.advance_tax_enabled:
        ledger = run_ledger(session, gstin)
        for month in months:
            for adv, table, amounts in table_11_contributions(ledger, month):
                tax = amounts["igst"] + amounts["cgst"] + amounts["sgst"]
                rows[adv.entry.cost_centre_id]["advance_11a" if table == "11a" else "advance_11b"] += tax

    out = []
    totals = dict.fromkeys(FIELDS, ZERO)
    ordered = [(c.id, c) for c in centres] + [(None, None)]
    for cc_id, centre in ordered:
        row = rows.get(cc_id)
        if row is None and (centre is None or not centre.active):
            continue  # skip empty Unassigned / inactive centres
        row = row or dict.fromkeys(FIELDS, ZERO)
        row["net"] = row["output_tax"] + row["advance_11a"] - row["advance_11b"] - row["itc"]
        for k in FIELDS:
            totals[k] += row[k]
        out.append({
            "cost_centre_id": cc_id, "name": centre.name if centre else "Unassigned",
            "code": centre.code if centre else None, "active": centre.active if centre else True,
            "outward_docs": len(docs[cc_id]["outward"]), "inward_docs": len(docs[cc_id]["inward"]), **row,
        })
    return {"from": months[0], "to": months[-1], "gstin": gstin.gstin, "advances_enabled": gstin.advance_tax_enabled,
            "rows": out, "totals": totals}
