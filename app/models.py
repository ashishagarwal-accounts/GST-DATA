"""Core tables: entities, GSTINs, import batches, invoice register, portal (2A/2B) documents, tax ledger.

All amounts are stored as positive Numeric(15, 2); the document type (invoice / credit note /
debit note) carries the sign. Periods are 'YYYY-MM' strings (the GST return period).
"""
import enum
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

Money = Numeric(15, 2)
ZERO = Decimal("0.00")


def utcnow() -> datetime:
    """Timestamps are stored as naive UTC; the API marks them as UTC on the way out."""
    return datetime.now(UTC).replace(tzinfo=None)


def _enum(cls):
    # Store enum values (not names) as VARCHAR so the DB stays readable and portable.
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class RegistrationType(str, enum.Enum):
    REGULAR = "regular"
    COMPOSITION = "composition"
    SEZ_UNIT = "sez_unit"
    ISD = "isd"
    CASUAL = "casual"


class Direction(str, enum.Enum):
    OUTWARD = "outward"
    INWARD = "inward"


class DocType(str, enum.Enum):
    INVOICE = "INV"
    CREDIT_NOTE = "CRN"
    DEBIT_NOTE = "DBN"


class SupplyCategory(str, enum.Enum):
    TAXABLE = "taxable"
    EXPORT_WITH_PAYMENT = "export_wpay"
    EXPORT_WITHOUT_PAYMENT = "export_wopay"
    SEZ_WITH_PAYMENT = "sez_wpay"
    SEZ_WITHOUT_PAYMENT = "sez_wopay"
    DEEMED_EXPORT = "deemed_export"
    NIL_RATED = "nil_rated"
    EXEMPT = "exempt"
    NON_GST = "non_gst"


class ImportKind(str, enum.Enum):
    SALES_REGISTER = "sales_register"
    PURCHASE_REGISTER = "purchase_register"
    GSTR2A = "gstr2a"
    GSTR2B = "gstr2b"
    ADVANCE_REGISTER = "advance_register"    # advances received / refunded in the period (from the bank book)
    ADVANCE_OPENING = "advance_opening"      # unadjusted advances at go-live, uploaded once per GSTIN

    @property
    def is_register(self) -> bool:
        return self in (ImportKind.SALES_REGISTER, ImportKind.PURCHASE_REGISTER)

    @property
    def is_advance(self) -> bool:
        return self in (ImportKind.ADVANCE_REGISTER, ImportKind.ADVANCE_OPENING)

    @property
    def is_spreadsheet(self) -> bool:
        return self.is_register or self.is_advance

    @property
    def direction(self) -> Direction:
        return Direction.OUTWARD if self is ImportKind.SALES_REGISTER else Direction.INWARD


class ImportSource(str, enum.Enum):
    TALLY = "tally"
    EXCEL = "excel"
    GST_PORTAL = "gst_portal"


class AdvanceEntryType(str, enum.Enum):
    ADVANCE = "advance"    # received in the batch period
    OPENING = "opening"    # unadjusted at go-live; tax already paid under the old process
    REFUND = "refund"      # advance returned to the customer


class TaxHead(str, enum.Enum):
    IGST = "IGST"
    CGST = "CGST"
    SGST = "SGST"
    CESS = "CESS"


