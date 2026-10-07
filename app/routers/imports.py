from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_user
from app.config import settings
from app.db import get_session
from app.ingest.values import month_range, validate_period
from app.models import Direction, Gstin, User, ImportBatch, ImportKind, ImportSource, Invoice, InvoiceItem, PortalDocument
from app.schemas import ImportBatchRead, ImportResult, InvoiceRead, PortalDocumentRead
from app.services.imports import ImportRejected, delete_batch, import_advances, import_portal, import_register

router = APIRouter(tags=["ingestion"])


def _gstin_or_404(session: Session, gstin: str) -> Gstin:
    record = session.scalar(select(Gstin).where(Gstin.gstin == gstin.strip().upper()))
    if not record:
        raise HTTPException(404, f"GSTIN {gstin} is not in the entity master")
    if not record.active:
        raise HTTPException(422, f"GSTIN {gstin} is inactive")
    return record


def _period(period: str) -> str:
    try:
        return validate_period(period)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _months(period: str | None, from_: str | None, to: str | None, required: bool = True) -> list[str] | None:
    try:
        if period:
            return [validate_period(period)]
        if from_ or to:
            return month_range(from_ or to, to or from_)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if required:
        raise HTTPException(422, "give period=YYYY-MM, or from=YYYY-MM and to=YYYY-MM")
    return None


def period_range(period: str | None = None, from_: str | None = Query(None, alias="from"),
                 to: str | None = None) -> list[str]:
    """Dependency: one return period, or every month of a from-to range."""
    return _months(period, from_, to)


def optional_period_range(period: str | None = None, from_: str | None = Query(None, alias="from"),
                          to: str | None = None) -> list[str] | None:
    return _months(period, from_, to, required=False)


async def _read(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > settings.upload_max_mb * 1024 * 1024:
        raise HTTPException(413, f"file is larger than {settings.upload_max_mb} MB")
    if not content:
        raise HTTPException(422, "file is empty")
    return content


def _rejected(exc: ImportRejected) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={
        "detail": "import rejected",
        "errors": [e.as_dict() for e in exc.errors],
        "warnings": [w.as_dict() for w in exc.warnings],
    })


def _result(outcome) -> ImportResult:
    return ImportResult(batch=ImportBatchRead.model_validate(outcome.batch),
                        replaced_batch_ids=outcome.replaced_batch_ids, details=outcome.details)


@router.post("/imports/register", response_model=ImportResult, status_code=201,
             responses={409: {"description": "already imported / duplicates"}, 422: {"description": "validation errors"}})
async def upload_register(
    gstin: str = Form(...), period: str = Form(..., description="YYYY-MM"),
    kind: ImportKind = Form(..., description="sales_register, purchase_register, advance_register or advance_opening"),
    source: ImportSource = Form(ImportSource.EXCEL, description="tally or excel"),
    replace: bool = Form(False), file: UploadFile = File(...), session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    if not kind.is_spreadsheet:
        raise HTTPException(422, "kind must be sales_register, purchase_register, advance_register or advance_opening")
    record, period, content = _gstin_or_404(session, gstin), _period(period), await _read(file)
    handler = import_advances if kind.is_advance else import_register
    try:
        outcome = handler(session, gstin=record, period=period, kind=kind, source=source,
                          file_name=file.filename or "upload", content=content, replace=replace,
                          created_by=user.email)
    except ImportRejected as exc:
        session.rollback()
        return _rejected(exc)
    return _result(outcome)


@router.post("/imports/portal", response_model=ImportResult, status_code=201,
             responses={409: {"description": "already imported"}, 422: {"description": "validation errors"}})
async def upload_portal(
    gstin: str = Form(...), period: str = Form(..., description="YYYY-MM"),
    kind: ImportKind = Form(..., description="gstr2a or gstr2b"),
    replace: bool = Form(False), file: UploadFile = File(...), session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    if kind.is_spreadsheet:
        raise HTTPException(422, "kind must be gstr2a or gstr2b")
    record, period, content = _gstin_or_404(session, gstin), _period(period), await _read(file)
    try:
        outcome = import_portal(session, gstin=record, period=period, kind=kind,
                                file_name=file.filename or "upload", content=content, replace=replace,
                          created_by=user.email)
    except ImportRejected as exc:
        session.rollback()
        return _rejected(exc)
    return _result(outcome)


@router.get("/imports", response_model=list[ImportBatchRead])
def list_imports(gstin: str | None = None, months: list[str] | None = Depends(optional_period_range),
                 session: Session = Depends(get_session)):
    stmt = select(ImportBatch).order_by(ImportBatch.period.desc(), ImportBatch.kind)
    if gstin:
        stmt = stmt.where(ImportBatch.gstin_id == _gstin_or_404(session, gstin).id)
    if months:
        stmt = stmt.where(ImportBatch.period.in_(months))
    return session.scalars(stmt).all()


@router.delete("/imports/{batch_id}", status_code=204)
def remove_import(batch_id: int, session: Session = Depends(get_session)):
    batch = session.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(404, f"import batch {batch_id} not found")
    delete_batch(session, batch)


@router.get("/portal-documents", response_model=list[PortalDocumentRead])
def list_portal_documents(gstin: str, months: list[str] = Depends(period_range), kind: ImportKind = ImportKind.GSTR2B,
                          session: Session = Depends(get_session)):
    record = _gstin_or_404(session, gstin)
    return session.scalars(
        select(PortalDocument)
        .where(PortalDocument.gstin_id == record.id, PortalDocument.period.in_(months),
               PortalDocument.source == kind)
        .order_by(PortalDocument.supplier_name, PortalDocument.invoice_date)
    ).all()


@router.get("/invoices", response_model=list[InvoiceRead])
def list_invoices(gstin: str, direction: Direction, months: list[str] = Depends(period_range),
                  session: Session = Depends(get_session)):
    record = _gstin_or_404(session, gstin)
    return session.scalars(
        select(Invoice).options(selectinload(Invoice.items).selectinload(InvoiceItem.cost_centre))
        .where(Invoice.gstin_id == record.id, Invoice.period.in_(months), Invoice.direction == direction)
        .order_by(Invoice.invoice_date, Invoice.invoice_no)
    ).all()
