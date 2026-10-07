import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import require_user
from app.db import get_session, init_db
from app.models import User
from app.gstin import gstin_check_char
from app.main import app


def make_gstin(state: str, pan: str, entity_no: str = "1") -> str:
    first14 = f"{state}{pan}{entity_no}Z"
    return first14 + gstin_check_char(first14)


OWN_PAN = "AAKFA1234B"
OWN_GSTIN = make_gstin("19", OWN_PAN)          # West Bengal
CUSTOMER_WB = make_gstin("19", "AABCC1111D")
CUSTOMER_MH = make_gstin("27", "AABCM2222E")
SUPPLIER_WB = make_gstin("19", "AADCS3333F")
BAD_CHECKSUM = CUSTOMER_WB[:-1] + ("A" if CUSTOMER_WB[-1] != "A" else "B")


def xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def as_json(data) -> bytes:
    return json.dumps(data).encode()


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def anon_client(session_factory):
    """A client with no one signed in (real authentication)."""
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    # Not used as a context manager, so the lifespan (which migrates MySQL) does not run.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client(anon_client):
    """A client signed in as an administrator (authentication itself is covered in test_auth.py)."""
    app.dependency_overrides[require_user] = lambda: User(id=0, email="tester@alcove.test", name="Tester", is_admin=True, active=True)
    return anon_client


@pytest.fixture
def own_gstin(client):
    entity = client.post("/api/entities", json={"name": "Alcove Mall Road LLP"}).json()
    r = client.post(f"/api/entities/{entity['id']}/gstins", json={"gstin": OWN_GSTIN, "legal_name": "Alcove Mall Road LLP"})
    assert r.status_code == 201, r.text
    return OWN_GSTIN
