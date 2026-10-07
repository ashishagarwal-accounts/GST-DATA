"""Persist parsed registers and portal downloads as import batches.

Rules:
- An import is all-or-nothing: any blocking error rejects the whole file.
- One active batch per (GSTIN, period, kind). Re-importing requires replace=True, which deletes
  the previous batch and its documents in the same transaction.
- A register invoice that already exists in another batch (same GSTIN, direction, doc type,
  number, counterparty and financial year) is rejected as a duplicate.
"""
import hashlib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ingest.advances import LAND_DEDUCTION_FACTOR, ServiceTypeInfo, parse_advances
from app.ingest.portal import parse_portal_json
from app.ingest.registers import parse_register
from app.ingest.values import Issue, financial_year_start
from app.models import (
    AdvanceEntry, AdvanceEntryType, Direction, Gstin, ImportBatch, ImportKind, ImportSource, Invoice, InvoiceItem,
    PortalDocument,
)
from app.services.advance_ledger import service_type_lookup
from app.services.cost_centres import cost_centre_lookup


ZERO = Decimal("0.00")


class ImportRejected(Exception):
    def __init__(self, errors: list[Issue], warnings: list[Issue] | None = None, status: int = 422):
        super().__init__(errors[0].message if errors else "import rejected")
        self.errors = errors
        self.warnings = warnings or []
        self.status = status


@dataclass
class ImportOutcome:
    batch: ImportBatch
    warnings: list[Issue]
    replaced_batch_ids: list[int] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _existing_batches(session: Session, gstin: Gstin, period: str, kind: ImportKind) -> list[ImportBatch]:
    return list(session.scalars(select(ImportBatch).where(
        ImportBatch.gstin_id == gstin.id, ImportBatch.period == period, ImportBatch.kind == kind)))


def _prepare(session: Session, gstin: Gstin, period: str, kind: ImportKind, content: bytes,
             replace: bool) -> tuple[list[ImportBatch], str, list[Issue]]:
    existing = _existing_batches(session, gstin, period, kind)
    if existing and not replace:
        raise ImportRejected([Issue(
            f"{kind.value} for {gstin.gstin} {period} was already imported (batch "
            f"{', '.join(str(b.id) for b in existing)}); re-upload with replace=true to overwrite")], status=409)

    sha = hashlib.sha256(content).hexdigest()
    warnings = []
    stmt = select(ImportBatch).where(ImportBatch.file_sha256 == sha, ImportBatch.kind == kind)
    if existing:
        stmt = stmt.where(ImportBatch.id.not_in([b.id for b in existing]))
    for other in session.scalars(stmt):
        warnings.append(Issue(
            f"identical file was already imported as batch {other.id} for {other.gstin.gstin} {other.period}"))
    return existing, sha, warnings


def _invoice_key(gstin_id, direction, doc_type, invoice_no, counterparty, invoice_date):
    return (gstin_id, direction, doc_type, invoice_no.upper(), counterparty or "", financial_year_start(invoice_date))


