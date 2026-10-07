import pytest

from app import auth
from app.auth import hash_password, verify_password

ADMIN = {"name": "Ashish Agarwal", "email": "Ashish@Example.com", "password": "Gst#2026pass"}


@pytest.fixture(autouse=True)
def reset_throttle():
    auth._FAILURES.clear()
    yield
    auth._FAILURES.clear()


def test_password_hashing():
    h = hash_password("Secret123")
    assert h.startswith("scrypt$") and verify_password("Secret123", h)
    assert not verify_password("secret123", h)
    assert hash_password("Secret123") != h  # salted


def test_everything_requires_sign_in(anon_client):
    assert anon_client.get("/api/entities").status_code == 401
    assert anon_client.get("/api/dashboard?period=2026-09").status_code == 401
    assert anon_client.get("/api/health").status_code == 200
    assert anon_client.get("/").status_code == 200  # the page itself loads and shows the sign-in screen


def test_first_admin_setup_then_login(anon_client):
    c = anon_client
    assert c.get("/api/auth/setup-status").json() == {"needs_setup": True}
    weak = c.post("/api/auth/setup", json={**ADMIN, "password": "12345678"})
    assert weak.status_code == 422 and "mix letters" in weak.text

    r = c.post("/api/auth/setup", json=ADMIN)
    assert r.status_code == 201 and r.json()["is_admin"] and r.json()["email"] == "ashish@example.com"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/me").json()["name"] == "Ashish Agarwal"
    assert c.get("/api/entities").status_code == 200
    assert c.post("/api/auth/setup", json=ADMIN).status_code == 409  # only once

    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/entities").status_code == 401

    assert c.post("/api/auth/login", json={"email": "ashish@example.com", "password": "wrong-pass1"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": " ASHISH@example.com ", "password": ADMIN["password"]})
    assert r.status_code == 200
    assert c.get("/api/auth/me").json()["last_login_at"]


def test_lockout_after_repeated_failures(anon_client):
    c = anon_client
    c.post("/api/auth/setup", json=ADMIN)
    c.post("/api/auth/logout")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": ADMIN["email"], "password": "nope-123"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert r.status_code == 429  # even the right password is refused while locked


def test_user_management_and_password_change(anon_client):
    c = anon_client
    c.post("/api/auth/setup", json=ADMIN)
    r = c.post("/api/users", json={"name": "Accounts Staff", "email": "staff@example.com", "password": "Staff#2026"})
    assert r.status_code == 201 and r.json()["is_admin"] is False
    staff_id = r.json()["id"]
    assert c.post("/api/users", json={"name": "X", "email": "staff@example.com", "password": "Staff#2026"}).status_code == 409
    me_id = c.get("/api/auth/me").json()["id"]
    assert c.patch(f"/api/users/{me_id}", json={"active": False}).status_code == 422  # cannot lock yourself out

    # staff can work but cannot manage users
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": "staff@example.com", "password": "Staff#2026"}).status_code == 200
    assert c.get("/api/entities").status_code == 200
    assert c.get("/api/users").status_code == 403
    assert c.post("/api/auth/password", json={"current_password": "bad", "new_password": "Newer#2026"}).status_code == 422
    assert c.post("/api/auth/password", json={"current_password": "Staff#2026", "new_password": "Newer#2026"}).status_code == 204

    # admin deactivates staff; staff can no longer sign in
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert c.patch(f"/api/users/{staff_id}", json={"active": False}).json()["active"] is False
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": "staff@example.com", "password": "Newer#2026"}).status_code == 401


def test_imports_record_who_uploaded(anon_client, session_factory):
    from tests.conftest import OWN_GSTIN, xlsx
    c = anon_client
    c.post("/api/auth/setup", json=ADMIN)
    entity = c.post("/api/entities", json={"name": "Alcove LLP"}).json()
    c.post(f"/api/entities/{entity['id']}/gstins", json={"gstin": OWN_GSTIN})
    r = c.post("/api/imports/register", data={"gstin": OWN_GSTIN, "period": "2026-09", "kind": "sales_register"},
               files={"file": ("s.xlsx", xlsx([["Invoice No", "Invoice Date", "Taxable Value", "CGST", "SGST"],
                                               ["S/1", "01-09-2026", 1000, 90, 90]]))})
    assert r.status_code == 201 and r.json()["batch"]["created_by"] == "ashish@example.com"


def test_web_files_are_never_served_stale(anon_client):
    r = anon_client.get("/")
    assert r.headers["cache-control"] == "no-cache"
    assert "/static/app.js?v=" in r.text and "/static/app.css?v=" in r.text
    assert anon_client.get("/static/app.js").headers["cache-control"] == "no-cache"
