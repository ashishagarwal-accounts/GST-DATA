"""GSTR-1 JSON in the GSTN schema, for upload on gst.gov.in (Returns > GSTR-1 > Prepare Offline > Upload).

The portal treats the upload as a draft: nothing is filed until the return is submitted there, and any
rejected records are listed in the portal's error report. Field names follow the GSTN GSTR-1 JSON
(the same layout the offline tool exports).
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.services.gstr1 import Gstr1

SCHEMA_VERSION = "GST3.2.2"

B2B_INV_TYPE = {
    "Regular B2B": "R", "Intra-State supplies attracting IGST": "R",
    "SEZ supplies with payment": "SEWP", "SEZ supplies without payment": "SEWOP", "Deemed Exp": "DE",
}
NIL_SUPPLY_TYPE = {
    "Inter-State supplies to registered persons": "INTRB2B",
    "Intra-State supplies to registered persons": "INTRAB2B",
    "Inter-State supplies to unregistered persons": "INTRB2C",
    "Intra-State supplies to unregistered persons": "INTRAB2C",
}
DOC_NATURE = {  # Table 13 document type numbers
    "Invoices for outward supply": 1, "Debit Note": 4, "Credit Note": 5, "Receipt voucher": 6, "Refund voucher": 8,
}


def _n(value) -> float:
    """Amounts as JSON numbers rounded to paise."""
    return round(float(Decimal(value or 0)), 2)


def _rate(value) -> float | int:
    """Whole rates as integers (18, not 18.0), fractional ones as decimals (1.5, 7.5)."""
    d = Decimal(value)
    return int(d) if d == d.to_integral_value() else float(d)


def _date(value: date) -> str:
    return value.strftime("%d-%m-%Y")


def _pos(label: str) -> str:
    return (label or "")[:2]


def _item_num(rate) -> int:
    # The offline tool numbers rate-wise items as rate x 100 + 1 (18% -> 1801); unique within a document.
    return int(Decimal(rate) * 100) + 1


def _itm(row: dict, heads=("iamt", "camt", "samt", "csamt")) -> dict:
    det = {"txval": _n(row["txval"]), "rt": _rate(row["rt"])}
    for h in heads:
        det[h] = _n(row[h])
    return {"num": _item_num(row["rt"]), "itm_det": det}


def _group(rows: list[dict], key) -> dict:
    groups: dict = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return groups


def build_gstr1_json(report: Gstr1) -> dict:
    s = report.sections
    year, month = report.period.split("-")
    out: dict = {"gstin": report.gstin.gstin, "fp": f"{month}{year}", "version": SCHEMA_VERSION, "hash": "hash"}

    if s["b2b"]:
        out["b2b"] = [{
            "ctin": ctin,
            "inv": [{
                "inum": inum, "idt": _date(lines[0]["idt"]), "val": _n(lines[0]["val"]), "pos": _pos(lines[0]["pos"]),
                "rchrg": lines[0]["rchrg"], "inv_typ": B2B_INV_TYPE.get(lines[0]["inv_typ"], "R"),
                "itms": [_itm(r) for r in lines],
            } for inum, lines in _group(docs, lambda r: r["inum"]).items()],
        } for ctin, docs in _group(s["b2b"], lambda r: r["ctin"]).items()]

    if s["b2cl"]:
        out["b2cl"] = [{
            "pos": pos,
            "inv": [{"inum": inum, "idt": _date(lines[0]["idt"]), "val": _n(lines[0]["val"]),
                     "itms": [_itm(r, ("iamt", "csamt")) for r in lines]}
                    for inum, lines in _group(docs, lambda r: r["inum"]).items()],
        } for pos, docs in _group(s["b2cl"], lambda r: _pos(r["pos"])).items()]

    if s["b2cs"]:
        out["b2cs"] = [{
            "sply_ty": r["sply_ty"], "pos": _pos(r["pos"]), "typ": r["typ"], "rt": _rate(r["rt"]),
            "txval": _n(r["txval"]), "iamt": _n(r["iamt"]), "camt": _n(r["camt"]), "samt": _n(r["samt"]),
            "csamt": _n(r["csamt"]),
        } for r in s["b2cs"]]

    if s["exp"]:
        out["exp"] = [{
            "exp_typ": exp_typ,
            "inv": [{"inum": inum, "idt": _date(lines[0]["idt"]), "val": _n(lines[0]["val"]),
                     "itms": [{"txval": _n(r["txval"]), "rt": _rate(r["rt"]), "iamt": _n(r["iamt"]), "csamt": _n(r["csamt"])}
                              for r in lines]}
                    for inum, lines in _group(docs, lambda r: r["inum"]).items()],
        } for exp_typ, docs in _group(s["exp"], lambda r: r["exp_typ"]).items()]

    if s["cdnr"]:
        out["cdnr"] = [{
            "ctin": ctin,
            "nt": [{
                "ntty": lines[0]["ntty"], "nt_num": num, "nt_dt": _date(lines[0]["nt_dt"]), "val": _n(lines[0]["val"]),
                "pos": _pos(lines[0]["pos"]), "rchrg": lines[0]["rchrg"],
                "inv_typ": B2B_INV_TYPE.get(lines[0]["inv_typ"], "R"), "itms": [_itm(r) for r in lines],
            } for num, lines in _group(docs, lambda r: r["nt_num"]).items()],
        } for ctin, docs in _group(s["cdnr"], lambda r: r["ctin"]).items()]

    if s["cdnur"]:
        out["cdnur"] = []
        for num, lines in _group(s["cdnur"], lambda r: r["nt_num"]).items():
            note = {"typ": lines[0]["ur_typ"], "ntty": lines[0]["ntty"], "nt_num": num, "nt_dt": _date(lines[0]["nt_dt"]),
                    "val": _n(lines[0]["val"]), "itms": [_itm(r, ("iamt", "csamt")) for r in lines]}
            if lines[0]["ur_typ"] == "B2CL":
                note["pos"] = _pos(lines[0]["pos"])
            out["cdnur"].append(note)

    if s["exemp"]:
        out["nil"] = {"inv": [{"sply_ty": NIL_SUPPLY_TYPE[r["desc"]], "nil_amt": _n(r["nil"]),
                               "expt_amt": _n(r["exempt"]), "ngsup_amt": _n(r["non_gst"])} for r in s["exemp"]]}

    for src, dest in (("at", "at"), ("atadj", "txpd")):
        if s[src]:
            out[dest] = [{
                "pos": pos, "sply_ty": rows[0]["sply_ty"],
                "itms": [{"rt": _rate(r["rt"]), "ad_amt": _n(r["txval"]), "iamt": _n(r["iamt"]), "camt": _n(r["camt"]),
                          "samt": _n(r["samt"]), "csamt": _n(r["csamt"])} for r in rows],
            } for pos, rows in _group(s[src], lambda r: _pos(r["pos"])).items()]

    hsn = {}
    for src, dest in (("hsn_b2b", "hsn_b2b"), ("hsn_b2c", "hsn_b2c")):
        if s[src]:
            hsn[dest] = [{
                "num": i, "hsn_sc": r["hsn"], "desc": (r["desc"] or "")[:30], "uqc": r["uqc"], "qty": _n(r["qty"]),
                "rt": _rate(r["rt"]), "txval": _n(r["txval"]), "iamt": _n(r["iamt"]), "camt": _n(r["camt"]),
                "samt": _n(r["samt"]), "csamt": _n(r["csamt"]),
            } for i, r in enumerate(s[src], start=1)]
    if hsn:
        out["hsn"] = hsn

    if s["docs"]:
        out["doc_issue"] = {"doc_det": [{
            "doc_num": DOC_NATURE[r["nature"]],
            "docs": [{"num": 1, "from": r["from"], "to": r["to"], "totnum": r["total"], "cancel": r["cancelled"],
                      "net_issue": r["total"] - r["cancelled"]}],
        } for r in s["docs"] if r["nature"] in DOC_NATURE]}

    return out