def import_register(session: Session, *, gstin: Gstin, period: str, kind: ImportKind, source: ImportSource,
                    file_name: str, content: bytes, replace: bool = False,
                    created_by: str | None = None) -> ImportOutcome:
    assert kind.is_register
    existing, sha, warnings = _prepare(session, gstin, period, kind, content, replace)
    parsed = parse_register(content, file_name, direction=kind.direction, own_gstin=gstin.gstin, period=period)
    warnings += parsed.warnings
    if parsed.errors:
        raise ImportRejected(parsed.errors, warnings)

    # Cost centre column (sales and purchases): line-level tag for cost-centre-wise GST tracking.
    cc_lookup = cost_centre_lookup(session, gstin.id)
    cost_centre_ids: dict[int, int] = {}
    unknown_cc = []
    for inv in parsed.invoices:
        for item in inv.items:
            if not item.cost_centre:
                continue
            cc_id = cc_lookup.get(item.cost_centre.lower())
            if cc_id:
                cost_centre_ids[id(item)] = cc_id
            else:
                unknown_cc.append(Issue(f"unknown cost centre '{item.cost_centre}' (set up under Entities: "
                                        f"{', '.join(sorted(cc_lookup)) or 'none'})", row=item.source_row, field="cost_centre"))
    if unknown_cc:
        raise ImportRejected(unknown_cc, warnings)
    if cc_lookup:
        untagged = sum(1 for inv in parsed.invoices for i in inv.items if id(i) not in cost_centre_ids)
        if untagged:
            warnings.append(Issue(f"{untagged} line(s) have no cost centre; they will show as Unassigned"))

    # Service type column (sales only): links invoice lines to advances of the same service.
    service_ids: dict[int, int] = {}
    if kind.direction is Direction.OUTWARD:
        lookup = service_type_lookup(session, gstin.id)
        unknown = []
        for inv in parsed.invoices:
            for item in inv.items:
                if not item.service_type:
                    continue
                st = lookup.get(item.service_type.lower())
                if st:
                    service_ids[id(item)] = st.id
                else:
                    unknown.append(Issue(f"unknown service type '{item.service_type}' (set up: "
                                         f"{', '.join(sorted(s.name for s in lookup.values())) or 'none'})",
                                         row=item.source_row, field="service_type"))
        if unknown:
            raise ImportRejected(unknown, warnings)

        # Service types are property-linked (flat sale, CAM, rent ...), so place of supply is the property's
        # location, i.e. the GSTIN's own state (IGST Act s.12(3)); the "assumed own state" warning is noise there.
        for inv in parsed.invoices:
            if inv.items and all(id(i) in service_ids for i in inv.items):
                warnings[:] = [w for w in warnings
                               if not (w.field == "place_of_supply" and w.row == inv.source_rows[0] and "assumed own state" in w.message)]

        # Land deduction is not in the register: the taxable value there is the full consideration. Report
        # two-thirds as taxable at the service type's rate and keep the deducted third as land value.
        by_id = {s.id: s for s in lookup.values()}
        for inv in parsed.invoices:
            for item in inv.items:
                st = by_id.get(service_ids.get(id(item)))
                if not st or not st.land_deduction:
                    continue
                full = item.taxable_value
                item.taxable_value = (full * LAND_DEDUCTION_FACTOR).quantize(Decimal("0.01"), ROUND_HALF_UP)
                item.land_value = full - item.taxable_value
                item.tax_rate = st.tax_rate
                # the parser judged the rate against the full value; redo it against the deducted value
                warnings[:] = [w for w in warnings if not (w.field == "tax_rate" and w.row == item.source_row)]
                tax = item.igst + item.cgst + item.sgst
                expected = item.taxable_value * st.tax_rate / 100
                if abs(tax - expected) > max(Decimal("1.00"), tax * Decimal("0.005")):
                    warnings.append(Issue(
                        f"invoice {inv.invoice_no}: tax {tax} is not {st.tax_rate}% of {item.taxable_value} "
                        f"(two-thirds of {full} after land deduction; expected {expected:.2f})",
                        row=item.source_row, field="tax_rate"))

    # Cross-batch duplicate check (excluding the batches this import will replace).
    replaced_ids = [b.id for b in existing]
    numbers = sorted({inv.invoice_no for inv in parsed.invoices})
    seen: dict[tuple, Invoice] = {}
    for chunk_start in range(0, len(numbers), 500):
        stmt = select(Invoice).where(
            Invoice.gstin_id == gstin.id, Invoice.direction == kind.direction,
            Invoice.invoice_no.in_(numbers[chunk_start:chunk_start + 500]))
        if replaced_ids:
            stmt = stmt.where(Invoice.import_batch_id.not_in(replaced_ids))
        for inv in session.scalars(stmt):
            seen[_invoice_key(gstin.id, inv.direction, inv.doc_type, inv.invoice_no,
                              inv.counterparty_gstin or inv.counterparty_name, inv.invoice_date)] = inv
    duplicates = []
    for inv in parsed.invoices:
        key = _invoice_key(gstin.id, kind.direction, inv.doc_type, inv.invoice_no,
                           inv.counterparty_gstin or inv.counterparty_name, inv.invoice_date)
        if key in seen:
            prior = seen[key]
            duplicates.append(Issue(
                f"{inv.doc_type.value} {inv.invoice_no} already imported in period {prior.period} "
                f"(batch {prior.import_batch_id})", row=inv.source_rows[0], field="invoice_no"))
    if duplicates:
        raise ImportRejected(duplicates, warnings, status=409)

    for old in existing:
        _delete_batch_rows(session, old)
    session.flush()

    batch = ImportBatch(
        gstin_id=gstin.id, period=period, kind=kind, source=source, file_name=file_name[:255],
        file_sha256=sha, record_count=len(parsed.invoices), warnings=[w.as_dict() for w in warnings],
        created_by=created_by,
    )
    session.add(batch)
    session.flush()
    for p in parsed.invoices:
        invoice = Invoice(
            gstin_id=gstin.id, import_batch_id=batch.id, period=period, direction=kind.direction,
            doc_type=p.doc_type, invoice_no=p.invoice_no, invoice_date=p.invoice_date,
            counterparty_gstin=p.counterparty_gstin, counterparty_name=p.counterparty_name,
            place_of_supply=p.place_of_supply, reverse_charge=p.reverse_charge,
            supply_category=p.supply_category, itc_eligible=p.itc_eligible, invoice_value=p.invoice_value,
            source_rows=p.source_rows,
            taxable_value=p.total("taxable_value"), igst=p.total("igst"), cgst=p.total("cgst"),
            sgst=p.total("sgst"), cess=p.total("cess"),
            items=[InvoiceItem(
                hsn=i.hsn, description=i.description, uqc=i.uqc, quantity=i.quantity, tax_rate=i.tax_rate,
                taxable_value=i.taxable_value, igst=i.igst, cgst=i.cgst, sgst=i.sgst, cess=i.cess,
                service_type_id=service_ids.get(id(i)), land_value=i.land_value,
                cost_centre_id=cost_centre_ids.get(id(i)),
            ) for i in p.items],
        )
        session.add(invoice)
    session.commit()
    return ImportOutcome(batch, warnings, replaced_ids, {
        "sheet": parsed.sheet, "header_row": parsed.header_row, "column_map": parsed.column_map,
        "unmapped_columns": parsed.unmapped_columns,
    })


