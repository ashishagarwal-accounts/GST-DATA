"""Advance ledger: replays advances, invoices and refunds for a GSTIN and derives GSTR-1 Tables 11A / 11B.

Adjustment rules
- An invoice line consumes advances of the same customer and the same service type, oldest first.
- Only advances dated on or before the invoice (and reported in the same or an earlier period) qualify.
- A manual rule (invoice no -> advance voucher no) is applied first and reserves that advance for
  that invoice, so oldest-first never hands it to another invoice.
- A refund consumes the advance named in "Against Voucher No", otherwise the oldest one.
- Tax set off on adjustment is the advance's own tax, pro rata to the amount consumed.

Reporting for period P
- 11A: advances received in P, to the extent still unadjusted at the end of P.
  (Received and adjusted in the same month appears in neither table.)
- 11B: adjustments and refunds in P of advances received in earlier periods or opening balances.

The ledger is recomputed on every call rather than stored, so it can never go stale after a
re-import or a rule change. Filed periods will be frozen once filing locks exist.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AdvanceEntry, AdvanceEntryType, AdvanceRule, Direction, DocType, Gstin, Invoice, InvoiceItem, ServiceType,
)

ZERO = Decimal("0.00")
PAISE = Decimal("0.01")
SPLIT = ("taxable_value", "igst", "cgst", "sgst")


def customer_key(name: str | None) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    text = re.sub(r"\b(M S|MS|MR|MRS|SHRI|SMT)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Allocation:
    advance: "OpenAdvance"
    kind: str                  # "invoice" | "refund"
    reference: str             # invoice no / refund voucher no
    on_date: date
    period: str
    amount: Decimal
    taxable_value: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    manual: bool = False


@dataclass
class OpenAdvance:
    entry: AdvanceEntry
    remaining: Decimal
    remaining_split: dict[str, Decimal]
    allocations: list[Allocation] = field(default_factory=list)

    @property
    def is_opening(self) -> bool:
        return self.entry.entry_type is AdvanceEntryType.OPENING

    def consume(self, amount: Decimal, *, kind: str, reference: str, on_date: date, period: str,
                manual: bool) -> Allocation:
        amount = min(amount, self.remaining)
        if amount == self.remaining:
            parts = dict(self.remaining_split)
        else:
            ratio = amount / self.remaining
            parts = {k: (v * ratio).quantize(PAISE, ROUND_HALF_UP) for k, v in self.remaining_split.items()}
        self.remaining -= amount
        for k in SPLIT:
            self.remaining_split[k] -= parts[k]
        alloc = Allocation(self, kind, reference, on_date, period, amount, manual=manual, **parts)
        self.allocations.append(alloc)
        return alloc


@dataclass
class LedgerResult:
    gstin: Gstin
    advances: list[OpenAdvance]
    allocations: list[Allocation]
    warnings: list[str]


def line_gross(item: InvoiceItem) -> Decimal:
    """Consideration incl. GST that an invoice line represents (deducted land value included)."""
    return item.taxable_value + item.land_value + item.igst + item.cgst + item.sgst + item.cess


def run_ledger(session: Session, gstin: Gstin) -> LedgerResult:
    warnings: list[str] = []
    entries = session.scalars(
        select(AdvanceEntry).options(selectinload(AdvanceEntry.service_type), selectinload(AdvanceEntry.cost_centre))
        .where(AdvanceEntry.gstin_id == gstin.id).order_by(AdvanceEntry.entry_date, AdvanceEntry.id)
    ).all()
    advances = [OpenAdvance(e, e.amount, {k: getattr(e, k) for k in SPLIT})
                for e in entries if e.entry_type is not AdvanceEntryType.REFUND]
    refunds = [e for e in entries if e.entry_type is AdvanceEntryType.REFUND]

    invoices = session.scalars(
        select(Invoice).options(selectinload(Invoice.items).selectinload(InvoiceItem.service_type))
        .where(Invoice.gstin_id == gstin.id, Invoice.direction == Direction.OUTWARD,
               Invoice.doc_type == DocType.INVOICE)
        .order_by(Invoice.invoice_date, Invoice.invoice_no)
    ).all()
    rules = session.scalars(select(AdvanceRule).where(AdvanceRule.gstin_id == gstin.id).order_by(AdvanceRule.id)).all()
    rules_by_invoice: dict[str, list[AdvanceRule]] = defaultdict(list)
    for rule in rules:
        rules_by_invoice[rule.invoice_no.upper()].append(rule)
    reserved = {r.advance_voucher_no.upper() for r in rules}
    rule_left = {r.id: r.amount for r in rules}
    rule_used: set[int] = set()

    by_customer: dict[str, list[OpenAdvance]] = defaultdict(list)
    for adv in advances:
        by_customer[customer_key(adv.entry.customer_name)].append(adv)
        if adv.entry.customer_gstin:
            by_customer[adv.entry.customer_gstin].append(adv)

    def candidates(name, gstin_no, service_type_id, on_date, period, cost_centre_id=None):
        pool = by_customer.get(customer_key(name), []) + (by_customer.get(gstin_no, []) if gstin_no else [])
        seen, out = set(), []
        for adv in sorted(pool, key=lambda a: (a.entry.entry_date, a.entry.id)):
            e = adv.entry
            if id(adv) in seen or adv.remaining <= 0 or e.service_type_id != service_type_id:
                continue
            if e.entry_date > on_date or (not adv.is_opening and e.period > period):
                continue
            if gstin_no and e.customer_gstin and gstin_no != e.customer_gstin:
                continue
            # an advance for one cost centre (project / branch) never adjusts against another's invoice
            if cost_centre_id and e.cost_centre_id and cost_centre_id != e.cost_centre_id:
                continue
            seen.add(id(adv))
            out.append(adv)
        return out

    allocations: list[Allocation] = []
    events = [(inv.invoice_date, 0, inv.invoice_no, inv) for inv in invoices] + \
             [(r.entry_date, 1, r.voucher_no, r) for r in refunds]
    events.sort(key=lambda ev: ev[:3])

    for on_date, _, _, obj in events:
        if isinstance(obj, Invoice):
            inv = obj
            for item in inv.items:
                if item.service_type_id is None:
                    continue
                need = line_gross(item)
                # 1. manual rules for this invoice
                for rule in rules_by_invoice.get(inv.invoice_no.upper(), []):
                    if need <= 0:
                        break
                    if rule_left[rule.id] is not None and rule_left[rule.id] <= 0:
                        continue
                    for adv in [a for a in advances if a.entry.voucher_no.upper() == rule.advance_voucher_no.upper()
                                and a.entry.service_type_id == item.service_type_id and a.remaining > 0
                                and (a.is_opening or a.entry.period <= inv.period)]:
                        if adv.entry.entry_date > inv.invoice_date:
                            warnings.append(f"Rule: advance {adv.entry.voucher_no} is dated after invoice "
                                            f"{inv.invoice_no}; not applied")
                            continue
                        take = need if rule_left[rule.id] is None else min(need, rule_left[rule.id])
                        alloc = adv.consume(take, kind="invoice", reference=inv.invoice_no, on_date=inv.invoice_date,
                                            period=inv.period, manual=True)
                        allocations.append(alloc)
                        rule_used.add(rule.id)
                        need -= alloc.amount
                        if rule_left[rule.id] is not None:
                            rule_left[rule.id] -= alloc.amount
                        if item.cost_centre_id and adv.entry.cost_centre_id and item.cost_centre_id != adv.entry.cost_centre_id:
                            warnings.append(f"Rule: advance {adv.entry.voucher_no} ({adv.entry.cost_centre.name}) adjusted "
                                            f"against invoice {inv.invoice_no} of another cost centre")
                        if customer_key(adv.entry.customer_name) != customer_key(inv.counterparty_name):
                            warnings.append(f"Rule: advance {adv.entry.voucher_no} ({adv.entry.customer_name}) adjusted "
                                            f"against invoice {inv.invoice_no} of {inv.counterparty_name}")
                # 2. oldest first
                for adv in candidates(inv.counterparty_name, inv.counterparty_gstin, item.service_type_id,
                                      inv.invoice_date, inv.period, item.cost_centre_id):
                    if need <= 0:
                        break
                    if adv.entry.voucher_no.upper() in reserved:
                        continue
                    alloc = adv.consume(need, kind="invoice", reference=inv.invoice_no, on_date=inv.invoice_date,
                                        period=inv.period, manual=False)
                    allocations.append(alloc)
                    need -= alloc.amount
        else:
            refund = obj
            need = refund.amount
            pool = candidates(refund.customer_name, refund.customer_gstin, refund.service_type_id,
                              refund.entry_date, refund.period, refund.cost_centre_id)
            if refund.against_voucher_no:
                pool = [a for a in pool if a.entry.voucher_no.upper() == refund.against_voucher_no.upper()]
                if not pool:
                    warnings.append(f"Refund {refund.voucher_no}: advance {refund.against_voucher_no} for "
                                    f"{refund.customer_name} / {refund.service_type.name} has no open balance")
            else:
                pool = [a for a in pool if a.entry.voucher_no.upper() not in reserved]
            for adv in pool:
                if need <= 0:
                    break
                alloc = adv.consume(need, kind="refund", reference=refund.voucher_no, on_date=refund.entry_date,
                                    period=refund.period, manual=bool(refund.against_voucher_no))
                allocations.append(alloc)
                need -= alloc.amount
            if need > 0:
                warnings.append(f"Refund {refund.voucher_no} to {refund.customer_name}: {need} exceeds the open "
                                f"{refund.service_type.name} advances; no tax reversed on that part")

    for rule in rules:
        if rule.id not in rule_used:
            warnings.append(f"Rule: advance {rule.advance_voucher_no} -> invoice {rule.invoice_no} was not applied "
                            "(invoice, advance or matching service type not found, or nothing left to adjust)")
    return LedgerResult(gstin, advances, allocations, warnings)


# ---------------------------------------------------------------- period reports

def _bucket() -> dict[str, Decimal]:
    return {"amount": ZERO, **dict.fromkeys(SPLIT, ZERO)}


def _rows(groups: dict[tuple, dict], own_state: str) -> list[dict]:
    rows = []
    for (pos, rate), values in sorted(groups.items()):
        if all(v == 0 for v in values.values()):
            continue
        rows.append({"place_of_supply": pos, "supply_type": "INTRA" if pos == own_state else "INTER",
                     "rate": rate, **values})
    return rows


def table_11_contributions(result: LedgerResult, period: str):
    """Yield (advance, "11a" | "11b", amounts) for the period - the single place the 11A / 11B rules live.

    11A: an advance received in the period, less what was adjusted / refunded within the same period.
    11B: each adjustment / refund in the period of an advance from an earlier period or an opening balance.
    """
    for adv in result.advances:
        e = adv.entry
        in_period = [a for a in adv.allocations if a.period == period]
        if e.entry_type is AdvanceEntryType.ADVANCE and e.period == period:
            amounts = {"amount": e.amount - sum((a.amount for a in in_period), ZERO)}
            for k in SPLIT:
                amounts[k] = getattr(e, k) - sum((getattr(a, k) for a in in_period), ZERO)
            yield adv, "11a", amounts
        elif adv.is_opening or e.period < period:
            for a in in_period:
                yield adv, "11b", {"amount": a.amount, **{k: getattr(a, k) for k in SPLIT}}


def period_report(result: LedgerResult, period: str) -> dict:
    own_state = result.gstin.state_code
    table_11a: dict[tuple, dict] = defaultdict(_bucket)
    table_11b: dict[tuple, dict] = defaultdict(_bucket)
    same_month = _bucket()
    received = _bucket()
    refunded = ZERO
    closing = _bucket()

    for adv, table, amounts in table_11_contributions(result, period):
        bucket = (table_11a if table == "11a" else table_11b)[(adv.entry.place_of_supply, adv.entry.tax_rate)]
        for k, v in amounts.items():
            bucket[k] += v

    for adv in result.advances:
        e = adv.entry
        in_period = [a for a in adv.allocations if a.period == period]
        if e.entry_type is AdvanceEntryType.ADVANCE and e.period == period:
            received["amount"] += e.amount
            for k in SPLIT:
                received[k] += getattr(e, k)
            for a in in_period:
                same_month["amount"] += a.amount
                for k in SPLIT:
                    same_month[k] += getattr(a, k)
        refunded += sum((a.amount for a in in_period if a.kind == "refund"), ZERO)

        if adv.is_opening or e.period <= period:
            upto = [a for a in adv.allocations if a.period <= period]
            closing["amount"] += e.amount - sum((a.amount for a in upto), ZERO)
            for k in SPLIT:
                closing[k] += getattr(e, k) - sum((getattr(a, k) for a in upto), ZERO)

    def tax(b):
        return b["igst"] + b["cgst"] + b["sgst"]

    rows_11a, rows_11b = _rows(table_11a, own_state), _rows(table_11b, own_state)
    total_11a = sum((tax(r) for r in rows_11a), ZERO)
    total_11b = sum((tax(r) for r in rows_11b), ZERO)
    return {
        "period": period,
        "enabled": result.gstin.advance_tax_enabled,
        "table_11a": rows_11a,
        "table_11b": rows_11b,
        "tax_11a": total_11a,
        "tax_11b": total_11b,
        "net_tax": total_11a - total_11b,
        "received": received,
        "adjusted_same_month": same_month,
        "refunded": refunded,
        "closing_balance": closing,
        "warnings": result.warnings,
    }


def ledger_rows(result: LedgerResult, period: str | None = None) -> list[dict]:
    """One row per advance with its adjustments, for the review screen."""
    out = []
    for adv in result.advances:
        e = adv.entry
        if period and not (adv.is_opening or e.period <= period):
            continue
        allocs = [a for a in adv.allocations if not period or a.period <= period]
        adjusted = sum((a.amount for a in allocs if a.kind == "invoice"), ZERO)
        refunded = sum((a.amount for a in allocs if a.kind == "refund"), ZERO)
        out.append({
            "id": e.id, "voucher_no": e.voucher_no, "entry_date": e.entry_date, "period": e.period,
            "entry_type": e.entry_type.value, "customer_name": e.customer_name, "customer_gstin": e.customer_gstin,
            "service_type": e.service_type.name, "cost_centre": e.cost_centre.name if e.cost_centre else None,
            "place_of_supply": e.place_of_supply, "tax_rate": e.tax_rate,
            "amount": e.amount, "taxable_value": e.taxable_value, "tax": e.igst + e.cgst + e.sgst,
            "adjusted": adjusted, "refunded": refunded, "balance": e.amount - adjusted - refunded,
            "allocations": [{
                "kind": a.kind, "reference": a.reference, "date": a.on_date, "period": a.period, "amount": a.amount,
                "tax": a.igst + a.cgst + a.sgst, "manual": a.manual,
            } for a in allocs],
        })
    return out


def service_type_lookup(session: Session, gstin_id: int) -> dict[str, ServiceType]:
    return {s.name.lower(): s for s in session.scalars(
        select(ServiceType).where(ServiceType.gstin_id == gstin_id, ServiceType.active.is_(True)))}
