"""Overview for a period (one month or a from-to range): what has been imported for each GSTIN, and
preliminary tax figures.

The tax figures are simple signed sums (credit notes negative) for orientation only; GSTR-3B will own the
real liability / ITC computation.
"""
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_session
from app.gstin import STATE_CODES
from app.models import (
    Direction, DocType, Entity, Gstin, ImportBatch, ImportKind, Invoice, PortalDocument,
)
from app.routers.imports import period_range
from app.schemas import as_utc
from app.services.advance_ledger import period_report, run_ledger

router = APIRouter(tags=["dashboard"])
ZERO = Decimal("0.00")
HEADS = ("taxable_value", "igst", "cgst", "sgst", "cess")


def _signed(model, column):
    return func.coalesce(func.sum(case((model.doc_type == DocType.CREDIT_NOTE, -column), else_=column)), 0)


def _sums(session: Session, model, months: list[str], *where) -> dict[int, dict[str, Decimal]]:
    rows = session.execute(
        select(model.gstin_id, *(_signed(model, getattr(model, h)) for h in HEADS))
        .where(model.period.in_(months), *where).group_by(model.gstin_id)
    ).all()
    out = {}
    for gstin_id, *values in rows:
        d = {h: Decimal(v).quantize(Decimal("0.01")) for h, v in zip(HEADS, values)}
        d["tax"] = d["igst"] + d["cgst"] + d["sgst"] + d["cess"]
        out[gstin_id] = d
    return out


def _empty() -> dict[str, Decimal]:
    return dict.fromkeys((*HEADS, "tax"), ZERO)


def _add(total: dict, part: dict) -> None:
    for k, v in part.items():
        total[k] += v


def _json(amounts: dict[str, Decimal]) -> dict[str, str]:
    # Exact decimal strings, as the Pydantic-backed endpoints return; never floats for money.
    return {k: str(v) for k, v in amounts.items()}


@router.get("/periods")
def list_periods(session: Session = Depends(get_session)):
    """Periods that have any imported data, newest first."""
    return session.scalars(select(ImportBatch.period).distinct().order_by(ImportBatch.period.desc())).all()


@router.get("/dashboard")
def dashboard(months: list[str] = Depends(period_range), session: Session = Depends(get_session)):
    gstins = session.scalars(
        select(Gstin).join(Entity).options(selectinload(Gstin.entity))
        .where(Gstin.active.is_(True), Entity.active.is_(True)).order_by(Entity.name, Gstin.gstin)
    ).all()

    # Import status per GSTIN and kind, summarised over the months of the range.
    batches = session.scalars(select(ImportBatch).where(ImportBatch.period.in_(months))
                              .order_by(ImportBatch.created_at)).all()
    status: dict[int, dict] = {}
    for b in batches:
        s = status.setdefault(b.gstin_id, {}).setdefault(b.kind.value, {
            "months": set(), "record_count": 0, "warning_count": 0})
        s["months"].add(b.period)
        s["record_count"] += b.record_count
        s["warning_count"] += len(b.warnings or [])
        s.update(batch_id=b.id, file_name=b.file_name, source=b.source.value, created_at=as_utc(b.created_at))
    for per_kind in status.values():
        for s in per_kind.values():
            s["months"], s["of"] = len(s["months"]), len(months)

    outward = _sums(session, Invoice, months, Invoice.direction == Direction.OUTWARD)
    itc_books = _sums(session, Invoice, months, Invoice.direction == Direction.INWARD, Invoice.itc_eligible.is_not(False))
    itc_2b = _sums(session, PortalDocument, months, PortalDocument.source == ImportKind.GSTR2B,
                   PortalDocument.itc_available.is_not(False))

    totals = {"outward": _empty(), "itc_books": _empty(), "itc_2b": _empty()}
    adv_totals = dict.fromkeys(("tax_11a", "tax_11b", "net_tax", "closing_amount"), ZERO)
    rows = []
    for g in gstins:
        o, ib, i2 = outward.get(g.id, _empty()), itc_books.get(g.id, _empty()), itc_2b.get(g.id, _empty())
        _add(totals["outward"], o)
        _add(totals["itc_books"], ib)
        _add(totals["itc_2b"], i2)
        advances = None
        if g.advance_tax_enabled:
            ledger = run_ledger(session, g)
            reports = [period_report(ledger, m) for m in months]
            advances = {"tax_11a": sum((r["tax_11a"] for r in reports), ZERO),
                        "tax_11b": sum((r["tax_11b"] for r in reports), ZERO),
                        "closing_amount": reports[-1]["closing_balance"]["amount"]}
            advances["net_tax"] = advances["tax_11a"] - advances["tax_11b"]
            for k, v in advances.items():
                adv_totals[k] += v
        imports = status.get(g.id, {})
        rows.append({
            "advances": advances and _json(advances),
            "gstin_id": g.id, "gstin": g.gstin, "entity_id": g.entity_id, "entity_name": g.entity.name,
            "trade_name": g.trade_name, "state_code": g.state_code, "state_name": STATE_CODES.get(g.state_code),
            "imports": {k.value: imports.get(k.value) for k in ImportKind},
            "outward": _json(o), "itc_books": _json(ib), "itc_2b": _json(i2),
        })
    return {"from": months[0], "to": months[-1], "months": months,
            "period": months[0] if len(months) == 1 else None,
            "totals": {k: _json(v) for k, v in totals.items()},
            "advances": _json(adv_totals) if any(g.advance_tax_enabled for g in gstins) else None,
            "gstins": rows}