def import_portal(session: Session, *, gstin: Gstin, period: str, kind: ImportKind, file_name: str,
                  content: bytes, replace: bool = False, created_by: str | None = None) -> ImportOutcome:
    assert not kind.is_spreadsheet
    existing, sha, warnings = _prepare(session, gstin, period, kind, content, replace)
    parsed = parse_portal_json(content, kind)
    warnings += parsed.warnings
    errors = list(parsed.errors)
    if not errors and parsed.gstin != gstin.gstin:
        errors.append(Issue(f"file is for GSTIN {parsed.gstin}, not {gstin.gstin}"))
    if not errors and parsed.period != period:
        errors.append(Issue(f"file is for period {parsed.period}, not {period}"))
    if errors:
        raise ImportRejected(errors, warnings)

    for old in existing:
        _delete_batch_rows(session, old)
    session.flush()

    batch = ImportBatch(
        gstin_id=gstin.id, period=period, kind=kind, source=ImportSource.GST_PORTAL, file_name=file_name[:255],
        file_sha256=sha, record_count=len(parsed.documents), warnings=[w.as_dict() for w in warnings],
        created_by=created_by,
    )
    session.add(batch)
    session.flush()
    session.add_all(PortalDocument(
        gstin_id=gstin.id, import_batch_id=batch.id, period=period, source=kind, **vars(d),
    ) for d in parsed.documents)
    session.commit()
    return ImportOutcome(batch, warnings, [b.id for b in existing])


