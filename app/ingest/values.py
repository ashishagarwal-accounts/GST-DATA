"""Cell-value parsing shared by all ingestion formats."""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
_EXCEL_EPOCH = date(1899, 12, 30)
_DATE_FORMATS = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d-%B-%Y",
    "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y", "%d/%b/%Y",
)
_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass
class Issue:
    message: str
    row: int | None = None
    field: str | None = None

    def as_dict(self) -> dict:
        return {"row": self.row, "field": self.field, "message": self.message}


def is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_decimal(value, places: Decimal = PAISE) -> Decimal | None:
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"expected a number, got {value!r}")
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
    else:
        text = str(value).strip().replace(",", "").replace("₹", "").replace(" ", "")
        negative = text.startswith("(") and text.endswith(")")
        text = text.strip("()")
        if text in ("-", ""):
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"'{value}' is not a number") from None
        if negative:
            number = -number
    return number.quantize(places, rounding=ROUND_HALF_UP)


def parse_date(value) -> date | None:
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 80000:
        return _EXCEL_EPOCH + timedelta(days=int(value))
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a recognised date (use DD-MM-YYYY)")


def parse_bool(value) -> bool | None:
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("y", "yes", "true", "1"):
        return True
    if text in ("n", "no", "false", "0"):
        return False
    raise ValueError(f"'{value}' is not Yes/No")


def clean_text(value, max_len: int | None = None) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Excel stores numeric invoice numbers / HSN codes as floats
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_len] if max_len else text


def validate_period(period: str) -> str:
    if not _PERIOD_RE.match(period):
        raise ValueError(f"period '{period}' must be YYYY-MM")
    return period


MAX_RANGE_MONTHS = 36


def month_range(start: str, end: str) -> list[str]:
    """All return periods from start to end inclusive ('2026-04', '2026-06' -> three months)."""
    validate_period(start)
    validate_period(end)
    if start > end:
        raise ValueError(f"period range starts ({start}) after it ends ({end})")
    y, m = map(int, start.split("-"))
    months = []
    while f"{y:04d}-{m:02d}" <= end:
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if len(months) > MAX_RANGE_MONTHS:
            raise ValueError(f"period range is longer than {MAX_RANGE_MONTHS} months")
    return months


def period_bounds(period: str) -> tuple[date, date]:
    year, month = map(int, period.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return start, end


def gstn_period_to_iso(rtnprd: str) -> str:
    """GSTN uses MMYYYY ('092026'); we use YYYY-MM."""
    rtnprd = str(rtnprd).strip()
    if not re.match(r"^(0[1-9]|1[0-2])\d{4}$", rtnprd):
        raise ValueError(f"'{rtnprd}' is not a GSTN return period (MMYYYY)")
    return f"{rtnprd[2:]}-{rtnprd[:2]}"


def financial_year_start(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1