class LedgerEntryType(str, enum.Enum):
    LIABILITY = "liability"
    ITC = "itc"
    ITC_REVERSAL = "itc_reversal"
    UTILISATION = "utilisation"
    CASH_PAYMENT = "cash_payment"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Entity(TimestampMixin, Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    pan: Mapped[str | None] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    gstins: Mapped[list["Gstin"]] = relationship(back_populates="entity", order_by="Gstin.gstin")


class Gstin(TimestampMixin, Base):
    __tablename__ = "gstins"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    gstin: Mapped[str] = mapped_column(String(15), unique=True)
    state_code: Mapped[str] = mapped_column(String(2))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    trade_name: Mapped[str | None] = mapped_column(String(200))
    registration_type: Mapped[RegistrationType] = mapped_column(_enum(RegistrationType), default=RegistrationType.REGULAR)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    advance_tax_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    advance_tax_from: Mapped[date | None] = mapped_column(Date)  # advances dated earlier are rejected

    entity: Mapped[Entity] = relationship(back_populates="gstins")
    service_types: Mapped[list["ServiceType"]] = relationship(back_populates="gstin", order_by="ServiceType.name")
    cost_centres: Mapped[list["CostCentre"]] = relationship(back_populates="gstin", order_by="CostCentre.name")


class CostCentre(TimestampMixin, Base):
    """Branch / project / site under a GSTIN. Lines of registers and advances can be tagged with one,
    for cost-centre-wise GST tracking; the return itself is still filed per GSTIN."""

    __tablename__ = "cost_centres"
    __table_args__ = (UniqueConstraint("gstin_id", "name", name="uq_cost_centre_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str | None] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    gstin: Mapped[Gstin] = relationship(back_populates="cost_centres")


class ServiceType(TimestampMixin, Base):
    """What an advance or invoice line is for (flat sale, CAM, rent ...). Advances only adjust
    against invoices of the same service type. Sale of flats / commercial space gets the
    deemed one-third land deduction: taxable value = 2/3 of the consideration."""

    __tablename__ = "service_types"
    __table_args__ = (UniqueConstraint("gstin_id", "name", name="uq_service_type_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    name: Mapped[str] = mapped_column(String(100))
    sac: Mapped[str | None] = mapped_column(String(8))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    land_deduction: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    gstin: Mapped[Gstin] = relationship(back_populates="service_types")


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (Index("ix_import_batches_gstin_period_kind", "gstin_id", "period", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[ImportKind] = mapped_column(_enum(ImportKind))
    source: Mapped[ImportSource] = mapped_column(_enum(ImportSource))
    file_name: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    record_count: Mapped[int] = mapped_column(Integer)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    gstin: Mapped[Gstin] = relationship()


class _Amounts:
    taxable_value: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    igst: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    cgst: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    sgst: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    cess: Mapped[Decimal] = mapped_column(Money, default=ZERO)


class Invoice(_Amounts, Base):
    """A document from Alcove's own books (sales or purchase register)."""

    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_gstin_period_dir", "gstin_id", "period", "direction"),
        Index("ix_invoices_lookup", "gstin_id", "direction", "invoice_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    doc_type: Mapped[DocType] = mapped_column(_enum(DocType), default=DocType.INVOICE)
    invoice_no: Mapped[str] = mapped_column(String(50))
    invoice_date: Mapped[date] = mapped_column(Date)
    counterparty_gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    counterparty_name: Mapped[str | None] = mapped_column(String(255))
    place_of_supply: Mapped[str | None] = mapped_column(String(2))
    reverse_charge: Mapped[bool] = mapped_column(Boolean, default=False)
    supply_category: Mapped[SupplyCategory] = mapped_column(_enum(SupplyCategory), default=SupplyCategory.TAXABLE)
    itc_eligible: Mapped[bool | None] = mapped_column(Boolean)  # inward only
    invoice_value: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    source_rows: Mapped[list] = mapped_column(JSON, default=list)

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True
    )


class InvoiceItem(_Amounts, Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    hsn: Mapped[str | None] = mapped_column(String(8))
    description: Mapped[str | None] = mapped_column(String(255))
    uqc: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    service_type_id: Mapped[int | None] = mapped_column(ForeignKey("service_types.id"))
    # Land-deduction services: the register carries the full consideration; taxable_value holds the
    # two-thirds reported in GSTR-1 and land_value the deducted third.
    land_value: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    cost_centre_id: Mapped[int | None] = mapped_column(ForeignKey("cost_centres.id"), index=True)

    invoice: Mapped[Invoice] = relationship(back_populates="items")
    service_type: Mapped[ServiceType | None] = relationship()
    cost_centre: Mapped[CostCentre | None] = relationship()

    @property
    def cost_centre_name(self) -> str | None:
        return self.cost_centre.name if self.cost_centre else None


class PortalDocument(_Amounts, Base):
    """A supplier document as auto-populated by GSTN in GSTR-2A or GSTR-2B."""

    __tablename__ = "portal_documents"
    __table_args__ = (Index("ix_portal_docs_gstin_period", "gstin_id", "period", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))
    source: Mapped[ImportKind] = mapped_column(_enum(ImportKind))  # gstr2a / gstr2b
    supplier_gstin: Mapped[str] = mapped_column(String(15), index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(255))
    doc_type: Mapped[DocType] = mapped_column(_enum(DocType))
    invoice_no: Mapped[str] = mapped_column(String(50))
    invoice_date: Mapped[date] = mapped_column(Date)
    place_of_supply: Mapped[str | None] = mapped_column(String(2))
    reverse_charge: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice_value: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    itc_available: Mapped[bool | None] = mapped_column(Boolean)
    itc_unavailable_reason: Mapped[str | None] = mapped_column(String(255))
    supplier_return_filed: Mapped[bool | None] = mapped_column(Boolean)
    supplier_filing_date: Mapped[date | None] = mapped_column(Date)
    supplier_filing_period: Mapped[str | None] = mapped_column(String(7))


class AdvanceEntry(_Amounts, Base):
    """An advance received (or opening balance) or a refund of one, from the bank book upload.

    amount is the gross money received/refunded, inclusive of GST. For advances the tax split
    (taxable_value, igst, cgst, sgst) is fixed at receipt; for refunds it is derived from the
    advances the refund consumes, so those columns stay zero.
    """

    __tablename__ = "advance_entries"
    __table_args__ = (Index("ix_advance_entries_gstin_period", "gstin_id", "period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))
    entry_type: Mapped[AdvanceEntryType] = mapped_column(_enum(AdvanceEntryType))
    voucher_no: Mapped[str] = mapped_column(String(50))
    entry_date: Mapped[date] = mapped_column(Date)
    customer_name: Mapped[str] = mapped_column(String(255))
    customer_gstin: Mapped[str | None] = mapped_column(String(15))
    service_type_id: Mapped[int] = mapped_column(ForeignKey("service_types.id"))
    place_of_supply: Mapped[str] = mapped_column(String(2))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    amount: Mapped[Decimal] = mapped_column(Money)
    against_voucher_no: Mapped[str | None] = mapped_column(String(50))  # refunds: which advance it returns
    narration: Mapped[str | None] = mapped_column(String(255))
    source_row: Mapped[int | None] = mapped_column(Integer)
    cost_centre_id: Mapped[int | None] = mapped_column(ForeignKey("cost_centres.id"), index=True)

    service_type: Mapped[ServiceType] = relationship()
    cost_centre: Mapped[CostCentre | None] = relationship()


class AdvanceRule(Base):
    """User instruction overriding oldest-first: adjust this advance against this invoice."""

    __tablename__ = "advance_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"), index=True)
    invoice_no: Mapped[str] = mapped_column(String(50))
    advance_voucher_no: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal | None] = mapped_column(Money)  # None = as much as both allow
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)  # stored lower-case; used to sign in
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class UserSession(Base):
    """Server-side login session. The browser holds a random token in an HttpOnly cookie; only its
    SHA-256 is stored, so a leaked database cannot be replayed as a session."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship()


class TaxLedgerEntry(Base):
    """Liability / ITC / utilisation postings per GSTIN per period. Populated by the GSTR-3B module."""

    __tablename__ = "tax_ledger_entries"
    __table_args__ = (Index("ix_tax_ledger_gstin_period", "gstin_id", "period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin_id: Mapped[int] = mapped_column(ForeignKey("gstins.id"))
    period: Mapped[str] = mapped_column(String(7))
    entry_type: Mapped[LedgerEntryType] = mapped_column(_enum(LedgerEntryType))
    tax_head: Mapped[TaxHead] = mapped_column(_enum(TaxHead))
    amount: Mapped[Decimal] = mapped_column(Money)
    offset_head: Mapped[TaxHead | None] = mapped_column(_enum(TaxHead))  # for utilisation: credit head used
    description: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


__all__ = [
    "Entity", "Gstin", "CostCentre", "ServiceType", "ImportBatch", "Invoice", "InvoiceItem", "PortalDocument",
    "AdvanceEntry", "AdvanceRule", "User", "UserSession", "TaxLedgerEntry",
    "RegistrationType", "Direction", "DocType", "SupplyCategory", "ImportKind", "ImportSource",
    "AdvanceEntryType", "TaxHead", "LedgerEntryType",
]
