"""Parse GSTR-2A and GSTR-2B JSON downloads from the GST portal.

GSTR-2B: root {"data": {"gstin", "rtnprd", "docdata": {"b2b": [...], "cdnr": [...], ...}}}
GSTR-2A: root {"gstin", "fp", "b2b": [...], "cdn": [...], ...}

Only B2B invoices and B2B credit/debit notes are loaded for now; other sections (amendments,
imports, ISD) are reported as warnings so nothing is silently dropped.
"""
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.gstin import parse_state_code
from app.ingest.values import Issue, clean_text, gstn_period_to_iso, parse_date, parse_decimal
from app.models import DocType, ImportKind

ZERO = Decimal("0.00")
_2B_LOADED = {"b2b", "cdnr"}
_2A_LOADED = {"b2b", "cdn"}
_2A_META = {"gstin", "fp", "chksum"}


@dataclass
class ParsedPortalDoc:
    supplier_gstin: str
    supplier_name: str | None
    doc_type: DocType
    invoice_no: str
    invoice_date: date
    place_of_supply: str | None
    reverse_charge: bool
    invoice_value: Decimal
    taxable_value: Decimal = ZERO
    igst: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    cess: Decimal = ZERO
    itc_available: bool | None = None
    itc_unavailable_reason: str | None = None
    supplier_return_filed: bool | None = None
    supplier_filing_date: date | None = None
    supplier_filing_period: str | None = None


@dataclass
class PortalParseResult:
    kind: ImportKind | None = None
    gstin: str | None = None
    period: str | None = None
    documents: list[ParsedPortalDoc] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)


def parse_portal_json(content: bytes, expected_kind: ImportKind) -> PortalParseResult:
    result = PortalParseResult()
    try:
        root = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        result.errors.append(Issue(f"not a valid JSON file: {exc}"))
        return result

    if isinstance(root, dict) and isinstance(root.get("data"), dict) and "docdata" in root["data"]:
        result.kind = ImportKind.GSTR2B
    elif isinstance(root, dict) and "fp" in root:
        result.kind = ImportKind.GSTR2A
    else:
        result.errors.append(Issue("file is not a GSTR-2A or GSTR-2B JSON download"))
        return result

    if result.kind is not expected_kind:
        result.errors.append(Issue(f"file is a {result.kind.value.upper()} download, expected {expected_kind.value.upper()}"))
        return result

    try:
        if result.kind is ImportKind.GSTR2B:
            _parse_2b(root["data"], result)
        else:
            _parse_2a(root, result)
    except (KeyError, TypeError, ValueError) as exc:
        result.errors.append(Issue(f"unexpected file structure: {exc}"))
    return result


def _amounts(items: list[dict], keys: dict[str, str]) -> dict[str, Decimal]:
    totals = dict.fromkeys(("taxable_value", "igst", "cgst", "sgst", "cess"), ZERO)
    for item in items:
        for ours, theirs in keys.items():
            totals[ours] += parse_decimal(item.get(theirs)) or ZERO
    return totals


def _yn(value) -> bool | None:
    if value is None:
        return None
    return str(value).strip().upper() == "Y"


def _note_type(value) -> DocType:
    return DocType.DEBIT_NOTE if str(value).strip().upper() == "D" else DocType.CREDIT_NOTE


# ---------------------------------------------------------------- GSTR-2B

_2B_KEYS = {"taxable_value": "txval", "igst": "igst", "cgst": "cgst", "sgst": "sgst", "cess": "cess"}
_2B_ITC_REASONS = {"P": "POS and supplier state are same but recipient state is different",
                   "C": "Return filed post annual cut-off"}


def _parse_2b(data: dict, result: PortalParseResult) -> None:
    result.gstin = data["gstin"]
    result.period = gstn_period_to_iso(data["rtnprd"])
    docdata = data.get("docdata") or {}
    for section, entries in docdata.items():
        if section not in _2B_LOADED and entries:
            result.warnings.append(Issue(f"GSTR-2B section '{section}' ({len(entries)} suppliers) not loaded yet"))

    for supplier in docdata.get("b2b", []):
        for inv in supplier.get("inv", []):
            result.documents.append(_doc_2b(supplier, inv, DocType.INVOICE, inv["inum"]))
    for supplier in docdata.get("cdnr", []):
        for note in supplier.get("nt", []):
            result.documents.append(_doc_2b(supplier, note, _note_type(note.get("typ")), note["ntnum"]))


def _doc_2b(supplier: dict, doc: dict, doc_type: DocType, number: str) -> ParsedPortalDoc:
    itcavl = str(doc.get("itcavl", "")).upper()
    reason = doc.get("rsn")
    return ParsedPortalDoc(
        supplier_gstin=supplier["ctin"], supplier_name=clean_text(supplier.get("trdnm"), 255),
        doc_type=doc_type, invoice_no=str(number), invoice_date=parse_date(doc["dt"]),
        place_of_supply=parse_state_code(doc.get("pos")), reverse_charge=_yn(doc.get("rev")) or False,
        invoice_value=parse_decimal(doc.get("val")) or ZERO,
        itc_available=None if itcavl == "T" else itcavl == "Y",
        itc_unavailable_reason=_2B_ITC_REASONS.get(reason, reason) if itcavl != "Y" else None,
        supplier_return_filed=True,  # a document only appears in 2B once the supplier has filed
        supplier_filing_date=parse_date(supplier.get("supfildt")),
        supplier_filing_period=gstn_period_to_iso(supplier["supprd"]) if supplier.get("supprd") else None,
        **_amounts(doc.get("items", []), _2B_KEYS),
    )


# ---------------------------------------------------------------- GSTR-2A

_2A_KEYS = {"taxable_value": "txval", "igst": "iamt", "cgst": "camt", "sgst": "samt", "cess": "csamt"}


def _parse_2a(root: dict, result: PortalParseResult) -> None:
    result.gstin = root["gstin"]
    result.period = gstn_period_to_iso(root["fp"])
    for section, entries in root.items():
        if section not in _2A_LOADED and section not in _2A_META and entries:
            result.warnings.append(Issue(f"GSTR-2A section '{section}' not loaded yet"))

    for supplier in root.get("b2b", []):
        for inv in supplier.get("inv", []):
            result.documents.append(_doc_2a(supplier, inv, DocType.INVOICE, inv["inum"], inv["idt"],
                                            inv.get("rchrg")))
    for supplier in root.get("cdn", []):
        for note in supplier.get("nt", []):
            result.documents.append(_doc_2a(supplier, note, _note_type(note.get("ntty")), note["nt_num"],
                                            note["nt_dt"], note.get("rchrg")))


def _doc_2a(supplier: dict, doc: dict, doc_type: DocType, number, doc_date, rchrg) -> ParsedPortalDoc:
    items = [i.get("itm_det", {}) for i in doc.get("itms", [])]
    return ParsedPortalDoc(
        supplier_gstin=supplier["ctin"], supplier_name=clean_text(supplier.get("trdnm"), 255),
        doc_type=doc_type, invoice_no=str(number), invoice_date=parse_date(doc_date),
        place_of_supply=parse_state_code(doc.get("pos")), reverse_charge=_yn(rchrg) or False,
        invoice_value=parse_decimal(doc.get("val")) or ZERO,
        supplier_return_filed=_yn(supplier.get("cfs")),
        supplier_filing_date=parse_date(supplier.get("fldtr1")),
        supplier_filing_period=gstn_period_to_iso(supplier["flprdr1"]) if supplier.get("flprdr1") else None,
        **_amounts(items, _2A_KEYS),
    )
