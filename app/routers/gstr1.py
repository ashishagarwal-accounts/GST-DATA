import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.routers.advances import exact
from app.routers.imports import _gstin_or_404, _period
from app.services.gstr1 import build_gstr1
from app.services.gstr1_excel import build_workbook
from app.services.gstr1_json import build_gstr1_json

router = APIRouter(tags=["gstr-1"])


@router.get("/gstr1")
def gstr1(gstin: str, period: str, session: Session = Depends(get_session)):
    """All GSTR-1 sections for review, with a section summary and the tax payable from the return."""
    report = build_gstr1(session, _gstin_or_404(session, gstin), _period(period))
    return exact({"gstin": report.gstin.gstin, "period": report.period, "summary": report.summary(),
                  "liability": report.liability(), "sections": report.sections, "warnings": report.warnings})


@router.get("/gstr1/export.json")
def gstr1_portal_json(gstin: str, period: str, session: Session = Depends(get_session)):
    """GSTR-1 JSON for direct upload on the GST portal (Returns > GSTR-1 > Prepare Offline > Upload)."""
    record = _gstin_or_404(session, gstin)
    report = build_gstr1(session, record, _period(period))
    payload = build_gstr1_json(report)
    return Response(json.dumps(payload, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="GSTR1_{record.gstin}_{payload["fp"]}.json"'})


@router.get("/gstr1/export.xlsx")
def gstr1_excel(gstin: str, period: str, session: Session = Depends(get_session)):
    record = _gstin_or_404(session, gstin)
    report = build_gstr1(session, record, _period(period))
    return Response(build_workbook(report),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="GSTR1_{record.gstin}_{report.period}.xlsx"'})
