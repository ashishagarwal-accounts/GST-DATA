"""GSTIN validation and GST state codes."""
import re

STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "96": "Foreign Country", "97": "Other Territory", "99": "Centre Jurisdiction",
}
FOREIGN_STATE_CODE = "96"

_STATE_BY_NAME = {name.lower(): code for code, name in STATE_CODES.items()}
_STATE_BY_NAME.update({
    "orissa": "21", "pondicherry": "34", "j&k": "01", "jammu & kashmir": "01",
    "andaman & nicobar islands": "35", "other countries": "96", "foreign": "96", "outside india": "96",
})

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_RE = re.compile(r"^[0-9]{2}[0-9A-Z]{13}$")


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = _CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36]


def gstin_error(value: str) -> str | None:
    """Return a human-readable problem with the GSTIN, or None if it is valid."""
    if not _GSTIN_RE.match(value):
        return f"'{value}' is not a 15-character GSTIN"
    if value[:2] not in STATE_CODES:
        return f"'{value}' has unknown state code {value[:2]}"
    if gstin_check_char(value[:14]) != value[14]:
        return f"'{value}' fails the GSTIN checksum"
    return None


def normalize_gstin(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def pan_of(gstin: str) -> str:
    return gstin[2:12]


def parse_state_code(value) -> str | None:
    """Accept '27', 27, '27-Maharashtra', 'Maharashtra'. Returns the 2-digit code or raises ValueError."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if isinstance(value, (int, float)) and float(value).is_integer():
        text = f"{int(value):02d}"
    m = re.match(r"^(\d{1,2})\b", text)
    if m:
        code = m.group(1).zfill(2)
        if code in STATE_CODES:
            return code
        raise ValueError(f"unknown state code '{text}'")
    code = _STATE_BY_NAME.get(text.lower())
    if code:
        return code
    raise ValueError(f"unknown place of supply '{text}'")