def import_advances(session: Session, *, gstin: Gstin, period: str, kind: ImportKind, source: ImportSource,
                    file_name: str, content: bytes, replace: bool = False,
                    created_by: str | None = None) -> ImportOutcome:
    """Advance register (per period) or opening balances (once per GSTIN; period = go-live month)."""
    assert kind.is_advance
    opening = kind is ImportKind.ADVANCE_OPENING
    if not gstin.advance_tax_enabled:
        raise ImportRejected([Issue(f"GST on advances is not enabled for {gstin.gstin}; enable it on the Advances page first")])
    if opening:
        existing = list(session.scalars(select(ImportBatch).where(
            ImportBatch.gstin_id == gstin.id, ImportBatch.kind == kind)))
        if existing and not replace:
            raise ImportRejected([Issue(
                f"opening balances for {gstin.gstin} were already imported (go-live {existing[0].period}); "
                "re-upload with replace=true to overwrite")], status=409)
        _, sha, warnings = _prepare(session, gstin, period, kind, content, replace=True)
    else:
        existing, sha, warnings = _prepare(session, gstin, period, kind, content, replace)

    lookup = {name: ServiceTypeInfo(s.id, s.name, s.tax_rate, s.land_deduction)
              for name, s in service_type_lookup(session, gstin.id).items()}
    parsed = parse_advances(content, file_name, opening=opening, own_gstin=gstin.gstin, period=period,
                            service_types=lookup, tax_from=gstin.advance_tax_from,
                            cost_centres=cost_centre_lookup(session, gstin.id))
    warnings += parsed.warnings
    if parsed.errors:
        raise ImportRejected(parsed.errors, warnings)

    if opening:
        first_period = session.scalar(select(ImportBatch.period).where(
            ImportBatch.gstin_id == gstin.id, ImportBatch.kind == ImportKind.ADVANCE_REGISTER)
            .order_by(ImportBatch.period).limit(1))
        if first_period and first_period < period:
            raise ImportRejected([Issue(
                f"advance registers already exist from {first_period}; the go-live period must not be later")])

    replaced_ids = [b.id for b in existing]
    stmt = select(AdvanceEntry).where(AdvanceEntry.gstin_id == gstin.id)
    if replaced_ids:
        stmt = stmt.where(AdvanceEntry.import_batch_id.not_in(replaced_ids))
    taken = {(e.entry_type is AdvanceEntryType.REFUND, e.voucher_no.upper(), e.customer_name.upper(), e.service_type_id,
              e.cost_centre_id): e
             for e in session.scalars(stmt)}
    duplicates = []
    for p in parsed.entries:
        prior = taken.get((p.entry_type is AdvanceEntryType.REFUND, p.voucher_no.upper(), p.customer_name.upper(),
                           p.service_type.id, p.cost_centre_id))
        if prior:
            duplicates.append(Issue(f"{p.voucher_no} for {p.customer_name} / {p.service_type.name} already imported "
                                    f"in period {prior.period}", row=p.source_row, field="voucher_no"))
    if duplicates:
        raise ImportRejected(duplicates, warnings, status=409)

    for old in existing:
        _delete_batch_rows(session, old)
    session.flush()

    batch = ImportBatch(
        gstin_id=gstin.id, period=period, kind=kind, source=source, file_name=file_name[:255], file_sha256=sha,
        record_count=len(parsed.entries), warnings=[w.as_dict() for w in warnings], created_by=created_by,
    )
    session.add(batch)
    session.flush()
    session.add_all(AdvanceEntry(
        gstin_id=gstin.id, import_batch_id=batch.id, period=period, entry_type=p.entry_type,
        voucher_no=p.voucher_no, entry_date=p.entry_date, customer_name=p.customer_name,
        customer_gstin=p.customer_gstin, service_type_id=p.service_type.id, place_of_supply=p.place_of_supply,
        tax_rate=p.service_type.tax_rate, amount=p.amount, against_voucher_no=p.against_voucher_no,
        narration=p.narration, source_row=p.source_row, cost_centre_id=p.cost_centre_id,
        taxable_value=p.split.taxable_value, igst=p.split.igst, cgst=p.split.cgst, sgst=p.split.sgst,
    ) for p in parsed.entries)
    session.commit()
    return ImportOutcome(batch, warnings, replaced_ids, {
        "sheet": parsed.sheet, "header_row": parsed.header_row, "column_map": parsed.column_map,
        "unmapped_columns": parsed.unmapped_columns,
    })


def _delete_batch_rows(session: Session, batch: ImportBatch) -> None:
    session.execute(delete(InvoiceItem).where(InvoiceItem.invoice_id.in_(
        select(Invoice.id).where(Invoice.import_batch_id == batch.id))))
    session.execute(delete(Invoice).where(Invoice.import_batch_id == batch.id))
    session.execute(delete(PortalDocument).where(PortalDocument.import_batch_id == batch.id))
    session.execute(delete(AdvanceEntry).where(AdvanceEntry.import_batch_id == batch.id))
    session.delete(batch)


def delete_batch(session: Session, batch: ImportBatch) -> None:
    _delete_batch_rows(session, batch)
    session.commit()
