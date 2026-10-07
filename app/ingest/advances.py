"""Parse the advance register (bank book export) and the opening-balance upload.

Template columns (header row may sit below title rows, as with registers):
  Date | Voucher No | Type | Customer Name | Customer GSTIN | Service Type | Amount (incl. GST) |
  Place of Supply | Against Voucher No | Narration
Opening balances use the same layout without Type; they may also carry the Taxable Value / IGST /
CGST / SGST actually paid under the old process, which are then used instead of a back-calculation.

Type: Advance (or Receipt) / Refund (or Payment) / Other (row ignored). Blank means Advance.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.gstin import gstin_error, normalize_gstin, parse_state_code
from app.ingest.registers import COST_CENTRE_ALIASES, find_header, normalize_header, read_tables
from app.ingest.values import Issue, clean_text, is_blank, parse_date, parse_decimal, period_bounds
from app.models import AdvanceEntryType

ZERO = Decimal("0.00")
PAISE = Decimal("0.01")
LAND_DEDUCTION_FACTOR = Decimal(2) / Decimal(3)  # taxable share after the deemed 1/3 land deduction

ADVANCE_ALIASES: dict[str, tuple[str, ...]] = {
    "entry_date": ("date", "receipt date", "voucher date", "vch date", "payment date", "entry date",
                   "original receipt date"),
    "voucher_no": ("voucher no", "vch no", "voucher number", "receipt no", "receipt number", "ref no",
                   "reference no", "original receipt no"),
    "entry_type": ("type", "entry type", "nature", "transaction type", "voucher type", "vch type"),
    "customer_name": ("customer name", "party name", "particulars", "customer", "party", "ledger name"),
    "customer_gstin": ("customer gstin", "gstin", "gstin/uin", "party gstin"),
    "service_type": ("service type", "type of service", "service category"),
    "amount": ("amount (incl. gst)", "amount incl gst", "amount including gst", "amount", "amount received",
               "receipt amount", "gross amount", "refund amount", "unadjusted amount (incl. gst)",
               "unadjusted amount", "balance amount"),
    "place_of_supply": ("place of supply", "pos"),
    "against_voucher_no": ("against voucher no", "against receipt no", "against advance", "adjust against"),
    "narration": ("narration", "remarks"),
    "cost_centre": COST_CENTRE_ALIASES,
    "taxable_value": ("taxable value",),
    "igst": ("igst", "igst amount"),
    "cgst": ("cgst", "cgst amount"),
    "sgst": ("sgst", "sgst amount", "sgst/utgst"),
}
REQUIRED = ("entry_date", "voucher_no", "customer_name", "service_type", "amount")
_LOOKUP = {normalize_header(a): f for f, aliases in ADVANCE_ALIASES.items() for a in aliases}


@dataclass
class ServiceTypeInfo:
    id: int
    name: str
    tax_rate: Decimal
    land_deduction: bool


@dataclass
class TaxSplit:
    taxable_value: Decimal
    igst: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO

    @property
    def tax(self) -> Decimal:
        return self.igst + self.cgst + self.sgst


def split_inclusive(amount: Decimal, rate: Decimal, land_deduction: bool, interstate: bool) -> TaxSplit:
    """Break a GST-inclusive amount into taxable value and tax.

    amount = price + tax, taxable = price x f (f = 2/3 with land deduction, else 1), tax = taxable x rate.
    So price = amount / (1 + f x rate).
    """
    f = LAND_DEDUCTION_FACTOR if land_deduction else Decimal(1)
    r = rate / Decimal(100)
    price = amount / (1 + f * r)
    taxable = (price * f).quantize(PAISE, ROUND_HALF_UP)
    tax = (taxable * r).quantize(PAISE, ROUND_HALF_UP)
    if interstate:
        return TaxSplit(taxable, igst=tax)
    cgst = (tax / 2).quantize(PAISE, ROUND_HALF_UP)
    return TaxSplit(taxable, cgst=cgst, sgst=tax - cgst)


@dataclass
class ParsedAdvance:
    entry_type: AdvanceEntryType
    voucher_no: str
    entry_date: date
    customer_name: str
    customer_gstin: str | None
    service_type: ServiceTypeInfo
    place_of_supply: str
    amount: Decimal
    split: TaxSplit
    against_voucher_no: str | None
    narration: str | None
    source_row: int
    cost_centre_id: int | None = None


@dataclass
class AdvanceParseResult:
    entries: list[ParsedAdvance] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    sheet: str | None = None
    header_row: int | None = None
    column_map: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    ignored_rows: int = 0


def parse_entry_type(value) -> AdvanceEntryType | None:
    """None means the row is not an advance or refund and should be skipped."""
    text = (clean_text(value) or "").lower()
    if not text or text.startswith(("advance", "receipt", "rcpt")):
        return AdvanceEntryType.ADVANCE
    if text.startswith(("refund", "payment", "pymt")):
        return AdvanceEntryType.REFUND
    if text.startswith(("other", "ignore", "n/a", "na", "contra", "journal")):
        return None
    raise ValueError(f"unknown type '{value}' (use Advance, Refund or Other)")


def parse_advances(content: bytes, filename: str, *, opening: bool, own_gstin: str, period: str,
                   service_types: dict[str, ServiceTypeInfo], tax_from: date | None,
                   cost_centres: dict[str, int] | None = None) -> AdvanceParseResult:
    result = AdvanceParseResult()
    try:
        tables = read_tables(content, filename)
    except Exception as exc:
        result.errors.append(Issue(f"could not read file: {exc}"))
        return result

    for sheet_name, rows in tables:
        found = find_header(rows, _LOOKUP, REQUIRED)
        if found:
            header_idx, mapping, unmapped = found
            result.sheet, result.header_row = sheet_name, header_idx + 1
            result.column_map = {f: str(rows[header_idx][c]).strip() for c, f in mapping.items()}
            result.unmapped_columns = unmapped
            break
    else:
        result.errors.append(Issue(
            "no header row found: need Date, Voucher No, Customer Name, Service Type and Amount columns"))
        return result

    own_state = own_gstin[:2]
    start, end = period_bounds(period)
    known = ", ".join(sorted(s.name for s in service_types.values())) or "none set up"
    cost_centres = cost_centres or {}
    has_tax_columns = any(f in mapping.values() for f in ("taxable_value", "igst", "cgst", "sgst"))
    seen: set[tuple] = set()

    for row_no, raw in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        rec = {f: (raw[c] if c < len(raw) else None) for c, f in mapping.items()}
        if is_blank(rec.get("voucher_no")) and is_blank(rec.get("amount")):
            continue
        errors_before = len(result.errors)

        def err(message, fld=None):
            result.errors.append(Issue(message, row=row_no, field=fld))

        def get(fld, parser):
            try:
                return parser(rec.get(fld))
            except ValueError as exc:
                err(str(exc), fld)
                return None

        if opening:
            entry_type = AdvanceEntryType.OPENING
            if not is_blank(rec.get("entry_type")) and get("entry_type", parse_entry_type) is AdvanceEntryType.REFUND:
                err("opening balances cannot contain refunds", "entry_type")
        else:
            try:
                entry_type = parse_entry_type(rec.get("entry_type"))
            except ValueError as exc:
                err(str(exc), "entry_type")
                continue
            if entry_type is None:
                result.ignored_rows += 1
                continue

        voucher_no = clean_text(rec.get("voucher_no"), 50)
        entry_date = get("entry_date", parse_date)
        customer = clean_text(rec.get("customer_name"), 255)
        amount = get("amount", parse_decimal)
        pos = get("place_of_supply", parse_state_code) or own_state
        st_name = clean_text(rec.get("service_type"), 100)
        service = service_types.get((st_name or "").lower())

        if not voucher_no:
            err("voucher number is missing", "voucher_no")
        if not customer:
            err("customer name is missing", "customer_name")
        if not st_name:
            err("service type is missing", "service_type")
        elif not service:
            err(f"unknown service type '{st_name}' (set up: {known})", "service_type")
        if amount is not None:
            if amount < 0 and entry_type is AdvanceEntryType.REFUND:
                amount = -amount  # refunds are often exported as negative receipts
            if amount <= 0:
                err("amount must be positive", "amount")
        elif errors_before == len(result.errors):
            err("amount is missing", "amount")
        if entry_date is None and not any(i.field == "entry_date" for i in result.errors[errors_before:]):
            err("date is missing", "entry_date")

        cc_name = clean_text(rec.get("cost_centre"), 100)
        cost_centre_id = None
        if cc_name:
            cost_centre_id = cost_centres.get(cc_name.lower())
            if cost_centre_id is None:
                err(f"unknown cost centre '{cc_name}' (set up under Entities: "
                    f"{', '.join(sorted(cost_centres)) or 'none'})", "cost_centre")

        customer_gstin = None
        gstin_raw = clean_text(rec.get("customer_gstin"))
        if gstin_raw and gstin_raw.upper() not in ("URP", "UNREGISTERED", "NA", "N/A", "-"):
            customer_gstin = normalize_gstin(gstin_raw)
            problem = gstin_error(customer_gstin)
            if problem:
                err(problem, "customer_gstin")

        if entry_date:
            if opening and entry_date >= start:
                err(f"opening balance dated {entry_date:%d-%m-%Y} must be before go-live period {period}", "entry_date")
            if not opening and entry_date > end:
                err(f"dated {entry_date:%d-%m-%Y}, after the end of {period}", "entry_date")
            if not opening and entry_date < start:
                result.warnings.append(Issue(f"dated {entry_date:%d-%m-%Y}, before {period}", row=row_no, field="entry_date"))
            if tax_from and not opening and entry_date < tax_from:
                err(f"dated before GST on advances was enabled ({tax_from:%d-%m-%Y})", "entry_date")

        if len(result.errors) > errors_before:
            continue

        key = (entry_type, voucher_no.upper(), customer.upper(), service.id, cost_centre_id)
        if key in seen:
            err(f"{voucher_no} for {customer} / {service.name} appears twice", "voucher_no")
            continue
        seen.add(key)

        interstate = pos != own_state
        split = split_inclusive(amount, service.tax_rate, service.land_deduction, interstate)
        if entry_type is AdvanceEntryType.REFUND:
            split = TaxSplit(ZERO)  # derived from the advances the refund consumes
        elif opening and has_tax_columns:
            given = {f: get(f, parse_decimal) for f in ("taxable_value", "igst", "cgst", "sgst")}
            if given["taxable_value"] is not None and len(result.errors) == errors_before:
                split = TaxSplit(given["taxable_value"], igst=given["igst"] or ZERO,
                                 cgst=given["cgst"] or ZERO, sgst=given["sgst"] or ZERO)
                if abs(split.taxable_value * service.tax_rate / 100 - split.tax) > Decimal("1.00"):
                    result.warnings.append(Issue(
                        f"tax {split.tax} is not {service.tax_rate}% of taxable {split.taxable_value}",
                        row=row_no, field="taxable_value"))

        result.entries.append(ParsedAdvance(
            entry_type=entry_type, voucher_no=voucher_no, entry_date=entry_date, customer_name=customer,
            customer_gstin=customer_gstin, service_type=service, place_of_supply=pos, amount=amount, split=split,
            against_voucher_no=clean_text(rec.get("against_voucher_no"), 50),
            narration=clean_text(rec.get("narration"), 255), source_row=row_no, cost_centre_id=cost_centre_id,
        ))

    if result.ignored_rows:
        result.warnings.append(Issue(f"{result.ignored_rows} row(s) marked Other were skipped"))
    if not result.entries and not result.errors:
        result.errors.append(Issue("no advance or refund rows found below the header"))
    return result
