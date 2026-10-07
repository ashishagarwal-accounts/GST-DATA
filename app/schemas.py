from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.gstin import gstin_error, normalize_gstin
from app.models import DocType, Direction, ImportKind, ImportSource, RegistrationType, SupplyCategory


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def as_utc(value: datetime) -> datetime:
    """DB timestamps are naive UTC; tag them so clients convert to local time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]


def _pan(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().upper()
    if len(value) != 10 or not (value[:5].isalpha() and value[5:9].isdigit() and value[9].isalpha()):
        raise ValueError(f"'{value}' is not a valid PAN")
    return value


class EntityCreate(BaseModel):
    name: str
    pan: str | None = None
    notes: str | None = None

    _check_pan = field_validator("pan")(_pan)


class EntityUpdate(BaseModel):
    name: str | None = None
    pan: str | None = None
    active: bool | None = None
    notes: str | None = None

    _check_pan = field_validator("pan")(_pan)


class GstinCreate(BaseModel):
    gstin: str
    legal_name: str | None = None
    trade_name: str | None = None
    registration_type: RegistrationType = RegistrationType.REGULAR
    effective_from: date | None = None
    effective_to: date | None = None

    @field_validator("gstin")
    @classmethod
    def _check_gstin(cls, value: str) -> str:
        value = normalize_gstin(value)
        problem = gstin_error(value)
        if problem:
            raise ValueError(problem)
        return value


class GstinUpdate(BaseModel):
    legal_name: str | None = None
    trade_name: str | None = None
    registration_type: RegistrationType | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    active: bool | None = None
    advance_tax_enabled: bool | None = None
    advance_tax_from: date | None = None


class CostCentreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    code: str | None = Field(default=None, max_length=30)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class CostCentreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    code: str | None = Field(default=None, max_length=30)
    active: bool | None = None


class CostCentreRead(ORM):
    id: int
    gstin_id: int
    name: str
    code: str | None
    active: bool


class ServiceTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sac: str | None = Field(default=None, max_length=8)
    tax_rate: Decimal = Field(ge=0, le=40)
    land_deduction: bool = False

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class ServiceTypeUpdate(BaseModel):
    sac: str | None = Field(default=None, max_length=8)
    tax_rate: Decimal | None = Field(default=None, ge=0, le=40)
    land_deduction: bool | None = None
    active: bool | None = None


class ServiceTypeRead(ORM):
    id: int
    gstin_id: int
    name: str
    sac: str | None
    tax_rate: Decimal
    land_deduction: bool
    active: bool


class AdvanceRuleCreate(BaseModel):
    gstin: str
    invoice_no: str = Field(min_length=1, max_length=50)
    advance_voucher_no: str = Field(min_length=1, max_length=50)
    amount: Decimal | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=255)


class AdvanceRuleRead(ORM):
    id: int
    invoice_no: str
    advance_voucher_no: str
    amount: Decimal | None
    note: str | None
    created_at: UtcDatetime


class GstinRead(ORM):
    id: int
    entity_id: int
    gstin: str
    state_code: str
    legal_name: str | None
    trade_name: str | None
    registration_type: RegistrationType
    effective_from: date | None
    effective_to: date | None
    active: bool
    advance_tax_enabled: bool
    advance_tax_from: date | None


class EntityRead(ORM):
    id: int
    name: str
    pan: str | None
    active: bool
    notes: str | None
    gstins: list[GstinRead]


class IssueRead(BaseModel):
    row: int | None = None
    field: str | None = None
    message: str


class ImportBatchRead(ORM):
    id: int
    gstin_id: int
    period: str
    kind: ImportKind
    source: ImportSource
    file_name: str
    record_count: int
    warnings: list[IssueRead]
    created_by: str | None
    created_at: UtcDatetime


class ImportResult(BaseModel):
    batch: ImportBatchRead
    replaced_batch_ids: list[int]
    details: dict


class InvoiceItemRead(ORM):
    hsn: str | None
    description: str | None
    uqc: str | None
    quantity: Decimal | None
    tax_rate: Decimal
    land_value: Decimal
    cost_centre_id: int | None
    cost_centre_name: str | None
    taxable_value: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    cess: Decimal


class InvoiceRead(ORM):
    id: int
    period: str
    direction: Direction
    doc_type: DocType
    invoice_no: str
    invoice_date: date
    counterparty_gstin: str | None
    counterparty_name: str | None
    place_of_supply: str | None
    reverse_charge: bool
    supply_category: SupplyCategory
    itc_eligible: bool | None
    invoice_value: Decimal
    taxable_value: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    cess: Decimal
    source_rows: list[int]
    items: list[InvoiceItemRead]


class PortalDocumentRead(ORM):
    id: int
    period: str
    source: ImportKind
    supplier_gstin: str
    supplier_name: str | None
    doc_type: DocType
    invoice_no: str
    invoice_date: date
    place_of_supply: str | None
    reverse_charge: bool
    invoice_value: Decimal
    taxable_value: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    cess: Decimal
    itc_available: bool | None
    itc_unavailable_reason: str | None
    supplier_return_filed: bool | None
    supplier_filing_date: date | None
    supplier_filing_period: str | None
